"""Transactions read back, Kivy's fees, the swap router, spot charts, order books (Section 19).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# Called by check 2, the AI tools and the Handler's chart, book and swap routes (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import json
import re
import time
import urllib.request
from urllib.parse import quote
from .config import K, disp
from .policy import call_watch, kivy_pkgs, pkg_watch, policy_consts
from .upstream import lend_post


# ── Section 19: transactions read back, fees, the swap router, spot charts, order books — Sui GraphQL (tx_detail),
#    Aftermath router (+ Cetus backup via builder.ts), DeepBook indexer, GeckoTerminal ──
DIGEST_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
# Suilend's receipt type, by the package that defines it (builder/suilend.ts CTOKEN_ORIGIN): only this one is a "Suilend receipt";
# a look-alike '…::reserve::CToken<…>' from any other package is an unknown coin (fix package 4, 2026-11-17)
SL_CTOKEN = '0xf95b06141ed4a174f239417323bde3f209b972f5930d8521ea38a52aff3a6ddf::reserve::CToken<'


# SuiVision link for a digest on the current network.
def explorer_tx(digest):
    return ('https://suivision.xyz/txblock/' if NET() == 'mainnet' else 'https://%s.suivision.xyz/txblock/' % NET()) + digest


ROUTER_FEE_WALLETS_SRV = {str(x).lower() for x in policy_consts('route_fee_wallets')}   # kivy_pkgs.json calls.consts (change 5)   # Aftermath router's fee wallet (the page trusts every route-only coin, 2026-10-25)
# the swap aggregators' entry programs: Aftermath router, Cetus aggregator v3 (Bluefin 7k dropped 2026-11-04). 2026-10-28: "hop from dex agg
# (aftermath, cetus, bluefin swap aggregator) is ok, where it go" — a coin only the route touches may go to any address
# (a pool's own fee wallet, e.g. MAGMA in an Aftermath AMM pool); the user's own coins keep the 1% limit and every receive check
# A route-only coin may go anywhere ONLY in a pure aggregator swap: an aggregator is called and nothing outside a router
# pulls value out of a protocol (review pre-launch H1, 2026-10-28: with 'any call to an aggregator', a crafted transaction
# could add a lending withdraw / perps collateral withdraw and send that coin — which never touches the wallet — to anyone).
# Framework (0x1, 0x2) helpers don't count. False positives only fall back to the strict rule.
OUTFLOW_FN = re.compile(policy_consts('pulls_value') or '.', re.I)   # kivy_pkgs.json calls.consts (change 5); missing = every call counts (stricter)


# check_tx: True when the calls are a plain aggregator swap (an allowlisted aggregator package, nothing pulling coins out),
# so a coin only the route touched is noted, not flagged.
def pure_agg_swap(calls):
    """Trust is anchored to package ids, never to a module name (review user-harm H1, 2026-10-29: '0xEVIL::router::drain' kept the
    swap 'pure'): a call to a package off the allowlist (or an unreadable allowlist) makes it not pure, so the strict rule applies.
    A vetted (allowlisted) aggregator adapter may have an outflow-sounding name — Aftermath's 0x05bb…::router::redeem_w1 (an LST
    redeem leg in real dust sweeps, 2026-10-28) — so 'router' calls of ALLOWLISTED packages don't count as pulls."""
    n = lambda a: re.sub(r'^0x0*', '', str(a or '').lower())
    cs = [str(c).split('::') for c in calls or [] if str(c).count('::') >= 2]
    allow = {n(p) for p in kivy_pkgs()} | {'1', '2', '3'}
    agg = any(n(c[0]) in {n(x) for x in AGG_PKGS_SRV} for c in cs)
    pull = any(OUTFLOW_FN.search(c[2]) and n(c[0]) not in ('1', '2') and not (c[1] == 'router' and n(c[0]) in allow) for c in cs)
    unlisted = any(n(c[0]) not in allow for c in cs)
    return bool(agg and not pull and not unlisted)


