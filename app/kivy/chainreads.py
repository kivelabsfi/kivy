"""Prices, coins, balances, the guard, chain reads: Bluefin + Tatum, Sui GraphQL, gRPC (Section 13).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# Called by every reader and by the Handler (server.py); the chain gateway shadow (site/shadow.py) is loaded here.
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import json
import math
import os
import re
import socket
import threading
import time
import urllib.request
from pathlib import Path
from . import settings as SETTINGS
from . import perpacct as PERPACCT
from .settings import BUILDER
from .settings import DATA
from .config import K, SPOT_IN
from .policy import type_key
from .upstream import lend_cli


# ── Section 13: prices, coins, balances, the guard, chain reads — Bluefin tickers + Tatum (fallback prices), Aftermath
#    price-info (coin types), Sui GraphQL (knowledge 'graphql' URL) with gRPC via builder.ts as backup; used by cards,
#    plans, the AI tools and GET /api/balances ──
_PX = {'t': 0.0, 'p': {}}


def prices():
    """USD marks from Bluefin's public tickers (read-only, cached a minute)."""
    if time.time() - _PX['t'] < 60 and _PX['p']:
        return _PX['p']
    p = {'USDC': 1.0}
    ASSETS = K()['assets']
    try:
        req = urllib.request.Request('https://api.sui-prod.bluefin.io/v1/exchange/tickers',
                                     headers={'User-Agent': 'curl/8.5.0'})
        for t in json.load(urllib.request.urlopen(req, timeout=10)):
            a = str(t.get('symbol', '')).split('-')[0]
            if a in ASSETS:
                p[a] = int(t['markPriceE9']) / 1e9
                try:                                     # 24h move, % (E9 fraction): the wallet panel's change pill
                    _PX.setdefault('c', {})[a] = int(t['priceChangePercent24hrE9']) / 1e7
                except Exception:
                    pass
    except Exception:
        pass
    miss = [a for a in ASSETS if a not in p]
    if miss:                                             # Bluefin down or missing a coin: Tatum (CoinGecko) as the backup
        p.update(tatum_rates(miss))
    now = time.time()
    for a in p:
        _PX.setdefault('seen', {})[a] = now              # when each price last came from a live source
    miss = [a for a in ASSETS if a not in p]
    if miss and _PX['p']:                                # both down: the last known marks, but never older than 10 minutes (review F-11)
        p.update({a: _PX['p'][a] for a in miss if a in _PX['p'] and now - (_PX.get('seen') or {}).get(a, 0) < 600})
    if len(p) > 1:
        _PX.update(t=time.time(), p=p)
    return p


TATUM_KEY = SETTINGS.key_file('tatum.key')         # systemd credentials (root-only /opt/kivy/keys) or the data folder (2026-11-14)
_TATUM = {'skip': {}, 'ip': None, 'ip_t': 0.0}


# The Tatum key file sits in the data folder (never logged); used only by tatum_rates.
def tatum_get(path, timeout=8):
    """GET api.tatum.io over IPv4 (the key's allowlist has only the server's IPv4 address; IPv6 gets 403)."""
    import http.client, socket, ssl
    if not TATUM_KEY.exists():
        raise RuntimeError('no Tatum key')
    if not _TATUM['ip'] or time.time() - _TATUM['ip_t'] > 600:
        _TATUM.update(ip=socket.getaddrinfo('api.tatum.io', 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0], ip_t=time.time())
    class C(http.client.HTTPSConnection):
        def connect(self):
            self.sock = ssl.create_default_context().wrap_socket(socket.create_connection((_TATUM['ip'], 443), timeout), server_hostname='api.tatum.io')
    c = C('api.tatum.io', timeout=timeout)
    c.request('GET', path, headers={'x-api-key': TATUM_KEY.read_text().strip(), 'User-Agent': 'kivy/1'})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b'{}')


