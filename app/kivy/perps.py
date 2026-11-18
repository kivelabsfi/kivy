"""Perps on Aftermath: markets, charts, the perps account, trade sentences, perps math (Section 4).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# Reads aftermath.finance REST directly; wallet perps data and every perps transaction go through builder.ts (lend_cli).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import json
import math
import re
import threading
import time
import urllib.request
from . import amounts as AM
from . import perpacct as PERPACCT
from . import perpwords as PERPWORDS
from .config import K, SPOT_NAME, disp, ui_cap
from .patterns import (
    MKT_SPACE_RE, PERP_DEP_RE, PERP_RE, PERP_WD_RE, TICKER_WORD_RE, TRADE_LEV_RE, TRADE_PX_RE, TRADE_SIZE_RE,
    _PERP)
from .upstream import lend_cli


# ── Section 4: perps on Aftermath — markets, charts, the perps account, trade sentences and perps math. Reads aftermath.finance
#    REST directly; wallet-specific perps data and every perps transaction go through builder.ts (lend_cli 'perp-*') ──
# ── live charts: Aftermath perps candles (crypto, stocks, indexes, gold/oil) ──
_MK = {'t': 0, 'v': {}}
_CANDLES = {}
TF_SPAN = {'1m': 6 * 3600, '15m': 3 * 86400, '30m': 6 * 86400, '1h': 12 * 86400, '4h': 50 * 86400,
           '1d': 300 * 86400, '3d': 365 * 86400, '1w': 365 * 86400}   # Aftermath: no 5m; history starts 2026-08-17, too-long ranges error
CHART_ALIASES = {'XBTC': 'BTC', 'GOLD': 'XAUT', 'XAU': 'XAUT', 'SILVER': 'XAG', 'OIL': 'WTI', 'SP500': 'US500', 'S&P': 'US500', 'SPX': 'SPX',
                 'NASDAQ': 'US100', 'NDX': 'US100', 'APPLE': 'AAPL', 'TESLA': 'TSLA', 'NVIDIA': 'NVDA', 'GOOGLE': 'GOOGL', 'BITCOIN': 'BTC',
                 'ETHEREUM': 'ETH', 'SOLANA': 'SOL', 'XBTC': 'BTC'}


# {SYMBOL: market object id} for every Aftermath perps market (cached 1 h); the key every perps call starts from.
def af_markets():
    if time.time() - _MK['t'] < 3600 and _MK['v']:
        return _MK['v']
    req = urllib.request.Request('https://aftermath.finance/api/perpetuals/all-markets',
                                 data=json.dumps({'collateralCoinType': COINS()['USDC']['type']}).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
    ms = json.load(urllib.request.urlopen(req, timeout=20)).get('markets') or []
    v = {m['marketParams']['baseAssetSymbol'].upper().removesuffix('USD'): m['objectId'] for m in ms}
    _MK.update(t=time.time(), v=v)
    return v


# Aftermath only tags Crypto / TradFi; Kivy splits TradFi so it's easier to browse
TRADFI_GROUPS = {'Indexes': {'US500', 'US100', 'JP225', 'KR200', 'INDIA50'},
                 'Commodities': {'XAUT', 'XAG', 'WTI', 'BRENT'},
                 'FX': {'USDKRW', 'USDBRL'}}


# The picker's tab for a market: Crypto, Stocks, Indexes, Commodities or FX.
def market_group(sym, category):
    if category != 'TradFi':
        return category
    for g, members in TRADFI_GROUPS.items():
        if sym in members:
            return g
    return 'Stocks'


_MKMETA = {'t': 0, 'v': {}}
_MKSTATS = {'t': 0, 'v': []}


# POST to Aftermath's public perps API (markets, 24h stats, prices); no key.
def af_post(path, body):
    req = urllib.request.Request('https://aftermath.finance/api/perpetuals/' + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
    return json.load(urllib.request.urlopen(req, timeout=25))


SAFE_IMG = re.compile(r'^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/%+=&?-]*$')


def perp_market(sym):
    """The perps market row for a symbol from the warm list ({} when unknown)."""
    return next((r for r in perp_market_list() if r['symbol'] == sym), {})


def perp_market_list(fresh=False):
    """Every Aftermath perps market with price, 24h change, volume and max leverage. A background thread refreshes
    it every 15 s, so requests read the warm copy and never wait on Aftermath."""
    if not fresh and _MKSTATS['v'] and (time.time() - _MKSTATS['t'] < 60 or _MKSTATS.get('busy')):
        return _MKSTATS['v']                         # older than a minute (the warm loop sleeps while nobody uses Kivy): read it fresh
    mk = af_markets()
    syms, ids = list(mk.keys()), list(mk.values())
    if time.time() - _MKMETA['t'] > 3600 or not _MKMETA['v']:
        meta = {}
        for m in af_post('markets', {'marketIds': ids}).get('marketDatas') or []:
            md, mp = m.get('metadata') or {}, m['market']['marketParams']
            meta[m['market']['objectId']] = {'category': md.get('category') or 'Other', 'name': md.get('displayName') or md.get('symbol'),
                                             'image': md.get('image') if SAFE_IMG.match(str(md.get('image') or '')) else None,
                                             'max_lev': round(1 / float(mp['marginRatioInitial'])) if mp.get('marginRatioInitial') else None}
        _MKMETA.update(t=time.time(), v=meta)
    stats = af_post('markets/24hr-stats', {'marketIds': ids}).get('marketsStats') or []
    funding, extra = {}, {}                          # estimated hourly funding (+ index price, open interest), refreshed with the stats
    try:
        for m in af_post('markets', {'marketIds': ids}).get('marketDatas') or []:
            mk = m.get('market') or {}
            funding[mk.get('objectId')] = float(mk.get('estimatedFundingRate') or 0) * 100
            extra[mk.get('objectId')] = (float(mk.get('indexPrice') or 0), float((mk.get('marketState') or {}).get('openInterest') or 0))
    except Exception:
        pass
    rows = []
    for sym, mid, st in zip(syms, ids, stats):
        md = _MKMETA['v'].get(mid, {})
        rows.append({'symbol': sym, 'name': md.get('name') or sym, 'category': market_group(sym, md.get('category') or 'Other'),
                     'price': st.get('markPrice') or st.get('basePrice'), 'change': round(float(st.get('priceChangePercentage') or 0) * 100, 2),
                     'volume': round(float(st.get('volumeUsd') or 0)), 'max_lev': md.get('max_lev'), 'image': md.get('image'),
                     'funding': funding.get(mid), 'index': (extra.get(mid) or (None, 0))[0] or None,
                     'oi_usd': ((extra.get(mid) or (0, 0))[1] * float(st.get('markPrice') or st.get('basePrice') or 0)) or None})
    rows.sort(key=lambda r: -r['volume'])
    _MKSTATS.update(t=time.time(), v=rows)
    return rows


# A typed name ('gold', 'tesla', 'btc') -> the perps market symbol, or None; used by every chart/perps route and reader.
def chart_symbol(word):
    w = re.sub(r'[^A-Za-z0-9&]', '', str(word or '')).upper()
    w = CHART_ALIASES.get(w, w)
    try:
        mk = af_markets()
    except Exception:
        return None
    return w if w in mk else (w.removesuffix('USD') if w.removesuffix('USD') in mk else None)


_REFRESHING = set()


def stale_ok(store, key, ttl, fn, max_age=300):
    """Cached value: fresh within ttl; up to max_age old it is returned at once and refreshed in the background
    (the trading page opened in 2-3 s waiting on Aftermath — 2026-10-21); older or missing = fetch now."""
    hit = store.get(key)
    age = time.time() - hit[0] if hit else None
    if hit and age < ttl:
        return hit[1]
    if hit and age < max_age:
        if key not in _REFRESHING:
            _REFRESHING.add(key)
            def run():
                try:
                    store[key] = (time.time(), fn())
                except Exception:
                    pass
                finally:
                    _REFRESHING.discard(key)
            threading.Thread(target=run, daemon=True).start()
        return hit[1]
    v = fn()
    store[key] = (time.time(), v)
    return v


# Chart candles for GET /api/chart: cached 5 s, refreshed in the background (stale_ok).
def candles(symbol, tf):
    return stale_ok(_CANDLES, (symbol, tf), 5, lambda: _candles(symbol, tf))


def _candles(symbol, tf):
    now = int(time.time() * 1000)
    body = {'marketId': af_markets()[symbol], 'fromTimestamp': now - TF_SPAN[tf] * 1000, 'toTimestamp': now, 'resolution': tf}
    req = urllib.request.Request('https://aftermath.finance/api/perpetuals/market/candle-history', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
    c = json.load(urllib.request.urlopen(req, timeout=20)).get('candles') or []
    return [[int(x['timestamp'] // 1000), x['open'], x['high'], x['low'], x['close'], x.get('volume') or 0] for x in c]


CHART_RE = re.compile(r'^\s*(?:show\s+(?:me\s+)?(?:the\s+|a\s+)?)?(?:live\s+)?(?:price\s+)?chart(?:\s+(?:of|for))?\s+([A-Za-z0-9&]{2,12})\s*[?.!]*\s*$'
                      r'|^\s*([A-Za-z0-9&]{2,12})\s+(?:live\s+)?(?:price\s+)?chart\s*[?.!]*\s*$', re.I)


# The wallet's Aftermath perps account via builder.ts 'perp-account' (has one? free collateral; every account found, the blocked
# ones, whether the user must pick), for the account this request names (kivy/perpacct.py); cached 15 s. Inside a chat answer an
# answer that says "pick one" raises PERPACCT.Choose, which the chat turns into the choice card (BLD-V1, 2026-11-17).
def perp_account(addr, fresh=False):
    pick = PERPACCT.current()
    hit = _PERP.get(addr)
    if hit and not fresh and time.time() - hit[0] < 15 and len(hit) > 2 and hit[2] == pick:
        return PERPACCT.need(hit[1])
    v = lend_cli('perp-account', addr)
    _PERP[addr] = (time.time(), v, pick)
    return PERPACCT.need(v)


def perp_accounts(msg, address):
    """"my perps accounts" (each account found, to pick from) and "open a new perps account" (the open card, unless the wallet
    already has one Kivy uses). The wallet is judged on its own here: no pick, never the choice-card detour."""
    new = PERPACCT.NEW_RE.match(msg or '')
    if not (new or PERPACCT.LIST_RE.match(msg or '')):
        return None
    if not address:
        return {'text': K()['no_wallet']}
    with PERPACCT.wallet_only():
        acct = perp_account(address, fresh=True)
    bt = PERPACCT.blocked_text(acct)
    if new and acct.get('has'):
        return {'text': 'Account #%s is already yours, with no open orders — Kivy uses it, so a second account isn\'t needed.' % acct.get('id')}
    if new or not acct.get('accounts'):
        return {'text': (bt + '\n' if bt else '') + 'Open a new Aftermath perps account — only your wallet will hold its key. Then deposit USDC as collateral.',
                'widget': {'type': 'perpopen'}}
    return PERPACCT.card(acct, 'perps')


# 'perps', 'deposit 5 to perps', 'withdraw all from aftermath' -> the perps account card (open / deposit / withdraw);
# called near the end of widget_reply. The card's button posts to /api/tx/perp.
def perp_widget(msg, address):
    pa = perp_accounts(msg, address)
    if pa:
        return pa
    dm, wm = PERP_DEP_RE.match(msg), PERP_WD_RE.match(msg)
    if not (PERP_RE.match(msg) or dm or wm):
        return None
    if not address:
        return {'text': K()['no_wallet']}
    acct = perp_account(address, fresh=True)
    if not acct.get('has'):                          # none, or only accounts another wallet can trade (⛔, never used: BLD-V1)
        bt = PERPACCT.blocked_text(acct)
        return {'text': (bt + '\nOpen a new one below, then deposit USDC as collateral.') if bt else
                        'You need an Aftermath perps account first. Open it once, then deposit USDC as collateral.',
                'widget': {'type': 'perpopen'}}
    col = float(acct.get('collateral') or 0)
    def preset(m, mx):
        w = (m.group(1) or '').lower() if m else ''
        if not w:
            return 0
        if w in ('all', 'max', 'everything'):
            return mx
        if w == 'half':
            return mx / 2
        if w.endswith('%'):                          # "deposit 50% of my usdc to perps": half, not 50 (QA 2026-11-17, RDR-V5)
            return round(mx * min(float(w.rstrip('% ')), 100.0) / 100, 6)
        return min(float(w), mx)
    if dm:
        have = float((balances(address) or {}).get('USDC') or 0)
        mx = round(have, 6)
        over = re.fullmatch(r'\d+(?:\.\d+)?', dm.group(1) or '') and float(dm.group(1)) > mx + 1e-9   # "deposit 999" holding 150: say it was capped (scan 2026-11-14 C8)
        return {'text': 'Slide to choose how much USDC to deposit into your Aftermath perps account.' + (
                    ' You hold %s USDC, so it starts at that — not the %s you typed.' % (dec(mx), dm.group(1)) if over else ''),
                'widget': {'type': 'perpdep', 'name': 'Aftermath perps', 'balance': have, 'max': mx, 'preset': preset(dm, mx), 'collateral': col}}
    if wm:
        if col <= 0:
            return {'text': 'Your Aftermath perps account has no USDC to withdraw.'}
        return {'text': 'Slide to choose how much USDC to take out of your Aftermath perps account.',
                'widget': {'type': 'perpwd', 'name': 'Aftermath perps', 'balance': col, 'max': round(col, 6), 'preset': preset(wm, round(col, 6)), 'collateral': col}}
    try:
        pos = lend_cli('perp-positions', address)
    except Exception:
        pos = {}
    held = {p['symbol'] for p in pos.get('positions') or []}
    sugg = []                                         # suggested markets removed: the card links to all markets instead
    n = len(pos.get('positions') or [])
    olist = [dict(o, symbol=g['symbol']) for g in (pos.get('positions') or []) + (pos.get('orders') or []) for o in (g.get('orders') or [])]
    no = len(olist)
    text = 'Your Aftermath perps account%s: ' % PERPACCT.number(acct) + (('%d open position%s' % (n, '' if n == 1 else 's')) if n else 'no open positions') + \
           ((' and %d open order%s.' % (no, '' if no == 1 else 's')) if no else '.') + PERPACCT.others(acct)
    return {'text': text, 'widget': {'type': 'perpacct', 'collateral': col, 'free': pos.get('free'), 'equity': pos.get('equity'), 'upnl': pos.get('upnl'),
                                     'positions': pos.get('positions') or [], 'orders': sum(len(g.get('orders') or []) for g in (pos.get('positions') or []) + (pos.get('orders') or [])),
                                     'suggest': sugg, 'order_list': olist}}


def positions_reply(address):
    """"my positions": the positions card of the account in use, named, and whether the wallet holds other accounts (several to
    pick from: the choice card, BLD-V1). Was inline in widget_reply."""
    if not address:
        return {'text': K()['no_wallet']}
    r = PERPACCT.need(lend_cli('perp-positions', address))
    if not r.get('has'):
        return {'text': PERPACCT.blocked_text(r) or 'You have no Aftermath perps account yet. Say "perps" to open one.'}
    return {'text': 'Your Aftermath perps positions and orders%s.%s' % ((' — Account' + PERPACCT.number(r)) if PERPACCT.number(r) else '', PERPACCT.others(r)),
            'widget': {'type': 'perppos', **r}}


PERP_MARKETS_RE = re.compile(r'^\s*(?:show\s+(?:me\s+)?(?:the\s+)?|list\s+(?:the\s+)?|all\s+)?(?:perps?\s+|aftermath\s+|trading\s+)?markets?\s*[?.!]*\s*$'
                             r'|^\s*what\s+can\s+i\s+trade\s*\??\s*$'
                             # "do you have any more markets", "what other markets are there", "show more markets", "which markets do you have"
                             r'|^\s*(?:do\s+you\s+have\s+|are\s+there\s+|got\s+|show\s+(?:me\s+)?|any\s+|what\s+|which\s+)?(?:any\s+)?(?:more|other|all|else|the)?\s*'
                             r'(?:perps?\s+|trading\s+)?markets?(?:\s+(?:do\s+you\s+have|are\s+there|can\s+i\s+trade|available|else|too))?\s*[?.!]*\s*$', re.I)


TRADE_RE = re.compile(r'^\s*(?:open\s+(?:a\s+)?|go\s+)?(long|short|buy|sell)\s+(?:on\s+)?([A-Za-z0-9&]{2,12})'
                      r'(?:\s+(?:with\s+|for\s+)?\$?\s*(\d+(?:\.\d+)?)\s*(?:usd|usdc|dollars?|\$)?)?'
                      r'(?:\s+(?:at\s+|with\s+)?(\d+(?:\.\d+)?)\s*x(?:\s+leverage)?)?(?:,?\s+(?:at\s+)?market(?:\s+order)?)?\s*[?.!]*\s*$', re.I)


POS_RE = re.compile(r'^\s*(?:(?:show|see|check|view)\s+(?:me\s+)?)?(?:my\s+)?(?:open\s+)?(?:perps?\s+|aftermath\s+)?(?:positions?|orders?|trades?)\s*[?.!]*\s*$', re.I)


# "i wanna trade bitcoin", "can i trade sol", "trade eth": spot or perps is the user's call — ask, don't guess
TRADE_ASK_RE = re.compile(r"^\s*(?:i\s+)?(?:wanna|want\s+to|would\s+like\s+to|'?d\s+like\s+to|like\s+to|let'?s|lets|can\s+i|how\s+(?:do|can)\s+i|help\s+me)?\s*"
                          r"trade\s+(?:some\s+)?(?:on\s+)?([A-Za-z0-9&]{2,14})\s*[?.!]*\s*$", re.I)


# a spot buy with no amount ("swap USDC to BTC", "buy BTC"): ask how much, with quick picks from what the wallet holds
SPOT_ASK_RE = re.compile(r'^\s*(?:swap|buy|get)\s+(?:some\s+)?(?:([A-Za-z]{2,8})\s+(?:to|for|into)\s+)?([A-Za-z]{2,10})\s*(?:on\s+spot)?\s*[?.!]*\s*$', re.I)


# "swap my sui", "sell half my sui", "swap all my usdc", "swap 0.1 sui": ONE coin, and it is the coin to PAY with (2026-11-13: a single
# coin was always read as the coin to GET, so "swap my sui" opened a card that buys SUI). Group 2 = amount, 3 = my / all my, 4 = coin.
SPOT_FROM_RE = re.compile(r'^\s*(swap|sell|convert|exchange|trade|change|dump)\s+(?:(\$?\d+(?:\.\d+)?\s*%?|all|half|max|everything|a\s+third|a\s+quarter)\s+(?:of\s+)?)?'
                          r'((?:my|the)\s+|all\s+(?:of\s+)?my\s+|some\s+(?:of\s+my\s+)?)?([A-Za-z]{2,10})\s*[?.!]*\s*$', re.I)


# 'buy sui', 'sell wal', bare 'swap': the spot swap card (Aftermath router quote); called by widget_reply and /api/market/view.
# The card's button posts to /api/tx/swap.
def spot_ask(msg, address):
    bare = bool(re.match(r'^\s*swap\s*[?.!]*\s*$', msg, re.I))
    if bare:                                         # bare "swap" (the menu / Swap button): USDC on top, SUI below — the user picks (2026-10-21)
        if not address:
            return {'text': K()['no_wallet']}
        msg = 'swap usdc to sui'
    spot = [a.upper() for a in (K().get('assets') or [])]
    alias = lambda x: ckey(CHART_ALIASES.get(re.sub(r'[^A-Za-z]', '', x or '').upper(), x or ''))
    keep = {'SUI': 0.05}                                 # leave a little SUI for gas
    fm = None if bare else SPOT_FROM_RE.match(msg)
    pay, preset, bal = None, 0, None
    if fm and alias(fm.group(4)) and fm.group(4).lower() not in ('it', 'that', 'this', 'them', 'all', 'some', 'everything'):
        c, aw, own = alias(fm.group(4)), (fm.group(2) or '').lower().strip(), bool(fm.group(3))
        if address and (aw or own or fm.group(1).lower() in ('swap', 'convert', 'exchange')):
            bal = {k: float(v or 0) for k, v in (all_balances(address) or {}).items()}
            have = max(bal.get(c, 0) - keep.get(c, 0), 0)
            if aw or own:                                # "my sui" / "half my sui" / "0.1 sui": theirs to pay with
                if have <= 0.000001:
                    return {'text': 'You have no %s in your wallet to swap.%s' % (disp(c), lent_hint(address, c))}
                pay = c
            elif have > 0.000001:                        # "swap sui" holding SUI: the coin held pays; holding none it is still the coin to get
                pay = c
            if pay and aw:
                px0 = float(coin_px(c) or 0)
                sh = share_said(aw)
                preset = have if aw in ('all', 'max', 'everything') else have * sh if sh is not None else \
                    (float(aw[1:]) / px0 if px0 else 0) if aw.startswith('$') else float(aw) if re.match(r'^\d+(?:\.\d+)?$', aw) else 0
                if preset > have * 1.000001:
                    return {'text': 'You have %s %s to swap%s — less than %s.' % (dec(have, 6), disp(c), ' (a little SUI stays for gas)' if c == 'SUI' else '', aw)}
        elif not address and (aw or own):
            return {'text': K()['no_wallet']}
    m = SPOT_ASK_RE.match(msg)
    if not m and not pay:
        return None
    if pay:
        to, frm = ('USDC' if pay != 'USDC' else 'SUI'), pay
    else:
        to, frm = alias(m.group(2)), (alias(m.group(1)) if m.group(1) else None)
    if not to or (not pay and m.group(1) and not frm) or frm == to:
        return None
    if not address:
        return {'text': K()['no_wallet']}
    bal = bal if bal is not None else {k: float(v or 0) for k, v in (all_balances(address) or {}).items()}
    spot = spot + [c for c in bal if c not in spot]          # listed coins they hold (USDsui, BLUE…) can pay too
    cands = [frm] if frm else [c for c in ('USDC', 'SUI') if c != to] + [c for c in spot if c not in ('USDC', 'SUI', to)]
    src = next((c for c in cands if bal.get(c, 0) - keep.get(c, 0) > 0.000001), None)
    if bare:
        src = 'USDC'                                 # even with none: the card says so and the user switches coins
    if not src:
        return {'text': 'You have no %s to swap for %s yet. Deposit some first (tap your balance, top right, for your address).' % (frm or 'coins', to)}
    px = dict(prices())
    held = {c: round(max(bal.get(c, 0) - keep.get(c, 0), 0), 9) for c in spot if bal.get(c, 0) - keep.get(c, 0) > 0.000001}
    for c in (src, to):                              # both sides can pay after a flip (the ⇅ button), even at 0
        held.setdefault(c, round(max(bal.get(c, 0) - keep.get(c, 0), 0), 9))
    for c in list(held) + [to]:
        if c not in px:
            px[c] = coin_px(c)
    # every coin Kivy can swap into, Kivy's own first, then lending versions (vSUI, USDsui…), then the rest A-Z (2026-10-20)
    ec = ecoins()
    first = list(COINS()) + [t for ts in ('USDsui', 'vSUI', 'haSUI', 'sSUI', 'LBTC', 'wBTC', 'suiWBTC') for t in [ts] if t in ec]
    to_opts = first + sorted((c for c in ec if c not in first), key=str.lower)
    icons = {c: coin_icon(c) for c in set(to_opts) | set(held)}
    return {'text': 'Pick the coins, type an amount or pick a share; the quote updates live.' if bare else
                    ('Swap %s%s — it starts on %s; pick the coin you want to receive%s. The quote updates live.'
                     % ((dec(preset, 6) + ' ') if preset else 'your ', disp(src), disp(to), '' if preset else ', then type an amount or pick a share')) if pay else
                    'Swap to %s — pick the coins, type an amount or pick a share; the quote updates live.' % disp(to),
            'widget': {'type': 'swap', 'from': src, 'to': to, **({'preset': round(preset, 9)} if pay and preset else {}), 'names': SPOT_NAME, 'held': held, 'cap_usd': ui_cap(), 'prices': px,
                       'slippage_pct': float(K()['slippage_cap_pct']), 'fee_pct': rev_fee_pct(), 'to_opts': to_opts, 'icons': {k: v for k, v in icons.items() if v}}}


BARE_RE = re.compile(r'^\s*(?:the\s+)?([A-Za-z0-9&]{2,14})(?:\s+(?:please|pls|plz))?\s*[?.!]*\s*$', re.I)


def deposit_ask(address, need=None, order=None):
    """Not enough in the perps account to trade: say so plainly, and the deposit card starts at what the order needs
    (2026-10-26: "A, but telling user that you have no funds in perp account, you need to deposit funds to perps account
    to trade" — it started at 0). The user can still slide it; nothing moves until they approve in their wallet."""
    free = 0.0
    try:
        free = float(perp_account(address, fresh=True).get('collateral') or 0)
    except Exception:
        pass
    dep = perp_widget('deposit to perps', address) or {}
    w = dep.get('widget')
    if not w or w.get('type') != 'perpdep':           # no perps account yet: its own "open it first" answer
        return dep
    if free <= 0:
        msg = 'You have no funds in your perps account — you need to deposit funds to your perps account to trade.'
    else:
        msg = 'Your perps account has %s USDC — not enough for this order. Deposit more to your perps account to trade.' % dec(free)
    if need and need > 0:
        want = math.ceil(float(need) * 100) / 100       # up to the cent, so the deposit covers it
        mn, ou = float((order or {}).get('min_usd') or 0), float((order or {}).get('usd') or 0)
        if mn and ou and ou < mn - 1e-9:                # the market's own minimum is what sets the size (perps scan 2026-11-13)
            msg += ' %s\'s minimum order is $%s — more than the $%s you asked for — and that minimum order needs about %s USDC%s.' % (
                order.get('symbol') or 'This market', num_plain(mn), num_plain(ou), dec(want), ' more' if free > 0 else '')
        else:
            msg += ' This order needs about %s USDC%s.' % (dec(want), ' more' if free > 0 else '')
        w['preset'] = round(min(want, float(w.get('max') or 0)), 6)
        if float(w.get('max') or 0) < want:
            msg += ' Your wallet has %s USDC.' % dec(float(w.get('max') or 0))
    return {'text': msg + ' Then ask for the order again.', 'widget': w}


def perp_confirm(address, w):
    """A typed / asked-for order with every detail known -> a short confirmation card (the full panel stays one tap away)."""
    t = (w or {}).get('trade') or {}
    if not address or not (t.get('usd') or 0) >= 1 or not t.get('lev'):
        return w
    try:
        perp_account(address)                        # several accounts, or one with orders on it: the choice card first (BLD-V1)
        pv = lend_cli('perp-preview', address, w['symbol'], int(t.get('side') or 0), float(t['usd']), float(t['lev']))
    except Exception:
        return w
    if not pv.get('ok'):
        return w
    PERPACCT.need(pv)
    ref = float(t.get('price') or 0) if t.get('type') == 'limit' else float(pv.get('mark') or 0)
    return {'type': 'perpconfirm', 'symbol': w['symbol'], 'image': w.get('image'), 'side': int(t.get('side') or 0),
            'usd': float(t['usd']), 'lev': float(t['lev']), 'otype': t.get('type') or 'market', 'price': ref,
            'base': (float(t['usd']) / ref) if ref else pv.get('base'), 'margin': pv.get('margin'), 'liq': pv.get('liquidation_price'),
            'fee': pv.get('fee_usd'), 'taker_pct': pv.get('fee_pct'), 'maker_pct': pv.get('maker_fee_pct'), 'max_lev': pv.get('max_lev'),
            'deposit': pv.get('deposit_needed'), 'mark': pv.get('mark'), 'min_usd': pv.get('min_usd'), 'chart': w}


MKT_OPEN_RE = re.compile(r"^\s*(?:kivy[,\s]+)?(?:(?:can|could)\s+you\s+)?(?:please\s+)?(?:open|show(?:\s+me)?|go\s+to|switch\s+to|bring\s+up|pull\s+up|take\s+me\s+to)"
                         r"\s+(?:the\s+)?([A-Za-z0-9&]{2,14})(?:\s+(?:market|chart|perps?|perpetuals?|spot|page))?(?:\s+(?:market|page))?(?:\s+please)?\s*[?.!]*\s*$", re.I)
MKT_VIEW_RE = re.compile(r"^\s*(?:show\s+(?:me\s+)?(?:the\s+)?|how(?:'s|\s+is)\s+|what(?:'s|\s+is)\s+)?([A-Za-z0-9&]{2,14})\s+"
                         r"(?:market|markets|price action|doing(?:\s+today)?|overview|full picture|order\s*book|orderbook)\s*[?.!]*\s*$", re.I)


TRADE_STOP = {'long', 'short', 'buy', 'sell', 'ape', 'into', 'times', 'ten', 'five', 'margin', 'exposure', 'entry', 'go', 'put', 'in', 'with', 'at', 'kivy', 'can', 'you', 'open', 'leverage', 'usd', 'usdc', 'the', 'and', 'for',
              'max', 'please', 'want', 'wanna', 'to', 'a', 'an', 'of', 'limit', 'order', 'position', 'i', 'me', 'my', 'some', 'perps', 'perp', 'market', 'price'}


def parse_trade(msg):
    """A whole trade sentence -> side, market, size, leverage, limit price ('i wanna buy tesla for 1$ at 350 with 1x leverage')."""
    t = ' ' + msg.lower() + ' '
    sm = re.search(r'\b(long|short|buy|sell|ape)\b', t)
    if not sm:
        return None
    t = re.sub(r'(?<=\d),(?=\d{3}\b)', '', t)          # "80,000" -> "80000"
    # a liquidation price the user wants ("liquidate me at 70000", "liq price 70k"): read, then hidden from the price reader (2026-10-23)
    t = re.sub(r'(\d+(?:\.\d+)?)k\b', lambda m: num_plain(float(m.group(1)) * 1000), t)
    lq = re.search(r'\b(?:get\s+)?liq(?:uidat(?:e|ed|ion))?(?:\s+(?:me|price|level|point))?(?:\s+(?:at|of|@|=|:|is|to\s+be|should\s+be|around|near))?\s*\$?\s*(\d+(?:\.\d+)?)', t)
    liq = float(lq.group(1)) if lq else 0
    if lq:
        t = t[:lq.start()] + ' ' * (lq.end() - lq.start()) + t[lq.end():]
    # "x3", "×3", "3×", "times 3", "$10x3": leverage too — "long eth $10 x3" was a $1 order at 1x, "…$10 times 3" $1 at 10x (QA 2026-11-17, RDR-V7)
    t = re.sub(r'(\$\s*\d+(?:\.\d+)?)\s*[x\u00d7](\d+(?:\.\d+)?)\b', r'\1 \2x', t)
    t = re.sub(r'(?<![\w.$])(?:[x\u00d7]|times)\s?(\d+(?:\.\d+)?)\b(?!\$|\s*(?:x\b|usdc?\b|dollars?\b|%))', r'\1x', t)
    t = re.sub(r'(\d+(?:\.\d+)?)\s*\u00d7', r'\1x', t)
    for wn, n in (('two', 2), ('three', 3), ('five', 5), ('ten', 10), ('twenty', 20), ('twenty-five', 25), ('fifty', 50)):
        t = re.sub(r'\b%s\s+times\b' % wn, ' %dx' % n, t)
    t = re.sub(r'(\d+(?:\.\d+)?)\s+times\b', r'\1x', t)
    lev = TRADE_LEV_RE.search(t)
    pxw = re.search(r'(?:\blimit\s+(?:order\s+)?(?:price\s+(?:of\s+)?|at\s+|@\s*)|\bentry\s+(?:at\s+|price\s+(?:of\s+)?)?|\b(?:limit|short|long|sell|buy)\b[^$\d]{0,25}\bat\s+(?=\$))\$?\s*(\d+(?:\.\d+)?)(?!\s*x\b)', t)
    mg = re.search(r'(?:\b(?:with|using)\s+\$?\s*(\d+(?:\.\d+)?)\s*(?:usdc|usd|\$)?\s*(?:of\s+)?(?:margin|collateral)\b|\b(\d+(?:\.\d+)?)\s*(?:usdc|usd|\$)?\s+(?:margin|collateral)\b)', t)
    # "all my margin", and also "all collateral", "max margin", "full perps balance", "all of the collateral" (2026-10-22)
    allm = re.search(r'\b(?:(?:using\s+|with\s+)?(?:all|max|full|whole|entire)\s+(?:of\s+)?(?:my\s+|the\s+)?(?:available\s+)?(?:perps?\s+)?(?:margin|usdc|collateral|balance)|max\s+usdc)\b', t)
    if pxw:
        t = t[:pxw.start(1)] + ' ' * len(pxw.group(1)) + t[pxw.end(1):]
    if mg:
        t = t[:mg.start()] + ' ' * (mg.end() - mg.start()) + t[mg.end():]
    size = TRADE_SIZE_RE.search(t)
    px = TRADE_PX_RE.search(t)
    # "US 500", "JP 225": a market name written with a space (2026-10-22)
    t = re.sub(r'\b([a-z]{2,4})\s+(\d{2,4})\b', lambda m: (m.group(1) + m.group(2)) if chart_symbol(m.group(1) + m.group(2)) else m.group(0), t)
    coin = None
    for w in re.findall(r'[a-z&][a-z0-9&]{1,9}', t):   # tickers with digits too: US500, JP225 (the owner's tests 2026-10-22)
        if w not in TRADE_STOP and chart_symbol(w):
            coin = w
            break
    # an amount written right before a ticker that has digits ("long 1 us500", "short 2 jp225") — read before the ticker is blanked
    qd = re.search(r'(?<![$\d.])(\d+(?:\.\d+)?)\s*%s\b' % re.escape(coin), t) if coin else None
    idx = re.sub(r'\D', '', chart_symbol(coin) or '') if coin else ''
    named = bool(idx) and re.search(r'\b%s\s+%s\b' % (re.escape(coin), idx), t)   # "nasdaq 100 $5": the 100 is the index's name, not the size
    if named:
        t = re.sub(r'\b%s\s+%s\b' % (re.escape(coin), idx), ' ' * (len(coin) + 1 + len(idx)), t)
    if coin and (re.search(r'\d', coin) or named):     # "jp225 $5": the ticker's digits are not a size or a price
        t = re.sub(r'\b%s\b' % re.escape(coin), ' ' * len(coin), t)
        lev = TRADE_LEV_RE.search(t)
        size = TRADE_SIZE_RE.search(t)
        px = TRADE_PX_RE.search(t)
    # an amount of the coin itself ("buy 0.01 btc", "short 200 sui") — not for tickers with digits
    qm = qd                                             # read before any blanking (digit tickers, "nasdaq 100")
    qty = float(qm.group(1)) if qm else 0
    if qm and size and size.start() <= qm.start(1) <= size.end():
        size = None                                     # "for 2 sui": the number is the amount of SUI, not dollars
    # a unit-less number under 1 ("for 0.007", "us500 0.007"): as dollars it's below every market's $1 minimum, as an amount
    # of the market it's a real order — read it as the amount (2026-10-23: "open more long position on US500, for 0.007")
    if coin and not qty and not mg:
        unit_less = size.group(3) if (size and size.group(3)) else None
        if not size:
            bm = re.search(r'(?<![$\d.\w])(0?\.\d+)(?![\d.]*\s*(?:x\b|\$|usd|usdc|dollars?|%))', t)
            unit_less = bm.group(1) if bm else None
        try:
            mpx = float(perp_market(chart_symbol(coin)).get('price') or 0)
        except Exception:
            mpx = 0
        if unit_less and float(unit_less) < 1 and mpx and float(unit_less) * mpx >= 1:
            qty, size = float(unit_less), None
    if not coin or not (size or mg or allm or qty or liq):
        return None
    usd = float(next(g for g in size.groups() if g)) if size else 0
    price = float(pxw.group(1)) if pxw else (float(px.group(1)) if px else 0)
    if price and abs(price - usd) < 1e-12 and not re.search(r'limit|@|when it', t):
        price = 0                                       # 'at 1$' was the size, not a price
    side = 'short' if sm.group(1) in ('short', 'sell') else 'long'
    return {'side': side, 'word': sm.group(1), 'coin': coin, 'sym': chart_symbol(coin), 'usd': usd,
            'lev': float(lev.group(1)) if lev else 0, 'price': price, 'margin': float(next(g for g in mg.groups() if g)) if mg else (-1 if allm and not size else 0),
            'qty': qty, 'liq': liq}


# Perps scenarios (reviewers, 2026-10-20): automation, managing open positions, loose margin words, leverage limits
PERP_AUTO_RE = re.compile(r'\b(?:every\s+(?:hour|day|minute|week|\d+\s*(?:min\w*|h\w*|hours?|days?))|automatic\w*|auto[-\s]?trad\w*|while\s+i\s+(?:sleep|am\s+away)'
                          r'|copy[-\s]?trad\w*|copy\s+(?:every|all|his|her|their)|sign\s+(?:them|it|these)\s+for\s+me|recurring|bot\s+trad\w*)\b', re.I)
PERP_ACT_RE = re.compile(r'\b(?:long|short|buy|sell|trade|trades|open|ape)\b', re.I)
PERP_MANAGE_RE = re.compile(r'\b((?<!how\s)close(?!\s+(?:to|enough|by|call)\b)|cancel|flip|reverse|reduce|take[\s-]*profit|stop[\s-]*loss|tp|sl|tp\s*/\s*sl|trailing\s+stop)\b', re.I)
PERP_CTX_RE = re.compile(r'\b(?:position|positions|order|orders|long|short|perps?|perpetuals?|trade|trades|limit)\b', re.I)
PERP_WD_LOOSE = re.compile(r'^\s*(?:withdraw|take|pull|remove|move|get|cash\s+out|free\s+up)\b(?!.*\b(?:suilend|navi|lend\w*|deposit|then|and|send|swap|convert|plus|including|keep)\b)(?!.*\b(?:together|along|as\s+well)\b.*\bwallet\b)'
                           r'(?!.*\b(?:into|to|in)\s+(?:my\s+|the\s+)?(?:aftermath\s+)?(?:perps?|margin|collateral|aftermath)\b).*\b(?:perps?|margin|aftermath)\b', re.I)
PERP_DEP_LOOSE = re.compile(r'^\s*(?:deposit|add|fund|top\s*up|put|move)\b(?!.*\b(?:suilend|navi|lend\w*|withdraw|then|and|swap|including|plus)\b)(?:.*\b(?:into|to|in)\s+(?:my\s+)?(?:aftermath\s+)?(?:perps?|margin)\b'
                            r'|\s+(?:\d+(?:\.\d+)?|all|half|max|some)?\s*(?:usdc\s+)?(?:of\s+)?(?:more\s+)?(?:margin|collateral)\s*[?.!]*\s*$)', re.I)


# "close X, then <next step>" (2026-11-11: "close perps position then withdraw USDC out" closed it and dropped the withdraw;
# "close my long then swap the usdc to sui" read SUI as the market). A close is its own transaction, so the close is read from
# its own clause and close_next() names the next step with a phrase Kivy turns into a card — never dropped.
CLOSE_THEN_VERBS = (r'lend|swap|send|deposit|put|park|withdraw|take|cash|move|pull|get|bring|transfer|convert|stake|buy|sell|open|go|'
                    r'leave|keep|use|reopen|re-open')
CLOSE_THEN_RE = re.compile(r'\s*,?\s*(?:\band\s+then\b|\bthen\b|\bafter\s+that\b|\bafterwards\b|\band\b(?=\s+(?:%s)\b)|,(?=\s*(?:%s)\b))\s*(?P<rest>\S.*)$'
                           % (CLOSE_THEN_VERBS, CLOSE_THEN_VERBS), re.I)
CLOSE_OUT_RE = re.compile(r'^(?:withdraw|take|cash|pull|get|bring|move|transfer)\b(?!.*\b(?:lend\w*|deposit|swap|send|convert|stake|buy|sell|into|on)\b)', re.I)


def close_next(rest, sym=''):
    """(sentence, next ask) for the step chained after a close: ('', '') when none. The page sends the next ask by itself once
    the close has landed (2026-11-11: "user tell to withdraw too"), so its card comes up for the second approval; every
    ask is one Kivy turns into a card (tests/test_closethen_20261111.py routes them)."""
    r = re.sub(r'\s+', ' ', rest or '').strip(' .!?,')
    if not r:
        return '', ''
    if re.match(r'(?:keep|leave)\b', r, re.I):
        return ' After it closes, the USDC stays in your perps account.', ''
    um = PERPWORDS.use_margin_trade(r)               # "close my eth long and use the margin to short btc 3x": the new order (scan 2026-11-14 C4)
    r = um or r
    if um or re.match(r'(?:open|go|buy|sell|reopen|re-open|flip)\b', r, re.I):   # a new order; no market named: the one just closed
        named = next((chart_symbol(x) for x in TICKER_WORD_RE.findall(r) if x.lower() not in TRADE_STOP | {'same', 'money', 'margin'} and chart_symbol(x)), None)
        say = ' When it has closed, the new order comes up for its own approval.'
        if (um or PERPWORDS.money_words(r)) and not re.match(r'(?i)(?:flip|reopen|re-open)\b', r) and re.search(r'(?i)\b(?:long|short)\b', r) and (named or sym):
            try:                                     # "open a btc long 2x with the same money" -> "long BTC 2x": a wording the order reader takes (C4)
                mx = float(perp_market(named or sym).get('max_lev') or 0)
            except Exception:
                mx = 0
            return say, PERPWORDS.order_ask(r, named or sym, mx)
        return say, r[:100] + ('' if named or not sym else ' on ' + sym)
    ca = PERPWORDS.close_amount_next(r)              # "withdraw 20 usdc and send it to bob.sui", "withdraw half the margin and …": that much leaves (C4)
    if ca:
        return ca
    # "then withdraw and swap to sui", "then withdraw the usdc and lend on suilend", "then move the money to navi": the margin is
    # what leaves, so the leading "withdraw (it / the usdc / the margin) and" is dropped — "withdraw my perps margin and withdraw
    # and swap to sui" was read as two starting coins (perps scan 2026-11-13)
    lead = re.match(r'(?i)^(withdraw|take|cash|pull|get|bring|move|transfer)\b(?:\s+out)?(?:\s+(?:it(?:\s+all)?|them|that|everything|all(?:\s+of\s+it)?|'
                    r'(?:(?:all\s+)?(?:the|my)\s+)?(?:usdc|money|margin|funds|cash|proceeds|collateral|profits?|rest)))?(?:\s+out)?\s*(?:(?:,|\band\s+then\b|\bthen\b|\band\b)\s*)?', r)   # "withdraw margin, swap …" too (C4)
    body = r[lead.end():].strip(' ,') if lead else r
    if CLOSE_OUT_RE.match(r) and not _mk_of(r) and not re.search(r'0x[0-9a-fA-F]{6,}|\.sui\b|@\w', r):
        ask = 'withdraw all my perps margin'
        say = ' When it has closed, the withdrawal to your wallet comes up for its own approval.'
    elif lead and not body:
        ask = 'withdraw all my perps margin'
        say = ' When it has closed, the withdrawal to your wallet comes up for its own approval.'
    elif lead and re.match(r'(?i)^(?:to|into|onto|over\s+to)\s+', body) and _mk_of(body):
        ask = 'move my perps margin ' + body[:100]      # "move the money to navi"
        say = ' When it has closed, lending what it frees comes up for its own approval.'
    elif lead and re.match(r'(?i)^(?:to|into)\s+(?:my\s+)?wallet\b\s*(?:(?:,|\band\s+then\b|\bthen\b|\band\b)\s*)?(?=\S)', body):
        body = re.sub(r'(?i)^(?:to|into)\s+(?:my\s+)?wallet\b\s*(?:(?:,|\band\s+then\b|\bthen\b|\band\b)\s*)?', '', body)
        ask = 'withdraw my perps margin and ' + body[:100]
        say = ' When it has closed, the next step (%s) comes up for its own approval.' % body[:80]
    elif lead and re.match(r'(?i)^(?:swap|convert|lend|deposit|put|park|send|transfer|stake|buy|sell|supply|keep)\b', body):
        ask = 'withdraw my perps margin and ' + body[:100]
        short = re.sub(r'0x[0-9a-fA-F]{6,}', lambda a: a.group(0)[:6] + '…' + a.group(0)[-4:], body)[:80]
        say = ' When it has closed, the next step (%s) comes up for its own approval.' % short
    else:
        ask = 'withdraw my perps margin and ' + r[:100]
        short = re.sub(r'0x[0-9a-fA-F]{6,}', lambda a: a.group(0)[:6] + '…' + a.group(0)[-4:], r)[:80]
        say = ' When it has closed, the next step (%s) comes up for its own approval.' % short
    return say, ask


def perp_manage(msg, address):
    """'close my BTC position', 'cancel my SUI order', 'stop loss on my long': the positions card, with what to tap. First (QA
    2026-11-17): "add margin to my sui long" is answered here, never read as a new order (RDR-V8); a trade sentence with two
    markets or two sides is asked about before the order reader takes the first one (RDR-V22)."""
    am = add_margin(msg, address)
    if am:
        return am
    ta = trade_ask(msg)
    if ta:
        return ta
    mm = PERP_MANAGE_RE.search(msg)
    rest = ''
    if mm and mm.group(1).lower() in ('close', 'reduce'):
        tm = CLOSE_THEN_RE.search(msg, mm.end())
        if tm:                                       # read the close from its own clause; the rest is the next step
            msg, rest = msg[:tm.start()], tm.group('rest')
            mm = PERP_MANAGE_RE.search(msg)
    names_mkt = mm and mm.group(1).lower() not in ('flip', 'reverse') and not re.search(r'\b(?:suilend|navi|lend\w*|ember|matrix\s*dock|wallet|vault|swap)\b', msg, re.I) \
        and any(chart_symbol(w) for w in TICKER_WORD_RE.findall(MKT_SPACE_RE.sub(r'\1\2', msg))
                           if w.lower() not in TRADE_STOP | {'close', 'cancel', 'half', 'all', 'flip', 'reduce'})   # "close half of my us500" (2026-10-23)
    close_all = bool(rest) and bool(mm) and bool(re.match(r'^\s*(?:please\s+)?close\s+(?:out\s+)?(?:everything|all|it\s+all|them\s+all|all\s+of\s+(?:it|them))\s*$', msg, re.I))   # "close everything then lend…" (2026-11-13)
    if not mm or not (PERP_CTX_RE.search(msg) or names_mkt or close_all or re.search(r'take[\s-]*profit|stop[\s-]*loss', msg, re.I)) or CHAIN_PERPS.search(msg) and re.search(r'\b(?:suilend|navi|lend)', msg, re.I):
        return None
    if not address:
        return {'text': K()['no_wallet']}
    perp_account(address)                            # several accounts, or one with orders on it: the choice card first (BLD-V1)
    r = PERPACCT.need(lend_cli('perp-positions', address))
    if not r.get('has'):
        return {'text': PERPACCT.blocked_text(r) or 'You have no Aftermath perps account yet, so nothing to %s.' % mm.group(1).lower()}
    syms = {p['symbol'] for p in r.get('positions') or []}
    word = None
    # market names with digits too ("US500", "us 500", "jp225"): found here, then hidden so their digits aren't read as a price
    # or an amount (2026-10-23: "set stop loss on us500 at 7300" became a stop-loss at 500)
    msg = MKT_SPACE_RE.sub(lambda m: (m.group(1) + m.group(2)) if chart_symbol(m.group(1) + m.group(2)) else m.group(0), msg)
    for w in TICKER_WORD_RE.findall(msg):
        if w.lower() not in TRADE_STOP | {'close', 'cancel', 'all', 'half', 'position', 'positions', 'orders', 'everything', 'flip', 'stop',
                                          'loss', 'take', 'profit', 'into', 'existing', 'now', 'but', 'leave', 'don', 'any', 'reverse'} and chart_symbol(w):
            word = chart_symbol(w)
            if re.search(r'\d', w):
                msg = re.sub(r'\b%s\b' % re.escape(w), ' ', msg)
            break
    act, fix = mm.group(1).lower(), ''
    pos = r.get('positions') or []
    if act in ('close', 'reduce', 'take profit', 'stop loss', 'tp', 'sl') or re.match(r'take|stop|tp|sl', act):
        if word and word not in syms:
            return {'text': 'You have no open %s position.%s' % (word, (' Open: ' + ', '.join(sorted(syms)) + '.') if syms else ''), 'widget': {'type': 'perppos', **r}}
        if not pos:
            return {'text': 'You have no open perps positions.'}
        mk = {x['symbol']: x for x in perp_market_list()}
        row = lambda p, f: {'symbol': p['symbol'], 'side': p['side'], 'size': p['size'], 'entry': p.get('entry'), 'liq': p.get('liquidation'),
                            'pnl': p.get('pnl'), 'frac': f, 'mark': (mk.get(p['symbol']) or {}).get('price'), 'image': (mk.get(p['symbol']) or {}).get('image')}
        every = re.search(r'\b(?:all|every|everything)\b.*\bpositions\b|\bpositions\b.*\ball\b|\bclose\s+(?:it\s+)?all\b(?!\s+(?:of\s+)?(?:my\s+)?\w+\s+(?:long|short|position)\b)|\bclose\s+everything\b', msg, re.I)
        if not word and (len(pos) > 1 and not (every and act in ('close', 'reduce'))):
            return {'text': 'Which position?', 'widget': {'type': 'choose', 'options': [
                {'label': '%s %s' % (p['symbol'], p['side']), 'sub': '%s %s' % (dec(p['size']), p['symbol']),
                 'ask': re.sub(r'\b(my|the)\b', r'\1 %s' % p['symbol'], msg, count=1, flags=re.I) if re.search(r'\b(my|the)\b', msg, re.I) else msg + ' ' + p['symbol']}
                for p in pos]}}
        pick = [p for p in pos if p['symbol'] == word] if word else pos
        if act in ('close', 'reduce'):
            f = 1.0
            ca = close_ask(msg, pick)                 # "close a fifth / 50 (of 20) / $5 of my sui long", "close it at 2": asked (QA 2026-11-17, RDR-V16)
            if ca:
                return ca
            fm = re.search(r'\b(half|a\s+third|third|a\s+quarter|quarter|(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:it|of\s+(?:my|the))?\b)', msg, re.I)
            if fm:
                w0 = fm.group(1).lower()
                if 'half' in w0:
                    f = 0.5
                elif 'third' in w0:
                    f = 1 / 3
                elif 'quarter' in w0:
                    f = 0.25
                elif fm.group(2):
                    f = float(fm.group(2)) / 100
                elif fm.group(3) and len(pick) == 1 and float(fm.group(3)) <= float(pick[0]['size']):
                    f = float(fm.group(3)) / float(pick[0]['size'])   # "close 0.0002 btc": that much of it
            elif act == 'reduce':
                return {'text': 'Reduce by how much?', 'widget': {'type': 'choose', 'options': [
                    {'label': l, 'sub': 'of the position', 'ask': 'close %s of my %s position' % (l, pick[0]['symbol'])} for l in ('25%', 'half', '75%')]}}
            if not (0.01 <= f <= 1):
                return {'text': 'Close between 1% and 100% of a position.'}
            rows = [row(p, round(f, 6)) for p in pick]
            what = ('%s%% of ' % ('%g' % round(f * 100, 2)) if f < 0.999 else '') + (('your %s %s' % (rows[0]['symbol'], rows[0]['side'])) if len(rows) == 1 else 'each position')
            nxt, then_ask = close_next(rest, rows[0]['symbol'] if len(rows) == 1 else '')   # the step chained after the close: named, then sent by the page
            return {'text': 'Close %s at market? Each close is its own transaction — confirm it in your wallet.%s' % (what, nxt),
                    'widget': {'type': 'perpclose', 'rows': rows, **({'next': then_ask} if then_ask else {})}}
        # take-profit / stop-loss
        gap = lambda stop: r'(?:(?!%s)[^\d$]){0,40}?\$?\s*(\d[\d,]*(?:\.\d+)?)' % stop   # "stop loss on my sui long at 0.79"
        slm = re.search(r'(?:stop[\s-]*loss|\bsl\b|\bstop\b)' + gap(r'take|profit|\btp\b|target'), msg, re.I)
        tpm = re.search(r'(?:take[\s-]*profit|\btp\b|\btarget\b)' + gap(r'stop|loss|\bsl\b'), msg, re.I)
        sl = float(slm.group(1).replace(',', '')) if slm else 0
        tp = float(tpm.group(1).replace(',', '')) if tpm else 0
        p = pick[0]
        if not (sl or tp):
            return {'text': 'At what price? E.g. "stop loss on my %s at …" or "take profit at …".' % p['symbol'], 'widget': {'type': 'perppos', **r}}
        rw = row(p, 1)
        mark, long_ = float(rw.get('mark') or 0), p['side'] == 'long'
        pc = lambda m_: bool(m_) and bool(re.match(r'\s*(?:%|percent\b)', msg[m_.end(1):], re.I))
        if mark and pc(slm):                         # "stop loss 10%": 10% from the price now, not a $10 price (review P-22)
            sl = round(mark * (1 - sl / 100.0) if long_ else mark * (1 + sl / 100.0), 8)
        if mark and pc(tpm):
            tp = round(mark * (1 + tp / 100.0) if long_ else mark * (1 - tp / 100.0), 8)
        far = [(n_, v_, m_) for n_, v_, m_ in (('stop-loss', sl, slm), ('take-profit', tp, tpm))
               if v_ and mark and not pc(m_) and (abs(v_ - mark) / mark > 0.5 if n_ == 'stop-loss' else not (1 / 3.0 <= v_ / mark <= 3))]
        if far and not re.search(r'\bexactly\b', msg, re.I):
            n_, v_, m_ = far[0]
            return {'text': 'A %s at %s is far from %s\'s price now (%s). Did you mean that price, or %s%%?' % (n_, dec(v_), p['symbol'], dec(mark), num_plain(v_)),
                    'widget': {'type': 'choose', 'options': [
                        {'label': '%s %s%% from the price now' % (n_.title(), num_plain(v_)), 'ask': msg[:m_.end(1)] + '%' + msg[m_.end(1):]},
                        {'label': '%s at exactly %s' % (n_.title(), dec(v_)), 'ask': msg + ' exactly'}]}}
        if mark:
            if sl and (sl >= mark if long_ else sl <= mark):
                return {'text': 'A stop-loss on a %s must be %s the price now (%s), or it would close at once.' % (p['side'], 'below' if long_ else 'above', dec(mark))}
            if tp and (tp <= mark if long_ else tp >= mark):
                return {'text': 'A take-profit on a %s must be %s the price now (%s).' % (p['side'], 'above' if long_ else 'below', dec(mark))}
        rw.update(sl=sl, tp=tp)
        return {'text': 'Set %s on your %s %s? It covers the whole position; confirm in your wallet.' % (
                    ' and '.join(x for x in ((('stop-loss %s' % dec(sl)) if sl else ''), (('take-profit %s' % dec(tp)) if tp else '')) if x), p['symbol'], p['side']),
                'widget': {'type': 'perptpsl', **rw}}
    elif act == 'cancel':
        fix = 'Tap Cancel next to the order below; your positions stay open.'
    elif act in ('flip', 'reverse'):
        fix = 'A flip is two steps: tap Close on %s below, then ask for the other side, e.g. "short %s $10 5x".' % (word or 'the position', word or 'BTC')
    else:
        fix = 'Trailing stops aren\'t in Kivy. You can set a fixed stop-loss or take-profit, e.g. "stop loss on my BTC at 75000".'
    return {'text': fix, 'widget': {'type': 'perppos', **r}}


def close_ask(msg, pick):
    """A close Kivy can't read as one share of the position: a share word ("a fifth", "two thirds", "some"), more than the
    position holds ("close 50 of my sui long" with 20), dollars ("close $5 of …") or a price ("close my sui long at 2": a close
    runs at market). The question, or None (QA 2026-11-17, RDR-V16)."""
    one = pick[0] if len(pick) == 1 else None
    name = ('your %s %s' % (one['symbol'], one['side'])) if one else 'each position'
    opts = {'type': 'choose', 'options': [{'label': l, 'sub': 'of the position', 'ask': 'close %s of my %s position' % (l, one['symbol'] if one else '')}
                                          for l in ('25%', 'half', '75%', 'all')]} if one else None
    uw = AM.unread(msg)
    if uw:
        return {'text': 'How much is "%s" of %s? Pick a share, or say it as a %% (e.g. "close 20%% of it").' % (uw, name), 'widget': opts}
    if any(a['kind'] == 'usd' for a in AM.amounts(msg)):
        return {'text': 'A close is a share of %s or an amount of the coin, not dollars. Say e.g. "close 25%%" or "close 5 %s".' % (name, one['symbol'] if one else 'SUI'), 'widget': opts}
    px = AM.limit_price(msg)
    if px and one:
        return {'text': 'A close runs now, at the market price — it can\'t wait for %s. To close at that price, set a take-profit or stop-loss on it, e.g. '
                        '"take profit at %s on my %s %s".' % (px, px, one['symbol'], one['side'])}
    n = next((a for a in AM.amounts(msg) if a['kind'] == 'units' and not a.get('weak')), None) or (AM.first(msg) and {'v': AM.first(msg)})
    if n and not re.search(r'%', msg) and one and float(n['v']) > float(one['size']) + 1e-12:
        return {'text': '%s is %s %s — close how much? Say an amount up to %s, or a share (e.g. "close 25%%").' % (name[0].upper() + name[1:], dec(one['size']), one['symbol'], dec(one['size'])),
                'widget': opts}
    return None


ADD_MARGIN_RE = re.compile(r'\b(?:add(?:ing)?|top(?:ping)?\s*up|put|increase|boost|more)\b[^.?!]*\b(?:margin|collateral)\b[^.?!]*\b(?:my|the|this|that)\s+(?:[a-z0-9&]+\s+)?(?:long|short|position)\b'
                           r'|\b(?:margin|collateral)\b[^.?!]*\b(?:on|to|for|of|into)\s+(?:my|the)\s+(?:[a-z0-9&]+\s+)?(?:long|short|position)\b[^.?!]*\b(?:add|top\s*up|more|increase)\b'
                           r'|\btop(?:ping)?\s*up\s+(?:my|the)\s+(?:[a-z0-9&]+\s+)?(?:long|short|position)\b'
                           r'|\b(?:save|rescue|protect)\s+(?:my|the)\s+(?:[a-z0-9&]+\s+)?(?:long|short|position)\b[^.?!]*\bliquidat', re.I)
ADD_TO_RE = re.compile(r'\badd\b(?:\s+\S+){1,3}?\s+(?:to|into|on)\s+(?:my|the)\s+([a-z0-9&]+)\s+(long|short)\b', re.I)


def add_margin(msg, address):
    """"add 5 usdc margin to my sui long", "top up my sui long with 5 usdc", "save my sui long from liquidation": margin for an
    OPEN position — it opened a NEW order (QA 2026-11-17, RDR-V8). Kivy can't add margin from the chat: it says so and shows
    the positions card, whose + Margin button does it. "add 5 usdc to my sui long" (margin or a bigger position?) is asked.
    None when the sentence is not that."""
    t = msg or ''
    to = ADD_TO_RE.search(t)
    if not (ADD_MARGIN_RE.search(t) or to):
        return None
    if not address:
        return {'text': K()['no_wallet']}
    perp_account(address)                            # several accounts, or one with orders on it: the choice card first (BLD-V1)
    r = PERPACCT.need(lend_cli('perp-positions', address))
    pos = r.get('positions') or [] if isinstance(r, dict) else []
    sm = re.search(r'\b(?:my|the)\s+([a-z0-9&]+)\s+(long|short)\b', t, re.I)
    sym = chart_symbol(sm.group(1)) if sm else None
    p = next((x for x in pos if x['symbol'] == sym and (not sm or x['side'] == sm.group(2).lower())), None) if sym else (pos[0] if len(pos) == 1 else None)
    if not pos or (sym and not p):
        return {'text': 'You have no open %s to add margin to.%s' % (('%s %s' % (sym, sm.group(2).lower())) if sym and sm else 'position',
                                                                  (' Open: ' + ', '.join('%s %s' % (x['symbol'], x['side']) for x in pos) + '.') if pos else ''),
                'widget': {'type': 'perppos', **r} if pos else None}
    name = ('your %s %s' % (p['symbol'], p['side'])) if p else 'the position'
    if to and not ADD_MARGIN_RE.search(t):
        amt = AM.first(t)
        bigger = '%s %s $%s' % (p['side'], p['symbol'], AM._plain(amt)) if p and amt else ''
        return {'text': 'Add it to %s as margin (the size stays, liquidation moves further away) or make the position bigger (a new order)?' % name,
                'widget': {'type': 'choose', 'options': [{'label': 'Add margin', 'sub': 'keeps the size', 'ask': 'add margin to my %s %s' % (p['symbol'], p['side']) if p else 'add margin to my position'}]
                           + ([{'label': 'Bigger position', 'sub': 'a new order: %s' % bigger, 'ask': bigger}] if bigger else [])}}
    return {'text': 'Kivy can\'t add margin from the chat yet — tap + Margin on %s below to add USDC to it. (An order would make the position bigger instead.)' % name,
            'widget': {'type': 'perppos', **r}}


SIDE_WORD_RE = re.compile(r'\b(long|short|buy|sell|ape)\b', re.I)


def one_market(msg):
    """The ONE perps market a trade sentence names (US500, JP225 too), or None when it names none, two different markets or two
    sides — the AI path's order-card safety net then adds no card (QA 2026-11-17, RDR-V22: it took the first market word)."""
    t = MKT_SPACE_RE.sub(lambda m: (m.group(1) + m.group(2)) if chart_symbol(m.group(1) + m.group(2)) else m.group(0), str(msg or ''))
    if len({'short' if w.lower() in ('short', 'sell') else 'long' for w in SIDE_WORD_RE.findall(t)}) > 1:
        return None
    mks = list(dict.fromkeys(chart_symbol(w) for w in TICKER_WORD_RE.findall(t) if w.lower() not in TRADE_STOP | {'kivy', 'can', 'you'} and chart_symbol(w)))
    return mks[0] if len(mks) == 1 else None


def trade_ask(msg):
    """A trade sentence that names two different markets ("pump is coming, long sol $10 3x", "long near the bottom on eth") or two
    sides ("sell my sol and long eth $10 2x"): the order reader took the first market / side word wherever it stood (QA
    2026-11-17, RDR-V22). Which one? A choice card, a plain ask, or None for a sentence with one market and one side."""
    pt = parse_trade(msg)
    if not pt:
        return None
    t = MKT_SPACE_RE.sub(lambda m: (m.group(1) + m.group(2)) if chart_symbol(m.group(1) + m.group(2)) else m.group(0), msg.lower())
    sides = list(dict.fromkeys('short' if w.lower() in ('short', 'sell') else 'long' for w in SIDE_WORD_RE.findall(t)))
    mks = list(dict.fromkeys(chart_symbol(w) for w in re.findall(r'[a-z&][a-z0-9&]{1,9}', t) if w not in TRADE_STOP and chart_symbol(w)))
    if len(sides) > 1:
        return {'text': 'That is two orders (a %s and a %s). One order per card — say the one you want, e.g. "%s %s $10 2x".' % (sides[0], sides[1], sides[-1], mks[-1] if mks else 'ETH')}
    if len(mks) < 2:
        return None
    if pt.get('price') or pt.get('margin') or pt.get('qty') or pt.get('liq'):
        return {'text': 'Which market — %s? Say the order again with one market.' % ' or '.join(mks[:4])}
    tail = ('' if not pt.get('usd') else ' $%s' % AM._plain(pt['usd'])) + ('' if not pt.get('lev') else ' %sx' % AM._plain(pt['lev']))
    return {'text': 'You named %d markets. Which one should the order be on?' % len(mks), 'widget': {'type': 'choose', 'options': [
        {'label': '%s %s%s' % (pt['side'].title(), x, tail), 'ask': '%s %s%s' % (pt['side'], x, tail)} for x in mks[:4]]}}


PERP_CUSHION = 1.005                                   # the order card keeps 0.5% of margin spare (price moves while signing)


def perp_solve(prm, side, margin, price=0, usd=0, qty=0, liq=0, lev=0):
    """Kivy's perps math (2026-10-23): margin M, leverage L, price P, amount Q, value N = Q*P = M*L, borrowed B = N - M,
    fee (maker if a price was given, else taker) and the liquidation price all follow from each other. Give it what the
    user said (M defaults to all the free perps margin) and it fills in the rest, or says why it can't.
    Liquidation (position margin m = N/L):  long  m + Q(X-P) = mmr*Q*X   short  m - Q(X-P) = mmr*Q*X."""
    P = float(price or prm['mark'])
    maker = bool(price)
    f = max(0.0, float(prm['maker'] if maker else prm['taker']))   # a maker rebate is never counted on
    mmr, mx, M = float(prm['mmr']), float(prm['max_lev']), float(margin or 0)
    long_ = side == 'long'
    liq_of = lambda L: (P * (1 - 1 / L) / (1 - mmr)) if long_ else (P * (1 + 1 / L) / (1 + mmr))
    out = {'price': P, 'maker': maker, 'fee_rate': f, 'mmr': mmr, 'max_lev': mx, 'margin_have': M}
    N = float(usd or 0) or float(qty or 0) * P
    if liq:                                             # a liquidation price: the leverage follows from P and X alone, the size from M
        X = float(liq)
        if (long_ and X >= P) or (not long_ and X <= P):
            return dict(out, ok=False, why='side', text='A %s is liquidated %s the entry price, so %s can\'t be its liquidation price (entry %s).'
                        % (side, 'below' if long_ else 'above', fmt_px(X), fmt_px(P)))
        L = P / (P - X * (1 - mmr)) if long_ else P / (X * (1 + mmr) - P)
        if L > mx + 1e-9:
            return dict(out, ok=False, why='too_close', lev_need=L, liq_best=liq_of(mx),
                        text='Liquidation at %s needs %.1fx; %s allows %gx, so the closest is %s.' % (fmt_px(X), L, prm['symbol'], mx, fmt_px(liq_of(mx))))
        if L < 1:
            return dict(out, ok=False, why='too_far', lev_need=L, liq_best=liq_of(1),
                        text='Even at 1x this %s is liquidated at %s — it can\'t be pushed to %s.' % (side, fmt_px(liq_of(1)), fmt_px(X)))
        if not N:                                       # all the margin: N = L*m, and M = cushion*m + N*f
            N = M / (PERP_CUSHION / L + f)
    elif not lev:                                       # a size: the leverage that uses all the margin
        if not N:
            return dict(out, ok=False, why='no_size', text='How much — an amount, a $ size, or a liquidation price?')
        room = M - N * f
        L = (PERP_CUSHION * N / room) if room > 0 else float('inf')
        if L > mx + 1e-9:
            nmax = M / (PERP_CUSHION / mx + f)
            return dict(out, ok=False, why='too_big', lev_need=L, usd_max=nmax, qty_max=nmax / P,
                        text='That needs %s; %s allows %gx. With %s USDC margin you can open up to $%s (%s %s).'
                        % (('%.0fx' % L) if L != float('inf') else 'more than you have', prm['symbol'], mx, num_plain(M), num_plain(round(nmax, 2)), num_plain(nmax / P), prm['symbol']))
        if L < 1:
            L = 1.0                                     # under 1x: 1x, using only the margin it needs (2026-10-23)
    else:
        L = float(lev)
    if not N:
        return dict(out, ok=False, why='no_size', text='How much — an amount, a $ size, or a liquidation price?')
    m = N / L
    return dict(out, ok=True, usd=round(N, 2), lev=round(L, 2), qty=N / P, margin_used=round(PERP_CUSHION * m + N * f, 6),
                borrowed=round(max(0.0, N - m), 2), liq_est=liq_of(L), fee=round(N * f, 6))


# Price text for cards: 2 decimals above $100, 4 above $1, else 6 significant digits.
def fmt_px(x):
    x = float(x)
    return '{:,.2f}'.format(x) if x >= 100 else '{:,.4f}'.format(x) if x >= 1 else '{:.6g}'.format(x)


# A plain number for chat text and card asks: up to 6 decimals, trailing zeros cut.
def num_plain(x):
    return ('%.6f' % float(x)).rstrip('0').rstrip('.')


def perp_params(sym):
    """A perps market's price, margin ratios and fees (live)."""
    r = lend_cli('perp-params', '0x0', sym)
    if not r.get('ok'):
        raise RuntimeError(r.get('error') or 'no market data')
    return r


def open_lev(address, sym):
    """The leverage of the user's open position on this market, or None — Aftermath won't place an order at another one."""
    try:
        for p in (lend_cli('perp-positions', address, timeout=40).get('positions') or []):
            if p.get('symbol') == sym and float(p.get('size') or 0) > 0:
                return float(p.get('leverage') or 0) or None
    except Exception:
        pass
    return None


def perp_math_card(pt, msg, address, lev):
    """Orders where Kivy works the numbers out (2026-10-23): an amount ("0.01 BTC"), a $ size or a liquidation price,
    and NO leverage -> the leverage (and size) that uses the margin — all the free perps margin unless they named some.
    With a leverage too, an amount just becomes its $ size. Returns a reply, or None to let the normal order path go on."""
    sym, side = pt['sym'], pt['side']
    if pt.get('liq') and pt.get('lev'):
        return {'text': 'The leverage sets where a position is liquidated, so I need one or the other. Which should I keep?',
                'widget': {'type': 'choose', 'options': [
                    {'label': '%gx leverage' % pt['lev'], 'sub': 'liquidation follows from it', 'ask': '%s %s %s%gx' % (side, sym, ('$%g ' % pt['usd']) if pt.get('usd') else '', pt['lev'])},
                    {'label': 'Liquidation at %s' % fmt_px(pt['liq']), 'sub': 'Kivy works out the leverage', 'ask': '%s %s liquidation at %s' % (side, sym, num_plain(pt['liq']))}]}}
    if not (pt.get('qty') or pt.get('liq') or (pt.get('usd') and not pt.get('lev'))):
        return None
    try:
        prm = perp_params(sym)
    except Exception:
        return None
    if pt.get('lev'):                                   # leverage given: an amount just becomes its $ size, the normal path does the rest
        if pt.get('qty') and not pt.get('usd'):
            pt['usd'] = round(pt['qty'] * float(pt.get('price') or prm['mark']), 2)
        return None
    free = float(pt['margin']) if (pt.get('margin') or 0) > 0 else float(perp_account(address, fresh=True).get('collateral') or 0)
    if free <= 0:
        return deposit_ask(address)
    sol = perp_solve(prm, side, free, pt.get('price') or 0, pt.get('usd') or 0, pt.get('qty') or 0, pt.get('liq') or 0)
    if not sol['ok']:
        opts = []
        if sol.get('why') == 'too_big':
            opts.append({'label': 'Open the max: $%s' % num_plain(round(sol['usd_max'] * 0.995, 2)), 'sub': '%gx, all your margin' % sol['max_lev'],
                         'ask': '%s %s $%s %gx' % (side, sym, num_plain(round(sol['usd_max'] * 0.995, 2)), sol['max_lev'])})
        if sol.get('why') == 'too_close':
            safe = sol['liq_best'] * (0.998 if side == 'long' else 1.002)       # a hair inside the limit, so it always opens
            sol['text'] = 'Liquidation at %s needs %.1fx; %s allows %gx, so the closest is about %s.' % (fmt_px(pt['liq']), sol['lev_need'], sym, sol['max_lev'], fmt_px(safe))
            opts.append({'label': 'Liquidation at %s' % fmt_px(safe), 'sub': 'the closest %s allows' % sym,
                         'ask': '%s %s liquidation at %s' % (side, sym, num_plain(round(safe, 6)))})
        return {'text': sol['text'], 'widget': {'type': 'choose', 'options': opts} if opts else None}
    usd, L = sol['usd'], sol['lev']
    img = perp_market(sym).get('image')
    for _ in range(3):                                  # Aftermath keeps a hair more margin than the formula: trim until it fits what's there
        cw = {'type': 'chart', 'symbol': sym, 'tf': '15m', 'image': img, 'trade': {'open': True, 'side': 1 if side == 'short' else 0, 'usd': usd, 'lev': L}}
        if pt.get('price'):
            cw['trade'].update({'type': 'limit', 'price': pt['price']})
        cf = perp_confirm(address, cw)
        if cf is cw:
            return None
        short_ = float(cf.get('deposit') or 0)
        if short_ <= 0.000001:
            break
        if pt.get('liq'):                               # liquidation fixed by the leverage: a slightly smaller size
            usd = round(usd * free / (free + short_) * 0.998, 2)
        elif L * (free + short_) / free * 1.002 <= sol['max_lev']:
            L = round(L * (free + short_) / free * 1.002, 2)   # the amount they asked for: a hair more leverage
        else:
            return deposit_ask(address, short_, cf)
    else:
        return deposit_ask(address, float(cf.get('deposit') or 0), cf)
    m = usd / L
    cf.update({'borrowed': round(max(0.0, usd - m), 2), 'liq_target': pt.get('liq') or 0, 'margin_all': not (pt.get('margin') or 0) > 0})
    lq = float(cf.get('liq') or sol['liq_est'] or 0)
    lq_txt = 'none at 1x' if lq < float(sol['price']) * 0.001 else fmt_px(lq)          # a 1x long can't be liquidated
    used = m * PERP_CUSHION + usd * sol['fee_rate']
    used_txt = ('all your %s' % num_plain(round(used, 4))) if not (pt.get('margin') or 0) > 0 and used >= free * 0.98 \
        else ('%s of your %s' % (num_plain(round(used, 4)), num_plain(round(free, 4))))
    why = ('liquidation at %s → %gx' % (fmt_px(pt['liq']), L)) if pt.get('liq') else ('%s %s = $%s → %gx' % (num_plain(pt['qty']), sym, num_plain(usd), L)) if pt.get('qty') \
        else ('$%s → %gx' % (num_plain(usd), L))
    return {'text': '%s %s: %s USDC margin, %s, borrowed ≈ $%s, liquidation %s. Confirm in your wallet.' % (
                side.title(), sym, used_txt, why, num_plain(cf['borrowed']), ('≈ ' + lq_txt) if lq_txt[0].isdigit() else lq_txt),
            'widget': cf}


def lev_check(msg, sym, lev):
    """Leverage the user typed vs the market's limit: (leverage to use, note) or (None, a question)."""
    mx = perp_market(sym).get('max_lev') or 10
    if re.search(r'(?:-|minus|negative)\s*\d+(?:\.\d+)?\s*x\b', msg, re.I) or re.search(r'\b0+(?:\.0+)?\s*x\b', msg, re.I):
        return None, {'text': 'Leverage must be between 1x and %gx for %s. Which leverage?' % (mx, sym),
                      'widget': {'type': 'choose', 'options': [{'label': '%gx' % x, 'sub': 'leverage', 'ask': re.sub(r'(?:-|minus|negative)?\s*\d+(?:\.\d+)?\s*x\b', ' %gx' % x, msg, count=1, flags=re.I)}
                                                              for x in sorted({1, min(3, mx), min(5, mx), mx})]}}
    if lev and (lev > mx or lev < 1):                 # never clamp silently (review P-6): say the limit and ask
        def _at(x):
            t = re.sub(r'(\d+(?:\.\d+)?)\s*(?:x\b|times\b)', '%gx' % x, msg, count=1, flags=re.I)
            return t if t != msg else '%s %gx' % (msg, x)
        return None, {'text': '%gx is %s for %s — it allows 1x to %gx. Which leverage?' % (lev, 'above the maximum' if lev > mx else 'below 1x', sym, mx),
                      'widget': {'type': 'choose', 'options': [{'label': '%gx' % x, 'sub': 'leverage', 'ask': _at(x)} for x in sorted({1, min(3, mx), min(5, mx), mx})]}}
    return lev, ''


BORROW_RE = re.compile(r'\bborrow\w*|\bloan\s+against\b|\b(?:use|using)\s+(?:my\s+)?(?:lent|deposited|supplied|lending)\s+\w+\s+as\s+(?:collateral|margin)\b'
                       r'|\b(?:lent|deposited|supplied)\s+\w+\s+as\s+(?:collateral|margin)\b', re.I)


BANK_RE = re.compile(r'\b(?:bank(?:\s+account)?|iban|swift|paypal|venmo|revolut|cash\s*app|wire\s+transfer|off-?ramp|fiat|credit\s+card|debit\s+card)\b', re.I)   # not 'cash out' alone: "cash out my usdsui from ember" is a withdrawal
FUNDING_Q_RE = re.compile(r'\bfunding(?:\s+rate)?s?\b', re.I)


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .situations import _mk_of  # noqa: E402
from .chainreads import COINS, all_balances, balances, ckey, coin_icon, dec, ecoins, prices  # noqa: E402
from .lending import coin_px, lent_hint, to_opts  # noqa: E402
from .chains import CHAIN_PERPS  # noqa: E402
from .cards import share_said  # noqa: E402
from .txreads import rev_fee_pct  # noqa: E402