AGG_PKGS_SRV = {str(x).lower() for x in policy_consts('aggregator_entry')}   # kivy_pkgs.json calls.consts (change 5)   # Bluefin 7k's program dropped with the router (2026-11-04)


def slip_for(a, b):
    """Swap slippage (a fraction) for the pair a -> b: the tightest tier that holds BOTH coins (stablecoins 0.05%, SUI and its
    staked versions 0.1%, big coins 0.2%), else Kivy's ceiling (0.5%). 2026-10-24: "as low as possible". Same rule in builder.ts."""
    k = K()
    cap = float(k['slippage_cap_pct'])
    t = k.get('slippage_tiers') or {}
    a, b = str(disp(a) if a else '').lower(), str(disp(b) if b else '').lower()
    best = cap
    for name, tier in t.items():
        if name.startswith('_') or not isinstance(tier, dict):
            continue
        cs = {str(c).lower() for c in tier.get('coins') or []}
        if a in cs and b in cs:
            best = min(best, float(tier.get('pct') or cap))
    return best / 100.0


# A landed transaction never changes (QA 2026-11-17 SRV-V5 / ATK-S2, fix package 5): one chain read per digest, not one per ask
# (240 a minute from one address went to the one free GraphQL address the whole server shares). Only finished answers are kept.
_TXD, TXD_MAX = {}, 5000