# Called by prices() for coins Bluefin has no ticker for.
def tatum_rates(syms):
    """{SYM: usd} from Tatum's price API for the coins its plan covers (free: SUI, USDC, BTC, ETH, SOL; DEEP/WAL need a paid plan,
    so a 402 benches that coin for a day). Backup only: display prices, never the guard."""
    out = {}
    for a in syms:
        if _TATUM['skip'].get(a, 0) > time.time():
            continue
        try:
            st, d = tatum_get('/v4/data/rate/symbol?symbol=%s&basePair=USD' % urllib.parse.quote(a))
            if st == 200 and float(d.get('value') or 0) > 0:
                out[a] = float(d['value'])
            elif st in (402, 404):                       # not in the plan / unknown coin
                _TATUM['skip'][a] = time.time() + 86400
            elif st == 403:                              # key or allowlist problem: try again in 10 minutes
                _TATUM['skip'][a] = time.time() + 600
        except Exception:
            pass
    return out


ADDR_RE = re.compile(r'^0x[0-9a-fA-F]{64}$')
_BAL = {}


def dec(x, sig=6, commas=True):
    """A plain decimal for people: 0.00000615, 1,234.5 — never 6.15e-06."""
    x = float(x or 0)
    if x == 0:
        return '0'
    places = max(0, sig - 1 - int(math.floor(math.log10(abs(x)))))
    t = (('{:,.%df}' if commas else '{:.%df}') % min(places, 12)).format(x)
    return t.rstrip('0').rstrip('.') if '.' in t else t


# 'mainnet' or 'testnet', from knowledge/kivy.json.
def NET():
    return K().get('network') or 'mainnet'


# Kivy's own coin list for the current network: {SYM: {'type', 'decimals', 'icon'?}} (knowledge/kivy.json).
def COINS():
    k = K()
    return k['coins_testnet'] if NET() == 'testnet' else k['coins']


# Every coin Suilend or NAVI lists (lending versions like USDsui / vSUI, other reserves, reward coins like BLUE):
# Kivy can swap, send and show these too (2026-10-20). A protocol listing is the filter — airdropped junk
# tokens are never listed, so they stay invisible and refused. Kivy's own coins keep their names (BTC = xBTC).
_ECO = {'t': 0, 'v': None}
_ECO_LOCK = threading.Lock()   # one coin-list read at a time (a start-up crowd waits for the one read instead of each making it)
_ECO_BG = threading.Lock()     # held while the background refresh runs
ECO_ALIAS = {'XBTC': 'BTC', 'SUIETH': 'ETH', 'USDSUI': 'USDsui'}
# Names by coin TYPE, never by a listing's label (labels change and collide; keys as ecoins' norm(): lower case, no leading zeros).
# 'suiWBTC' is ONLY Sui Bridge's wBTC (0xaafb…::btc::BTC, metadata "wBTC by Sui Bridge"; Suilend lists it as suiWBTC, NAVI as wBTC).
# 0x0041f9…::wbtc::WBTC is LayerZero's OFT "Wrapped BTC", which both lending lists call plain 'WBTC' — a name the Wormhole WBTC
# (0x0277…::coin::COIN) already holds in coin_pins — so it is 'lzWBTC' here. Until 2026-10-29 it was aliased to 'suiWBTC' by
# mistake: the pin refused it ("kivy alert: coin label suiWBTC …") and the coin was invisible to swaps and sends.
ECO_TYPE_ALIAS = {'0xaafb102dd0902f5055cadecd687fb5b71ca82ef0e0285d90afde828ec58ca96b::btc::btc': 'suiWBTC',
                  '0x41f9f9344cac094454cd574e333c4fdb132d7bcc9379bcd4aab485b2a63942::wbtc::wbtc': 'lzWBTC'}


# Extended list: also every coin the lending markets list, via builder.ts 'known-coins' (loaded once at start-up by main).
def ecoins():
    """{SYM: {'type', 'decimals', 'icon'}} — Kivy's coins first, then every listed coin. Cached an hour; after that the old list
    is served while ONE background thread reads the new one, so a visitor never waits (up to 120 s) for it (review scan
    2026-11-15 S6). Only the very first read — no list at all yet — is made in the caller's thread."""
    v = _ECO['v']
    if v and time.time() - _ECO['t'] < 3600:
        return v
    if v:
        _eco_refresh_later()
        return v
    with _ECO_LOCK:
        if _ECO['v'] and time.time() - _ECO['t'] < 3600:   # another request read it while this one waited
            return _ECO['v']
        return _eco_read()