def tx_detail(digest):
    """What a transaction did, read from the chain: who sent what to whom, fee, time (cached once it has landed)."""
    hit = _TXD.get(digest)
    if hit is not None:
        return json.loads(hit)
    out = _tx_detail_read(digest)
    if isinstance(out, dict) and not out.get('error') and out.get('status') and out.get('time_utc'):
        if len(_TXD) >= TXD_MAX:
            for k in list(_TXD)[:TXD_MAX // 5]:   # the oldest fifth goes (dicts keep insertion order)
                _TXD.pop(k, None)
        _TXD[digest] = json.dumps(out)
    return out


def _tx_detail_read(digest):
    if not DIGEST_RE.match(digest or ''):
        return {'error': 'That does not look like a transaction id.'}
    q = ('query($d: String!) { transaction(digest: $d) { digest sender { address } effects { status timestamp '
         'executionError { message abortCode module { name } } '
         'balanceChanges { nodes { owner { address } coinType { repr } amount } } '
         'gasEffects { gasSummary { computationCost storageCost storageRebate } } } } }')
    t = (gql(q, {'d': digest}) or {}).get('transaction')
    shadow_note('tx', {'d': digest}, t)
    if not t or not t.get('effects'):
        return {'error': 'Not found on the chain yet — it can take a few seconds.'}
    e = t['effects']
    norm = lambda x: re.sub(r'^0x0*', '0x', x or '')
    syms = {norm(v['type']): (sym, int(v['decimals'])) for sym, v in ecoins().items()}   # every coin Kivy knows (USDsui has 6 decimals, not the default 9 — 2026-10-26)
    sender = (t.get('sender') or {}).get('address')
    g = e.get('gasEffects', {}).get('gasSummary') or {}
    fee_mist = int(g.get('computationCost') or 0) + int(g.get('storageCost') or 0) - int(g.get('storageRebate') or 0)
    by_coin, unknown = {}, set()                      # unknown: names given to types off Kivy's list (their moves say unknown: true)
    for n in (e.get('balanceChanges') or {}).get('nodes') or []:
        ct = norm(n['coinType']['repr'])
        if ct in syms:
            sym, dp = syms[ct]
        elif ct.startswith(SL_CTOKEN):
            sym, dp = 'Suilend receipt (cUSDC)' if syms.get(norm(ct.rstrip('>').split(',')[-1].strip()), ('',))[0] == 'USDC' else 'Suilend receipt', 6
        else:                                         # never a listed name for a type off the list: anyone can publish '::usdc::USDC'
            sym, dp = coin_name(ct), 9                # (QA 2026-11-17 OPS-V1 / ATK-V3); a fake '::sui::SUI' is never added to the SUI line
            unknown.add(sym)
        amt = int(n['amount']) / 10 ** dp
        if sym == 'SUI' and (n['owner'] or {}).get('address') == sender and fee_mist > 0:
            amt += fee_mist / 1e9                      # the fee is shown on its own line
        if abs(amt) > 1e-12:
            by_coin.setdefault(sym, []).append(((n['owner'] or {}).get('address'), amt))
    # Kivy's own fee: what Kivy's fee address received in SUI — shown like the network fee, not as a payment (2026-10-24).
    # The swap router's own fee wallet is part of the swap, not a payment either.
    kf, kfee = kivy_fee(), 0.0
    if by_coin.get('SUI'):
        rows = by_coin['SUI']
        if kf:
            kfee = sum(x for a, x in rows if str(a or '').lower() == kf['address'] and x > 0)
        drop = {kf['address']} if kfee else set()
        drop |= ROUTER_FEE_WALLETS_SRV
        rows = [(a, x) for a, x in rows if str(a or '').lower() not in drop]
        rows = [(a, x + kfee) if a == sender and x < 0 else (a, x) for a, x in rows]   # the sender's SUI out, less Kivy's fee
        by_coin['SUI'] = [(a, x) for a, x in rows if abs(x) > 1e-12]
        if not by_coin['SUI']:
            del by_coin['SUI']
    moves = []
    for sym, rows in by_coin.items():
        outs = [(a, -x) for a, x in rows if x < 0]
        ins = [(a, x) for a, x in rows if x > 0]
        unk = {'unknown': True} if sym in unknown else {}   # the page shows such a coin with no icon
        for a, x in outs:
            moves.append({'coin': disp(sym), 'amount': dec(x, 9, False), 'from': a, 'to': ins[0][0] if len(ins) == 1 else None, **unk})
        if not outs:
            for a, x in ins:
                moves.append({'coin': disp(sym), 'amount': dec(x, 9, False), 'from': None, 'to': a, **unk})
    return {'digest': t['digest'], 'status': e.get('status'), 'time_utc': e.get('timestamp'), 'sender': sender,
            **({'why': tx_err_plain(e.get('executionError'))} if str(e.get('status')).upper() != 'SUCCESS' and e.get('executionError') else {}),   # failed: why, in plain words
            'moves': moves, 'fee_sui': dec(max(fee_mist, 0) / 1e9, 6, False), 'kivy_fee_sui': dec(kfee, 6, False) if kfee else '0',
            'explorer': explorer_tx(t['digest'])}


def rev_fee():
    """Kivy's own swap fee for Aftermath's router ({recipient, feePercentage}), or None while it is off.
    Set in knowledge/kivy.json 'revenue' — built 2026-10-20, OFF during testing."""
    r = K().get('revenue') or {}
    try:
        pct = float(r.get('swap_fee_pct') or 0)
    except (TypeError, ValueError):
        return None
    addr = str(r.get('address') or '')
    if r.get('enabled') is True and ADDR_RE.match(addr) and 0 < pct <= 1:
        return {'recipient': addr.lower(), 'feePercentage': pct / 100}
    return None


def kivy_fee():
    """Kivy's flat fee per transaction: {'address', 'mist', 'sui'} or None while off (knowledge/kivy.json 'revenue', 2026-10-22)."""
    r = K().get('revenue') or {}
    try:
        sui = float(r.get('flat_fee_sui') or 0)
    except (TypeError, ValueError):
        return None
    addr = str(r.get('address') or '')
    if r.get('enabled') is True and ADDR_RE.match(addr) and 0 < sui <= 1:
        return {'address': addr.lower(), 'mist': str(int(round(sui * 1e9))), 'sui': sui}
    return None


# Kivy's Aftermath referral code from knowledge/kivy.json, or '' (used by GET /api/fee and POST /api/perp/referral).
def ref_code():
    c = str((K().get('revenue') or {}).get('aftermath_ref_code') or '').strip()
    return c if re.match(r'^[A-Za-z0-9_-]{2,40}$', c) else ''


# Kivy's swap fee as a %, for the cards' cost line (0 while off).
def rev_fee_pct():
    f = rev_fee()
    return round(f['feePercentage'] * 100, 4) if f else 0


def rev_lend_pct():
    """Kivy's fee on lending deposits, in % (0 while off). Split off the deposit by the builder."""
    if not rev_fee():                                     # same switch and address as the swap fee
        return 0
    try:
        p = float((K().get('revenue') or {}).get('lend_fee_pct') or 0)
    except (TypeError, ValueError):
        return 0
    return p if 0 < p <= 1 else 0


def route_body(body):
    """A router request: Kivy's fee (paid out of the swap, inside the quote), and never through a blocked protocol
    (knowledge/kivy.json route_blocklist — Metastable is winding down, 2026-10-25)."""
    if rev_fee():
        body['externalFee'] = rev_fee()
    if K().get('route_blocklist'):
        body['protocolBlacklist'] = list(K()['route_blocklist'])
    return body


# Called by POST /api/tx/swap: Aftermath trade-route + v1/transactions/trade (a tx kind the browser completes), then builder.ts
# /swapcheck runs it; a failing route is rebuilt by /swapalt (Cetus).
def swap_tx(addr, a, b, amt, min_out=0):
    """Aftermath router: route + a complete transaction for this wallet to sign (1% slippage)."""
    k = K(); ca, cb = cinfo(a), cinfo(b)
    base = str(base_units(amt, ca['decimals'])) + 'n'
    def post(p, body):
        req = urllib.request.Request('https://aftermath.finance/api/router/' + p, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
        return urllib.request.urlopen(req, timeout=30).read().decode()
    body = route_body({'coinInType': ca['type'], 'coinOutType': cb['type'], 'coinInAmount': base})
    route = json.loads(post('trade-route', body))
    slip = slip_for(a, b)
    out = int(str(route['coinOut']['amount']).rstrip('n')) / 10 ** int(cb['decimals'])
    if min_out and out > 0 and out * (1 - slip) < min_out <= out:   # the chain itself must enforce the minimum the user approved (user-harm #10, H30)
        slip = max(0.0001, 1 - min_out / out)
    # v1 endpoint: returns the transaction *kind*; the browser adds sender + gas. The old unversioned
    # 'transactions/trade' fails when the input coin sits in an address balance (gasless USDC lands there).
    try:
        kind = json.loads(post('v1/transactions/trade', {'walletAddress': addr, 'completeRoute': route, 'slippage': slip}))['txKind']
    except urllib.error.HTTPError as exc:            # Aftermath's own test run aborted (often: the price moved and the approved minimum
        print('kivy swap: Aftermath refused to build (%s), trying Cetus' % str(exc)[:120], flush=True)   # is tight) -> backups (2026-10-29)
        kind = None
    res = {'tx_kind': kind, 'expected_out': out, 'min_out': out * (1 - slip), 'coin_out': b, 'fee_pct': rev_fee_pct(), 'router': 'Aftermath'}
    # the transaction is run here first; when Aftermath's route fails in that run, the same swap is built with Cetus
    # (same coins, amount and slippage; its programs are on the allowlist). Bluefin 7k dropped 2026-11-04 ("only aftermath and cetus")
    chk = {}
    try:
        chk = lend_post('/swapcheck', {'address': addr, 'kind': kind}, timeout=60) if kind else {'ok': False, 'route_fail': True}
        unlisted = []                                 # Aftermath's route calls a program not on the allowlist: rebuild it, don't refuse (review H1 follow-up 2026-10-29)
        if not (chk.get('ok') is False and chk.get('route_fail')):
            kc = lend_post('/kind-calls', {'kind': kind}, timeout=20)
            if kc.get('ok') and kc.get('calls'):
                kt = {'commands': [{'MoveCall': c} for c in kc['calls']]}
                unlisted = pkg_watch('/api/tx/swap', kt) or call_watch('/api/tx/swap', kt, {'swap'})   # + the call rules (2026-11-13)
        if (chk.get('ok') is False and chk.get('route_fail')) or unlisted:
            alt = lend_post('/swapalt', {'address': addr, 'from': ca['type'], 'to': cb['type'], 'amount': base, 'slip': slip,
                                         'min': str(base_units(min_out, cb['decimals'])) if min_out else '0'}, timeout=90)   # the approved minimum, enforced on chain by the backup too (audit nv #5)
            if alt.get('ok'):
                d = 10 ** int(cb['decimals'])
                res.update(tx_kind=alt['tx_kind'], expected_out=int(alt['expected_base']) / d, min_out=int(alt['min_base']) / d, router=alt.get('router'))
            else:
                print('kivy swap backup failed: %s' % str(alt.get('error'))[:200], flush=True)
    except Exception as exc:                          # the check is extra: if it can't run, the card works as before
        print('kivy swap check skipped: %s' % str(exc)[:160], flush=True)
    if not res.get('tx_kind'):                       # Aftermath refused and no backup built it: the same plain error as before
        raise RuntimeError('no router could build this swap right now')
    res.update(swap_gas(chk, kind, res))
    return res


def swap_gas(chk, kind, res):
    """The network fee of THIS transaction, from the builder's own run of it (/swapcheck), for the swap card's fee line: the
    page then needs no second build to show it (fix package 4, 2026-11-15). Nothing when the card got another transaction
    (the Cetus backup) or the run gave no numbers; the page then estimates it itself, as before."""
    g = chk.get('gas') if isinstance(chk, dict) and chk.get('ok') is True else None
    if not isinstance(g, dict) or res.get('tx_kind') != kind:
        return {}
    try:
        net = int(g['comp']) + int(g['storage']) - int(g['rebate'])
    except (KeyError, TypeError, ValueError):
        return {}
    return {'gas_sui': net / 1e9}


_QUOTE = {}


def swap_quote(a, b, amt):
    """Live quote only (no transaction): what `amt` of a buys of b right now. Cached 5 s per pair+amount."""
    key = (a, b, round(amt, 9), rev_fee_pct())
    hit = _QUOTE.get(key)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    ca, cb = cinfo(a), cinfo(b)
    base = str(base_units(amt, ca['decimals'])) + 'n'
    body = route_body({'coinInType': ca['type'], 'coinOutType': cb['type'], 'coinInAmount': base})
    req = urllib.request.Request('https://aftermath.finance/api/router/trade-route', data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
    route = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    out = int(str(route['coinOut']['amount']).rstrip('n')) / 10 ** int(cb['decimals'])
    slip = slip_for(a, b)
    px = dict(prices())
    for c in (a, b):
        if c not in px:
            px[c] = coin_px(c)
    r = {'out': out, 'min_out': out * (1 - slip), 'rate': (out / amt) if amt else 0, 'slippage_pct': slip * 100, 'fee_pct': rev_fee_pct(),
         'px_from': px.get(a), 'px_to': px.get(b), 'value_usd': amt * px[a] if a in px else None}
    _QUOTE[key] = (time.time(), r)
    return r


_SPOT = {}


DEEPBOOK = 'https://deepbook-indexer.mainnet.mystenlabs.com/'


def deepbook_pool(sym):
    """Kivy coin -> its DeepBook <COIN>_USDC pool name, matched by exact coin type (cached a day)."""
    hit = _SPOT.get('dbpools')
    if not hit or time.time() - hit[0] > 86400:
        norm = lambda t: re.sub(r'^0x0*', '0x', t.lower() if t.startswith('0x') else '0x' + t.lower())
        pools = json.load(urllib.request.urlopen(urllib.request.Request(DEEPBOOK + 'get_pools', headers={'User-Agent': 'kivy'}), timeout=15))
        bt = {norm(p['base_asset_id']): p['pool_name'] for p in pools if p['pool_name'].endswith('_USDC')}
        hit = (time.time(), {c: bt.get(norm(v['type'])) for c, v in COINS().items()})
        _SPOT['dbpools'] = hit
    return hit[1].get(sym)


def deepbook_chart(sym, pool):
    """Spot price straight from DeepBook's order book: 15-min candles (cached 15 s) + last trade price (cached 3 s)."""
    get = lambda u: json.load(urllib.request.urlopen(urllib.request.Request(DEEPBOOK + u, headers={'User-Agent': 'kivy'}), timeout=15))
    hit = _SPOT.get(('db', sym))
    if not hit or time.time() - hit[0] > 15:
        c = get('ohclv/%s?interval=15m&limit=200' % pool).get('candles') or []
        hit = (time.time(), sorted([[int(x[0] // 1000), x[1], x[2], x[3], x[4], (x[5] if len(x) > 5 else 0) * x[4]] for x in c]))   # volume in USD
        _SPOT[('db', sym)] = hit
    tk = _SPOT.get('dbticker')
    if not tk or time.time() - tk[0] > 3:
        tk = (time.time(), get('ticker'))
        _SPOT['dbticker'] = tk
    last = (tk[1].get(pool) or {}).get('last_price')
    return {'symbol': sym, 'candles': hit[1], 'mark': last or (hit[1][-1][4] if hit[1] else None), 'source': 'DeepBook ' + pool.replace('_', '/')}


# GET /api/spot/chart: DeepBook candles when the pool trades enough, else GeckoTerminal (gecko_chart).
def spot_chart(sym):
    try:                                              # DeepBook first (on-chain order book); DEX-pool data otherwise
        pool = deepbook_pool(sym)
        if pool:
            r = deepbook_chart(sym, pool)
            # a thin pool (few recent trades) gives a stale price: only trust DeepBook when it actually trades
            if len(r['candles']) >= 40 and time.time() - r['candles'][-1][0] < 3 * 3600:
                return r
    except Exception:
        pass
    return gecko_chart(sym)


def gecko_chart(sym):
    """Price chart for a coin with no perps market (WAL, DEEP): 15-min candles from its deepest Sui DEX pool
    (GeckoTerminal, free and rate-limited -> pool cached for a day, candles for 60 s) + Aftermath's live price."""
    t = COINS()[sym]['type']
    get = lambda u: json.load(urllib.request.urlopen(urllib.request.Request(u, headers={'accept': 'application/json', 'User-Agent': 'kivy'}), timeout=20))
    try:
        hit = _spot_candles(sym, t, get, quote)
    except Exception:                                 # rate-limited / down: last good candles, if any
        hit = _SPOT.get(('ohlc', sym)) or (0, [])
    live = None
    try:
        lp = _SPOT.get(('px', sym))
        if lp and time.time() - lp[0] < 5:
            live = lp[1]
        else:
            r = json.load(urllib.request.urlopen(urllib.request.Request('https://aftermath.finance/api/price-info', data=json.dumps({'coins': [t]}).encode(),
                                                 headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'}), timeout=15))
            live = float(list(r.values())[0]['price'])
            _SPOT[('px', sym)] = (time.time(), live)
    except Exception:
        pass
    return {'symbol': sym, 'candles': hit[1], 'mark': live or (hit[1][-1][4] if hit[1] else None), 'source': 'Sui DEX pools'}


# GeckoTerminal: the coin's deepest Sui pool (cached a day) and its 15-min candles (cached a minute).
def _spot_candles(sym, t, get, quote):
    pool = _SPOT.get(('pool', sym))
    if not pool or time.time() - pool[0] > 86400:
        d = get('https://api.geckoterminal.com/api/v2/networks/sui-network/tokens/%s/pools?page=1' % quote(t, safe=''))
        pool = (time.time(), d['data'][0]['attributes']['address'])
        _SPOT[('pool', sym)] = pool
    hit = _SPOT.get(('ohlc', sym))
    if not hit or time.time() - hit[0] > 60:
        o = get('https://api.geckoterminal.com/api/v2/networks/sui-network/pools/%s/ohlcv/minute?aggregate=15&limit=200&token=%s'
                % (quote(pool[1], safe=''), quote(t, safe='')))
        c = sorted([[int(x[0]), x[1], x[2], x[3], x[4], (x[5] if len(x) > 5 else 0)] for x in o['data']['attributes']['ohlcv_list']])
        hit = (time.time(), c)
        _SPOT[('ohlc', sym)] = hit
    return hit


_BOOK = {}


def chart_stats(sym):
    """Funding, open interest (in coins) and 24h volume for one perps market."""
    mk = (af_post('markets', {'marketIds': [af_markets()[sym]]}).get('marketDatas') or [{}])[0].get('market') or {}
    return {'funding_pct_h': float(mk.get('estimatedFundingRate') or 0) * 100,
            'next_funding_ms': int(str(mk.get('nextFundingTimestampMs') or 0).rstrip('n') or 0),
            'oi': float((mk.get('marketState') or {}).get('openInterest') or 0), 'index': float(mk.get('indexPrice') or 0),
            'volume_24h': perp_market(sym).get('volume')}


def market_book(venue, sym):
    """Order book (10 levels a side) + last 20 trades. venue 'perp' = Aftermath perps, 'spot' = DeepBook. Cached 2 s."""
    key = (venue, sym)
    hit = _BOOK.get(key)
    if hit and time.time() - hit[0] < 2:
        return hit[1]
    if venue == 'perp':
        mid = af_markets()[sym]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as pool:              # book and trades at the same time (2026-10-21)
            f_tr = pool.submit(af_post, 'market/order-history', {'marketId': mid, 'limit': 50})
            ob = ((af_post('markets/orderbooks', {'marketIds': [mid]}).get('orderbooks') or [{}])[0].get('orderbook') or {})
            tr_all = f_tr.result()
        asks = sorted([[float(x['price']), float(x['size'])] for x in ob.get('asks') or []])[:60]
        bids = sorted([[float(x['price']), float(x['size'])] for x in ob.get('bids') or []], reverse=True)[:60]
        tr = tr_all.get('orders') or []
        trades = [{'t': int(x['timestamp']), 'side': 'buy' if x.get('side') == 0 else 'sell', 'price': float(x.get('orderPrice') or 0),
                   'size': float(x.get('sizeFilled') or 0)} for x in tr]
    else:
        pool = deepbook_pool(sym)
        if not pool:
            raise ValueError('no DeepBook pool')
        get = lambda u: json.load(urllib.request.urlopen(urllib.request.Request(DEEPBOOK + u, headers={'User-Agent': 'kivy'}), timeout=15))
        ob = get('orderbook/%s?level=2&depth=60' % pool)
        asks = sorted([[float(p), float(q)] for p, q in ob.get('asks') or []])[:60]
        bids = sorted([[float(p), float(q)] for p, q in ob.get('bids') or []], reverse=True)[:60]
        trades = [{'t': int(x['timestamp']), 'side': 'buy' if x.get('type') == 'buy' else 'sell', 'price': float(x['price']),
                   'size': float(x.get('base_volume') or 0)} for x in (get('trades/%s?limit=50' % pool) or [])]
    r = {'asks': asks, 'bids': bids, 'trades': trades[:50], 'market_id': af_markets()[sym] if venue == 'perp' else None}
    _BOOK[key] = (time.time(), r)
    return r


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import af_markets, af_post, perp_market  # noqa: E402
from .journeys import coin_name, tx_err_plain  # noqa: E402
from .chainreads import ADDR_RE, COINS, NET, base_units, cinfo, dec, ecoins, gql, prices, shadow_note  # noqa: E402
from .lending import coin_px  # noqa: E402