def _eco_refresh_later():
    """Start the background read of the coin list unless one is already running."""
    if not _ECO_BG.acquire(blocking=False):
        return

    def run():
        try:
            with _ECO_LOCK:
                if not (_ECO['v'] and time.time() - _ECO['t'] < 3600):
                    _eco_read()
        except Exception as exc:                     # the old list stays; the next request after a minute tries again
            _ECO['t'] = time.time() - 3600 + 60
            print('kivy coin list: refresh failed (%s), kept the last list' % type(exc).__name__, flush=True)
        finally:
            _ECO_BG.release()
    threading.Thread(target=run, name='kivy-coin-list', daemon=True).start()


# One read of the coin list (builder.ts 'known-coins' + the coin pins). A failed read keeps the last good list's listed coins
# beside Kivy's own (it used to replace them with Kivy's few coins for a minute: USDsui, vSUI … vanished) and retries in a minute.
def _eco_read():
    norm = type_key
    v = {sym: {'type': c['type'], 'decimals': int(c['decimals']), 'icon': None} for sym, c in COINS().items()}
    types, names = {norm(c['type']) for c in v.values()}, {x.lower() for x in v}
    rows = []
    if NET() == 'mainnet':
        try:
            rows = lend_cli('known-coins', timeout=120).get('coins') or []
        except Exception:
            rows = []
    icons = Path(__file__).resolve().parent.parent / 'static' / 'icons'
    pf = Path(os.environ.get('KIVY_COIN_PINS') or (DATA / 'coin_pins.json'))
    try:                                                 # {SYM: coin type} as first seen (review L2, 2026-10-29)
        pins = {k: norm(t) for k, t in json.loads(pf.read_text()).items()} if pf.exists() else {}
    except Exception:
        pins = {}
    by_type = {t: s for s, t in pins.items()}
    fresh = {}
    for c in rows:
        sym = ECO_TYPE_ALIAS.get(norm(c.get('type'))) or by_type.get(norm(c.get('type'))) or ECO_ALIAS.get(str(c.get('sym')).upper(), str(c.get('sym')))
        if pins.get(sym) and pins[sym] != norm(c.get('type')):
            seen = _ECO.setdefault('clash', set())     # once per process, not every hourly reload
            if (sym, norm(c.get('type'))) not in seen:
                seen.add((sym, norm(c.get('type'))))
                print('kivy alert: coin label %s is also listed for %s, but it is pinned to %s — kept the pinned coin' % (sym, c.get('type'), pins[sym]), flush=True)
            continue
        if not re.match(r'^[A-Za-z][A-Za-z0-9.]{1,11}$', sym) or re.search(r'points?$|^lp\b', sym, re.I):
            continue                                     # LP shares and points tokens aren't coins to trade
        if norm(c.get('type')) in types or sym.lower() in names or not re.match(r'^0x[0-9a-fA-F]+::\w+::\w+$', str(c.get('type'))):
            continue                                     # first listing wins (NAVI has two different WBTC)
        types.add(norm(c['type'])); names.add(sym.lower())
        icon = next(('/icons/%s.%s' % (sym, e) for e in ('png', 'svg') if (icons / ('%s.%s' % (sym, e))).exists()), None)
        v[sym] = {'type': c['type'], 'decimals': int(c['dec']), 'icon': icon}
        if sym not in pins:
            fresh[sym] = norm(c['type'])
    if fresh:
        try:
            pf.write_text(json.dumps(dict(pins, **fresh), indent=1, sort_keys=True))
        except Exception:
            pass
    if not rows and _ECO['v']:                         # the read failed: the last good list's listed coins stay
        for sym, c in _ECO['v'].items():
            if sym not in v and norm(c['type']) not in types:
                v[sym] = c
                types.add(norm(c['type']))
        if len(v) > len(COINS()) and NET() == 'mainnet':
            print('kivy coin list: read failed, kept the last list (%d coins)' % len(v), flush=True)
    _ECO['v'], _ECO['t'] = v, (time.time() if rows else time.time() - 3600 + 60)   # a failed read retries in a minute
    return v


def ckey(word):
    """A typed coin name -> Kivy's key for it ('usdsui' -> 'USDsui', 'xbtc' -> 'BTC'), or None if not a known coin."""
    w = re.sub(r'[^A-Za-z0-9.]', '', str(word or ''))
    if not w:
        return None
    up = SPOT_IN.get(w.upper(), ECO_ALIAS.get(w.upper(), w.upper()))
    if up in COINS():
        return up
    e = ecoins()
    if up in e:
        return up
    return next((k for k in e if k == w), None) or next((k for k in e if k.lower() == w.lower()), None)


# Coin info for a Kivy key, from Kivy's list or the extended one; KeyError for an unknown coin.
def cinfo(sym):
    return COINS().get(sym) or ecoins()[sym]


def coin_icon(sym):
    """Kivy's own icon file for a coin ('/icons/USDsui.png'), or None."""
    k = ckey(sym) or sym
    icons = Path(__file__).resolve().parent.parent / 'static' / 'icons'
    return next(('/icons/%s.%s' % (n, e) for n in (k, str(sym)) if re.match(r'^[A-Za-z0-9.]{2,12}$', str(n))
                 for e in ('png', 'svg') if (icons / ('%s.%s' % (n, e))).exists()), None)


def all_balances(address):
    """balances() plus the listed coins outside Kivy's list that the wallet holds (USDsui, vSUI, BLUE…)."""
    b = balances(address)
    if b is None:
        return None
    b = dict(b)
    for x in other_balances(address):
        b[x['sym']] = x['amount']
    return b


GUARD_GAS_MIST = 100_000_000        # most net SUI a Kivy transaction may spend on gas/storage (0.1 SUI; real: ~0.0002-0.012)


# Every POST /api/tx/* answer carries one; the page's check 1 enforces it before signing, and check_tx reads the same
# guard (saved in the plan) after signing.
def guard(out=None, recv=None, ctokens=False, pay=None, gas=GUARD_GAS_MIST):
    """What the approved action may do to the user's wallet. The browser simulates the final transaction
    and refuses to sign if it spends more than `out`, receives less than `recv`, spends other coins,
    or moves the user's objects to someone else (audit T11: a successful simulation is not enough)."""
    return {'out': out or {}, 'in': recv or {}, 'ctokens': ctokens, 'pay': pay_with_max(pay), 'gas': str(gas)}


def pay_with_max(pay):
    """The guard's payments, each with `max`: the most that address may receive of that coin, in base units (review scan S2,
    2026-11-15 — check 2 held a recipient to a minimum only). A caller whose `amount` is a minimum ("the rest", an estimate)
    gives its own `max`; an exact payment's maximum is its amount. Same shape back (one payment or a list)."""
    if pay is None:
        return None
    ps = [dict(p, max=str(p['max'] if p.get('max') is not None else p['amount'])) for p in (pay if isinstance(pay, list) else [pay])]
    return ps if isinstance(pay, list) else ps[0]


def plain_num(x):
    """The shortest exact decimal for a float, never in e-notation ('84123.45', '0.0000123') — for builder arguments."""
    t = repr(float(x))
    if 'e' in t or 'E' in t:
        from decimal import Decimal
        t = format(Decimal(t), 'f')
    return t[:-2] if t.endswith('.0') else t


def base_units(amt, decimals):
    """An amount in exact base units: decimal maths, rounded DOWN (never more than the user typed) — audit V16."""
    from decimal import Decimal, ROUND_DOWN
    return int((Decimal(str(amt)) * (Decimal(10) ** int(decimals))).to_integral_value(rounding=ROUND_DOWN))


def guard_units(coin, amt):
    """Approved amount in base units, with 0.1% + 1 unit of room for float rounding."""
    c = cinfo(SPOT_IN.get(coin, coin))
    return c['type'], str(int(math.ceil(float(amt) * 10 ** int(c['decimals']) * 1.001)) + 1)


def grpc_backup(cmd, *args, timeout=15):
    """Sui gRPC through kivy-lend (Mysten's fullnode): the backup when GraphQL fails. Public JSON-RPC is retired, 2026-10-23."""
    q = urllib.parse.urlencode([('a', x) for x in args])
    d = json.load(urllib.request.urlopen('%s/%s?%s' % (BUILDER, cmd, q), timeout=timeout))
    if not d.get('ok'):
        raise RuntimeError(str(d.get('error'))[:200])
    return d


# Chain gateway shadow mode (2026-11-12, "chain gateway A"; shadow.py): the reads below stay the source of truth; after
# each one, shadow_note() hands the answer to a background checker that asks the gateway (kivy-chain, :8798) the same
# question and counts whether they agree. It never blocks, never raises and changes no answer. A copy of this file without
# shadow.py runs without it. Review: GET /internal/shadow, the "kivy shadow:" lines. Cutover = the owner's OK, not before.
try:
    import shadow as SHADOW  # noqa: E402
except ImportError:
    SHADOW = None


def shadow_note(route, params, truth):
    if SHADOW is not None:
        try:
            if NET() == 'mainnet':
                SHADOW.note(route, params, truth)
        except Exception:
            pass


# One Sui GraphQL query (URL from knowledge/kivy.json 'graphql' for the network); raises on GraphQL errors.
def gql(query, variables=None):
    url = K()['graphql'][NET()]
    req = urllib.request.Request(url, data=json.dumps({'query': query, 'variables': variables or {}}).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'kivy/1'})
    out = json.load(urllib.request.urlopen(req, timeout=15))
    if out.get('errors'):
        raise RuntimeError(str(out['errors'])[:200])
    return out['data']


def balances(address):
    """{SYMBOL: amount} for Kivy's coins at `address` on the current network. Read-only, cached 20s."""
    if not address or not ADDR_RE.match(address):
        return None
    hit = _BAL.get((NET(), address))
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    coins = COINS()
    by_type = {v['type']: (sym, int(v['decimals'])) for sym, v in coins.items()}
    norm = lambda t: re.sub(r'^0x0*', '0x', t)            # 0x000…02::sui::SUI == 0x2::sui::SUI
    by_type.update({norm(t): v for t, v in list(by_type.items())})
    out = {sym: 0.0 for sym in coins}
    try:                                                 # GraphQL first (JSON-RPC is being retired); every page of coins
        rows, cur = [], None
        for _ in range(10):
            d = gql('query($a: SuiAddress!, $c: String) { address(address: $a) { balances(first: 50, after: $c) { pageInfo { hasNextPage endCursor } nodes { coinType { repr } totalBalance } } } }', {'a': address, 'c': cur})
            bl = (d.get('address') or {}).get('balances') or {}
            rows += [(n['coinType']['repr'], n['totalBalance']) for n in bl.get('nodes') or []]
            if not (bl.get('pageInfo') or {}).get('hasNextPage'):
                break
            cur = bl['pageInfo']['endCursor']
        shadow_note('balances', {'a': address}, rows)
    except Exception:
        if NET() != 'mainnet':
            raise
        rows = [(r.get('coinType'), r.get('totalBalance')) for r in grpc_backup('grpc-balances', address, timeout=30)['balances']]
    for t, amt in rows:
        hit_t = by_type.get(t) or by_type.get(norm(t or ''))
        if hit_t:
            out[hit_t[0]] = int(amt or 0) / 10 ** hit_t[1]
    _BAL[(NET(), address)] = (time.time(), out)
    _BAL[('raw', NET(), address)] = (time.time(), rows)
    return out


_TPX = {}


def token_prices(types, max_age=None):
    """USD price by coin type from Aftermath (vSUI ≠ SUI, LBTC ≠ xBTC). Cached a minute; {} on failure."""
    want = [t for t in set(types) if not (_TPX.get(t) and time.time() - _TPX[t][0] < 60)]
    if want:
        try:
            req = urllib.request.Request('https://aftermath.finance/api/price-info', data=json.dumps({'coins': want}).encode(),
                                         headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
            r = json.load(urllib.request.urlopen(req, timeout=15))
            for t in want:
                p = float(((r or {}).get(t) or {}).get('price') or 0)
                if math.isfinite(p) and p > 0:
                    _TPX[t] = (time.time(), p)
        except Exception:
            pass
    now = time.time()   # max_age: a price older than this is not returned (cap checks fail closed during an outage — audit V06)
    return {t: _TPX[t][1] for t in types if t in _TPX and (max_age is None or now - _TPX[t][0] <= max_age)}


def other_balances(address):
    """Coins the wallet holds that Kivy's list leaves out but the lending markets list (USDsui, vSUI, LBTC…):
    [{sym, amount, px, type}]. Read-only, best effort."""
    if balances(address) is None:
        return []
    rows = (_BAL.get(('raw', NET(), address)) or (0, []))[1]
    norm = type_key
    try:
        known = {norm(c['type']): (sym, c['decimals'], c['type'], c.get('icon')) for sym, c in ecoins().items() if sym not in COINS()}
    except Exception:
        return []
    out = []
    for t, amt in rows:
        k = known.get(norm(t))
        if k and int(amt or 0) > 0:
            out.append({'sym': k[0], 'amount': int(amt) / 10 ** k[1], 'type': k[2], 'icon': k[3]})
    px = token_prices([x['type'] for x in out])
    for x in out:
        x['px'] = px.get(x['type']) or coin_px(x['sym'])
    return out

def defi_positions(address):
    """USDC lent through any of Kivy's markets, plus rewards waiting — best effort, never blocks a balance."""
    out = {'lent': [], 'rewards_usd': 0.0}
    try:
        pos = lend_positions(address)
        for p in PROTOS:
            usdc = float((pos.get(p) or {}).get('usdc') or 0)
            if usdc >= 0.01:
                out['lent'].append({'market': PROTOS[p], 'usdc': usdc, 'apr': apr_of(p), 'coin': 'USDC', 'amount': usdc, 'usd': usdc})
    except Exception:
        pass
    for x in other_lent(address):                        # SUI, vSUI, USDsui… lent too
        out['lent'].append({'market': x['name'], 'usdc': 0, 'apr': x['total'], 'coin': x['coin'], 'amount': x['lent'], 'usd': x['lent'] * x['px']})
    try:
        out['rewards_usd'] = float(lend_cli('rewards', address).get('usd') or 0)
    except Exception:
        pass
    try:
        with PERPACCT.quiet():                           # a summary: never "which perps account?" (BLD-V1)
            a = perp_account(address)
        out['perps'] = float(a.get('collateral') or 0) if a.get('has') else 0.0
    except Exception:
        out['perps'] = 0.0
    return out


# The wallet's coins as plain text with $ values — the rule-based reply()'s balance answer.
def balances_text(address):
    k = K()
    b = balances(address)
    if b is None:
        return k['no_wallet']
    px = prices()
    held = [(sym, amt) for sym, amt in b.items() if amt > 0]
    for x in other_balances(address):
        held.append((x['sym'], x['amount']))
        if x['px']:
            px = dict(px, **{x['sym']: x['px']})
    d = defi_positions(address)
    if not held and not d['lent'] and d.get('perps', 0) < 0.01:
        return 'You have 0 USDC.'
    parts = []
    if held:
        parts.append('Found in your wallet:\n' + '\n'.join(
            '• %s  %s%s' % (sym, dec(amt), ('  (≈ $%.2f)' % (amt * px[sym]) if sym in px else '')) for sym, amt in held))
    if d['lent']:
        parts.append('Lent (earning):\n' + '\n'.join(
            '• %s %s on %s%s' % (dec(x['amount']), x['coin'], x['market'], ('  · %.2f%% a year' % x['apr']) if x['apr'] else '') for x in d['lent']))
    if d.get('perps', 0) >= 0.01:
        parts.append('Aftermath perps account: %s USDC collateral' % dec(d['perps']))
    if d['rewards_usd'] > 0:
        parts.append('Rewards to claim: %s — say "claim rewards".' % usd_small(d['rewards_usd']))
    return '\n\n'.join(parts)


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import perp_account  # noqa: E402
from .situations import usd_small  # noqa: E402
from .lending import PROTOS, apr_of, coin_px, lend_positions, other_lent  # noqa: E402
