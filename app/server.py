#!/usr/bin/env python3
"""kivy.kivelabs.xyz — Kivy, a public finance agent on Sui mainnet (updated 2026-10-18).

Security:
  - public (KIVY_PUBLIC=1); KIVY_PUBLIC=0 locks /api/chat behind the dashboard login
  - non-custodial: Kivy builds unsigned transactions; the user's own wallet signs each one,
    after the browser checks the final transaction against the approved action (guard / audit T11)
  - KNOWS ONLY ITS OWN FOLDER: replies come from knowledge/kivy.json in the Kivy project folder,
    public market prices and an allowlisted-tool AI (brain.py) — no shell, nothing else on this server
  - binds 127.0.0.1; reached only through the cloudflared tunnel; runs as user `kivy`, sandboxed
  - caps: $20 per swap / send / lend; perps uncapped (2026-10-18)
"""
import difflib
import base64
import hashlib
import hmac
import calendar
import ipaddress
import json
import math
import os
import re
import secrets
import time
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accounts  # noqa: E402  chat history per verified Google email
import metrics  # noqa: E402  Kivy's own numbers: users, PTBs/actions, volume, retention, failures, feedback (2026-10-22)

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get('KIVY_PORT', 8772))
AUTH_USER = os.environ.get('DASHBOARD_USER', '')
AUTH_PASS = os.environ.get('DASHBOARD_PASS', '')
COOKIE, TTL = 'kivy_session', 30 * 24 * 3600
SECRET_PATH = HERE / '.session_secret'
if not SECRET_PATH.exists():
    SECRET_PATH.write_bytes(secrets.token_bytes(32))
    os.chmod(SECRET_PATH, 0o600)
SECRET = SECRET_PATH.read_bytes()
ORIGIN = 'https://kivy.kivelabs.xyz'

# ── everything Kivy knows comes from its own folder ─────────────────────────────
KIVY_HOME = Path(os.environ.get('KIVY_HOME', '/srv/kivy')).resolve()
KNOWLEDGE = (KIVY_HOME / 'knowledge' / 'kivy.json').resolve()
assert KIVY_HOME in KNOWLEDGE.parents, 'knowledge must live inside the Kivy folder'


def _load_brain():
    """Kivy's AI brain lives in the Kivy folder too (brain.py)."""
    import importlib.util
    path = (KIVY_HOME / 'brain.py').resolve()
    assert KIVY_HOME in path.parents
    spec = importlib.util.spec_from_file_location('kivy_brain', str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    BRAIN = _load_brain()
except Exception as _exc:          # no brain -> rule-based replies only
    print('kivy: AI brain unavailable (%s)' % _exc, flush=True)
    BRAIN = None

_RATE = []                         # timestamps of AI calls, a simple per-minute budget
AI_PER_MIN = int(os.environ.get('KIVY_AI_PER_MIN', 60))          # everyone together (was 12: a busy minute sent every user the help menu, 2026-10-23)
AI_PER_VISITOR_MIN = int(os.environ.get('KIVY_AI_PER_VISITOR_MIN', 10))   # one visitor can't use up everyone's share
AI_PER_DAY = int(os.environ.get('KIVY_AI_PER_DAY', 3000))            # hard ceiling: ~$1.50 a day at ~$0.0005 an answer
# Abuse limits by the REAL internet address (Cloudflare's CF-Connecting-IP; the visitor id comes from the browser and a script can
# rotate it). Bad-actor review 2026-10-24: one script could spend the whole day's AI in ~50 minutes and leave everyone "busy".
AI_PER_IP_MIN = int(os.environ.get('KIVY_AI_PER_IP_MIN', 12))
AI_PER_IP_DAY = int(os.environ.get('KIVY_AI_PER_IP_DAY', 250))      # generous for a shared office/mobile address, small next to 3,000
HEAVY_PER_IP_MIN = int(os.environ.get('KIVY_HEAVY_PER_IP_MIN', 240))   # cards, quotes, builds, chat: per address per minute
WALLETS_PER_IP_HOUR = int(os.environ.get('KIVY_WALLETS_PER_IP_HOUR', 40))   # different wallets one address may look up per hour
_IPQ = {'ai_min': {}, 'ai_day': {}, 'heavy': {}, 'wal': {}, 'day': ''}
_IPQ_LOCK = threading.Lock()


_LIMIT_SEEN = {}


def limit_hit(kind, ip):
    """One log line per address and kind every 10 minutes when a limit stops someone — kivy_watch alerts on these."""
    k, now = (kind, ip), time.time()
    if now - _LIMIT_SEEN.get(k, 0) > 600:
        _LIMIT_SEEN[k] = now
        if len(_LIMIT_SEEN) > 20000:
            _LIMIT_SEEN.clear()
        print('kivy limit: %s stopped for %s' % (kind, re.sub(r'(\d+)\.(\d+)$', 'x.x', str(ip))), flush=True)   # the address shortened


def ip_quota(kind, ip, key=None):
    ok = _ip_quota(kind, ip, key)
    if not ok:
        limit_hit(kind, ip)
    return ok


def _ip_quota(kind, ip, key=None):
    """True when this real IP may do one more `kind` ('ai', 'heavy', 'wallet' with the address as key); records it."""
    if not ip:
        return True                                  # local scripts and tests (no Cloudflare header)
    now = time.time()
    with _IPQ_LOCK:
        today = time.strftime('%Y-%m-%d', time.gmtime(now))
        if _IPQ['day'] != today:
            _IPQ.update(ai_day={}, day=today)
        for d in ('ai_min', 'ai_day', 'heavy', 'wal'):
            if len(_IPQ[d]) > 50000:
                _IPQ[d].clear()
        if kind == 'ai':
            m = _IPQ['ai_min'].setdefault(ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= AI_PER_IP_MIN or _IPQ['ai_day'].get(ip, 0) >= AI_PER_IP_DAY:
                return False
            m.append(now); _IPQ['ai_day'][ip] = _IPQ['ai_day'].get(ip, 0) + 1
            return True
        if kind == 'heavy':
            m = _IPQ['heavy'].setdefault(ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= HEAVY_PER_IP_MIN:
                return False
            m.append(now)
            return True
        if kind == 'fb':                              # feedback: 20 a minute per address (rotating visitor ids can't flood the log)
            m = _IPQ['heavy'].setdefault('fb:' + ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= 20:
                return False
            m.append(now)
            return True
        if kind == 'wallet' and key:
            w = _IPQ['wal'].setdefault(ip, {})
            for a in [a for a, t in w.items() if now - t > 3600]:
                w.pop(a, None)
            if key.lower() not in w and len(w) >= WALLETS_PER_IP_HOUR:
                return False
            w[key.lower()] = now
            return True
    return True
_RATE_V, _RATE_DAY = {}, {'day': '', 'n': 0}
AI_BUSY = "I'm getting a lot of questions right now — ask me again in a moment."
PUBLIC = os.environ.get('KIVY_PUBLIC', '1') == '1'   # open to everyone; set KIVY_PUBLIC=0 to lock it again


LEND_ASK_RE = re.compile(r'\b(?:differen\w*|safe\w*|risk\w*|why|vs|versus|which\s+is\s+better|collateral|liquidat\w*|bad\s+debt)\b', re.I)   # not 'explain': "explain APR vs APY" is a lesson
MARKETS_RE = re.compile(r"^\s*(?:i\s+want\s+to\s+|let'?s\s+|can\s+i\s+)?(?:lend|lending|earn|yield|deposit|supply)(?:\s+(?:my\s+)?usdc)?\s*[?.!]*\s*$|\blending\s+(markets?|rates?|options?|yields?)\b|\bwhere\s+(can|should|do)\s+i\s+lend\b|\bcompare\s+(lending|yields?|rates?)\b|\bbest\s+(lending\s+)?(yield|rate|apy|apr)\b", re.I)


REWARD_Q_RE = re.compile(r'^\s*(?:how\s+much|what|any|do\s+i\s+have|did\s+i\s+earn)\b[^.?!]{0,30}\brewards?\b[^.?!]{0,25}[?.!]*\s*$|\bworth\s+claiming\b', re.I)
CLAIM_SCOPES = {'all': '', 'p-suilend': 'Suilend', 'p-navi': 'NAVI'}   # claim everything, or one protocol's markets (2026-10-25)
CLAIM_PROTO_RE = re.compile(r'^\s*claim\s+(?:all\s+)?(?:my\s+)?(?:the\s+)?(suilend|navi)\s+(?:lending\s+)?rewards?\s*[?.!]*\s*$|^\s*claim\s+(?:my\s+)?(?:lending\s+)?rewards?\s+(?:from|on)\s+(suilend|navi)\s*[?.!]*\s*$', re.I)
CLAIM_RE = re.compile(r'^\s*(?:claim\s+(?:it\s+all|everything|all\s+of\s+(?:them|it))|claim(?:\s+(?:my|all))?(?:\s+(?:lending\s+)?rewards?)?|(?:my\s+)?(?:lending\s+)?rewards?|show\s+(?:my\s+)?rewards?)\s*[?.!]*\s*$', re.I)


WITHDRAW_RE = re.compile(r'^\s*(?:i\s+want\s+to\s+|let\'?s\s+|can\s+i\s+)?(?:withdraw(?:al)?|redeem|take\s+(?:out|back)|cash\s+out)(?:\s+(?:it\s+)?(?:all|everything))?'
                         r'(?:\s+(?:from\s+)?(?:my\s+)?(?:usdc|money|funds|lending|lent|lended|loans?|deposits?)(?:\s+usdc)?)?\s*[?.!]*\s*$', re.I)
WITHDRAW_FROM_RE = re.compile(r'^\s*(?:withdraw|redeem|take\s+out)\s+(?:(?:all|the|my|of)\s+)*(?:usdc\s+)?(?:from|on|in|out\s+of)\s+(.+?)\s*[?.!]*\s*$', re.I)   # + "withdraw USDC from Suilend Ember" (2026-10-22)


PERP_WORD = r'(?:aftermath(?:\s+(?:perps?|perpetuals?|futures|account))?|perps?|pereps?|preps?|peprs?|perpetuals?|futures|perp\s+account)'   # common typos too
PERP_RE = re.compile(r'^\s*(?:my\s+|open\s+(?:a\s+)?)?' + PERP_WORD + r'(?:\s+(?:account|balance|margin|collateral|equity))?\s*[?.!]*\s*$|^\s*(?:my\s+)?aftermath(?:\s+(?:account|balance))?\s*$'
                     r'|^\s*(?:show\s+)?(?:my\s+)?(?:perps?\s+|aftermath\s+)?(?:margin|collateral)\s+balance\s*[?.!]*\s*$', re.I)
PERP_DEP_RE = re.compile(r'^\s*(?:deposit|add|fund|top\s*up|put)(?:\s+(\d+(?:\.\d+)?|all|max|half))?(?:\s+(?:usdc|of\s+my\s+usdc|my\s+usdc))?\s+(?:to|into|in|on)\s+(?:my\s+)?' + PERP_WORD + r'\s*[?.!]*\s*$', re.I)
PERP_WD_RE = re.compile(r'^\s*(?:withdraw|take\s+out|remove|pull)(?:\s+(\d+(?:\.\d+)?|all|max|half|everything))?(?:\s+(?:usdc|my\s+usdc))?\s+(?:from|out\s+of)\s+(?:my\s+)?' + PERP_WORD + r'\s*[?.!]*\s*$', re.I)
_PERP = {}


# ── live charts: Aftermath perps candles (crypto, stocks, indexes, gold/oil) ──
_MK = {'t': 0, 'v': {}}
_CANDLES = {}
TF_SPAN = {'1m': 6 * 3600, '15m': 3 * 86400, '30m': 6 * 86400, '1h': 12 * 86400, '4h': 50 * 86400,
           '1d': 300 * 86400, '3d': 365 * 86400, '1w': 365 * 86400}   # Aftermath: no 5m; history starts 2026-08-17, too-long ranges error
CHART_ALIASES = {'XBTC': 'BTC', 'GOLD': 'XAUT', 'XAU': 'XAUT', 'SILVER': 'XAG', 'OIL': 'WTI', 'SP500': 'US500', 'S&P': 'US500', 'SPX': 'SPX',
                 'NASDAQ': 'US100', 'NDX': 'US100', 'APPLE': 'AAPL', 'TESLA': 'TSLA', 'NVIDIA': 'NVDA', 'GOOGLE': 'GOOGL', 'BITCOIN': 'BTC',
                 'ETHEREUM': 'ETH', 'SOLANA': 'SOL', 'XBTC': 'BTC'}


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


def market_group(sym, category):
    if category != 'TradFi':
        return category
    for g, members in TRADFI_GROUPS.items():
        if sym in members:
            return g
    return 'Stocks'


_MKMETA = {'t': 0, 'v': {}}
_MKSTATS = {'t': 0, 'v': []}


def af_post(path, body):
    req = urllib.request.Request('https://aftermath.finance/api/perpetuals/' + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
    return json.load(urllib.request.urlopen(req, timeout=25))


SAFE_IMG = re.compile(r'^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/%+=&?-]*$')


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


def perp_account(addr, fresh=False):
    hit = _PERP.get(addr)
    if hit and not fresh and time.time() - hit[0] < 15:
        return hit[1]
    v = lend_cli('perp-account', addr)
    _PERP[addr] = (time.time(), v)
    return v


def perp_widget(msg, address):
    dm, wm = PERP_DEP_RE.match(msg), PERP_WD_RE.match(msg)
    if not (PERP_RE.match(msg) or dm or wm):
        return None
    if not address:
        return {'text': K()['no_wallet']}
    acct = perp_account(address, fresh=True)
    if not acct.get('has'):
        return {'text': 'You need an Aftermath perps account first. Open it once, then deposit USDC as collateral.',
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
        return min(float(w), mx)
    if dm:
        have = float((balances(address) or {}).get('USDC') or 0)
        mx = round(have, 6)
        return {'text': 'Slide to choose how much USDC to deposit into your Aftermath perps account.',
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
    text = 'Your Aftermath perps account: ' + (('%d open position%s' % (n, '' if n == 1 else 's')) if n else 'no open positions') + \
           ((' and %d open order%s.' % (no, '' if no == 1 else 's')) if no else '.')
    return {'text': text, 'widget': {'type': 'perpacct', 'collateral': col, 'free': pos.get('free'), 'equity': pos.get('equity'), 'upnl': pos.get('upnl'),
                                     'positions': pos.get('positions') or [], 'orders': sum(len(g.get('orders') or []) for g in (pos.get('positions') or []) + (pos.get('orders') or [])),
                                     'suggest': sugg, 'order_list': olist}}


PERP_MARKETS_RE = re.compile(r'^\s*(?:show\s+(?:me\s+)?(?:the\s+)?|list\s+(?:the\s+)?|all\s+)?(?:perps?\s+|aftermath\s+|trading\s+)?markets?\s*[?.!]*\s*$'
                             r'|^\s*what\s+can\s+i\s+trade\s*\??\s*$'
                             # "do you have any more markets", "what other markets are there", "show more markets", "which markets do you have"
                             r'|^\s*(?:do\s+you\s+have\s+|are\s+there\s+|got\s+|show\s+(?:me\s+)?|any\s+|what\s+|which\s+)?(?:any\s+)?(?:more|other|all|else|the)?\s*'
                             r'(?:perps?\s+|trading\s+)?markets?(?:\s+(?:do\s+you\s+have|are\s+there|can\s+i\s+trade|available|else|too))?\s*[?.!]*\s*$', re.I)


TRADE_RE = re.compile(r'^\s*(?:open\s+(?:a\s+)?|go\s+)?(long|short|buy|sell)\s+(?:on\s+)?([A-Za-z0-9&]{2,12})'
                      r'(?:\s+(?:with\s+|for\s+)?\$?\s*(\d+(?:\.\d+)?)\s*(?:usd|usdc|dollars?|\$)?)?'
                      r'(?:\s+(?:at\s+|with\s+)?(\d+(?:\.\d+)?)\s*x(?:\s+leverage)?)?(?:,?\s+(?:at\s+)?market(?:\s+order)?)?\s*[?.!]*\s*$', re.I)


POS_RE = re.compile(r'^\s*(?:show\s+)?(?:my\s+)?(?:open\s+)?(?:perps?\s+|aftermath\s+)?(?:positions?|orders?|trades?)\s*[?.!]*\s*$', re.I)


# "i wanna trade bitcoin", "can i trade sol", "trade eth": spot or perps is the user's call — ask, don't guess
TRADE_ASK_RE = re.compile(r"^\s*(?:i\s+)?(?:wanna|want\s+to|would\s+like\s+to|'?d\s+like\s+to|like\s+to|let'?s|lets|can\s+i|how\s+(?:do|can)\s+i|help\s+me)?\s*"
                          r"trade\s+(?:some\s+)?(?:on\s+)?([A-Za-z0-9&]{2,14})\s*[?.!]*\s*$", re.I)


# a spot buy with no amount ("swap USDC to BTC", "buy BTC"): ask how much, with quick picks from what the wallet holds
SPOT_ASK_RE = re.compile(r'^\s*(?:swap|buy|get)\s+(?:some\s+)?(?:([A-Za-z]{2,8})\s+(?:to|for|into)\s+)?([A-Za-z]{2,10})\s*(?:on\s+spot)?\s*[?.!]*\s*$', re.I)


def spot_ask(msg, address):
    bare = bool(re.match(r'^\s*swap\s*[?.!]*\s*$', msg, re.I))
    if bare:                                         # bare "swap" (the menu / Swap button): USDC on top, SUI below — the user picks (2026-10-21)
        if not address:
            return {'text': K()['no_wallet']}
        msg = 'swap usdc to sui'
    m = SPOT_ASK_RE.match(msg)
    if not m:
        return None
    spot = [a.upper() for a in (K().get('assets') or [])]
    alias = lambda x: ckey(CHART_ALIASES.get(re.sub(r'[^A-Za-z]', '', x or '').upper(), x or ''))
    to, frm = alias(m.group(2)), (alias(m.group(1)) if m.group(1) else None)
    if not to or (m.group(1) and not frm) or frm == to:
        return None
    if not address:
        return {'text': K()['no_wallet']}
    bal = {k: float(v or 0) for k, v in (all_balances(address) or {}).items()}
    spot = spot + [c for c in bal if c not in spot]          # listed coins they hold (USDsui, BLUE…) can pay too
    keep = {'SUI': 0.05}                                 # leave a little SUI for gas
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
                    'Swap to %s — pick the coins, type an amount or pick a share; the quote updates live.' % disp(to),
            'widget': {'type': 'swap', 'from': src, 'to': to, 'names': SPOT_NAME, 'held': held, 'cap_usd': ui_cap(), 'prices': px,
                       'slippage_pct': float(K()['slippage_cap_pct']), 'fee_pct': rev_fee_pct(), 'to_opts': to_opts, 'icons': {k: v for k, v in icons.items() if v}}}


BARE_RE = re.compile(r'^\s*(?:the\s+)?([A-Za-z0-9&]{2,14})(?:\s+(?:please|pls|plz))?\s*[?.!]*\s*$', re.I)


def deposit_ask(address, need=None):
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
        pv = lend_cli('perp-preview', address, w['symbol'], int(t.get('side') or 0), float(t['usd']), float(t['lev']))
    except Exception:
        return w
    if not pv.get('ok'):
        return w
    ref = float(t.get('price') or 0) if t.get('type') == 'limit' else float(pv.get('mark') or 0)
    return {'type': 'perpconfirm', 'symbol': w['symbol'], 'image': w.get('image'), 'side': int(t.get('side') or 0),
            'usd': float(t['usd']), 'lev': float(t['lev']), 'otype': t.get('type') or 'market', 'price': ref,
            'base': (float(t['usd']) / ref) if ref else pv.get('base'), 'margin': pv.get('margin'), 'liq': pv.get('liquidation_price'),
            'fee': pv.get('fee_usd'), 'taker_pct': pv.get('fee_pct'), 'maker_pct': pv.get('maker_fee_pct'), 'max_lev': pv.get('max_lev'),
            'deposit': pv.get('deposit_needed'), 'mark': pv.get('mark'), 'chart': w}


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
    for wn, n in (('two', 2), ('three', 3), ('five', 5), ('ten', 10), ('twenty', 20), ('twenty-five', 25), ('fifty', 50)):
        t = re.sub(r'\b%s\s+times\b' % wn, ' %dx' % n, t)
    t = re.sub(r'(\d+(?:\.\d+)?)\s+times\b', r'\1x', t)
    lev = re.search(r'(\d+(?:\.\d+)?)\s*x\b', t)
    pxw = re.search(r'(?:\blimit\s+(?:order\s+)?(?:price\s+(?:of\s+)?|at\s+|@\s*)|\bentry\s+(?:at\s+|price\s+(?:of\s+)?)?|\b(?:limit|short|long|sell|buy)\b[^$\d]{0,25}\bat\s+(?=\$))\$?\s*(\d+(?:\.\d+)?)(?!\s*x\b)', t)
    mg = re.search(r'(?:\b(?:with|using)\s+\$?\s*(\d+(?:\.\d+)?)\s*(?:usdc|usd|\$)?\s*(?:of\s+)?(?:margin|collateral)\b|\b(\d+(?:\.\d+)?)\s*(?:usdc|usd|\$)?\s+(?:margin|collateral)\b)', t)
    # "all my margin", and also "all collateral", "max margin", "full perps balance", "all of the collateral" (2026-10-22)
    allm = re.search(r'\b(?:(?:using\s+|with\s+)?(?:all|max|full|whole|entire)\s+(?:of\s+)?(?:my\s+|the\s+)?(?:available\s+)?(?:perps?\s+)?(?:margin|usdc|collateral|balance)|max\s+usdc)\b', t)
    if pxw:
        t = t[:pxw.start(1)] + ' ' * len(pxw.group(1)) + t[pxw.end(1):]
    if mg:
        t = t[:mg.start()] + ' ' * (mg.end() - mg.start()) + t[mg.end():]
    size = re.search(r'(?:\$\s*(\d+(?:\.\d+)?)(?!\s*x)|(\d+(?:\.\d+)?)\s*(?:\$|usdc?\b|dollars?\b)|\bfor\s+(\d+(?:\.\d+)?)\b(?!\s*x))', t)   # "5 usdc" too (2026-10-24)
    px = re.search(r'(?:\blimit\s+(?:at\s+)?|\bat\s+(?:a\s+price\s+of\s+)?|@\s*|\bwhen\s+it\s+(?:hits|reaches)\s+)\$?\s*(\d+(?:\.\d+)?)(?!\s*(?:x|\$|usd|dollars?)\b)', t)
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
        lev = re.search(r'(\d+(?:\.\d+)?)\s*x\b', t)
        size = re.search(r'(?:\$\s*(\d+(?:\.\d+)?)(?!\s*x)|(\d+(?:\.\d+)?)\s*(?:\$|usdc?\b|dollars?\b)|\bfor\s+(\d+(?:\.\d+)?)\b(?!\s*x))', t)   # "5 usdc" too (2026-10-24)
        px = re.search(r'(?:\blimit\s+(?:at\s+)?|\bat\s+(?:a\s+price\s+of\s+)?|@\s*|\bwhen\s+it\s+(?:hits|reaches)\s+)\$?\s*(\d+(?:\.\d+)?)(?!\s*(?:x|\$|usd|dollars?)\b)', t)
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
            mpx = float(next((r.get('price') for r in perp_market_list() if r['symbol'] == chart_symbol(coin)), 0) or 0)
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


def perp_manage(msg, address):
    """'close my BTC position', 'cancel my SUI order', 'stop loss on my long': the positions card, with what to tap."""
    mm = PERP_MANAGE_RE.search(msg)
    names_mkt = mm and mm.group(1).lower() not in ('flip', 'reverse') and not re.search(r'\b(?:suilend|navi|lend\w*|ember|matrix\s*dock|wallet|vault|swap)\b', msg, re.I) \
        and any(chart_symbol(w) for w in re.findall(r'[A-Za-z&][A-Za-z0-9&]{1,9}', re.sub(r'\b([A-Za-z]{2,4})\s+(\d{2,4})\b', r'\1\2', msg))
                           if w.lower() not in TRADE_STOP | {'close', 'cancel', 'half', 'all', 'flip', 'reduce'})   # "close half of my us500" (2026-10-23)
    if not mm or not (PERP_CTX_RE.search(msg) or names_mkt or re.search(r'take[\s-]*profit|stop[\s-]*loss', msg, re.I)) or CHAIN_PERPS.search(msg) and re.search(r'\b(?:suilend|navi|lend)', msg, re.I):
        return None
    if not address:
        return {'text': K()['no_wallet']}
    r = lend_cli('perp-positions', address)
    if not r.get('has'):
        return {'text': 'You have no Aftermath perps account yet, so nothing to %s.' % mm.group(1).lower()}
    syms = {p['symbol'] for p in r.get('positions') or []}
    word = None
    # market names with digits too ("US500", "us 500", "jp225"): found here, then hidden so their digits aren't read as a price
    # or an amount (2026-10-23: "set stop loss on us500 at 7300" became a stop-loss at 500)
    msg = re.sub(r'\b([A-Za-z]{2,4})\s+(\d{2,4})\b', lambda m: (m.group(1) + m.group(2)) if chart_symbol(m.group(1) + m.group(2)) else m.group(0), msg)
    for w in re.findall(r'[A-Za-z&][A-Za-z0-9&]{1,9}', msg):
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
            then = re.search(r'\b(?:and|then)\s+(?:then\s+)?(lend|swap|send|deposit|put)\b', msg, re.I)
            f = 1.0
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
            nxt = (' After it closes, the USDC stays in your perps account — then ask me to %s it (e.g. "withdraw from aftermath and lend on navi"); '
                   'that is a separate approval.' % then.group(1).lower()) if then else ''
            return {'text': 'Close %s at market? Each close is its own transaction — confirm it in your wallet.%s' % (what, nxt),
                    'widget': {'type': 'perpclose', 'rows': rows}}
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


def fmt_px(x):
    x = float(x)
    return '{:,.2f}'.format(x) if x >= 100 else '{:,.4f}'.format(x) if x >= 1 else '{:.6g}'.format(x)


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
    img = next((r['image'] for r in perp_market_list() if r['symbol'] == sym), None)
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
            return deposit_ask(address, short_)
    else:
        return deposit_ask(address, float(cf.get('deposit') or 0))
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
    mx = next((r.get('max_lev') for r in perp_market_list() if r['symbol'] == sym), None) or 10
    if re.search(r'(?:-|minus|negative)\s*\d+(?:\.\d+)?\s*x\b', msg, re.I) or re.search(r'\b0+(?:\.0+)?\s*x\b', msg, re.I):
        return None, {'text': 'Leverage must be between 1x and %gx for %s. Which leverage?' % (mx, sym),
                      'widget': {'type': 'choose', 'options': [{'label': '%gx' % x, 'sub': 'leverage', 'ask': re.sub(r'(?:-|minus|negative)?\s*\d+(?:\.\d+)?\s*x\b', ' %gx' % x, msg, count=1, flags=re.I)}
                                                              for x in sorted({1, min(3, mx), min(5, mx), mx})]}}
    if lev and lev > mx:
        return mx, ' %gx is above %s\'s maximum, so this order uses %gx.' % (lev, sym, mx)
    return lev, ''


BORROW_RE = re.compile(r'\bborrow\w*|\bloan\s+against\b|\b(?:use|using)\s+(?:my\s+)?(?:lent|deposited|supplied|lending)\s+\w+\s+as\s+(?:collateral|margin)\b'
                       r'|\b(?:lent|deposited|supplied)\s+\w+\s+as\s+(?:collateral|margin)\b', re.I)


BANK_RE = re.compile(r'\b(?:bank(?:\s+account)?|iban|swift|paypal|venmo|revolut|cash\s*app|wire\s+transfer|off-?ramp|fiat|credit\s+card|debit\s+card)\b', re.I)   # not 'cash out' alone: "cash out my usdsui from ember" is a withdrawal
FUNDING_Q_RE = re.compile(r'\bfunding(?:\s+rate)?s?\b', re.I)


# ── User journeys answered by Kivy itself (2026-10-24, user-scenario review: these all went to the AI with little data) ──
# 1 exit everything · 2 best rate / move to the best market (every coin) · 3 activity + earnings · 4 common questions by code
EXIT_RE = re.compile(r"^\s*(?:please\s+)?(?:(?:get|take)\s+me\s+out(?:\s+of)?(?:\s+(?:everything|it\s+all|all(?:\s+of\s+it)?|all\s+my\s+positions|my\s+positions|this))?"
                     r"|i\s+want\s+(?:to\s+get\s+)?out(?:\s+of\s+everything)?|exit\s+(?:everything|all(?:\s+(?:my\s+)?positions)?|it\s+all)"
                     r"|(?:cash|pull)\s+out\s+(?:of\s+)?(?:everything|it\s+all|all)|close\s+(?:out\s+)?everything"
                     r"|(?:withdraw|take\s+out|pull)\s+(?:out\s+)?(?:everything|it\s+all|all\s+my\s+(?:money|funds|positions))"
                     r"|(?:sell|convert|turn|move|put)\s+(?:everything|it\s+all|all\s+my\s+(?:money|funds|coins|positions))\s+(?:in)?to\s+(?:usdc|stables?|stablecoins?|cash)"
                     r")(?:\s+(?:in)?to\s+(?:usdc|stables?|stablecoins?|cash))?(?:\s+(?:now|please|asap|right\s+now))*\s*[?.!]*\s*$", re.I)


def exit_reply(address):
    """"Get me out of everything": every position as one ordered list, each step one approval with Kivy's usual checks.
    Order: close perps (frees the margin), margin to the wallet, then each lent coin withdrawn as USDC."""
    if not address:
        return {'text': K()['no_wallet']}
    opts = []
    try:
        pp = lend_cli('perp-positions', address, timeout=40).get('positions') or []
    except Exception:
        pp = []
    if pp:
        opts.append({'label': 'Close %s' % ('your %s %s' % (pp[0]['symbol'], pp[0]['side']) if len(pp) == 1 else 'all %d perps positions' % len(pp)),
                     'sub': 'at market price', 'ask': 'close all my positions'})
    try:
        free = float(perp_account(address, fresh=True).get('collateral') or 0)
    except Exception:
        free = 0
    if pp or free >= 0.01:
        opts.append({'label': 'Move perps margin to your wallet', 'sub': ('%s USDC free now' % dec(free, 6)) if free >= 0.01 else 'after closing',
                     'value': ('$%s' % dec(free, 4)) if free >= 0.01 else '',
                     'ask': 'withdraw all my margin from perps'})
    try:
        for p, v in (lend_positions(address, fresh=True) or {}).items():
            amt = float((v or {}).get('usdc') or 0)
            if amt >= 0.01:
                opts.append({'label': 'Withdraw USDC from %s' % PROTOS.get(p, p), 'sub': '%s USDC' % dec(amt, 6), 'value': '$%s' % dec(amt, 4),
                             'ask': 'withdraw all USDC from %s' % PROTOS.get(p, p)})
        for r in other_lent(address, fresh=True):
            opts.append({'label': 'Withdraw %s from %s as USDC' % (r['coin'], r['name']), 'sub': '%s %s' % (dec(r['lent'], 6), r['coin']),
                         'value': '$%s' % dec(r['lent'] * (r.get('px') or 0), 4), 'ask': 'withdraw all %s from %s as USDC' % (r['coin'], r['name'])})
    except Exception:
        pass
    if not opts:
        return {'text': "You have nothing in lending or perps — everything is already in your wallet."}
    n = len(opts)
    return {'text': ('Here is everything, in the order to do it. Each step is its own transaction: tap one, check it, approve it in your wallet.' if n > 1
                     else 'One step gets you out:') + ' Nothing moves until you approve.',
            'widget': {'type': 'choose', 'options': [dict(o, label='%d. %s' % (i + 1, o['label']) if n > 1 else o['label']) for i, o in enumerate(opts)]}}


BEST_RE = re.compile(r"\b(?:(?:move|switch|shift)\s+(?:all\s+)?(?:of\s+)?(?:my\s+)?(?P<c1>[a-z][a-z0-9]{1,9})\s+(?:in)?to\s+(?:the\s+)?(?:best|highest|top)\b"
                     r"|(?:is\s+there|any|find(?:\s+me)?|got)\s+(?:a\s+)?(?:better|higher)\s+(?:rate|yield|apy|apr|market|place)\s+(?:for|on)\s+(?:my\s+)?(?P<c2>[a-z][a-z0-9]{1,9})"
                     r"|(?:where|which\s+market)\s+(?:can|should|do)\s+i\s+(?:earn|get|make)\s+(?:the\s+)?(?:most|best|highest)(?:\s+(?:rate|yield|apy|interest))?\s+(?:on|for|with)\s+(?:my\s+)?(?P<c3>[a-z][a-z0-9]{1,9})"
                     r"|(?:best|highest|top)\s+(?:rate|yield|apy|apr|market)\s+(?:for|on)\s+(?:my\s+)?(?P<c4>[a-z][a-z0-9]{1,9}))", re.I)


def coin_rates(asset=None):
    """{market name: % a year} for one coin (any coin a market lists, 2026-10-24: the AI only knew USDC rates)."""
    tok = 'USDC' if not asset or str(asset).upper() == 'USDC' else lend_token(str(asset))
    if not tok:
        return {'error': '%s is not a coin any lending market lists' % asset}
    rows = [r for r in lend_rates_all() if not r.get('rate_only') and
            str({'USDSUI': 'USDsui'}.get(r.get('token'), r.get('token')) or r['asset']).lower() == tok.lower()]
    return {'coin': tok, 'apr_pct_by_market': {LEND_MKTS.get(r['market'], r.get('name') or r['market']): round(float(r['total_apr']), 2)
                                               for r in sorted(rows, key=lambda r: -float(r['total_apr']))}}


def best_reply(msg, address):
    """"Is there a better rate for my USDsui?", "move my USDsui to the best market": every market for that coin from live
    rates, where the user has it now, and the move one tap away when it pays (any coin, not only USDC)."""
    m = BEST_RE.search(msg)
    if not m:
        return None
    word = next(g for g in m.groups() if g)
    tok = 'USDC' if word.lower() == 'usdc' else lend_token(word)
    if not tok:
        return None
    nt = lambda t: re.sub(r'^0x0*', '0x', str(t or '').lower())
    rows = [r for r in lend_rates_all() if not r.get('rate_only') and
            str({'USDSUI': 'USDsui'}.get(r.get('token'), r.get('token')) or r['asset']).lower() == tok.lower()]
    if not rows:
        return None
    rows.sort(key=lambda r: -float(r['total_apr']))
    name = lambda r: LEND_MKTS.get(r['market'], r.get('name') or r['market'])
    best = rows[0]
    mine = []
    if address:
        try:
            if tok == 'USDC':
                mine = [{'id': p, 'name': PROTOS[p], 'lent': float(v.get('usdc') or 0), 'total': apr_of(p) or 0}
                        for p, v in (lend_positions(address) or {}).items() if float((v or {}).get('usdc') or 0) >= 0.01]
            else:
                mine = [r for r in other_lent(address) if str(r['coin']).lower() == tok.lower()]
        except Exception:
            mine = []
    top = '\n'.join('• %s — %.2f%% a year' % (name(r), float(r['total_apr'])) for r in rows[:4])
    opts = []
    if mine:
        lines = []
        for x in mine:
            gain = float(best['total_apr']) - float(x.get('total') or 0)
            if x['id'] == best['market'] or gain < 0.1:
                lines.append('Your %s %s on %s already earns about the best rate (%.2f%%).' % (dec(x['lent'], 6), disp(tok), x['name'], float(x.get('total') or 0)))
            else:
                yr = x['lent'] * (x.get('px') or 1) * gain / 100
                lines.append('Your %s %s on %s earns %.2f%%; %s pays %.2f%% — about $%s more a year.' % (
                    dec(x['lent'], 6), disp(tok), x['name'], float(x.get('total') or 0), name(best), float(best['total_apr']), dec(yr, 3)))
                opts.append({'label': 'Move it to %s' % name(best), 'sub': '%.2f%% a year · one approval' % float(best['total_apr']),
                             'ask': 'move all %s from %s to %s' % (disp(tok), x['name'], name(best))})
        text = '%s rates right now:\n%s\n\n%s' % (disp(tok), top, '\n'.join(lines))
    else:
        text = '%s rates right now:\n%s\n\nThe best is %s.' % (disp(tok), top, name(best))
        opts.append({'label': 'Lend %s on %s' % (disp(tok), name(best)), 'sub': '%.2f%% a year' % float(best['total_apr']), 'ask': 'lend %s on %s' % (disp(tok), name(best))})
    opts.append({'label': 'See all %s markets' % disp(tok), 'sub': 'with collateral', 'ask': 'lending markets for %s' % disp(tok)})
    return {'text': text + '\nRates change; the collateral column in the table shows each market\'s risk.', 'widget': {'type': 'choose', 'options': opts}}


ACT_RE = re.compile(r"^\s*(?:(?:show|list|see|check)\s+(?:me\s+)?)?(?:my\s+)?(?:recent\s+|latest\s+|last\s+(?:few\s+)?)?(?:history|activity|transactions|txs?|moves)\s*[?.!]*\s*$"
                    r"|\bwhat\s+(?:did|have)\s+i\s+(?:do|done|did)\b|\bwhere\s+(?:did|has|is)\s+(?:all\s+)?my\s+(?:money|funds|usdc|sui|coins?|\w{2,10})\s+(?:go|gone)\b"
                    r"|\bwhat\s+happened\s+(?:to|with)\s+my\s+(?:money|funds|coins?|usdc|sui|\w{2,10})\b|\bi\s+(?:didn'?t|did\s+not|never)\s+(?:get|receive)\s+(?:my\s+)?\w+"
                    r"|\bmy\s+(?:last\s+)?(?:transaction|tx|swap|send|order)\s+(?:failed|didn'?t\s+work|did\s+not\s+work)\b|\bwhy\s+did\s+my\s+(?:transaction|tx|swap|send)\s+fail\b"
                    r"|\bdid\s+(?:my|the)\s+(?:transaction|tx|swap|send|transfer|deposit|withdrawal?)\s+(?:go\s+through|work|succeed|land|arrive)\b"
                    r"|^\s*(?:show\s+)?my\s+last\s+(?:transaction|tx|swap|send|transfer|deposit|withdrawal|move)\s*[?.!]*\s*$|\bmy\s+(?:transaction|tx|swap|send)\s+(?:is\s+)?(?:stuck|pending)\b", re.I)


def recent_activity(address, n=10):
    """The wallet's latest transactions from the chain: time, status, what moved for THIS wallet, Kivy's label."""
    q = ('query($a: SuiAddress!, $n: Int) { transactions(last: $n, filter: {affectedAddress: $a}) { nodes { digest sender { address } '
         'effects { status timestamp balanceChanges { nodes { owner { address } coinType { repr } amount } } } } } }')
    nodes = ((gql(q, {'a': address, 'n': n}) or {}).get('transactions') or {}).get('nodes') or []
    norm = lambda x: re.sub(r'^0x0*', '0x', x or '')
    syms = {norm(v['type']): (sym, int(v['decimals'])) for sym, v in COINS().items()}
    try:
        for r in lend_rates_all():                    # lending coins' names too (USDsui, sSUI…)
            if r.get('type') and r.get('token'):
                syms.setdefault(norm(r['type']), ({'USDSUI': 'USDsui'}.get(r['token'], r['token']), int(r.get('decimals') or 9)))
    except Exception:
        pass
    labels = tx_labels([x['digest'] for x in nodes], owner=address)   # only what THIS wallet signed (audit nv #8)
    out = []
    for x in reversed(nodes):
        e = x.get('effects') or {}
        mv = {}
        for b in (e.get('balanceChanges') or {}).get('nodes') or []:
            if norm((b.get('owner') or {}).get('address')) != norm(address):
                continue
            ct = norm(b['coinType']['repr'])
            if '::reserve::CToken<' in ct:
                continue
            sym, dp = syms.get(ct, (ct.split('::')[-1], 9))
            mv[sym] = mv.get(sym, 0) + int(b['amount']) / 10 ** dp
        out.append({'digest': x['digest'], 'time': e.get('timestamp'), 'ok': str(e.get('status')).upper() == 'SUCCESS',
                    'mine': norm((x.get('sender') or {}).get('address')) == norm(address), 'label': labels.get(x['digest']),
                    'moves': {k: v for k, v in mv.items() if abs(v) > 1e-9}})
    return out


def activity_reply(msg, address):
    if not address:
        return {'text': K()['no_wallet']}
    try:
        acts = recent_activity(address, 8)
    except Exception:
        return {'text': "I couldn't read your activity from the chain just now — try again in a moment, or open the wallet's Activity tab."}
    if not acts:
        return {'text': 'No transactions for this wallet yet.'}
    def line(a):
        t = str(a.get('time') or '')[:16].replace('T', ' ')
        mv = ', '.join('%s%s %s' % ('+' if v > 0 else '−', dec(abs(v), 6), disp(k)) for k, v in a['moves'].items()) or 'no coin change'
        what = re.sub(r'^(?:chain|compose)\s+', '', a.get('label') or ('sent by you' if a['mine'] else 'received'))
        what = {'sendmany': 'Send'}.get(what, what[:1].upper() + what[1:])
        return '• %s UTC — %s%s: %s' % (t, what, '' if a['ok'] else ' — FAILED (only the network fee was spent)', mv)
    day = re.search(r'\b(today|yesterday)\b', msg, re.I)
    if day:
        want = time.strftime('%Y-%m-%d', time.gmtime(time.time() - (86400 if day.group(1).lower() == 'yesterday' else 0)))
        on = [a for a in acts if str(a.get('time') or '').startswith(want)]
        if not on:
            return {'text': 'No transactions %s (UTC). Your latest:\n' % day.group(1).lower() + '\n'.join(line(a) for a in acts[-5:])}
        acts = on
    fails = [a for a in acts if not a['ok']]
    head = 'Your latest transactions (newest last):'
    if re.search(r'\bfail|didn\'?t\s+work|did\s+not\s+work', msg, re.I):
        head = ('Your last failed one: %s — a failed transaction changes nothing except the network fee. Your latest transactions:' % fails[-1]['digest']
                if fails else 'None of your recent transactions failed. Your latest:')
    elif re.search(r"\b(?:didn'?t|did\s+not|never)\s+(?:get|receive)\b", msg, re.I):
        head = ('Every coin that reached this wallet shows as + below. If something is missing, check the sender used this exact address; '
                'a transaction to another address can\'t be pulled back. Your latest:')
    return {'text': head + '\n' + '\n'.join(line(a) for a in acts[-8:]) + '\nTap a transaction in the wallet\'s Activity tab for every step.'}


EARN_RE = re.compile(r"\bhow\s+much\s+(?:have\s+i|did\s+i|am\s+i|do\s+i|will\s+i)\s+(?:earn|earned|earning|make|made|making|get|getting)\b"
                     r"|\b(?:what(?:'s|\s+is|\s+are)\s+)?my\s+(?:earnings|interest|yield|returns?|apy|apr|income|profit|pnl|p&l|p\s*n\s*l)\b"
                     r"|\bhow\s+(?:am\s+i|are\s+my\s+(?:positions|investments))\s+doing\b", re.I)


WALLET_BAL_RE = re.compile(r"^\s*(?:(?:what(?:'s|\s+is|\s+are)|show(?:\s+me)?|check)\s+)?(?:my\s+)?(?:wallet\s+)?(?:balances?|holdings|coins)(?:\s+in\s+my\s+wallet)?\s*[?.!]*\s*$|^\s*what(?:'s|\s+is)\s+in\s+my\s+wallet\s*[?.!]*\s*$", re.I)
TOTAL_RE = re.compile(r"^\s*what\s+(?:do\s+i\s+(?:hold|have|own)|am\s+i\s+holding)(?:\s+(?:right\s+)?now)?(?:\s*(?:,|and)\s+(?:what(?:'s|\s+is)\s+it\s+worth|how\s+much\s+is\s+it(?:\s+worth)?))?\s*[?.!]*\s*$"
                      r"|^\s*(?:(?:what(?:'s|\s+is)|show(?:\s+me)?|check)\s+)?(?:my\s+)?(?:total|overall|whole|full|net)\s+(?:balance|worth|value|portfolio)\s*[?.!]*\s*$"
                      r"|^\s*(?:what(?:'s|\s+is)\s+)?my\s+(?:net\s*worth|portfolio(?:\s+value)?)\s*[?.!]*\s*$|^\s*how\s+much\s+(?:money\s+)?do\s+i\s+have(?:\s+in\s+total|\s+altogether|\s+overall)?\s*[?.!]*\s*$", re.I)


def total_reply(address):
    """Everything the wallet holds through Kivy, from live data: wallet coins, lent coins, perps margin (2026-10-25)."""
    if not address:
        return {'text': K()['no_wallet']}
    wal = 0.0
    try:
        b = all_balances(address) or {}
        wal = sum(float(v or 0) * float(coin_px(c) or 0) for c, v in b.items())
    except Exception:
        b = {}
    d = defi_positions(address)
    lent = sum(float(x.get('usd') or 0) for x in d.get('lent') or [])
    try:                                                 # perps: equity (margin + open PnL), the same number the wallet panel adds
        pp = lend_cli('perp-positions', address, timeout=40)
        perps = float(pp.get('equity') or 0) if pp.get('has') else 0.0
    except Exception:
        perps = float(d.get('perps') or 0)
    rw = float(d.get('rewards_usd') or 0)
    m = lambda v: '$%.2f' % v
    lines = ['• Wallet: %s' % m(wal), '• Lent: %s' % m(lent) + (' (%s)' % ', '.join('%s on %s' % (disp(x['coin']), x['market']) for x in d['lent'][:4]) if d.get('lent') else '')]
    if perps:
        lines.append('• Perps: %s' % m(perps))
    if rw > 0:
        lines.append('• Rewards to claim: %s' % usd_small(rw))
    return {'text': 'Total: about %s\n' % m(wal + lent + perps) + '\n'.join(lines) + '\nOpen the wallet panel for each coin.'}


def earn_reply(address):
    """What the wallet earns: each lent position at today's rate (per day / week / year), lending rewards waiting, perps PnL."""
    if not address:
        return {'text': K()['no_wallet']}
    rows, per_year, total = [], 0.0, 0.0
    try:
        for p, v in (lend_positions(address) or {}).items():
            amt = float((v or {}).get('usdc') or 0)
            if amt >= 0.01:
                rows.append((amt, 'USDC', PROTOS.get(p, p), float(apr_of(p) or 0), 1.0))
        for r in other_lent(address):
            rows.append((float(r['lent']), r['coin'], r['name'], float(r.get('total') or 0), float(r.get('px') or 0)))
    except Exception:
        pass
    lines = []
    for amt, coin, mk, apr, px in rows:
        usd = amt * px
        yr = usd * apr / 100
        per_year += yr; total += usd
        lines.append('• %s %s on %s at %.2f%% a year ≈ $%s a week, $%s a year' % (dec(amt, 6), disp(coin), mk, apr, dec(yr / 52, 3), dec(yr, 3)))
    perp = ''
    try:
        pp = lend_cli('perp-positions', address, timeout=40).get('positions') or []
        if pp:
            up = sum(float(x.get('upnl') or x.get('pnl') or 0) for x in pp)
            perp = '\nPerps: %d open position%s, %s$%s unrealised right now.' % (len(pp), '' if len(pp) == 1 else 's', '−' if up < 0 else '+', dec(abs(up), 4))
    except Exception:
        pass
    if not lines and not perp:
        return {'text': "You have nothing lent and no perps positions, so nothing is earning yet. Ask \"where can I earn the most on my USDC?\" to start."}
    text = ''
    if lines:
        text = 'At today\'s rates (they change):\n' + '\n'.join(lines)
        if len(lines) > 1:
            text += '\nTogether: about $%s a week on $%s (%.2f%% overall).' % (dec(per_year / 52, 3), dec(total, 4), per_year / total * 100 if total else 0)
        text += '\nThese are estimates from the current rate — Kivy doesn\'t track your exact earnings since you deposited yet.'
    text += perp
    opts = [{'label': 'Better rates?', 'sub': 'compare markets', 'ask': 'is there a better rate for my %s' % (disp(rows[0][1]) if rows else 'USDC')},
            {'label': 'Claim rewards', 'sub': 'if any are waiting', 'ask': 'claim my rewards'}]
    return {'text': text.strip(), 'widget': {'type': 'choose', 'options': opts} if rows else None}


# 4: common questions with one right answer, by code: instant, and no AI budget (they were most of the AI calls)
COMMON_QS = [
    # small talk and app questions: answered by code (2026-10-25 scenario pass: each cost an AI call of ~7k tokens)
    (r"^\s*(?:hi|hello|hey|yo|gm|good\s+(?:morning|afternoon|evening)|sup|hiya)(?:\s+(?:kivy|there|all))?\s*[!.👋]*\s*$",
     "Hi! Tell me what you want to do — e.g. \"swap 1 SUI to USDC\", \"lend my USDC\", \"what's my total balance\" — and I'll prepare it for your approval."),
    (r"^\s*(?:thanks?(?:\s+you)?|thx|ty|cheers|great|nice|cool|awesome|perfect|lol|haha+|👍|🙏)(?:\s+(?:kivy|so\s+much|a\s+lot))?\s*[!.]*\s*$",
     "Any time. Ask me whenever you want to move, lend or check something."),
    (r"\b(?:delete|clear|remove|erase|wipe)\s+(?:all\s+)?(?:my\s+)?(?:chats?|chat\s+history|conversations?|history)\b",
     "Open the menu (top left) — each saved chat has a trash button. Deleting removes it from Kivy's server too. Your transactions stay on the chain; nobody can delete those."),
    (r"^\s*(?:log\s*out|logout|sign\s*out|disconnect(?:\s+(?:my\s+)?wallet)?)\s*[.!]*\s*$",
     "Open the menu (top left), tap your wallet (\"… · connected\") › Disconnect all. That disconnects Kivy and signs you out on every device."),
    (r"^\s*(?:how\s+(?:do|can)\s+i\s+)?(?:connect|change|switch)\s+(?:my\s+|a\s+|the\s+|to\s+(?:another|a\s+different)\s+)?(?:wallet|account)s?\s*[?.!]*\s*$",
     "Open the menu (top left) › Connect wallet. To use another account, switch it in your wallet app — Kivy follows whichever account the wallet is set to."),
    (r"^\s*(?:what|which)\s+(?:coins?|tokens?|assets?|cryptos?)\s+(?:do\s+you\s+|can\s+you\s+|does\s+kivy\s+)?(?:support|have|trade|work\s+with|handle)\b.*$",
     "@assets"),
    (r"^\s*(?:what|which)\s+(?:network|chain|blockchain)\b.*$|^\s*(?:is\s+(?:this|kivy)\s+(?:on\s+)?)?(?:mainnet|testnet)\s*\??\s*$",
     "Kivy works on the Sui mainnet — real coins. Your wallet must be set to Sui mainnet too."),
    (r"^\s*(?:ok(?:ay)?,?\s+)?(?:cancel|stop|never\s*mind|nevermind|forget\s+it|don'?t\s+do\s+it|abort|no\s+thanks?)\s*[.!]*\s*$",
     "Okay — nothing happens. A card only does something when you approve it in your wallet."),
    (r"\b(?:undo|reverse|revert|refund)\b.{0,30}\b(?:transaction|tx|swap|send|that|it|last)\b|^\s*undo\s*[?.!]*\s*$|\bcan\s+i\s+get\s+(?:it|my\s+money)\s+back\b",
     "A transaction on Sui can't be undone once it's confirmed — not by Kivy, not by anyone. What I can do is the opposite move: swap back, "
     "withdraw what you lent, or close a position. Tell me which."),
    (r"\b(?:sent|send|transferred)\b.{0,25}\bwrong\s+(?:address|wallet|person)\b|\bwrong\s+(?:address|wallet)\b.{0,25}\b(?:sent|send|transfer)",
     "Coins sent to the wrong address can't be pulled back — the chain has no undo. If you know who owns that address, ask them to send it back. "
     "Ask me \"show my last transaction\" to see exactly where it went. From now on: every send card shows the full address — check it before you approve."),
    (r"^\s*(?:is\s+(?:this|it|kivy)|are\s+you)\s+(?:safe|secure|legit|a\s+scam|trustworthy)\s*[?.!]*\s*$|\bcan\s+(?:you|kivy)\s+(?:steal|take|touch|move)\s+my\s+(?:money|funds|coins|crypto)\b"
     r"|\bdo\s+(?:you|kivy)\s+(?:hold|have|keep|store|custody)\s+my\s+(?:money|funds|coins|crypto|keys|wallet)\b|\bwho\s+(?:holds|has)\s+my\s+(?:money|funds|keys)\b",
     "Kivy never holds your money or your keys — everything stays in your own wallet. Kivy prepares a transaction, checks it against what you asked "
     "(before you sign and again after), and nothing moves until you approve it in your wallet. The real risks are the markets themselves: prices, "
     "leverage, and the lending markets' collateral."),
    (r"^\s*(?:help|what\s+can\s+(?:you|kivy)\s+do|what\s+do\s+you\s+do|what\s+(?:are\s+)?(?:your|kivy'?s)\s+features|what\s+(?:things\s+)?can\s+i\s+(?:do|ask)(?:\s+here)?|commands|menu\s+of\s+commands|show\s+me\s+what\s+you\s+can\s+do)\s*[?.!]*\s*$", "@help"),
    (r"^\s*(?:(?:kivy'?s?\s+)?fees?|costs?|how\s+much\s+(?:does\s+(?:it|this|kivy)|do\s+you)\s+cost|is\s+(?:it|this|kivy)\s+free|what\s+(?:are\s+(?:the|your)\s+)?fees?|do\s+you\s+charge(?:\s+(?:a\s+)?fees?)?|any\s+fees?|pricing)\s*[?.!]*\s*$"
     r"|\b(?:is|are)\s+(?:it|this|kivy|transactions?|swap\w*|send\w*|lend\w*|withdraw\w*|everything)\b.{0,30}\bfree\b|\bfree\b.{0,30}\b(?:gas|fees?)\b"
     r"|\b(?:do|will|must)\s+i\s+(?:pay|need)\s+(?:for\s+|any\s+)?(?:gas|fees?)\b|\bhow\s+much\s+(?:is|does|will)\s+(?:the\s+)?gas\b|\bwho\s+pays\s+(?:the\s+)?gas\b",
     "@fees"),   # fee and gas questions: a fixed true answer (the AI once said Kivy's fee "covers the network gas", 2026-10-25)
    (r"^\s*(?:how\s+(?:do\s+i|to|can\s+i)\s+(?:start|begin|get\s+started|use\s+(?:this|kivy|it))|getting\s+started|where\s+do\s+i\s+start|what\s+(?:is\s+this|do\s+i\s+do(?:\s+here)?))\s*[?.!]*\s*$",
     "Three steps: 1) Connect your Sui wallet (bottom left). 2) Tell me what you want in plain words — \"swap 5 SUI to USDC\", \"where can I earn the most on my USDC?\", "
     "\"long BTC 3x with $10\". 3) Check the card I show and approve it in your wallet. Nothing moves without that last step."),
]


def common_answer(msg):
    for pat, ans in COMMON_QS:
        if re.search(pat, msg, re.I):
            if ans == '@fees':
                kf = kivy_fee()
                return {'text': 'Asking me is free. Every transaction pays a small Sui network fee (gas, usually under 0.01 SUI) from your wallet. '
                                + (('Kivy\'s own fee: simple swaps, sends and lending are free; a multi-step transaction (several actions in one approval) '
                                    'includes %g SUI, always shown on its card before you approve.' % kf['sui']) if kf else 'Kivy itself charges no fee.')}
            if ans == '@help':
                return {'text': K()['help']}
            if ans == '@assets':
                k = K()
                return {'text': ('I work with these coins: %s — plus every coin Suilend or NAVI lists (USDsui, vSUI, haSUI, sSUI, LBTC, wBTC, BLUE, '
                                 'NAVX…): swap, send, lend, and withdraw as another coin in one approval. Anything unlisted I refuse.') % ', '.join(k['assets'])}
            return {'text': ans}
    return None


# plain swap orders ("swap 0.95 SUI to USDC"): the same card the AI's swap tool builds, without the AI (instant, no budget)
SWAP_EXACT_RE = re.compile(r'^\s*(?:swap|convert|trade)\s+(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?|all|max|half|a\s+quarter|a\s+third)\s+(?:of\s+)?(?:my\s+)?([a-z][a-z0-9]{1,9})\s+(?:to|for|into)\s+([a-z][a-z0-9]{1,9})\s*[.!]*\s*$', re.I)


AMOUNT_ONLY_RE = re.compile(r'^\s*(all|everything|max|half|a\s+quarter|\$?\d+(?:\.\d+)?%?)(?:\s+(?:of\s+it|please|pls|[a-z]{2,10}))?\s*[.!]*\s*$', re.I)


def amount_followup(msg, address, history):
    """A bare amount ("all", "half", "5") answering Kivy's "How much … ?": the user's previous request, with that amount put in
    (2026-10-24: "all" after "How much USDC should I start with?" got the help menu)."""
    m = AMOUNT_ONLY_RE.match(msg)
    if not m or not history:
        return None
    hs = [h for h in history if isinstance(h, dict)]
    last = next((i for i in range(len(hs) - 1, -1, -1) if hs[i].get('role') in ('model', 'assistant', 'kivy')), None)
    if last is None or not re.match(r'^\s*how\s+much\b', str(hs[last].get('text') or hs[last].get('content') or ''), re.I):
        return None
    prev = next((str(h.get('text') or h.get('content') or '') for h in reversed(hs[:last]) if h.get('role') == 'user'), '')
    if not prev or AMOUNT_ONLY_RE.match(prev):
        return None
    amt = m.group(1).replace('everything', 'all')
    new = re.sub(r'\b(?:my|that|the|some|this)\s+(?=[A-Za-z]{2,10}\b)', amt + ' ', prev, count=1, flags=re.I)
    if new == prev:
        new = re.sub(r'^(\s*\w+)\s+', lambda x: x.group(1) + ' ' + amt + ' ', prev, count=1)
    return widget_reply(new, address, hs[:last])


def journey_answers(msg, address):
    if EXIT_RE.match(msg):
        return exit_reply(address)
    if BEST_RE.search(msg):
        br = best_reply(msg, address)
        if br:
            return br
    if ACT_RE.search(msg):
        return activity_reply(msg, address)
    if EARN_RE.search(msg) and not re.search(r'\b(?:swap|send|withdraw|lend|deposit|long|short|close)\b', msg, re.I):
        return earn_reply(address)
    if TOTAL_RE.search(msg):
        return total_reply(address)
    if WALLET_BAL_RE.search(msg):                        # "what is my wallet balance": the coins, from live data
        return {'text': balances_text(address) if address else K()['no_wallet']}
    ca = common_answer(msg)
    if ca:
        return ca
    pm = re.match(r'^\s*(?:what(?:\'s|\s+is)\s+(?:the\s+)?)?(?:current\s+)?(?:price\s+of\s+|how\s+much\s+is\s+(?:1\s+|one\s+)?)?([a-z][a-z0-9]{1,9})(?:\s+price)?(?:\s+(?:now|today|right\s+now))?\s*[?.!]*$', msg, re.I)
    if pm and re.search(r'\bprice\b|\bhow\s+much\s+is\b', msg, re.I) and (ckey(pm.group(1)) or lend_token(pm.group(1))):
        c = ckey(pm.group(1)) or lend_token(pm.group(1))      # "what is the price of sui": Kivy's live price, no AI (2026-10-24)
        px = coin_px(c)
        if px:
            return {'text': '%s is $%s right now.' % (disp(c), dec(px, 6))}
    if re.search(r'\bprices?\b', msg, re.I) and not re.search(r'\b(?:swap|send|lend|buy|sell|long|short|alert|when)\b', msg, re.I):
        cs = []                                          # "price of btc and eth": every coin named, live, no AI (2026-10-25)
        for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', msg):
            c = ckey(w) or lend_token(w)
            if c and c not in cs and w.lower() not in ('price', 'prices', 'of', 'and', 'the', 'now', 'today'):
                cs.append(c)
        if len(cs) >= 2:
            rows = []
            for c in cs[:8]:
                px, ch = coin_px(c), coin_change(c)
                rows.append('• %s  $%s%s' % (disp(c), dec(px, 6) if px else '—', ('  (%s%.2f%% 24h)' % ('+' if ch >= 0 else '', ch)) if px and ch is not None else ''))
            return {'text': 'Right now:\n' + '\n'.join(rows)}
    qm = re.match(r'^\s*(?:(?:get\s+(?:me\s+)?(?:a\s+)?)?quote\s+(?:for\s+)?(?P<a1>\d+(?:\.\d+)?)\s+(?P<c1>[a-z][a-z0-9]{1,9})\s+(?:to|for|in|into)\s+(?P<d1>[a-z][a-z0-9]{1,9})'
                  r'|how\s+much\s+(?P<d2>[a-z][a-z0-9]{1,9})\s+(?:(?:would|will|do|can|could)\s+i\s+get\s+)?(?:for|from)\s+(?P<a2>\d+(?:\.\d+)?)\s+(?P<c2>[a-z][a-z0-9]{1,9}))\s*[?.!]*\s*$', msg, re.I)
    if qm:                                               # a quote is the router's real answer, no AI and nothing to sign (2026-10-25)
        a, b = ckey(qm.group('c1') or qm.group('c2')), ckey(qm.group('d1') or qm.group('d2'))
        amt = float(qm.group('a1') or qm.group('a2'))
        if a and b and a != b and amt > 0:
            try:
                out = float(swap_quote(a, b, amt)['out'])
                return {'text': '%s %s ≈ %s %s right now (the swap router\'s quote, before Kivy\'s fee and gas). Say "swap %s %s to %s" to prepare it.'
                                % (num_plain(amt), disp(a), dec(out, 6), disp(b), num_plain(amt), a, b)}
            except Exception:
                return {'text': "I couldn't get a quote from the swap router right now. Try again in a minute."}
    sm = SWAP_EXACT_RE.match(msg)
    if sm and ckey(sm.group(2)) and ckey(sm.group(3)):
        a, b = ckey(sm.group(2)), ckey(sm.group(3))
        return plan('swap', _amt(sm.group(1), a, address), a, b, holdings=all_balances(address) if address else None)
    return None


def choose_icons(w):
    """Every choice card gets icons where its options name a thing (2026-10-24: "a table with icons"): a perps market's image,
    a lent coin = coin icon + protocol badge, a wallet coin = its icon, perps margin = Aftermath. The page draws a card whose
    options have icons as a table. Options that are just words (a leverage, "spot or perps") stay as they are."""
    if not isinstance(w, dict) or w.get('type') != 'choose':
        return w
    try:
        pm = {r['symbol']: r.get('image') for r in perp_market_list()}
    except Exception:
        pm = {}
    STOP = {'all', 'my', 'the', 'to', 'as', 'on', 'in', 'from', 'of', 'and', 'then', 'it', 'a', 'at', 'with', 'for', 'more', 'max', 'half', 'usd', 'spot',
            'send', 'swap', 'lend', 'buy', 'sell', 'withdraw', 'close', 'open', 'long', 'short', 'chart', 'price', 'see', 'move', 'claim', 'your', 'order',
            'market', 'markets', 'first', 'another', 'coin', 'or', 'quarter', 'deposit', 'trade', 'add', 'take', 'out', 'back', 'now', 'best', 'rate', 'rates'}
    for o in w.get('options') or []:
        if o.get('icon') or o.get('coin'):
            continue
        t = ' %s %s ' % (o.get('label') or '', o.get('ask') or '')   # the label first: "Buy xBTC" is about xBTC, not the USDC it's bought with
        low = t.lower()
        proto = 'navi' if re.search(r'\bnavi\b', low) else 'suilend' if re.search(r'\bsuilend\b', low) else None
        perpish = re.search(r'\b(?:chart|perps?|long|short|close|position|leverage|margin|aftermath|tp|sl|stop)\b', low)
        words = [x for x in re.findall(r'[A-Za-z][A-Za-z0-9.&]{1,11}', t) if x.lower() not in STOP]
        if not proto:                                  # a perps market: by context ("long BTC"), or a name that is only a market (TSLA, gold)
            sym = next((chart_symbol(x) for x in words if chart_symbol(x) and pm.get(chart_symbol(x)) and (perpish or not (lend_token(x) or ckey(x)))), None)
            if sym:
                o['icon'] = pm[sym]; o['coin'] = sym
                continue
            if perpish and re.search(r'\b(?:margin|perps?\s+account|aftermath)\b', low):
                o['icon'] = '/icons/aftermath.png'; o['coin'] = 'Aftermath'
                continue
        c = next((lend_token(x) or ckey(x) for x in words if (lend_token(x) or ckey(x))), None)
        if c:
            o['coin'] = disp(c)
            o['icon'] = coin_icon(c)
            if proto and re.search(r'\b(?:lent|lend|withdraw|on|from)\b', low):
                o['proto'] = proto
    if sum(1 for o in w.get('options') or [] if o.get('icon') or o.get('coin')) >= max(1, len(w.get('options') or []) // 2):
        w['table'] = True
    return w


# ── More situations, answered by Kivy itself (2026-10-24 "think about new more situations", 30-case review) ──
# Most rewrite the message into a form Kivy already builds and checks (one approval where the builder allows it);
# the rest are honest answers. Rebalance / gather / dollar + "except" amounts / one coin into several / several into one /
# staked SUI / close-then-lend / automation / no gas / put it back.
STAKED_SUI = ('vSUI', 'haSUI', 'sSUI', 'afSUI', 'stSUI')
AUTO_RE = re.compile(r"\b(?:(?:every|each)\s+(?:day|week|month|hour|morning|night|time|weekend|(?:mon|tues|wednes|thurs|fri|satur|sun)day|\d+\s+(?:days?|weeks?|months?|hours?))"
                     r"|on\s+(?:mon|tues|wednes|thurs|fri|satur|sun)days|daily|weekly|monthly|hourly|automatic(?:ally)?|auto[- ]?(?:compound|rebalance|swap|lend)\w*|whenever"
                     r"|remind\s+me|alert\s+me|notify\s+me|set\s+(?:up\s+)?(?:an?\s+)?(?:alert|reminder|rule))\b"
                     r"|\bif\b.{0,60}\b(?:drops?|falls?|goes\s+(?:up|down|below|above)|rises?|reach(?:es)?|hits?|below|above|under|over)\b.{0,60}\b(?:move|swap|sell|buy|withdraw|lend|close|switch|put)\b", re.I)
GAS_RE = re.compile(r"\b(?:no|not\s+enough|out\s+of|zero|0)\s+(?:sui\s+)?(?:for\s+)?gas\b|\bno\s+sui\s+(?:for|to\s+pay)\b|\bcan'?t\s+pay\s+(?:the\s+)?gas\b|\bneed\s+gas\b"
                    r"|\b(?:pay|cover|sponsor|front)\s+(?:the\s+|my\s+)?gas\b|\bgas\s*(?:-|\s)?(?:free|less)\b|\bfree\s+gas\b", re.I)   # "can you pay gas for me" (2026-10-25)
PUTBACK_RE = re.compile(r"\b(?:put|move|send)\s+(?:it|them|everything|my\s+money)\s+back\b|\bback\s+(?:to\s+)?where\s+it\s+was\b|\bgo\s+back\s+to\s+how\s+it\s+was\b", re.I)
MONEY_RE = r'(?:\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:usd|dollars?|bucks)\b)'


def mk_name(key):
    """A market as the chain reader reads it back ("navi main", "suilend main", "Suilend Ember")."""
    return MKT_EXACT.get(key, LEND_MKTS.get(key, key))


REAL_COINS = {t.lower() for t in STAKED_SUI}         # real coins Kivy may not list: never auto-"corrected" into another coin (2026-10-25)
STAKED_ASK = re.compile(r'\b(?:the\s+)?(?:(?:best|highest(?:[\s-]+paying)?|top)\s+)?(?:liquid(?:ly)?\s+)?staked\s+sui\b|\b(?:an?\s+)?lsts?\b', re.I)
STAKED_NAMED = re.compile(r'\b(?:to|into|for)\s+(?:my\s+)?(?:%s)\b' % '|'.join(STAKED_SUI), re.I)   # a staked coin named as the TARGET ("withdraw my ssui…" is a source)


def staked_ranked(mk=None):
    """The staked SUI coins Kivy can lend (today sSUI, vSUI, haSUI), best lending rate first; mk = only that market."""
    rows = [(t, r) for t in STAKED_SUI for r in lend_rows_for(t)
            if not mk or r['market'] == mk or (mk.startswith('fam:') and r['market'].split('-')[0] == mk[4:])]
    return sorted(rows, key=lambda x: -float(x[1]['total_apr']))


def staked_line(ranked, where=None):
    """'haSUI pays the most of the 3 staked SUI Kivy can lend (haSUI 2.46%, sSUI 2.35%, vSUI 2.22% a year).'"""
    seen = []
    for t, r in ranked:
        if t not in [x[0] for x in seen]:
            seen.append((t, r))
    rates = ', '.join('%s %.2f%%' % (t, float(r['total_apr'])) for t, r in seen)
    if where and len(seen) == 1:
        return '%s is the staked SUI you can lend on %s (%s a year).' % (seen[0][0], where, rates)
    return '%s pays the most of the %d staked SUI Kivy can lend%s (%s a year).' % (seen[0][0], len(seen), ' on ' + where if where else '', rates)


def _mk_of(text):
    k, _ = chain_market(text or '')
    return k if k and k not in ('best', 'there') else None


def _rows_on(rows, mk, coin=None):
    """The user's lent rows on a named market (exact key or protocol family), optionally one coin."""
    return [r for r in rows if (not coin or r['coin'].lower() == coin.lower())
            and (r['id'] == mk or (mk.startswith('fam:') and r['id'].split('-')[0] == mk[4:]))]


def safest_market(coin):
    """The best rate among markets whose borrowers post broad collateral (5+ coins): the lower-risk choice."""
    col = lend_collateral()
    rows = [r for r in lend_rows_for(coin) if len((col.get(r['market']) or {}).get('coins') or []) >= 5]
    return rows[0]['market'] if rows else None


DUST_USD = 0.01                                       # a coin worth less than this is dust: the wallet panel hides it
DUST_RE = re.compile(r'\bdust\b|\b(?:tiny|small|little)\s+(?:coins?|balances?|amounts?|bits|tokens?)\b', re.I)   # not "leftover": that is the rest of a coin
DUST_TO_RE = re.compile(r'\b(?:(?:in)?to|as)\s+(?:my\s+|some\s+)?([a-z][a-z0-9]{1,9})\b', re.I)


def dust_coins(address, to=None):
    """The verified coins (Kivy's list + every coin the lending markets list) worth under $0.01, SUI aside (it pays gas):
    ([{sym, type, dec, amount, usd, icon}], how many coins Kivy doesn't know, which it never touches)."""
    bal = balances(address) or {}
    out, seen = [], set()
    norm = lambda t: re.sub(r'^0x0*', '0x', str(t or '').lower())
    rows = [(c, float(v or 0), cinfo(c)['type'], int(cinfo(c)['decimals']), coin_px(c), coin_icon(c)) for c, v in bal.items() if c in COINS()]
    rows += [(x['sym'], float(x['amount']), x['type'], int(cinfo(x['sym'])['decimals']), x.get('px'), x.get('icon')) for x in other_balances(address)]
    for c, v, t, d, px, icon in rows:
        if c == 'SUI' or c == to or v <= 0 or norm(t) in seen:
            continue
        seen.add(norm(t))
        if px and 0 < v * float(px) < DUST_USD:
            out.append({'sym': c, 'type': t, 'dec': d, 'amount': v, 'usd': v * float(px), 'icon': icon})
    known = {norm(c['type']) for c in ecoins().values()}
    raw = (_BAL.get(('raw', NET(), address)) or (0, []))[1]
    unknown = sum(1 for t, amt in raw if int(amt or 0) > 0 and norm(t) not in known)
    return sorted(out, key=lambda x: -x['usd']), unknown


def dust_build(address, to, dust):
    """Build the sweep (simulated) and add up what it gets against what it costs. Returns the card, or a text reply."""
    body = {'address': address, 'to': to, 'items': [{'sym': x['sym'], 'type': x['type'], 'dec': x['dec']} for x in dust]}
    out = lend_post('/dust', body)
    if not out.get('ok') and re.search(r'would fail right now', str(out.get('error') or '')) and 'router' not in str(out.get('error') or ''):
        out = lend_post('/dust', body)                # a pool refused a tiny amount (lend_post already retries router aborts)
    if not out.get('ok'):
        return {'text': "I couldn't prepare the dust sweep right now (%s). Try again in a minute." % str(out.get('error') or '')[:120]}
    legs = out.get('legs') or []
    why = {}
    for x in out.get('skipped') or []:                # grouped: "sSUI and haSUI stay: their swap route uses…"
        why.setdefault(x['why'], []).append(disp(x['sym']))
    skipped = '; '.join('%s: %s' % (' and '.join(v) if len(v) < 3 else ', '.join(v[:-1]) + ' and ' + v[-1],
                                    re.sub(r'^its ', 'their ' if len(v) > 1 else 'its ', w)) for w, v in why.items())
    if not legs:
        return {'text': 'None of your dust can be swapped right now%s.' % (' (' + skipped + ')' if skipped else '')}
    psui, pto = coin_px('SUI') or 0, (1.0 if to in ('USDC', 'USDsui') else coin_px(to) or 0)
    g = out.get('gas') or {}
    gas_sui = (int(g.get('comp') or 0) + int(g.get('storage') or 0) - int(g.get('rebate') or 0)) / 1e9   # below zero: Sui pays back more than it costs
    fee = kivy_fee() if len(legs) >= 2 else None       # Kivy's flat fee: multi-step transactions (2026-10-22)
    fee_sui = float(fee['sui']) if fee else 0.0
    by = {x['type']: x for x in dust}
    got_usd = float(out['expected']) * pto
    net = got_usd - (gas_sui + fee_sui) * psui
    coins = [{'sym': l['sym'], 'icon': (by.get(l['type']) or {}).get('icon') or coin_icon(l['sym']), 'amount': l['amount'],
              'usd': (by.get(l['type']) or {}).get('usd') or 0, 'type': l['type']} for l in legs]
    verdict = ('Worth it: you end up about $%s ahead.' % dec(net, 2) if net > 0 else
               'Not worth it for the money: it costs about $%s more than you get. Approve only if you want a tidy wallet.' % dec(-net, 2))
    return {'text': '%d dust coin%s into %s, one approval. %s%s' % (len(legs), '' if len(legs) == 1 else 's', disp(to), verdict,
                                                                   (' Left out — %s.' % skipped) if skipped else ''),
            'widget': {'type': 'dust', 'to': to, 'to_icon': coin_icon(to), 'coins': coins, 'types': [c['type'] for c in coins],
                       'value_usd': sum(c['usd'] for c in coins), 'expected': out['expected'], 'got_usd': got_usd,
                       'objects': sum(int(l.get('objects') or 0) for l in legs), 'gas_sui': gas_sui, 'gas_usd': gas_sui * psui,
                       'fee_sui': fee_sui, 'fee_usd': fee_sui * psui, 'net_usd': net}}


def usd_small(v):
    """'$0.37', or '<$0.01' for a sum too small to show in cents (2026-10-25: show it, don't hide it)."""
    v = float(v or 0)
    return '$%.2f' % v if v >= 0.01 else '<$0.01' if v > 0 else '$0'


def claim_card(address, only='all'):
    """Every reward waiting in every market — even tiny ones — claimed in ONE transaction, with what it nets after the
    gas (and Kivy's fee, if any): the same sum as the dust sweep (2026-10-25)."""
    try:
        r = lend_cli('build', address, only, 'claim')
    except Exception:
        r = {}
    if not r.get('ok'):
        if 'No rewards' in str(r.get('error') or ''):
            return {'text': 'You have no %slending rewards to claim right now.' % ((CLAIM_SCOPES.get(only) + ' ') if CLAIM_SCOPES.get(only) else '')}
        return {'text': "I couldn't prepare the claim right now (%s). Try again in a minute." % str(r.get('error') or 'builder busy')[:100]}
    items = [{'name': LEND_MKTS.get(x['market'], x['market']), 'market': x['market'], 'symbol': x['symbol'], 'coin_type': x.get('coinType'),
              'icon': coin_icon(x['symbol']), 'amount': x['amount'], 'usd': round(x['usd'], 6)} for x in (r.get('rewards') or [])]
    if not items:
        return {'text': 'You have no lending rewards to claim right now.'}
    got = float(r.get('usd') or 0)
    psui = coin_px('SUI') or 0
    g = r.get('gas') or {}
    gas_sui = (int(g.get('comp') or 0) + int(g.get('storage') or 0) - int(g.get('rebate') or 0)) / 1e9
    fee = kivy_fee() if len(intent_actions('/api/tx/lend', {'action': 'claim'}, {})) >= 2 else None   # a claim is one step: free today
    fee_sui = float(fee['sui']) if fee else 0.0
    net = got - (gas_sui + fee_sui) * psui
    mk = sorted({x['market'] for x in items}, key=lambda m: -sum(x['usd'] for x in items if x['market'] == m))
    verdict = ('Worth it: you end up about %s ahead after gas.' % usd_small(net) if net > 0 else
               'Not worth it yet: the gas is about %s more than the rewards. They keep growing — claiming later pays more.' % usd_small(-net))
    who = CLAIM_SCOPES.get(only) or ''
    return {'text': '%s from %d market%s, %s in all — one approval claims them all. %s' % ((who + ' rewards') if who else 'Rewards', len(mk), '' if len(mk) == 1 else 's', usd_small(got), verdict),
            'widget': {'type': 'rewards', 'items': items, 'usd': round(got, 6), 'markets': mk, 'only': only, 'who': who,
                       'gas_sui': gas_sui, 'gas_usd': gas_sui * psui, 'fee_sui': fee_sui, 'fee_usd': fee_sui * psui, 'net_usd': net}}


DUST_TARGETS = ('SUI', 'USDC', 'USDsui')
try:                                                 # the 2048 seed-phrase words (public list): a pasted seed phrase is never sent on (2026-10-25)
    BIP39 = set(json.load(open(Path(__file__).resolve().parent / 'bip39_english.json')))
except Exception:
    BIP39 = set()
    print('kivy: bip39_english.json missing — the seed-phrase guard only catches private keys', flush=True)
SEED_WARN = ("That looks like a seed phrase or private key, so I didn't read, keep or pass it on. Never share it with anyone — Kivy included: "
             "whoever has it owns your wallet, and real support never asks for it. If you've pasted it anywhere else, move your coins to a new wallet now.")
ADVICE_RE = re.compile(r"\b(?:is|are)\s+\$?[a-z]{2,10}\s+(?:a\s+)?(?:good|bad|smart|safe)\s+(?:investment|buy|bet)\b|\bshould\s+i\s+(?:buy|sell|invest(?:\s+in)?|ape(?:\s+into)?|hold|dump)\b(?!.*\b(?:lend|navi|suilend|ember|market)\b)"
                       r"|\bworth\s+(?:buying|investing)\b|\bwill\s+\$?[a-z]{2,10}\s+(?:go\s+up|go\s+down|pump|dump|moon|crash|rise|fall)\b|\bprice\s+predictions?\b|\bgood\s+time\s+to\s+(?:buy|sell)\b", re.I)
SEED_ASK_RE = re.compile(r'\b(?:seed|recovery|secret|backup)\s+(?:phrase|words?)\b|\bprivate\s+keys?\b|\bmnemonic\b', re.I)


FILLER_RE = re.compile(r'^\s*(?:(?:ok(?:ay)?|alright|all\s+right|yes|yeah|yep|sure|hey|hi|hello|so|well|um+|kivy)\b[\s,!.:-]*)+', re.I)
ACTION_START = re.compile(r'^(?:please\s+|pls\s+|just\s+)?(?:swap|send|transfer|lend|deposit|withdraw|claim|sweep|close|open|long|short|move|convert|buy|sell|stake|put)\b', re.I)


NUM_WORDS = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10'}


def tidy_amounts(msg):
    """Amounts as people type them: ".1" = 0.1 (it was read as 1 — ten times more), "0,1" = 0.1 (a comma before 1-2 digits is a
    decimal point; "1,000" stays a thousand), "0.1sui" = "0.1 sui", "1k" = 1000, "five sui" = "5 sui". Addresses are never touched."""
    def tok(t):
        if t.lower().startswith('0x'):
            return t
        t = re.sub(r'(?<![\d.,])\.(\d)', r'0.\1', t)
        t = re.sub(r'(?<=\d),(\d{1,2})(?!\d)', r'.\1', t)
        t = re.sub(r'(?i)^(\$?)(\d+(?:\.\d+)?)k$', lambda m: m.group(1) + ('%g' % (float(m.group(2)) * 1000)), t)
        m = re.match(r'^(\$?\d+(?:\.\d+)?)([A-Za-z][A-Za-z0-9]{1,9})$', t)
        if m and (ckey(m.group(2)) or lend_token(m.group(2))):
            t = m.group(1) + ' ' + m.group(2)
        return t
    out = ' '.join(tok(t) for t in str(msg or '').split(' '))
    return re.sub(r'(?i)\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(?=([A-Za-z]{2,10})\b)',
                  lambda m: NUM_WORDS[m.group(1).lower()] + ' ' if (ckey(m.group(2)) or lend_token(m.group(2))) else m.group(0), out)


def tidy_wording(msg):
    """Everyday wrappers around a request, removed before any reader sees it (2026-10-27): a trailing "please"/"thanks"
    (it made 'ember please' read as Suilend Main), "can you …" before an action, "get/take/pull X (back) out of M" = withdraw,
    and a trailing "and withdraw it" (read as a second starting coin)."""
    t = ' '.join(str(msg or '').split())
    t = re.sub(r'[\s,.!]*\b(?:please|pls|plz|thanks|thank you|thx|for me|kindly)\s*[.!]*\s*$', '', t, flags=re.I)
    t = re.sub(r'^(?:(?:can|could|would|will)\s+you\s+)(?:please\s+)?(?=(?:get|take|pull|withdraw|swap|convert|send|lend|deposit|move)\b)', '', t, flags=re.I)
    t = re.sub(r'^withdraw\s+it\s*:\s*', 'withdraw ', t, flags=re.I)
    t = re.sub(r'[\s,]*(?:and\s+)?withdraw\s+(?:it|them)\s*[.!]*$', '', t, flags=re.I)
    t = re.sub(r'^(?:get|take|pull)\s+((?:\d[\d.,]*|all|half)\s+\w+)\s+(?:back\s+)?out\s+of\s+', r'withdraw \1 from ', t, flags=re.I)
    return t or msg


def strip_filler(msg):
    """"ok send 1 usdc to …", "hey kivy swap …": the request without the greeting — only when an action follows, so a bare "yes" stays."""
    m = FILLER_RE.match(msg or '')
    rest = msg[m.end():] if m else msg
    return rest if m and ACTION_START.match(rest) else msg


RAW_ERR_RE = re.compile(r'MoveAbort|abort code|simulation failed|dry.?run|Traceback|TypeError|ReferenceError|ECONN|ETIMEDOUT|fetch failed|'
                        r'status code \d|HTTP Error|<html|\{"|\bat \S+\.(?:ts|js):\d|0x[0-9a-f]{20,}::', re.I)   # SDK / upstream wording, not Kivy's


def looks_secret(text, run_min=12):
    """A seed phrase (12+ words in a row from the BIP39 list — real chat tops out at 6; 8 for text that is stored) or a Sui private key."""
    t = str(text or '')
    if re.search(r'suiprivkey1[0-9a-z]{20,}', t, re.I):
        return True
    run = best = 0
    for w in re.findall(r'[a-z]+', t.lower()):
        run = run + 1 if w in BIP39 else 0
        best = max(best, run)
    return best >= run_min


def dust_reply(msg, address):
    """'swap all my dust to usdc', 'sweep dust', 'clean up the tiny coins' (2026-10-25): every verified coin under
    $0.01 into one coin the user picks, in ONE transaction, with what it gets against what it costs."""
    if not DUST_RE.search(msg) or QUESTION_RE.search(msg) and not re.search(r'\b(?:swap|sweep|convert|sell|clean|clear|collect|gather|worth|how\s+much|value)\b', msg, re.I):
        return None
    if re.search(r'^\s*(?:what\s+is|what\'?s)\s+(?:a\s+)?dust(?:\s+coins?)?\s*[?.!]*\s*$|\bdust\s+means?\b', msg, re.I):
        return None                                   # the concept: the FAQ answers it
    if re.search(r'\b(?:withdr[ae]w\w*|margin|perps?|lend\w*|deposit\w*|supply|send|then)\b', msg, re.I):
        return None                                   # a step of something else ("…the dust from navi, then lend it"): the chain reader
    if not ADDR_RE.match(str(address or '')):
        return {'text': 'Connect your wallet and I\'ll find your dust — the coins worth under $0.01 that the wallet panel hides.'}
    m = DUST_TO_RE.search(msg)
    to = ((ckey(m.group(1)) or lend_token(m.group(1))) if m else None) or 'SUI'   # no coin named: straight to the card in SUI, with a picker on it (2026-10-25)
    dust, unknown = dust_coins(address, to)
    left = (' %d coin%s Kivy doesn\'t recognise %s left alone.' % (unknown, '' if unknown == 1 else 's', 'is' if unknown == 1 else 'are')) if unknown else ''
    if not dust:
        return {'text': 'No dust: every coin Kivy recognises in your wallet is worth $0.01 or more (SUI is kept for gas).' + left}
    if NET() != 'mainnet':
        return {'text': 'Dust sweeps need the swap router, which is mainnet-only.'}
    r = dust_build(address, to, dust)
    if AUTO_RE.search(msg) and r.get('text'):         # "every week": Kivy can't repeat it, but here is one now
        r['text'] = "Kivy can't sweep on a schedule — every sweep needs your approval. Here's one now: " + r['text']
    if r.get('widget'):
        r['widget']['to_opts'] = [to] + [c for c in DUST_TARGETS if c != to]   # the card's "Receive as" picker
    if left and r.get('text'):
        r['text'] += left
    return r


def situations(msg, address):
    low = msg.lower()
    try:
        dr = dust_reply(msg, address)
    except Exception as exc:
        print('kivy dust:', str(exc)[:160], flush=True)
        dr = {'text': "I couldn't check your dust right now. Try again in a minute."} if DUST_RE.search(msg) else None
    if dr:
        return dr
    adv = ADVICE_RE.search(msg)
    if adv:
        c = next((ckey(w) or lend_token(w) for w in re.findall(r'[A-Za-z]{2,10}', msg) if (ckey(w) or lend_token(w)) and w.lower() not in ('buy', 'sell', 'good', 'now')), None)
        px, ch = (coin_px(c), coin_change(c)) if c else (None, None)
        fact = (' %s is $%s right now%s.' % (disp(c), dec(px, 6), (' (%s%.2f%% in 24 hours)' % ('+' if ch >= 0 else '', ch)) if ch is not None else '')) if px else ''
        return {'text': "I can't tell you whether to buy, sell or hold — nobody can know where a price goes, and that call is yours." + fact +
                        ' I can show the chart, the lending rates, or prepare a trade you decide on.'}
    if SEED_ASK_RE.search(msg):
        return {'text': "Never share your seed phrase or private key — not with Kivy, not with \"support\", not with anyone. Whoever has it owns your wallet; "
                        "anyone asking for it is trying to steal your coins. Kivy never needs it: your own wallet signs every step. Keep it written down offline. "
                        "If you already shared it, move your coins to a new wallet right away."}
    if AUTO_RE.search(msg) and not QUESTION_RE.search(msg) or re.match(r'^\s*(?:can|could)\s+(?:you|kivy)\b.*\b(?:every|automatic|whenever|if\b.*\b(?:drops?|falls?|below|above))', low):
        return {'text': "Kivy doesn't run automatic, repeating or if-this-then-that moves — every transaction waits for your own approval in "
                        "your wallet, so nothing can happen while you're away. Ask me any time and I'll check the rates and prepare it then."}
    if GAS_RE.search(msg):
        return {'text': "Every Sui transaction pays a small network fee (gas) in SUI — usually under 0.01 SUI. Sending USDC is free with Kivy "
                        "(the fee is covered), but swaps, lending and perps need a little SUI. Get about 0.1 SUI from an exchange or a friend "
                        "to this wallet; that covers many transactions. Kivy can't pay gas for you."}
    if PUTBACK_RE.search(msg):
        return {'text': "A finished transaction can't be undone, but I can do the opposite move. Say where it should go, e.g. "
                        "\"withdraw my USDC from NAVI and lend it on Ember\". Ask \"what did I do today?\" to see your last moves."}
    if not address:
        return None
    rows = None
    def lent():
        nonlocal rows
        if rows is None:
            rows = lend_positions_rows(address)
        return rows
    def coin_in(text):
        return next((c for c in (lend_token(w) or ckey(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', text)
                                 if w.lower() not in ('my', 'all', 'the', 'from', 'to', 'on', 'in', 'of', 'and', 'it', 'half', 'rest', 'lent', 'navi', 'suilend', 'ember', 'main', 'prime')) if c), None)

    # rebalance: "rebalance my usdc 50/50 between ember and navi", "even out my usdc across suilend and navi"
    m = re.search(r'\b(?:re-?balance|even\s+out|balance\s+out|equalize|equalise)\b(?P<mid>.*?)\b(?:between|across|over)\s+(?P<tgt>.+?)\s*[.!?]*$', msg, re.I)
    if m:
        parts = [x.strip() for x in re.split(r'\s*(?:,|&|\band\b)\s*', m.group('tgt')) if x.strip()]
        mks = [_mk_of(x) for x in parts]
        ratio = re.search(r'(\d{1,2})\s*(?:/|-|:)\s*(\d{1,2})', m.group('mid'))
        if len(parts) == 2 and all(mks):
            coin = coin_in(m.group('mid')) or next((r['coin'] for r in lent() if _rows_on([r], mks[0]) or _rows_on([r], mks[1])), None)
            if not coin:
                return {'text': 'You have nothing lent on those markets to rebalance.'}
            w1 = (int(ratio.group(1)) / 100.0) if ratio and int(ratio.group(1)) + int(ratio.group(2)) == 100 else 0.5
            a_rows, b_rows = _rows_on(lent(), mks[0], coin), _rows_on(lent(), mks[1], coin)
            a, b = sum(r['lent'] for r in a_rows), sum(r['lent'] for r in b_rows)
            total = a + b
            if total <= 0:
                return {'text': 'You have no %s lent on those markets.' % disp(coin)}
            diff = a - total * w1
            if abs(diff) < max(0.01, total * 0.005):
                return {'text': 'Already balanced: %s %s on %s and %s on %s.' % (dec(a, 6), disp(coin), parts[0], dec(b, 6), parts[1])}
            frm_rows, to_mk = (a_rows, mks[1]) if diff > 0 else (b_rows, mks[0])
            if len(frm_rows) != 1:
                return None
            to_key = to_mk if not to_mk.startswith('fam:') else next((r['market'] for r in lend_rows_for(coin) if r['market'].split('-')[0] == to_mk[4:]), None)
            if not to_key:
                return None
            return {'_rewrite': 'withdraw %s %s from %s and lend it on %s' % (dec(abs(diff), 9, False), coin, mk_name(frm_rows[0]['id']), mk_name(to_key)),
                    '_say': 'To make it %d/%d: move %s %s from %s to %s.' % (round(w1 * 100), 100 - round(w1 * 100), dec(abs(diff), 6), disp(coin),
                                                                            frm_rows[0]['name'], LEND_MKTS.get(to_key, to_key))}

    # "move half of my ember usdc to navi": the market said before the coin
    m = re.match(r'^\s*(?P<v>move|shift|transfer|switch)\s+(?P<amt>half|all|everything|a\s+third|a\s+quarter|\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?(?P<mk>[a-z]+(?:\s+(?:main|prime|ember|market))?)\s+(?P<coin>[a-z][a-z0-9]{1,9})\s+(?:to|into|over\s+to)\s+(?P<to>.+?)\s*[.!]*$', msg, re.I)
    if m and _mk_of(m.group('mk')) and (lend_token(m.group('coin')) or ckey(m.group('coin'))):
        return {'_rewrite': '%s %s of my %s from %s to %s' % (m.group('v'), m.group('amt'), m.group('coin'), m.group('mk'), m.group('to'))}

    # gather: "move all my lent usdc to the best market", "put all my usdc in one place", "move my usdc to the safest market"
    m = re.search(r'\b(?:move|put|gather|consolidate|combine|bring|shift)\b(?P<mid>.*?)\b(?:to|into|in|on)\s+(?:the\s+|a\s+|one\s+)?(?P<where>best(?:\s+(?:market|rate|yield|place))?|highest\s+(?:rate|yield)|one\s+(?:place|market)|together|single\s+market|safest(?:\s+market)?|safe\s+market|lowest[- ]risk(?:\s+market)?|(?:a\s+)?market\s+with\s+broad\w*\s+collateral)\s*[.!]*$', msg, re.I)
    if m and not re.search(r'\b(?:wallet|perps?|margin)\b', msg, re.I):
        coin = coin_in(m.group('mid'))
        mine = [r for r in lent() if not coin or r['coin'].lower() == coin.lower()]
        coins = {r['coin'] for r in mine}
        if not mine:
            return {'text': 'You have %s lent to move.' % ('no ' + disp(coin) if coin else 'nothing')}
        if len(coins) > 1:
            return {'text': 'You have several coins lent (%s). Which one should I gather? e.g. "move all my %s to the best market".' % (', '.join(sorted(map(disp, coins))), disp(sorted(coins)[0]))}
        coin = mine[0]['coin']
        safe = re.search(r'saf|risk|broad', m.group('where'), re.I)
        best = safest_market(coin) if safe else next((r['market'] for r in lend_rows_for(coin)), None)
        if not best:
            return {'text': 'No market lists %s right now.' % disp(coin)}
        src = [r for r in mine if r['id'] != best]
        if not src:
            return {'text': 'All your %s is already on %s — the %s market for it right now.' % (disp(coin), LEND_MKTS.get(best, best), 'safest' if safe else 'best')}
        why = ('the safest choice: broad collateral' if safe else 'the best rate') + ' (%.2f%% a year)' % float(next((r['total_apr'] for r in lend_rows_for(coin) if r['market'] == best), 0))
        return {'_rewrite': 'withdraw all %s from %s and lend it on %s' % (coin, ' and '.join(mk_name(r['id']) for r in src), mk_name(best)),
                '_say': 'Gathering your %s on %s — %s.' % (disp(coin), LEND_MKTS.get(best, best), why)}

    # withdraw a dollar amount / everything except N / a plain amount: the withdraw card, starting at that amount
    wto, wmsg = None, msg                             # "... and swap to SUI" / "... as SUI": the card swaps it in the same approval (2026-10-26)
    mto = re.search(r'\s+(?:as|(?:and\s+|then\s+|,\s*)*(?:swap|convert|change)(?:\s+\w+){0,3}?\s+(?:to|into))\s+([A-Za-z]{2,10})\s*[?.!]*\s*$', msg, re.I)
    if mto and ckey(mto.group(1)) and not market_only(mto.group(1)):
        wto, wmsg = ckey(mto.group(1)), msg[:mto.start()]
    m = re.match(r'^\s*(?:withdraw|take\s+out|pull(?:\s+out)?|redeem|take)\s+(?P<what>.+?)\s+(?:from|out\s+of|off)\s+(?P<mk>[a-z]+(?:\s+(?:main|prime|ember|market|high\s*yield))?)'
                 r'(?:\s+(?:except|but\s+keep|leaving|leave|keeping)\s+(?P<keep>\$?\s*\d+(?:\.\d+)?)\s*(?P<kc>[a-z]{2,10})?(?:\s+(?:there|in\s+it|lent))?)?\s*[.!]*$', wmsg, re.I)
    if m and _mk_of(m.group('mk')):
        what = m.group('what')
        usd = re.search(MONEY_RE, what, re.I)
        num = None if usd else re.search(r'^(?:my\s+)?(\d+(?:\.\d+)?)\s*([a-z][a-z0-9]{1,9})?(?:\s+of\s+(?:my\s+)?([a-z][a-z0-9]{1,9}))?$', what.strip(), re.I)
        allw = re.match(r'^(?:all|everything|all\s+of\s+it|it\s+all)(?:\s+(?:my\s+)?([a-z][a-z0-9]{1,9}))?$', what.strip(), re.I) if m.group('keep') else None
        if usd or num or allw:
            coin = coin_in(what) or (coin_in(m.group('kc') or '') if m.group('kc') else None)
            here = _rows_on(lent(), _mk_of(m.group('mk')), coin)
            if len(here) != 1:
                if not here:
                    return {'text': 'You have no %s lent on %s.' % (disp(coin) if coin else 'coins', m.group('mk'))}
                return None                              # several coins there and none named: the normal withdraw choice asks
            row = here[0]
            px = 1.0 if row['coin'] in ('USDC', 'USDsui', 'USDT') else float(row.get('px') or coin_px(row['coin']) or 0)
            if usd:
                if not px:
                    return {'text': "I can't get a live %s price right now to turn dollars into %s." % (disp(row['coin']), disp(row['coin']))}
                amt = float(usd.group(1) or usd.group(2)) / px
            elif allw:
                k = m.group('keep').replace('$', '').strip()
                keep = float(k) / px if '$' in m.group('keep') and px else float(k)
                amt = row['lent'] - keep
                if amt <= 0:
                    return {'text': 'You have %s %s lent on %s — nothing above %s to withdraw.' % (dec(row['lent'], 6), disp(row['coin']), row['name'], dec(keep, 6))}
            else:
                amt = float(num.group(1))
            if amt > row['lent'] + 1e-9:
                return {'text': 'You have %s %s lent on %s — less than %s.' % (dec(row['lent'], 6), disp(row['coin']), row['name'], dec(amt, 6))}
            card = withdraw_card(row, wto if wto and wto != row['coin'] else None)
            card['widget']['preset'] = round(amt, 6)
            if usd:
                card['text'] = '$%s of %s is about %s %s. ' % (usd.group(1) or usd.group(2), disp(row['coin']), dec(amt, 6), disp(row['coin'])) + card['text']
            return card

    # lend a dollar amount: "lend $5 worth of sui on navi"
    m = re.match(r'^\s*(?P<v>lend|deposit|supply|put)\s+' + MONEY_RE + r'\s+(?:worth\s+)?(?:of\s+)?(?:my\s+)?(?P<coin>[a-z][a-z0-9]{1,9})\s+(?P<rest>(?:on|in|into|at)\s+.+?)\s*[.!]*$', msg, re.I)
    if m and (lend_token(m.group('coin')) or ckey(m.group('coin'))):
        c = lend_token(m.group('coin')) or ckey(m.group('coin'))
        px = 1.0 if c in ('USDC', 'USDsui', 'USDT') else float(coin_px(c) or 0)
        if px:
            return {'_rewrite': '%s %s %s %s' % (m.group('v'), dec(float(m.group(2) or m.group(3)) / px, 6, False), c, m.group('rest'))}

    # keep some for gas: "keep 0.1 sui for gas and lend the rest of my sui on navi"
    m = re.search(r'\b(?:keep|leave)\s+(?P<k>\d+(?:\.\d+)?)\s*(?P<c>sui)\b(?:\s+(?:for\s+gas|in\s+(?:my\s+)?wallet))?\s*(?:,|and|then)?\s*(?P<rest>.+)$', msg, re.I)
    if m and re.search(r'\b(?:the\s+rest|rest\s+of|everything\s+else|remaining)\b', m.group('rest'), re.I):
        have = float((all_balances(address) or {}).get('SUI') or 0)
        amt = have - float(m.group('k'))
        if amt <= 0:
            return {'text': 'You have %s SUI — nothing left after keeping %s for gas.' % (dec(have, 6), m.group('k'))}
        new = re.sub(r'\b(?:the\s+rest\s+of\s+(?:my\s+)?sui|the\s+rest|rest\s+of\s+(?:my\s+)?sui|everything\s+else|the\s+remaining\s+sui|remaining\s+sui)\b', '%s SUI' % dec(amt, 6, False), m.group('rest'), count=1, flags=re.I)
        if new != m.group('rest'):
            return {'_rewrite': new}

    # one coin into several: "swap half my sui to usdc and the other half to usdsui", "split my sui 70/30 into usdc and deep"
    m = re.search(r'\b(?:swap|convert|split|divide|turn)\b\s+(?:half\s+(?:of\s+)?)?(?:all\s+)?(?:my\s+)?(?P<a>[a-z][a-z0-9]{1,9})\s*(?P<r>\d{1,2}\s*(?:/|-|:)\s*\d{1,2})?\s+(?:in)?to\s+(?P<b>[a-z][a-z0-9]{1,9})\s+and\s+(?:the\s+other\s+half\s+(?:in)?to\s+|the\s+rest\s+(?:in)?to\s+)?(?P<c>[a-z][a-z0-9]{1,9})\s*[.!]*$', msg, re.I)
    if m:
        a, b, c = (lend_token(m.group(x)) or ckey(m.group(x)) for x in ('a', 'b', 'c'))
        if a and b and c and len({a, b, c}) == 3:
            have = max(float((all_balances(address) or {}).get(a) or 0) - (0.05 if a == 'SUI' else 0), 0)
            if have <= 0:
                return {'text': 'You have no %s in your wallet to swap.' % disp(a)}
            r = re.findall(r'\d{1,2}', m.group('r') or '')
            w1 = int(r[0]) / 100.0 if len(r) == 2 and int(r[0]) + int(r[1]) == 100 else 0.5
            x, y = round(have * w1, 6), round(have - have * w1, 6)
            return {'text': 'A swap gives one coin, so this is two swaps — two approvals:' + (' (keeping 0.05 SUI for gas)' if a == 'SUI' else ''),
                    'widget': {'type': 'choose', 'options': [{'label': 'Swap %s %s to %s' % (dec(x, 6), disp(a), disp(b)), 'sub': '%d%%' % round(w1 * 100), 'ask': 'swap %s %s to %s' % (dec(x, 9, False), a, b)},
                                                             {'label': 'Swap %s %s to %s' % (dec(y, 6), disp(a), disp(c)), 'sub': '%d%%' % (100 - round(w1 * 100)), 'ask': 'swap %s %s to %s' % (dec(y, 9, False), a, c)}]}}

    # several coins into one: "convert my vsui and hasui to sui", "clean up my wallet"
    m = re.match(r'^\s*(?:swap|convert|turn|change|sell)\s+(?:all\s+)?(?:my\s+)?(?P<list>[a-z][a-z0-9]{1,9}(?:\s*(?:,|and|&)\s*[a-z][a-z0-9]{1,9})+)\s+(?:in)?to\s+(?P<to>[a-z][a-z0-9]{1,9})\s*[.!]*$', msg, re.I)
    clean = re.search(r'\b(?:clean\s*up|tidy(?:\s+up)?|sweep|consolidate)\s+(?:my\s+)?(?:wallet|dust|small\s+coins|leftovers?)\b|\b(?:dust|leftovers?)\s+(?:in)?to\s+usdc\b', msg, re.I)
    if clean and re.search(r'\b(?:then|lend\w*|deposit\w*|supply|send|put|park)\b', msg, re.I):
        clean = None                                 # "sweep my dust, then lend it": the chain reader builds both steps
    if m or clean:
        bal = {k: float(v or 0) for k, v in (all_balances(address) or {}).items()}
        to = (lend_token(m.group('to')) or ckey(m.group('to'))) if m else 'USDC'
        if m:
            src = [lend_token(x) or ckey(x) for x in re.findall(r'[a-z][a-z0-9]{1,9}', m.group('list'), re.I) if x.lower() not in ('and',)]
            if not to or not all(src):
                return None
        else:
            src = [c for c, v in bal.items() if c not in ('SUI', 'USDC') and v > 0]
        opts, tiny = [], []
        for c in src:
            if c == to:
                continue
            v = bal.get(c, 0) - (0.05 if c == 'SUI' else 0)
            usd = v * float(coin_px(c) or 0)
            if v <= 0:
                continue
            if usd < 0.01:
                tiny.append('%s (%s)' % (disp(c), dec(v, 4)))
                continue
            opts.append({'label': 'Swap all %s to %s' % (disp(c), disp(to)), 'sub': '%s %s · about $%s' % (dec(v, 6), disp(c), dec(usd, 3)), 'ask': 'swap %s %s to %s' % (dec(v, 9, False), c, to)})
        note = (' Too small to swap one by one: %s.' % ', '.join(tiny)) if tiny else ''
        if tiny and to:                             # the dust goes in one sweep instead (2026-10-25)
            opts.append({'label': 'Sweep the dust into %s' % disp(to), 'sub': '%d coins under $0.01 · one approval' % len(tiny), 'ask': 'swap all my dust to %s' % to})
        if not opts:
            return {'text': ('Nothing worth swapping to %s.' % disp(to)) + note}
        if len(opts) == 1 and tiny and to:          # only dust: straight to the sweep card (2026-10-25: no one-option chooser)
            return dust_reply('sweep my dust into %s' % to, address)
        return {'text': ('One swap per coin — %d approvals:' % len([o for o in opts if 'dust' not in o['ask']]) if len(opts) > 1 else 'One swap:') + note, 'widget': {'type': 'choose', 'options': opts}}

    # staked SUI: "swap my sui to the best staked sui and lend it", "stake my sui"
    if re.search(r'\b(?:stake|staking|staked\s+sui|liquid\s+stak\w*|lst)\b', msg, re.I) and re.search(r'\bsui\b', msg, re.I) and not re.search(r'\bunstake\b', msg, re.I) \
            and not re.search(r'\b(?:withdr[ae]w\w*|send|then|move)\b|,', msg, re.I) and not STAKED_NAMED.search(msg):   # more steps / a named LST: the chain reader (2026-10-25)
        lend_at = re.search(r'\b(?:lend|deposit|put|park|supply)\w*\b(.*)$', msg, re.I)
        mk = _mk_of(lend_at.group(1)) if lend_at else None
        ranked = staked_ranked(mk)                   # "…and lend it on suilend": the best staked SUI there
        if not ranked:
            return None
        t, r = ranked[0]
        where = LEND_MKTS.get(r['market'], r['market']) if mk else None
        if re.search(r'\b(?:lend|deposit|earn|and\s+put)\b', msg, re.I):
            return {'_rewrite': 'swap my sui to %s and lend it on %s' % (t, mk_name(r['market'])),
                    '_say': staked_line(ranked, where) + (' That is on top of the staking reward built into its price.' if where else
                                                          ' It lends on %s, on top of the staking reward built into its price.' % LEND_MKTS.get(r['market'], r['market']))}
        plain = next((x for x in lend_rows_for('SUI')), None)
        opts = [{'label': 'Swap to %s and lend it' % t, 'sub': 'staking reward + %.2f%% a year on %s' % (float(r['total_apr']), LEND_MKTS.get(r['market'], r['market'])),
                 'ask': 'swap my sui to the best staked sui and lend it'},
                {'label': 'Just swap to %s' % t, 'sub': 'staking reward only, kept in your wallet', 'ask': 'swap my sui to %s' % t}]
        if plain:
            opts.append({'label': 'Lend plain SUI', 'sub': '%.2f%% a year on %s' % (float(plain['total_apr']), LEND_MKTS.get(plain['market'], plain['market'])), 'ask': 'lend sui on %s' % mk_name(plain['market'])})
        return {'text': 'Kivy stakes through liquid staking: swap SUI into sSUI, vSUI or haSUI (each earns the staking reward in its price), and lending it earns more on top.',
                'widget': {'type': 'choose', 'options': opts}}

    # close a position, then lend: the close is its own transaction — say the next step instead of dropping it
    m = re.search(r'^(?P<close>.*\bclose\b.*?)\s*(?:,|and\s+then|then|and)\s+(?:then\s+)?(?:lend|deposit|put|park)\s+(?:it|the\s+money|the\s+usdc|the\s+margin|that)\s+(?:on|in|into|at)\s+(?P<to>.+?)\s*[.!]*$', msg, re.I)
    if m and _mk_of(m.group('to')):
        r = widget_reply(m.group('close'), address)
        if r and isinstance(r.get('widget'), dict) and str(r['widget'].get('type', '')).startswith('perp'):
            r['text'] = (r.get('text') or '') + ' After it closes, say "move my perps margin to %s" to lend what it frees — that is a second approval.' % m.group('to')
            return r
    return None


def quick_answers(msg, address):
    """Plain questions with a definite answer, before any pattern could misread them (role-play review, 2026-10-23)."""
    low = msg.lower()
    # a bank, card or cash-out: Kivy moves coins between Sui addresses only
    if BANK_RE.search(msg) and re.search(r'\b(?:send|transfer|withdraw|move|pay|cash|get|deposit|wire)\b', low):
        return {'text': "I can only move coins between Sui addresses — I can't send to a bank account or card. To cash out, people usually "
                        "send USDC to their own account on an exchange that supports Sui, sell it there, and withdraw to their bank."}
    # "which is better, lending or perps?" — a comparison, not a market list
    if re.search(r'\blend\w*\b', low) and re.search(r'\bperps?\b|\btrad\w*\b|\bleverage\b', low) and re.search(r'\b(?:better|vs|versus|or|differen\w*|compare)\b', low) \
            and not re.search(r'\b(?:move|withdraw|deposit|swap|send|take|put|open|close)\b', low):
        try:
            rr = sorted(float(v.get('total_apr') or 0) for v in lend_rates().values() if float(v.get('total_apr') or 0) > 0)
            band = 'about %.0f–%.0f%% a year on USDC right now' % (rr[0], rr[-1]) if rr else 'a steady yearly rate'
        except Exception:
            band = 'a steady yearly rate'
        return {'text': "They're different jobs. Lending earns " + band + " and your deposit stays the same size — "
                        "the risk is the market's collateral. Perps is trading: with leverage your gains and losses are multiplied, and a big enough "
                        "move against you liquidates the position. For income, lending; for a view on a price, perps — with money you can afford to lose.",
                'widget': None}
    # "how close am i to liquidation?" — each open position: price, liquidation price, how far away
    if re.search(r'\bliquidat\w*|\bliq\b', low) and re.search(r'\b(?:how\s+(?:close|far|near)|distance|am\s+i|risk|safe|danger|near)\b', low) \
            and not LEND_WORD_RE.search(msg) and address:
        try:
            r = lend_cli('perp-positions', address, timeout=40)
            pos = r.get('positions') or []
            if not pos:
                return {'text': "You have no open perps positions, so nothing can be liquidated right now."}
            mk = {x['symbol']: x for x in perp_market_list()}
            lines = []
            for p in pos:
                px, lq = float((mk.get(p['symbol']) or {}).get('price') or 0), float(p.get('liquidation') or 0)
                if not (px and lq):
                    continue
                gap = (px - lq) / px * 100 if p['side'] == 'long' else (lq - px) / px * 100
                lines.append('• %s %s %gx: price %s, liquidation %s — %.1f%% %s.%s' % (p['symbol'], p['side'], float(p.get('leverage') or 0), fmt_px(px), fmt_px(lq),
                             abs(gap), 'away' if gap > 0 else 'PAST it', ' That is near — adding margin pushes it further away.' if 0 < gap < 5 else ''))
            if lines:
                return {'text': 'How far each position is from liquidation:\n' + '\n'.join(lines), 'widget': {'type': 'perppos', **r}}
        except Exception:
            pass
    # goals — "passive income", "earn something on my money", "what should I do with my idle usdc": a personal answer from what
    # they hold and today's rates, with the next step one tap away (role-play review 2026-10-23: the AI kept asking instead)
    if address and re.search(r'\bpassive\s+income\b|\b(?:earn|make)\s+(?:something|money|interest|yield|a\s+return)\b|\bidle\b|\bput\s+(?:my\s+)?(?:money|usdc|funds?)\s+to\s+work\b|\bgrow\s+my\s+(?:money|savings|funds?)\b', low) \
            and not re.search(r'\b\d', low) and not re.search(r'\b(?:swap|send|withdraw|remove|pull|move|then|close|long|short)\b|\btake\s+(?:\S+\s+){0,3}out\b', low):   # a stated move is a chain, not advice
        try:
            hold = all_balances(address) or {}
            perp_free = float(perp_account(address).get('collateral') or 0)
            stab = {c: float(hold.get(c) or 0) for c in ('USDC', 'USDsui') if float(hold.get(c) or 0) >= 0.5}
            best = {}
            for r in lend_rates_all():
                if not r.get('rate_only') and (r['asset'] not in best or float(r['total_apr']) > float(best[r['asset']]['total_apr'])):
                    best[r['asset']] = r
            bu = best.get('USDC')
            if re.search(r'\blow\s+risk|\bsafe|\bcareful|\bconservative', low):   # low risk: the best rate among markets with broad collateral (5+ coins)
                col = lend_collateral()
                broad = [r for r in lend_rates_all() if r['asset'] == 'USDC' and not r.get('rate_only') and len((col.get(r['market']) or {}).get('coins') or []) >= 5]
                if broad:
                    bu = max(broad, key=lambda r: float(r['total_apr']))
            lines, opts = [], []
            if bu and (perp_free >= 0.5 or stab):
                nm = LEND_MKTS.get(bu['market'], bu.get('name') or bu['market'])
                if stab:
                    c, a = max(stab.items(), key=lambda x: x[1])
                    lines.append('You have %s %s sitting in your wallet — lent on %s it would earn about %.2f%% a year (≈ $%.2f).' % (dec(a), disp(c), nm, float(bu['total_apr']), a * float(bu['total_apr']) / 100))
                    opts.append({'label': 'Lend my %s on %s' % (disp(c), nm), 'sub': '%.2f%% a year' % float(bu['total_apr']), 'ask': 'lend all my %s on %s' % (c, nm)})
                if perp_free >= 0.5:
                    lines.append('Your perps account has %s USDC free and not working — moved to %s it would earn about %.2f%% a year (≈ $%.2f).' % (
                        dec(perp_free), nm, float(bu['total_apr']), perp_free * float(bu['total_apr']) / 100))
                    opts.append({'label': 'Move perps USDC to %s' % nm, 'sub': 'one approval', 'ask': 'withdraw all my usdc from aftermath and lend it on %s' % nm})
            sui = spendable('SUI', hold)
            bs = best.get('SUI')
            if bs and sui * float(coin_px('SUI') or 0) >= 1:
                lines.append('Your %s SUI could earn about %.2f%% a year on %s (its price still moves with SUI).' % (dec(sui), float(bs['total_apr']), LEND_MKTS.get(bs['market'], bs.get('name') or bs['market'])))
                opts.append({'label': 'Lend my SUI', 'sub': '%.2f%% a year' % float(bs['total_apr']), 'ask': 'lend all my sui on %s' % LEND_MKTS.get(bs['market'], bs['market'])})
            if lines:
                low_risk = (' I picked %s for low risk: its borrowers post many large coins as collateral, so the risk is spread (no market is risk-free). '
                            'Ember pays more but takes only one collateral coin.' % LEND_MKTS.get(bu['market'], bu['market'])) if bu and re.search(r'\blow\s+risk|\bsafe|\bcareful|\bconservative', low) else ''
                opts.append({'label': 'See all lending markets', 'sub': 'rates and collateral', 'ask': 'lending markets'})
                return {'text': 'The steady way to earn is lending.\n' + '\n'.join('• ' + x for x in lines) + low_risk, 'widget': {'type': 'choose', 'options': opts[:4]}}
        except Exception:
            pass
    # "what is the funding rate on btc?" — the live number
    if FUNDING_Q_RE.search(msg) and re.search(r'\b(?:what|how\s+much|current|rate|now)\b', low):
        sym = next((chart_symbol(w) for w in re.findall(r'[A-Za-z&][A-Za-z0-9&]{1,9}', msg) if w.lower() not in TRADE_STOP | {'funding', 'what', 'rate', 'how', 'much', 'is', 'on', 'now', 'current'} and chart_symbol(w)), None)
        if sym:
            r = next((x for x in perp_market_list() if x['symbol'] == sym), None)
            if r and r.get('funding') is not None:
                f = float(r['funding'])
                who = 'longs pay shorts' if f > 0 else 'shorts pay longs' if f < 0 else 'no one pays right now'
                return {'text': '%s funding is %+.4f%% per hour right now (%s) — about %+.2f%% a day if it stays there. It changes every hour '
                                'with the gap between the perps price and the index.' % (sym, f, who, f * 24)}
    return None


def widget_reply(msg, address, history=None):
    qa = quick_answers(msg, address)
    if qa:
        return qa
    try:
        ja = journey_answers(msg, address)          # exit everything, best rate, activity, earnings, common questions (2026-10-24)
    except Exception:
        ja = None
    if ja:
        return ja
    try:
        fu = amount_followup(msg, address, history)
    except Exception:
        fu = None
    if fu:
        return fu
    if not getattr(XW, 'sit_depth', 0):              # rebalance, gather, $ / except amounts, coin splits, staked SUI… (2026-10-24)
        try:
            st = situations(msg, address)
        except Exception:
            st = None
        if st and '_rewrite' in st:
            XW.sit_depth = 1
            try:
                r = widget_reply(st['_rewrite'], address, history)
            finally:
                XW.sit_depth = 0
            if r and st.get('_say'):
                r = dict(r, text=st['_say'] + ' ' + (r.get('text') or ''))
            if r:
                return r
        elif st:
            return st
    try:
        xr = xw_parse(msg, address, history)          # several of the user's own accounts in one sentence (2026-10-21)
    except Exception:
        xr = None
    if xr:
        return xr
    try:
        sr = send_reply(msg, address, history)        # sends, before anything that could misread them (2026-10-21)
    except Exception:
        sr = None
    if sr:
        return sr
    if re.match(r'^\s*(?:buy|sell)\s+[A-Za-z]{2,10}\s+(?:page|market|screen)\s*[?.!]*\s*$', msg, re.I):
        try:                                         # "buy wal page": the spot page, not a swap card (2026-10-21)
            ms = market_switch(msg, address, history)
        except Exception:
            ms = None
        if ms:
            return ms
    try:
        bl = best_lend(msg, address, history) if not QUESTION_RE.search(msg) else None
    except Exception:
        bl = None
    if bl:
        return bl
    if BORROW_RE.search(msg) and not QUESTION_RE.search(msg):
        return {'text': 'Kivy never borrows, and lent coins can\'t be perps collateral while they stay lent. What works: withdraw them and '
                        'deposit into your perps account in one approval — e.g. "withdraw my USDC from Suilend Ember and deposit it into '
                        'aftermath" — then open the trade from the perps card.'}
    _named = [ckey(w) or lend_token(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', msg) if (ckey(w) or lend_token(w)) and w.lower() not in ('all', 'my', 'from')]
    _rows = lend_positions_rows(address) if address and re.search(r'\beverywhere|\bfrom\s+all|\ball\s+(?:my\s+)?(?:markets|protocols|places)', msg, re.I) else []
    _multi = [c for c in {r['coin'] for r in _rows} if sum(1 for r in _rows if r['coin'] == c) >= 2]
    if _named and any(str(n).lower() == str(c).lower() for n in _named for c in _multi):
        pass                                          # one coin in several markets: the chain reader withdraws them all in ONE approval (2026-10-24)
    elif re.search(r'\b(?:withdraw|take\s+out|pull)\b.*\b(?:everything|all)\b.*\b(?:everywhere|from\s+all|all\s+(?:my\s+)?(?:markets|protocols|places))\b', msg, re.I) and address:
        rows = _rows or lend_positions_rows(address)
        opts = [{'label': 'All your %s (%d markets)' % (disp(c), sum(1 for r in rows if r['coin'] == c)), 'sub': 'one approval',
                 'ask': 'withdraw all %s from everywhere and keep it in my wallet' % c} for c in _multi]
        opts += [{'label': '%s on %s' % (disp(r['coin']), r['name']), 'sub': '%s lent' % dec(r['lent']), 'ask': 'withdraw all %s from %s' % (r['coin'], r['name'])}
                 for r in rows if r['coin'] not in _multi]
        try:
            pa = perp_account(address, fresh=True)
            if pa.get('has') and float(pa.get('collateral') or 0) > 0:
                opts.append({'label': 'USDC in your perps account', 'sub': '%s free' % dec(pa['collateral']), 'ask': 'withdraw all from perps'})
        except Exception:
            pass
        if not opts:
            return {'text': 'You have nothing lent and no free perps margin to withdraw.'}
        return {'text': ('Each coin is its own approval. Which first?' if _multi else 'Each withdrawal is its own approval. Which first?'),
                'widget': {'type': 'choose', 'options': opts[:6]}}
    if PERP_AUTO_RE.search(msg) and PERP_ACT_RE.search(msg) and not QUESTION_RE.search(msg):
        return {'text': 'Kivy never trades on its own — no schedules, bots or copy-trading. Every order is one you ask for and sign in '
                        'your wallet. Tell me one order (e.g. "long SUI $10 5x") and I\'ll set it up now.'}
    try:
        pm = perp_manage(msg, address)
    except Exception:
        pm = None
    if pm:
        return pm
    if PERP_WD_LOOSE.search(msg) and not PERP_WD_RE.match(msg):
        am = re.search(r'\b(\d+(?:\.\d+)?|all|max|half|everything)\b', msg, re.I)
        msg = 'withdraw %s from perps' % (am.group(1) if am else '')
    elif PERP_DEP_LOOSE.search(msg) and not PERP_DEP_RE.match(msg) and not CHAIN_PERPS.search(msg.split(' into ')[0] if ' into ' in msg else ''):
        am = re.search(r'\b(\d+(?:\.\d+)?|all|max|half)\b', msg, re.I)
        msg = 'deposit %s to perps' % (am.group(1) if am else '')
    msg = re.sub(r'\s+', ' ', msg)
    bt = re.search(r'\b(?:move|shift|transfer)\s+(?:(\d+(?:\.\d+)?|all|half)\s+)?(?:my\s+)?(?:usdc\s+)?between\s+(?:my\s+)?(?:lending|lent|earn)\s+and\s+(?:my\s+)?(?:perps?|aftermath|margin)\b', msg, re.I)
    if bt:                                           # "move 10 usdc between my lending and perps": which way? (2026-10-21)
        a = bt.group(1) or 'all'
        return {'text': 'Which way should the USDC go?', 'widget': {'type': 'choose', 'options': [
            {'label': 'Lending → perps margin', 'sub': 'withdraw from a lending market, deposit as margin', 'ask': 'withdraw %s usdc from lending and deposit it into perps' % a},
            {'label': 'Perps margin → lending', 'sub': 'withdraw margin, lend it at the best rate', 'ask': 'withdraw %s usdc from aftermath and lend it on the best market' % a}]}}
    try:
        ip = inproto(msg, address)                   # "change vsui to hasui in navi": swap a lent coin in place (2026-10-21)
    except Exception:
        ip = None
    if isinstance(ip, dict):
        return ip
    if ip:
        msg = ip
    _sl = SWAPLEND_RE.search(msg)
    if not (_sl and lend_token(_sl.group(2)) and lend_token(_sl.group(3))):   # swap-then-lend has its own card (with a slider); moves are chains
        try:
            ch = chain_parse(msg, address)
        except Exception as exc:
            ch = {'text': 'I couldn\'t plan those steps right now (%s). Try again, or one step at a time.' % str(exc)[:80]}
        if ch is not None:
            return ch
    un = re.search(r'\b(long|short)\b.*?\b(?:with|for)\s+(\d+(?:\.\d+)?)\b(?!\s*(?:x\b|usdc|usd|\$|dollars?|%|times|of))', msg, re.I)
    if un and '$' not in msg and not re.search(r'\d\s*(?:x|usdc|usd|dollars?)\b', msg[:un.start(2)], re.I):   # "short BTC with 10": $, USDC margin or 10x?
        sym = next((chart_symbol(w) for w in re.findall(r'[A-Za-z&][A-Za-z0-9&]{1,9}', msg) if w.lower() not in TRADE_STOP | {'use', 'max'} and chart_symbol(w)), None)   # US500 too
        mpx = float(next((r.get('price') for r in perp_market_list() if r['symbol'] == sym), 0) or 0) if sym else 0
        if sym and float(un.group(2)) < 1 and mpx and float(un.group(2)) * mpx >= 1:
            sym = None                                   # "long btc for 0.001": an amount of BTC, not a $ or margin question (the trade reader takes it)
        if sym:
            n, sd = un.group(2), un.group(1).lower()
            return {'text': 'What does %s mean here?' % n, 'widget': {'type': 'choose', 'options': [
                {'label': '$%s order' % n, 'sub': '%s %s, size $%s' % (sd, sym, n), 'ask': '%s %s $%s' % (sd, sym, n)},
                {'label': '%s USDC margin' % n, 'sub': 'the money you put in', 'ask': '%s %s with %s usdc margin' % (sd, sym, n)},
                {'label': '%sx leverage' % n, 'sub': 'then pick a size', 'ask': '%s %s %sx' % (sd, sym, n)}]}}
    pt = parse_trade(msg)
    um = None if pt else re.match(r'^\s*(?:go\s+)?(long|short)\s+([A-Za-z][A-Za-z0-9&]{1,14})\b(?=.*(?:\$\s*\d|\d\s*(?:\$|usd\b|usdc\b)|\d\s*x\b))', msg, re.I)
    if um and um.group(2).lower() not in TRADE_STOP and not chart_symbol(um.group(2)) and not ckey(um.group(2)):
        # "short DOGECOIN2 $5": a trade on a market that doesn't exist — say so plainly, offer real ones (never guess)
        return {'text': "%s isn't one of the perps markets I can trade. Pick one of these, or open the market list." % um.group(2).upper(),
                'widget': {'type': 'choose', 'options': [{'label': '%s %s' % (um.group(1).title(), x), 'sub': 'perps', 'ask': 'chart %s' % x} for x in ('BTC', 'ETH', 'SOL', 'SUI')]
                           + [{'label': 'All markets', 'sub': 'search 59 perps markets', 'ask': 'market'}]}}
    if pt and address:
        spot = [a.upper() for a in (K().get('assets') or [])]
        word_spot = CHART_ALIASES.get(pt['coin'].upper(), pt['coin'].upper())
        ambiguous = pt['word'] in ('buy', 'sell', 'ape') and not pt['lev'] and not re.search(r'\bperps?\b', msg, re.I) and not pt.get('margin') and not pt.get('liq') and word_spot in spot   # 'buy BTC for $5' could be spot (a liquidation price means perps)
        if ambiguous:                                   # 'buy BTC for $5': spot or perps? ask, with both orders ready to go
            sn = disp(word_spot)
            if pt.get('qty') and not pt.get('usd'):     # "buy 0.01 btc": an amount of the coin (2026-10-23)
                usd_q = round(pt['qty'] * float(pt.get('price') or coin_px(word_spot) or 0), 2)
                spot_o = {'label': 'Spot — %s %s %s' % ('sell' if pt['word'] == 'sell' else 'buy', num_plain(pt['qty']), sn), 'sub': 'Real %s in your wallet' % sn,
                          'ask': ('swap %g USDC to %s' % (usd_q, word_spot)) if pt['word'] == 'buy' else ('swap %s %s to USDC' % (num_plain(pt['qty']), word_spot))}
                perp_o = {'label': 'Perps — %s %s %s' % (pt['side'], num_plain(pt['qty']), pt['sym']), 'sub': 'All your perps margin; Kivy works out the leverage',
                          'ask': '%s %s %s perps%s' % (pt['side'], num_plain(pt['qty']), pt['coin'], (' at %s' % num_plain(pt['price'])) if pt.get('price') else '')}
            else:
                spot_o = {'label': 'Spot — %s $%g of %s' % ('sell' if pt['word'] == 'sell' else 'buy', pt['usd'], sn), 'sub': 'Real %s in your wallet' % sn,
                          'ask': ('swap %g USDC to %s' % (pt['usd'], word_spot)) if pt['word'] == 'buy' else ('swap $%g of %s to USDC' % (pt['usd'], word_spot))}
                perp_o = {'label': 'Perps — %s %s $%g' % (pt['side'], pt['sym'], pt['usd']), 'sub': 'All your perps margin; Kivy works out the leverage',
                          'ask': '%s %s $%g perps' % (pt['side'], pt['sym'], pt['usd'])}
            return {'text': 'Spot or perps for this %s order?' % sn, 'widget': {'type': 'choose', 'options': [spot_o, perp_o]}}
        if not ambiguous:
            lev, lnote = lev_check(msg, pt['sym'], pt['lev'])
            if lev is None:
                return lnote
            ol = open_lev(address, pt['sym'])            # Aftermath keeps ONE leverage per market: an open position fixes it (Error 1022, 2026-10-23)
            if ol:
                if pt.get('liq'):
                    return {'text': 'Your open %s position is %gx, and Aftermath keeps one leverage per market — so a new liquidation price isn\'t possible '
                                    'while it\'s open. Add to it at %gx instead?' % (pt['sym'], ol, ol),
                            'widget': {'type': 'choose', 'options': [{'label': 'Add at %gx' % ol, 'sub': 'same leverage as your position',
                                                                      'ask': '%s %s %s%gx' % (pt['side'], pt['sym'], ('%s %s ' % (num_plain(pt['qty']), pt['coin'])) if pt.get('qty') else ('$%g ' % pt['usd']) if pt.get('usd') else '', ol)}]}}
                if abs(float(pt.get('lev') or 0) - ol) > 1e-9:
                    lnote += ' Same %gx as your open %s position (Aftermath keeps one leverage per market).' % (ol, pt['sym'])
                pt['lev'] = lev = ol
            sv = perp_math_card(pt, msg, address, lev)   # amount / $ size / liquidation price without a leverage: Kivy's perps math
            if sv:
                return sv
            lev = lev or 1                               # no leverage named -> 1x (safest); the card can change it
            if pt.get('margin') == -1:                   # "all my perps margin": what the account holds
                pt['margin'] = float(perp_account(address, fresh=True).get('collateral') or 0)
                if pt['margin'] <= 0:
                    return deposit_ask(address)
            if pt.get('margin'):                         # "using 10 USDC margin": that is the money in, the order is bigger
                pt['usd'] = round(pt['margin'] * 0.97 / (1.05 / lev + 0.0009), 2)
                lnote += ' %g USDC in at %gx ≈ $%s order.' % (pt['margin'], lev, pt['usd'])
            img = next((r['image'] for r in perp_market_list() if r['symbol'] == pt['sym']), None)
            cw = {'type': 'chart', 'symbol': pt['sym'], 'tf': '15m', 'image': img,
                  'trade': {'open': True, 'side': 1 if pt['side'] == 'short' else 0, 'usd': pt['usd'], 'lev': lev}}
            if pt['price']:
                cw['trade'].update({'type': 'limit', 'price': pt['price']})
            cf = perp_confirm(address, cw)
            if cf is not cw and (cf.get('deposit') or 0) > 0.000001:
                return deposit_ask(address, cf['deposit'])
            if cf is not cw:
                return {'text': 'Check your %s order and confirm in your wallet.%s' % (pt['sym'], lnote), 'widget': cf}
    try:
        ms = market_switch(msg, address, history)    # by name, group or typo (2026-10-21)
    except Exception:
        ms = None
    if ms:
        return ms
    mvm = MKT_VIEW_RE.match(msg) or MKT_OPEN_RE.match(msg)
    if mvm and mvm.group(1).lower() not in ('perps', 'perp', 'lending', 'spot', 'all', 'more', 'the', 'other', 'any'):
        mv = market_overview(mvm.group(1), address)
        if mv:
            mv['venue'] = 'spot' if (re.search(r'\bspot\b', msg, re.I) and mv['spot']) or not mv['perp'] else 'perp'
            return {'text': 'Opening the %s trading page.' % ((mv['spot_name'] + ' spot') if mv['venue'] == 'spot' else (mv['perp'] + ' perps')), 'widget': mv}
    bm = BARE_RE.match(msg)
    if bm:
        w = re.sub(r'[^A-Za-z0-9]', '', bm.group(1)).upper()
        sym = chart_symbol(bm.group(1))
        last = next((str(h.get('text') or '') for h in reversed(history or []) if isinstance(h, dict) and h.get('role') == 'model'), '')
        if sym and re.search(r'perps markets|live chart|long or short', last, re.I):
            msg = 'chart ' + sym                        # they just looked at the perps markets: open that one
        elif sym or CHART_ALIASES.get(w, w) in [a.upper() for a in (K().get('assets') or [])] or ckey(bm.group(1)):
            ia = intent_ask(bm.group(1), address, history)   # otherwise: what to do with it — lend, withdraw, swap, perps, price
            if ia:
                return ia
            msg = 'trade ' + bm.group(1)
    try:
        sa = spot_ask(msg, address)
    except Exception:
        sa = None                                    # never break chat over a quick-pick helper; the AI takes it from here
    if sa:
        return sa
    ta = TRADE_ASK_RE.match(msg)
    if ta:
        sym = chart_symbol(ta.group(1))
        spot = [a.upper() for a in (K().get('assets') or [])]
        w = re.sub(r'[^A-Za-z0-9]', '', ta.group(1)).upper()
        sp = sym if sym in spot else (CHART_ALIASES.get(w, w) if CHART_ALIASES.get(w, w) in spot else None)
        mv = market_overview(ta.group(1), address)
        if mv and mv['perp'] and mv['spot']:            # both exist: the user picks the venue first
            return {'text': 'Which market do you want to trade %s on?' % mv['perp'],
                    'widget': {'type': 'choose', 'options': [
                        {'label': 'Perps — long or short %s' % mv['perp'], 'sub': 'Leverage, order book, stop-loss / take-profit', 'ask': 'open %s perps' % mv['perp']},
                        {'label': 'Spot — buy or sell %s' % mv['spot_name'], 'sub': 'Swap for real %s in your wallet' % mv['spot_name'], 'ask': 'open %s spot' % mv['spot_name']}]}}
        if mv:
            mv['venue'] = 'perp' if mv['perp'] else 'spot'
            return {'text': 'Opening the %s trading page.' % ((mv['perp'] + ' perps') if mv['perp'] else (mv['spot_name'] + ' spot')), 'widget': mv}
        if sym or sp:
            name = sym or sp
            opts = []
            if sym:
                opts.append({'label': 'Perps — long or short %s' % name, 'sub': 'Leverage, live chart, stop-loss / take-profit', 'ask': 'chart %s' % name})
            if sp and sp != 'USDC':
                opts.append({'label': 'Spot — buy or sell %s' % name, 'sub': 'Swap your coins for real %s in your wallet' % name, 'ask': 'buy %s' % name})
            return {'text': 'Which market do you want to trade %s on?' % name, 'widget': {'type': 'choose', 'options': opts}}
    if POS_RE.match(msg):
        if not address:
            return {'text': K()['no_wallet']}
        r = lend_cli('perp-positions', address)
        if not r.get('has'):
            return {'text': 'You have no Aftermath perps account yet. Say "perps" to open one.'}
        return {'text': 'Your Aftermath perps positions and orders.', 'widget': {'type': 'perppos', **r}}
    tm = TRADE_RE.match(msg)
    if tm and chart_symbol(tm.group(2)):
        sym = chart_symbol(tm.group(2))
        img = next((r['image'] for r in perp_market_list() if r['symbol'] == sym), None)
        side = 0 if tm.group(1).lower() in ('long', 'buy') else 1
        usd, lev = float(tm.group(3) or 0), float(tm.group(4) or 0)
        mx = next((r.get('max_lev') for r in perp_market_list() if r['symbol'] == sym), None) or 10
        lev, note = lev_check(msg, sym, lev)
        if lev is None:
            return note
        # "with 0.5 usdc" / "for 1 USDC" = the money put in (margin); "$10" = the order size
        if usd and re.search(r'\b(?:with|for)\s+\$?\s*' + re.escape(tm.group(3)) + r'\s*usdc', msg, re.I):
            lv = lev or mx
            usd, lev = round(usd * 0.97 / (1.05 / lv + 0.0009), 2), lv
            note = ' %s USDC in at %gx ≈ $%s order.' % (tm.group(3), lev, usd)
        if not usd and lev and address and not re.search(r'\ball\b', msg, re.I) and float(perp_account(address, fresh=True).get('collateral') or 0) > 0:
            # a leverage but no size: all the free perps margin at that leverage (the owner's rule: all margin unless told, 2026-10-23)
            return widget_reply('%s %s %gx all margin' % ('short' if side else 'long', sym, lev), address, history)
        if not usd:                                     # no size given: ask how big (and offer the market view), no chart
            sd, lx = ('short' if side else 'long'), (' %gx' % lev if lev else '')
            return {'text': 'How big should the %s %s be?' % (sym, sd),
                    'widget': {'type': 'choose', 'options': [
                        {'label': '$5 order', 'sub': '%s %s%s' % (sd, sym, lx), 'ask': '%s %s $5%s' % (sd, sym, lx)},
                        {'label': '$10 order', 'sub': '%s %s%s' % (sd, sym, lx), 'ask': '%s %s $10%s' % (sd, sym, lx)},
                        {'label': 'All my perps USDC', 'sub': 'biggest order your perps balance allows', 'ask': '%s %s for max usdc%s' % (sd, sym, lx)},
                        {'label': 'See the %s market first' % sym, 'sub': 'chart, order book and trades', 'ask': '%s market' % sym}]}}
        cw = {'type': 'chart', 'symbol': sym, 'tf': '15m', 'image': img, 'trade': {'open': True, 'side': side, 'usd': usd, 'lev': lev}}
        cf = perp_confirm(address, cw)
        if cf is not cw and (cf.get('deposit') or 0) > 0.000001:
            return deposit_ask(address, cf['deposit'])
        if cf is not cw:
            return {'text': 'Check your %s order and confirm in your wallet.%s' % (sym, note), 'widget': cf}
        return {'text': 'Here is %s — check the order below and sign when ready.%s' % (sym, note), 'widget': cw}
    if PERP_MARKETS_RE.match(msg):
        rows = perp_market_list()
        return {'text': 'Aftermath perps markets right now. Tap one for its live chart.',
                'widget': {'type': 'perpmarkets', 'rows': rows}}
    cm = CHART_RE.match(msg)
    if cm:
        sym = chart_symbol(cm.group(1) or cm.group(2))
        if not sym:
            return {'text': "I don't have a chart for that. Try BTC, ETH, SUI, SOL, gold, AAPL, TSLA, NVDA or US500."}
        img = None
        try:
            img = next((r['image'] for r in perp_market_list() if r['symbol'] == sym), None)
        except Exception:
            pass
        return {'text': 'Live %s / USD chart (Aftermath perps price).' % sym, 'widget': {'type': 'chart', 'symbol': sym, 'tf': '15m', 'image': img}}
    pw = perp_widget(msg, address)
    if pw is not None:
        return pw
    """Buttons-and-slider answers for lending; built from live data, no AI needed."""
    if re.match(r'^\s*(?:about\s+)?volo(?:\s+vaults?)?\s*[?.!]*\s*$', msg, re.I):
        return {'text': 'Volo vaults (NAVI\'s sister protocol — the BTC vaults and outside-curator vaults like Stable Curator #1) are shown '
                        'for their rates only for now. They work differently: a deposit is queued and your vault shares arrive later, '
                        'when Volo\'s operator processes it, and a withdrawal is queued the same way — not settled in one transaction '
                        'like everything else Kivy does. Kivy may add them later; for now you can use them in the Volo app.'}
    sl = SWAPLEND_RE.search(msg)                         # "swap USDC to USDsui then lend it on the best market"
    if sl:
        a, b = lend_token(sl.group(2)), lend_token(sl.group(3))
        if a in K()['assets'] and b and a != b:
            word = sl.group(1) or (re.search(r'\b(all|everything|max|half)\b', sl.group(4), re.I) or [None, None])[1]
            amt = None
            if word:
                amt = word_amount(word, a, balances(address)) if word.lower() in WORD_AMOUNTS else float(word)
            where = re.search(r'\b(?:on|in|at|with|into)\s+(?!it\b|them\b)(.+?)\s*[?.!]*\s*$', sl.group(5), re.I)
            return lend_card(b, where.group(1) if where else None, address, amt, src=a)
    lo = re.match(r'^\s*(?:lend|supply|deposit)\s+(?:([\d.]+|all|everything|max|half)\s+(?:of\s+)?)?(?:my\s+)?([A-Za-z]{2,10})(?:\s+(?:on|in|to|at)\s+((?:(?!\b(?:and|then|plus)\b|[,;&]).)+?))?\s*[?.!]*\s*$', msg, re.I)   # "…on navi and 0.2 on suilend": two steps, the chain reader's
    coin = lend_token(lo.group(2)) if lo and lo.group(2).upper() != 'USDC' else None
    if coin:                                             # any coin but USDC (whose cards are below, unchanged)
        a0 = lo.group(1)                                 # "lend all my sui on navi": the slider starts at that share (2026-10-25)
        amt = (spendable(coin, all_balances(address) or {}) * WORD_AMOUNTS[a0.lower()] if address else None) if a0 and a0.lower() in WORD_AMOUNTS \
            else float(a0) if a0 else None                 # the same spendable amount (gas kept back) the card's slider uses
        return lend_card(coin, lo.group(3), address, amt)
    mv = move_parse(msg)
    if mv and address:
        return move_card(mv, address)
    wto = None                                           # "withdraw USDsui as USDC" / "... and swap it to USDC"
    mto = re.search(r'\s+(?:as|to|into|(?:and\s+|then\s+|,\s*)*(?:swap|convert|change)(?:\s+\w+){0,3}?\s+(?:to|into))\s+([A-Za-z]{2,10})\s*[?.!]*\s*$', msg, re.I)
    if mto and re.match(r'^\s*(?:withdraw|redeem|take)\b', msg, re.I) and ckey(mto.group(1)) and not market_only(mto.group(1)):
        wto, msg = ckey(mto.group(1)), msg[:mto.start()]
    wc = WCOIN_RE.match(msg)
    if wc and wc.group(3) and CHAIN_SPLIT.search(wc.group(3)):
        wc = None                                    # "withdraw my ssui from suilend main, swap … then …": more steps follow — the chain reader's (2026-10-24)
    wtok = lend_token(wc.group(2)) if wc and wc.group(2).upper() != 'USDC' else None
    if wtok and address:                                 # "withdraw SUI (from NAVI)"
        mine = [r for r in other_lent(address, fresh=True) if r['coin'].lower() == wtok.lower()]
        fam = re.match(r'^\s*(suilend|navi)\s*[?.!]*\s*$', wc.group(3) or '', re.I)
        if fam:                                          # "from suilend" = any Suilend market (Main, Ember, Matrixdock)
            mine = [r for r in mine if r['id'].split('-')[0] == fam.group(1).lower()]
        elif wc.group(3):
            want = lend_pick([{'market': r['id'], 'name': r['name'], 'total_apr': r['total']} for r in mine], wc.group(3), wtok)
            mine = [r for r in mine if want and r['id'] == want['market']]
        if not mine:
            return {'text': 'You have no %s lent%s through Kivy.' % (disp(wtok), (' on ' + wc.group(3)) if wc.group(3) else '')}
        if len(mine) == 1 and wto and wto not in to_opts(wtok):   # "withdraw vSUI as haSUI": the card can't, a chain can (2026-10-21)
            return chain_parse('withdraw %s %s from %s, swap it to %s, then keep it in my wallet' % (wc.group(1) or 'all', wtok, MKT_EXACT.get(mine[0]['id'], mine[0]['name']), wto), address)
        if len(mine) == 1:
            return wd_preset(withdraw_card(mine[0], wto), msg)
        return {'text': 'You have %s in %d markets. Which one?' % (disp(wtok), len(mine)), 'widget': {'type': 'positions', 'rows': mine}}
    # "which lending market is safest?", "what's the difference between the markets?": a question, not a request for the
    # table — the AI explains (collateral = risk) and shows the table itself (2026-10-23)
    if LEND_ASK_RE.search(msg) and lend_ctx(msg, history):
        try:
            rr = lend_risk_reply(msg, address, history)
        except Exception:
            rr = None
        if rr:
            return rr
    if MARKETS_RE.search(msg) and not LEND_ASK_RE.search(msg):
        rates = lend_rates()
        pos = lend_positions(address) if address else {}
        rows = []
        for p in sorted([p for p in PROTOS if p in rates], key=lambda p: -float((rates.get(p) or {}).get('total_apr') or 0)):
            r = rates.get(p) or {}
            ps = pos.get(p) or {}
            lent = float(ps.get('usdc') or 0) + float(ps.get('in_app') or 0)
            rows.append({'id': p, 'name': PROTOS[p], 'kind': 'Vault' if p in VAULTS else 'Lending', 'collat': collat_row(p),
                         'total': round(float(r.get('total_apr') or 0), 2),
                         'lent': round(lent, 6) if lent >= 0.01 else 0})
        try:                                               # every coin, with an asset filter (2026-10-19)
            order = ['USDC', 'SUI'] + [a for a in K()['assets'] if a not in ('USDC', 'SUI')]
            allr = sorted(lend_rates_all(), key=lambda r: (order.index(r['asset']) if r['asset'] in order else 99, -float(r['total_apr'])))
            seen, best_done, rows2 = set(), set(), []
            nt = lambda t: re.sub(r'^0x0*', '0x', str(t or '').lower())
            # matched by coin TYPE, not name: NAVI lists both "WBTC" and "wBTC" (different coins)
            others = {(x['id'], nt(x.get('type'))): x['lent'] for x in other_lent(address)} if address else {}
            for r in allr:
                usdc = r['asset'] == 'USDC' and not r.get('token')    # real USDC keeps its own cards
                tokn = {'USDSUI': 'USDsui'}.get(r.get('token'), r.get('token')) or r['asset']
                lent = next((x['lent'] for x in rows if x['id'] == r['market']), 0) if usdc else others.get((r['market'], nt(r.get('type'))), 0)
                nm = LEND_MKTS.get(r['market'], r.get('name') or r['market'])
                rows2.append({'id': r['market'], 'name': r.get('name') if r['market'] in SUI_VAULT_MKTS else nm, 'asset': r['asset'], 'icon': r.get('icon'),
                              'proto': r.get('protocol'), 'rate_only': bool(r.get('rate_only')), 'px': coin_px(tokn) or coin_px(r['asset']) or 0,
                              'token': {'USDSUI': 'USDsui'}.get(r.get('token'), r.get('token')) or {'BTC': 'xBTC', 'USDC': 'USDC'}.get(r['asset']),   # base coin named too; one spelling of USDsui
                              'total': round(float(r['total_apr']), 2), 'lent': lent, 'best': not r.get('rate_only') and r['asset'] not in best_done,
                              'collat': None if r.get('rate_only') else collat_row(r['market']),
                              'ask': 'about volo vaults' if r.get('rate_only') else nm if usdc else 'lend %s on %s' % (tokn, nm)})
                seen.add(r['asset'])
                if not r.get('rate_only'):
                    best_done.add(r['asset'])          # green = the best market Kivy can lend into
            if rows2:
                said = [ckey(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', msg) if ckey(w)]   # "lending markets for vSUI": open on its tab (2026-10-21)
                cur = next((a for a in seen for c in said if a.lower() == str(c).lower()), None)
                # a lent coin that lives under another tab ("lending markets for USDsui" = the Stable tab): open that tab with
                # the coin's rows in focus — the wallet tab's lent rows ask this (2026-10-24)
                toks = {str(r['token']).lower(): r['asset'] for r in rows2 if r.get('token')}
                focus = next((str(c) for c in said if str(c).lower() in toks), None) or next((a for a in seen for c in said if a.lower() == str(c).lower()), None)
                cur = cur or (toks[focus.lower()] if focus else None)
                focus = {'BTC': 'xBTC'}.get(focus, focus)       # the table names the base BTC rows xBTC
                return {'text': ('Here are the %s lending markets right now. Tap a market to lend.' % disp(focus or cur)) if cur else
                                'Here are the lending markets right now. Pick an asset; tap a market to lend.',
                        'widget': {'type': 'markets', 'coin': cur or 'USDC', 'assets': [a for a in order if a in seen], 'rows': rows2,
                                   'focus': focus}}
        except Exception:
            pass                                           # fall back to the USDC table below
        return {'text': 'Here are the USDC lending markets right now. Tap one to lend.',
                'widget': {'type': 'markets', 'coin': 'USDC', 'rows': rows}}
    if not WITHDRAW_FROM_RE.match(msg) and market_in(msg) and len(msg.split()) <= 7:
        last = next((str(h.get('text') or '') for h in reversed(history or []) if isinstance(h, dict) and h.get('role') == 'model'), '')
        if re.search(r'withdraw|no USDC in|no USDC lent', last, re.I):
            msg = 'withdraw from ' + re.sub(r'^\s*(?:no,?\s*)?(?:i\s+mean[t]?\s+|i\s+meant\s+)?', '', msg, flags=re.I)
    wm = None if WITHDRAW_RE.match(msg) else WITHDRAW_FROM_RE.match(msg)   # "withdraw from lending" is not a market name
    if WITHDRAW_RE.match(msg) or wm:
        if not address:
            return {'text': K()['no_wallet']}
        pos = lend_positions(address, fresh=True)
        # every coin lent, as one list (2026-10-20: "withdraw from suilend" missed his USDsui — it only looked at USDC)
        allpos = [{'id': p, 'name': PROTOS[p], 'total': round(apr_of(p) or 0, 2), 'lent': round(v, 6), 'coin': 'USDC', 'px': 1.0,
                   'icon': '/icons/USDC.svg'}
                  for p, v in ((p, float((pos.get(p) or {}).get('usdc') or 0)) for p in PROTOS) if v >= 0.01]   # dust doesn't count
        allpos += other_lent(address, fresh=True)
        allpos.sort(key=lambda r: -r['lent'] * (r.get('px') or 0))
        target = market_only(wm.group(1)) if wm else None
        if wm and not target:
            return None                                  # not a market name: let the normal reader handle it
        fam, where = None, None
        if wm and target in ('suilend', 'navi'):
            t = wm.group(1).lower()
            if not re.search(r'\bmain\b', t):
                fam = target                             # "suilend" alone = Main + Ember + Matrixdock; "navi" = NAVI + its vaults
        if fam:
            mine = [r for r in allpos if r['id'].split('-')[0] == fam]
            where = 'Suilend' if fam == 'suilend' else 'NAVI'
        elif target:
            mine = [r for r in allpos if r['id'] == target]
            where = PROTOS.get(target, target)
        else:
            mine = allpos
        if re.search(r'(?<!to\s)(?<!into\s)(?<!as\s)(?<!for\s)\busdc\b', msg, re.I):   # the coin was named: only that coin (2026-10-22); "cash out to usdc" names what you get
            mine = [r for r in mine if r.get('coin') == 'USDC']
        if not mine:
            if fam:
                names = ', '.join(('Main' if LEND_MKTS[p] == where else LEND_MKTS[p].replace(where + ' ', '').replace('Main Market', 'Main'))
                                  for p in LEND_MKTS if p.split('-')[0] == fam)
                return {'text': 'You have nothing lent on %s through Kivy (checked %s).' % (where, names)}
            if where:
                return {'text': 'You have nothing lent on %s through Kivy.' % where}
            if allpos:                                   # "withdraw my usdc" with only sSUI lent: say what IS there, one tap each
                return {'text': 'You have no USDC lent. This is what you have lent:',
                        'widget': {'type': 'choose', 'options': [{'label': '%s on %s' % (disp(r['coin']), r['name']), 'sub': '%s %s' % (dec(r['lent'], 6), disp(r['coin'])),
                                                                  'ask': 'withdraw my %s from %s' % (r['coin'], r['name'])} for r in allpos[:6]]}}
            return {'text': 'You have nothing lent right now. Say "lend" to see the markets.'}
        if len(mine) == 1:                               # one position: straight to its slider
            return wd_preset(withdraw_card(mine[0], wto), msg)
        return {'text': ('You have %d positions on %s. Tap one to withdraw.' % (len(mine), where)) if where
                        else 'Here is what you have lent. Tap one to withdraw.',
                'widget': {'type': 'positions', 'rows': mine}}
    cp = CLAIM_PROTO_RE.search(msg)
    if cp:                                            # "claim my suilend rewards": only that protocol's markets (2026-10-25)
        if not address:
            return {'text': K()['no_wallet']}
        return claim_card(address, 'p-' + (cp.group(1) or cp.group(2)).lower())
    if CLAIM_RE.search(msg) or REWARD_Q_RE.search(msg):   # "how much rewards do i have", "is it worth claiming": the card has the numbers
        if not address:
            return {'text': K()['no_wallet']}
        return claim_card(address)
    p = market_only(msg)
    if p:
        if not address:
            return {'text': K()['no_wallet']}
        have = float((balances(address) or {}).get('USDC') or 0)
        cap = ui_cap()
        mx = round(min(have, cap), 6)
        aw = re.search(r'\b(all|everything|max|half|\d+(?:\.\d+)?)\b', msg, re.I)    # the amount typed: "lend all usdc on …", "lend 0.5 usdc …"
        pre = 0
        if aw:
            w0 = aw.group(1).lower()
            pre = mx if w0 in ('all', 'everything', 'max') else mx / 2 if w0 == 'half' else min(float(w0), mx)
        return {'text': 'Slide to choose how much USDC to lend on %s.' % PROTOS[p],
                'widget': {'type': 'lend', 'protocol': p, 'name': PROTOS[p], 'apr': round(apr_of(p) or 0, 2), 'lend_fee_pct': rev_lend_pct(),
                           'balance': have, 'max': mx, 'cap': cap, 'preset': round(pre, 6)}}
    return None


def tx_labels(digests, owner=None):
    """{digest: what Kivy called it} from Kivy's own transaction log. Public: the transactions are public on chain and every
    Kivy transaction pays Kivy's fee on chain, so these labels reveal nothing new (2026-10-22). owner: only that sender's."""
    out = {}
    if not digests:
        return out
    me = '%s…%s' % (owner[:6], owner[-4:]) if owner else None
    try:
        text = Path(KIVY_DIARY).read_text()
        for m in re.finditer(r'^- \*\*[^*]+\*\* — (\S+) · ([^·\n]+?) · .*?/txblock/([1-9A-HJ-NP-Za-km-z]{32,44})\)', text, re.M):
            if m.group(3) in digests and (me is None or m.group(1).lower() == me.lower()) and m.group(2).strip() != 'transaction':
                out[m.group(3)] = m.group(2).strip() + (' ⚠️' if 'check ⚠️' in m.group(0) else '')   # 'transaction' = no Kivy plan: the chain's own title stays
    except Exception:
        pass
    return out


def nice_label(s):
    """'swaplend Suilend Ember USDC→USDsui' -> 'Swap & lend · Ember USDC→USDsui' (same wording as the Activity tab)."""
    for a, b in (('swaplend', 'Swap & lend ·'), ('withdrawswap', 'Withdraw & swap ·'), ('move', 'Move ·'), ('chain', 'Steps ·'), ('lend deposit', 'Lend ·'),
                 ('lend withdraw', 'Withdraw ·'), ('lend claim', 'Claim rewards')):
        if s.lower().startswith(a):
            s = b + s[len(a):]
            break
    s = re.sub(r'\s*\(backfilled\)', '', s, flags=re.I)
    s = re.sub(r'\b(Suilend|NAVI)\s+(?=\S)', '', s).replace('Main Market', 'Main')
    return s[:1].upper() + s[1:]


_MEM = {}


def wallet_memory(address):
    """What Kivy remembers about a wallet — rebuilt from public data each time, never stored (2026-10-20):
    its recent actions through Kivy (chain + Kivy's transaction log) and what it has lent right now. Cached 60 s."""
    hit = _MEM.get(address)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    acts, lent = [], []
    try:
        d = gql('query($a: SuiAddress!) { transactions(last: 30, filter: {affectedAddress: $a}) { nodes { digest effects { status timestamp } } } }',
                {'a': address})
        nodes = list(reversed(((d.get('transactions') or {}).get('nodes')) or []))
        L = tx_labels({n['digest'] for n in nodes}, owner=address)   # a Kivy tx someone else sent you is not yours (audit nv #8)
        for n in nodes:
            if n['digest'] in L and len(acts) < 6:
                e = n.get('effects') or {}
                acts.append({'what': nice_label(L[n['digest']]), 'when': str(e.get('timestamp') or '')[:16].replace('T', ' ') + ' UTC',
                             'ok': e.get('status') == 'SUCCESS'})
    except Exception:
        pass
    try:
        pos = lend_positions(address)
        for p in PROTOS:
            v = float((pos.get(p) or {}).get('usdc') or 0)
            if v >= 0.01:
                lent.append({'coin': 'USDC', 'amount': v, 'usd': v, 'market': PROTOS[p], 'apr': apr_of(p) or 0})
        for x in other_lent(address):
            lent.append({'coin': x['coin'], 'amount': x['lent'], 'usd': x['lent'] * x['px'], 'market': x['name'], 'apr': x['total']})
    except Exception:
        pass
    lent.sort(key=lambda x: -x['usd'])
    held = []
    try:
        for k, v in (all_balances(address) or {}).items():
            if float(v or 0) > 0 and float(v) * float(coin_px(k) or 0) >= 0.02:
                held.append((k, float(v)))
    except Exception:
        pass
    m = {'actions': acts, 'lent': lent, 'held': held}
    _MEM[address] = (time.time(), m)
    return m


def memory_text(m):
    """The memory as lines for the AI's prompt."""
    out = []
    if m.get('actions'):
        out.append('- Their recent actions through Kivy (newest first): ' + '; '.join(
            '%s %s%s' % (a['when'], a['what'], '' if a['ok'] else ' (failed)') for a in m['actions']))
    if m.get('held'):
        out.append('- In their wallet: ' + ', '.join('%s %s' % (dec(v), k) for k, v in m['held']))
    if m.get('lent'):
        out.append('- Lent right now: ' + '; '.join('%s %s on %s at %.2f%% a year' % (dec(x['amount']), x['coin'], x['market'], x['apr'])
                                                    for x in m['lent']))
    return '\n'.join(out)


def welcome_line(m):
    """Home screen, wallet connected: one short line from the memory (empty for a new wallet)."""
    bits = []
    if m.get('actions'):
        a = m['actions'][0]
        try:
            day = time.strftime('%b %d', time.strptime(a['when'][:10], '%Y-%m-%d')).replace(' 0', ' ')
        except ValueError:
            day = a['when'][:10]
        bits.append('Last time: %s (%s).' % (a['what'].replace(' ⚠️', '').strip(), day))   # no warning marks on the greeting
    if m.get('lent'):
        x = m['lent'][0]
        bits.append('Your %s %s on %s earns %.2f%% a year now.' % (dec(x['amount']), x['coin'], x['market'], x['apr']))
    return ('Welcome back. ' + ' '.join(bits)) if bits else ''


# ── What does the user mean? job -> coin -> market; ask when more than one fits (2026-10-20) ──
# Runs after the exact command patterns and before the AI. Typos are forgiven ("witdraw", "usdsiu"), and a message
# that names a coin but no job (or two jobs) gets tappable choices built from the user's own wallet.
INTENT_VERBS = {
    'swap': ['swap', 'convert', 'exchange', 'change', 'buy', 'sell', 'ape'],
    'lend': ['lend', 'supply', 'deposit', 'earn', 'yield', 'park', 'stash'],
    'withdraw': ['withdraw', 'redeem', 'unlend'],
    'send': ['send', 'transfer', 'pay'],
    'claim': ['claim', 'harvest', 'rewards'],
    'perps': ['long', 'short', 'leverage', 'perps', 'perp', 'futures'],
    'price': ['price', 'chart'],
    'balance': ['balance', 'holdings', 'portfolio'],
}
VERB_OF = {w: t for t, ws in INTENT_VERBS.items() for w in ws}
COIN_NAMES = {'bitcoin': 'BTC', 'btc': 'BTC', 'ethereum': 'ETH', 'ether': 'ETH', 'solana': 'SOL', 'walrus': 'WAL', 'deepbook': 'DEEP',
              'usd': 'USDC', 'dollar': 'USDC', 'dollars': 'USDC', 'stablecoin': 'USDC'}
QUESTION_RE = re.compile(r"^\s*(?:what\s+(?:does|is|are|do|would|will|fees?)|what's|whats|how\s+(?:does|do|is|much|many|long)|why|explain|meaning\s+of)\b|\bmean\??\s*$"
                         r"|^\s*(?:do|does|am|is|are|can|could|should|would|will|who|when|which)\s+\w+.*\?\s*$", re.I)
AMOUNT_WORD_RE = re.compile(r'\b(\d+(?:\.\d+)?|all|half|max|everything)\b', re.I)
INTENT_FORMS = set('lent lended lending swapped swapping sent sending withdrew withdrawn withdrawing claimed claiming deposited '
                   'depositing bought selling sold earned earning paid parked'.split())
# everyday words that must never be "corrected" into a coin ('keep' is one letter from DEEP — 2026-10-20)
INTENT_FORMS |= set('keep leave hold take use put move shift pull push give rest half margin collateral aftermath perps perp wallet '
                    'proceeds remaining remainder everything whole other plus minus then after before into onto from with without '
                    'market markets account position split part parts each both first second rewards balance chart price pool '
                    'safe best good high yield prime main ember navi suilend some most less more liquid deposit there here '
                    'whale whales close cancel flip profit loss stop entry exposure leverage leveraged start started bot bots trading '
                    'park those these them send keep fund funds money asset assets crypto holdings whole entire '
                    'well along including plus together remaining remain leftover'.split())
INTENT_STOP = set('up so it as to in on of or and my me the an is be do you all max half some any what about how please pls then '
                  'with for from into at can want wanna would like need now this that coin coins token tokens money out back take '
                  'put get much many more less best yes no ok okay hi hello thanks thank sure maybe just also too'.split())


ENGLISH_ONLY = ('Kivy understands English only for now. Please write your request in English — for example '
                '"lend 10 USDC", "swap 5 SUI to USDC" or "withdraw my USDsui".')


def not_english(msg):
    """True when the message has letters from another script (Thai, Chinese, Cyrillic…). Kivy reads English only
    (2026-10-20) and says so instead of guessing. Emoji, numbers and symbols are fine."""
    import unicodedata
    for ch in msg:
        if ch.isalpha() and ord(ch) > 0x24F:
            try:
                if 'LATIN' not in unicodedata.name(ch):
                    return True
            except ValueError:
                return True
    # Latin-script languages too ("quanto ho nel portafoglio?" got a typo suggestion, 2026-10-23): most real words not English
    try:
        bare = re.sub(r'0x[0-9a-f]+|\b[a-z0-9]{20,}\b|@[\w.-]+|\b[\w-]+\.sui\b', ' ', msg.lower())   # addresses and names aren't language
        words = [w for w in re.findall(r"[a-z]{3,}", bare) if not ckey(w) and not chart_symbol(w)]
        known = ENGLISH | {'kivy', 'usdc', 'navi', 'suilend', 'perps', 'perp', 'lend', 'lending', 'swap', 'apy', 'apr', 'defi', 'crypto',
                           'pls', 'plz', 'thx', 'ape', 'lol', 'hey', 'gm', 'yo', 'wanna', 'gonna', 'ok', 'okay',
                           'ember', 'matrixdock', 'aftermath', 'vault', 'vaults', 'collateral', 'usdsui', 'perpetual', 'perpetuals'}
        foreign = [w for w in words if w not in known and not near_english(w)]
        return len(foreign) >= 3 and len(foreign) * 10 >= len(words) * 6
    except Exception:
        return False


def other_unknown(msg):
    """Words Kivy neither knows nor can read as a typo — then a "did you mean" would be a guess at a foreign sentence
    ("quanto ho nel portafoglio?" -> "portfolio?", 2026-10-23): let the AI answer instead (it replies in English)."""
    try:
        fixed = {f[0].lower() for f in (intent_parse(msg).get('fixes') or [])}
        bare = re.sub(r'0x[0-9a-f]+|\b[a-z0-9]{20,}\b|@[\w.-]+|\b[\w-]+\.sui\b', ' ', msg.lower())
        import difflib
        wallets = ['slush', 'vera', 'nightly', 'phantom', 'suiet', 'surf', 'okx', 'bitget', 'martian', 'ethos', 'backpack', 'wallet']   # wallet names, typos too ("slsuh")
        return [w for w in re.findall(r'[a-z]{3,}', bare) if w not in fixed and w not in ENGLISH and not ckey(w) and not chart_symbol(w)
                and not near_english(w) and w not in {'kivy', 'navi', 'suilend', 'perps', 'ember', 'matrixdock', 'aftermath', 'usdsui', 'pls', 'plz', 'ape', 'apy', 'apr'}
                and not difflib.get_close_matches(w, wallets, n=1, cutoff=0.6)]
    except Exception:
        return []


def near_english(w, cutoff=0.8):
    """A word one typo away from a common English word ('swpa', 'balanse') — still English."""
    if len(w) < 4:
        return False
    import difflib
    return bool(difflib.get_close_matches(w, ENGLISH_LONG.get(w[0], ()), n=1, cutoff=cutoff))


try:                                                  # 10,000 common English words (google-10000-english, no swears): never "fixed" as typos
    ENGLISH = set(Path(__file__).resolve().parent.joinpath('words_en.txt').read_text().split())
    ENGLISH_LONG = {}
    for _w in ENGLISH:
        if len(_w) >= 4:
            ENGLISH_LONG.setdefault(_w[0], []).append(_w)
except Exception:
    ENGLISH = set()
    ENGLISH_LONG = {}
NEVER_FIX = {'chats', 'chat', 'usdt', 'twap', 'tell', 'their', 'year', 'great', 'buggy', 'fetch', 'fetcher', 'headings', 'learns', 'shortcut', 'pepe', 'rebalance', 'rebalancing', 'rebalanced', 'equalize', 'equalise', 'consolidate', 'staked', 'staking', 'unstake', 'tidy', 'leftovers', 'dust', 'safest', 'collateral', 'everywhere', 'gas'}   # + the new-situation words, 2026-10-24 ('rebalance' became 'balance')   # USDT is another coin, TWAP an order type (2026-10-22 scan)


def stem_tool(lw):
    """'balances' -> balance, 'deposits' -> deposit, 'shorting' -> short, 'withdrawal' -> withdraw: an ending, not a typo."""
    for end in ('ing', 'es', 'ed', 'al', 's'):
        if lw.endswith(end) and len(lw) - len(end) >= 3:
            base = lw[:-len(end)]
            for b in (base, base + 'e'):
                if b in VERB_OF:
                    return VERB_OF[b]
    return None


def intent_parse(msg):
    """{'tools', 'coins', 'fixes'} — the jobs and coins a message names, typos corrected (fixes = [(typed, meant)])."""
    tools, coins, fixes = [], [], []
    for w in re.findall(r'[A-Za-z][A-Za-z0-9.]*', msg):
        w = w.rstrip('.')                            # "…to vSUI." is not a typo (2026-10-21)
        lw = w.lower()
        if lw in COIN_NAMES:
            coins.append(COIN_NAMES[lw]); continue
        if lw in VERB_OF:
            tools.append(VERB_OF[lw]); continue
        if lw in INTENT_STOP or lw in INTENT_FORMS or market_only(w):
            continue
        k = ckey(w) if (len(lw) >= 3 or w.isupper()) else None
        if k:
            coins.append(k); continue
        st = stem_tool(lw)
        if st:                                       # an ending of a job word: understood, no "did you mean"
            tools.append(st); continue
        if lw in ENGLISH or lw in NEVER_FIX or chart_symbol(w):   # a real word or a real market is not a typo (tell -> sell, chat -> chart…)
            continue
        if len(lw) >= 4:                             # a typo of a job or a coin?
            v = difflib.get_close_matches(lw, list(VERB_OF), n=1, cutoff=0.74)   # 'swpa' -> swap
            if v:
                tools.append(VERB_OF[v[0]]); fixes.append((w, v[0])); continue
            nm = difflib.get_close_matches(lw, list(COIN_NAMES), n=1, cutoff=0.8)       # 'etherium' -> ETH
            if nm:
                coins.append(COIN_NAMES[nm[0]]); fixes.append((w, COIN_NAMES[nm[0]])); continue
            names = {x.lower(): x for x in ecoins()}
            c = difflib.get_close_matches(lw, list(names), n=1, cutoff=0.75)            # 'vsiu' -> vSUI, 'wbct' -> wBTC
            if c:
                coins.append(names[c[0]]); fixes.append((w, names[c[0]])); continue
            vn = difflib.get_close_matches(lw, ['aftermath', 'suilend', 'navi', 'perps', 'margin', 'matrixdock', 'ember', 'collateral'], n=1, cutoff=0.78)
            if vn:                                   # 'aftremath' -> aftermath, 'perpz' -> perps
                fixes.append((w, vn[0]))
    if re.search(r'\b(?:take|pull|get|cash|move)\b(?:\s+\w+){0,3}\s+out\b|\b(?:take|get|bring|want)\s+(?:\w+\s+){0,2}back\b', msg, re.I):
        tools.insert(0, 'withdraw')                  # "take my USDsui out", "pull it out", "take it back"
    dedup = lambda xs: [x for i, x in enumerate(xs) if x not in xs[:i]]
    return {'tools': dedup(tools), 'coins': dedup(coins), 'fixes': fixes}


def intent_option(tool, c, address):
    """One tappable choice: (label, sub, message), or None when it doesn't apply to this coin / wallet."""
    hold = (all_balances(address) or {}) if address else {}
    held = float(hold.get(c) or 0) > 0
    if tool == 'lend':
        rows = lend_rows_for(c) if c else []
        if c and not rows:
            return None
        return ('Lend %s' % disp(c) if c else 'Lend', ('Best now: %s · %.2f%% a year' % (LEND_MKTS.get(rows[0]['market'], rows[0]['name']), float(rows[0]['total_apr'])))
                if rows else 'See every lending market', 'lend %s' % c if c else 'lending markets')
    if tool == 'withdraw':
        mine = [r for r in other_lent(address) if r['coin'] == c] if (address and c and c != 'USDC') else []
        if c == 'USDC' and address:
            mine = [1 for p, v in (lend_positions(address) or {}).items() if float((v or {}).get('usdc') or 0) >= 0.01]
        if c and not mine:
            return None
        return ('Withdraw your %s' % disp(c) if c else 'Withdraw', 'From lending back to your wallet', 'withdraw %s' % c if c else 'withdraw')
    if tool == 'swap':
        if not c:
            return ('Swap', 'Trade one coin for another', 'swap')
        return (('Swap %s to another coin' % disp(c), 'You hold %s' % dec(float(hold.get(c) or 0)), 'swap %s to %s' % (c, 'USDC' if c != 'USDC' else 'SUI'))
                if held else ('Buy %s' % disp(c), 'Swap another coin into it', 'swap %s to %s' % ('USDC' if c != 'USDC' else 'SUI', c)))
    if tool == 'perps':
        sym = chart_symbol(c) if c else None
        return ('Long or short %s' % sym, 'Perps with leverage', 'open %s perps' % sym) if sym else (None if c else ('Perps', 'Long or short with leverage', 'perps'))
    if tool == 'price':
        sym = chart_symbol(c) if c else None
        return ('%s price & chart' % sym, 'Live chart', 'chart %s' % sym) if sym else None
    if tool == 'send':
        return ('Send %s' % disp(c), 'To an address or a .sui name', 'send %s' % c) if (c and held) else None
    if tool == 'claim':
        return ('Claim lending rewards', 'All markets in one go', 'claim rewards')
    if tool == 'balance':
        return ('Wallet balance', 'Every coin, lent and in perps', 'wallet balance')
    return None


def best_lend(msg, address, history=None, p=None):
    """"deposit all USDC in to suilend best yield market": the lend card on that best market, amount kept (2026-10-20)."""
    p = p or intent_parse(msg)
    bm = re.search(r'\b(lend|deposit|supply|put|park)\b(.*?)\b(?:in\s*to|into|on|in|at)\s+(?:the\s+)?(?:(suilend|navi)(?:\'?s)?\s+)?(?:best|highest|top)\b(?:\s+(?:yield|rate|apy|apr))?(?:\s+(suilend|navi))?', msg, re.I)
    if bm and re.search(r'\b(?:withdr[ae]w\w*|swap\w*|convert\w*|move|take\s+out|pull|sell|close)\b', msg[:bm.start(1)], re.I):
        return None                                  # "withdraw from aftermath then deposit in suilend best": a chain (2026-10-21)
    if bm and len(p['coins']) == 1 and not msg.strip().endswith('?'):   # "deposit all USDC in to suilend best yield market": do it there
        fam = bm.group(3) or bm.group(4) or ''
        rows = [r for r in lend_rows_for(p['coins'][0]) if not r.get('rate_only') and (not fam or r['market'].startswith(fam.lower()))]
        if rows:
            best = max(rows, key=lambda r: float(r['total_apr']))
            amt = re.search(r'\b(all|everything|max|half|\d+(?:\.\d+)?)\b', bm.group(2), re.I)
            ask = 'lend %s%s on %s' % ((amt.group(1) + ' ') if amt else '', p['coins'][0], LEND_MKTS.get(best['market'], best['name']))
            r = widget_reply(ask, address, history) or intent_ask(ask, address, history)
            if r:
                r['text'] = '%s pays the most%s right now (%.2f%% a year). ' % (LEND_MKTS.get(best['market'], best['name']), (' on ' + fam.capitalize()) if fam else '',
                                                                              float(best['total_apr'])) + r.get('text', '')
                return r
    return None


def intent_ask(msg, address, history=None):
    """A clarifying question with choices, or None when the message is clear (or not about Kivy's jobs)."""
    if intent_parse(msg)['fixes'] and other_unknown(msg):
        return None                                      # a foreign sentence: no typo guess (2026-10-23)
    if len(msg) > 120:
        return None
    p = intent_parse(msg)
    if re.search(r"\b(?:don'?t|do\s+not|never|stop)\b", msg, re.I):
        return None                                  # "don't swap…, just explain": never act on a no — the AI answers
    if QUESTION_RE.search(msg) and not (p['tools'] == ['price'] and len(p['coins']) == 1):
        return None                                  # a question ("what does lend mean?"): the AI explains; prices still chart
    bl = best_lend(msg, address, history, p)
    if bl:
        return bl
    if re.search(r'\bbest\s+(?:place|rate|yield|market|apy|apr)\b|\bwhere\s+(?:should|can|do)\s+i\s+(?:put|park|lend)\b', msg, re.I) and len(p['coins']) == 1 \
            and not re.search(r'\b(?:withdr[ae]w\w*|swap\w*|convert\w*|move|then|send)\b', msg, re.I):   # several steps: the chain reader (2026-10-21)
        rows = lend_rows_for(p['coins'][0])          # "best place for usdsui?": the lending table, on that coin's tab
        r = widget_reply('lending markets', address, history) if rows else None
        if r and (r.get('widget') or {}).get('type') == 'markets':
            r['widget']['coin'] = rows[0]['asset']
            r['text'] = 'Here is where %s earns the most right now (best first). Tap a market to lend.' % disp(p['coins'][0])
            return r
    if p['fixes']:                                    # a typo: show what we think they meant, one tap to run it
        fixed = msg
        for typed, meant in p['fixes']:
            fixed = re.sub(r'\b%s\b' % re.escape(typed), meant, fixed, count=1)
        try:
            xr = xw_parse(fixed, address, history) if not QUESTION_RE.search(msg) else None
            if xr:
                if (xr.get('widget') or {}).get('type') == 'xwallet':
                    xr['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + xr.get('text', '')
                return xr
        except Exception:
            pass
        try:
            ms = send_reply(fixed, address, history) if not QUESTION_RE.search(msg) else None
            if ms is None and SEND_VERB_RE.match(msg):
                ms = send_reply(msg, address, history)    # the "typo" was a word like "fund": read it as written
            if ms:
                if ms.get('plan') or (ms.get('widget') or {}).get('type') == 'choose':
                    ms['text'] = ('I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes'])) + ms.get('text', '') if send_reply(msg, address, history) is None else ms.get('text', '')
                return ms
        except Exception:
            ms = None
        try:
            ms = market_switch(fixed, address, history) if not QUESTION_RE.search(msg) else None
        except Exception:
            ms = None
        if ms:                                       # "switch to etherium": just open it (only opens a page, nothing to approve)
            ms['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + ms.get('text', '')
            return ms
        try:                                         # a typo inside a multi-step request: read it fixed — the card still asks for approval (2026-10-21)
            ip = inproto(fixed, address) if not QUESTION_RE.search(msg) else None
            ch = ip if isinstance(ip, dict) else chain_parse(ip or fixed, address) if not QUESTION_RE.search(msg) else None
        except Exception:
            ch = None
        if ch is not None:
            ch['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + (ch.get('text') or '')
            return ch
        try:                                         # a single step: the card it would open typed right, with the reading shown (2026-10-25)
            wr = widget_reply(fixed, address, history) if not QUESTION_RE.search(msg) and not any(t.lower() in REAL_COINS for t, _ in p['fixes']) else None   # "afSUI" is a real coin, not a typo for haSUI: ask
        except Exception:
            wr = None
        if wr and isinstance(wr.get('widget'), dict):
            wr['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + (wr.get('text') or '')
            return wr
        return {'text': 'Did you mean "%s"?' % fixed,
                'widget': {'type': 'choose', 'options': [{'label': fixed, 'sub': 'Tap to run it', 'ask': fixed}]}}
    tools, coins = p['tools'], p['coins']
    combo = re.search(r'\b(then|and then|after that)\b', msg, re.I)
    pron = re.search(r'\b(it|that|this|them|those)\b', msg, re.I)
    if pron and len(coins) < 2:
        return None                                  # "lend it", "swap that to usdc": the AI reads the chat to know what "it" is
    if not tools and len(coins) == 2 and len(msg.split()) <= 4:     # "SUI / USDC": which way?
        a, b = coins
        opts = [{'label': 'Swap %s → %s' % (disp(a), disp(b)), 'sub': 'Pay %s, get %s' % (disp(a), disp(b)), 'ask': 'swap %s to %s' % (a, b)},
                {'label': 'Swap %s → %s' % (disp(b), disp(a)), 'sub': 'Pay %s, get %s' % (disp(b), disp(a)), 'ask': 'swap %s to %s' % (b, a)}]
        o = intent_option('price', a, address)
        if o:
            opts.append({'label': o[0], 'sub': o[1], 'ask': o[2]})
        return {'text': 'Which way?', 'widget': {'type': 'choose', 'options': opts}}
    if tools == ['swap'] and len(coins) == 2 and not combo:          # loose swap ("อยาก swap sui to usdc", "swap half my sui to usdc")
        am = AMOUNT_WORD_RE.search(msg)
        cmd = 'swap %s%s to %s' % ((am.group(1) + ' ') if am else '', coins[0], coins[1])
        if cmd.lower() != msg.strip().lower():
            r = reply(cmd, address) if am else widget_reply(cmd, address, history)
            if r is not None and (not am or r.get('plan') or r.get('text')):
                return r
    if tools == ['swap'] and len(coins) == 1 and not combo and not re.search(r'\d', msg):   # "ape into btc" = buy it
        r = widget_reply(('trade %s' if re.search(r'\bape\b', msg, re.I) else 'buy %s') % coins[0], address, history)   # "ape": spot or perps?
        if r is not None:
            return r
    # clear job + at most one coin, said loosely ("i want to earn on my usdsui"): run it as the plain command
    plain = {'lend': 'lend %s', 'withdraw': 'withdraw %s', 'claim': 'claim rewards', 'balance': 'wallet balance', 'price': 'chart %s'}
    if len(tools) == 1 and len(coins) <= 1 and not combo and tools[0] in plain and (tools[0] == 'lend' or not re.search(r'\d', msg)):
        c = coins[0] if coins else ''
        if '%s' not in plain[tools[0]] or c or tools[0] in ('lend', 'withdraw'):
            cmd = (plain[tools[0]] % c if '%s' in plain[tools[0]] else plain[tools[0]]).strip()
            if tools[0] == 'lend' and not c:
                cmd = 'lending markets'
            if tools[0] == 'withdraw' and c and re.search(r'\b(?:to|into|as|for)\s+%s\b' % re.escape(c), msg, re.I):
                cmd = 'withdraw to %s' % c           # "cash out to usdc": USDC is what you get, not the coin to look for
            mw = re.search(r'\b((?:suilend|navi)(?:\s+(?:main|ember|matrix\w*|high\s*yield|prime))?|ember|matrix\w*|high\s*yield|prime)\b', msg, re.I) \
                if tools[0] in ('lend', 'withdraw') and c else None
            am = AMOUNT_WORD_RE.search(msg) if tools[0] == 'lend' else None
            if am and c:
                cmd = 'lend %s %s' % (am.group(1), c)
            if mw and c == 'USDC' and tools[0] == 'lend':
                cmd = mw.group(1)                    # USDC's own card: just the market name
            elif mw:
                cmd += (' on %s' if tools[0] == 'lend' else ' from %s') % mw.group(1)
            if cmd.lower() != msg.strip().lower():
                r = widget_reply(cmd, address, history)
                w = (r or {}).get('widget') or {}
                if w.get('type') == 'withdraw':
                    wd_preset(r, msg)                    # "withdraw half my usdc from navi": the rewrite dropped "half"; the user's words set the slider
                if am and w.get('type') == 'lend' and not w.get('preset'):   # "lend 0.1 usdc on navi": keep the amount typed (2026-10-22)
                    w0, mx = am.group(1).lower(), float(w.get('max') or 0)
                    w['preset'] = round(mx if w0 in ('all', 'everything', 'max') else mx / 2 if w0 == 'half' else min(float(w0), mx), 6)
                if r is not None:
                    return r
    if (len(tools) >= 2 and not combo and len(coins) <= 1) or (not tools and len(coins) == 1 and len(msg.split()) <= 5):
        c = coins[0] if coins else None
        cands = tools if tools else (['withdraw', 'lend', 'swap', 'perps', 'price'])
        opts = []
        for t in cands:
            try:
                o = intent_option(t, c, address)
            except Exception:
                o = None
            if o and all(o[2] != x['ask'] for x in opts):
                opts.append({'label': o[0], 'sub': o[1], 'ask': o[2]})
        if len(opts) >= 2:
            return {'text': ('What would you like to do with %s?' % disp(c)) if c else 'Which one did you mean?',
                    'widget': {'type': 'choose', 'options': opts[:4]}}
        if len(opts) == 1 and not tools:
            return {'text': 'Did you mean this?', 'widget': {'type': 'choose', 'options': opts}}
    return None


def latest_digest(address):
    """The wallet's newest transaction on chain — "show my last transaction" with nothing in the chat yet (2026-10-23)."""
    if not address:
        return ''
    try:
        d = gql('query($a: SuiAddress!) { transactions(last: 1, filter: {sentAddress: $a}) { nodes { digest } } }', {'a': address})
        n = ((d.get('transactions') or {}).get('nodes') or [])
        return n[-1]['digest'] if n else ''
    except Exception:
        return ''


def last_digest(history):
    for h in reversed(history or []):
        m = re.search(r'transaction ([1-9A-HJ-NP-Za-km-z]{32,44})', str((h or {}).get('text') or ''))
        if m:
            return m.group(1)
    return ''


AI_FAKE_PLAN = re.compile(r"\b(?:i\s+(?:have|'ve)\s+(?:planned|prepared|set\s+up|created|built)|here'?s\s+(?:the|your)\s+(?:plan|transaction)|(?:confirm|approve|sign)\s+(?:it|this|the\s+\w+)?\s*(?:plan\s+|transaction\s+)?in\s+your\s+wallet|do\s+you\s+approve|please\s+(?:confirm|approve))\b", re.I)


def wallet_snapshot(address):
    """A few lines the AI sees with every answer: wallet coins, lent coins, perps — all from Kivy's short caches."""
    out = []
    try:
        hold = all_balances(address) or {}
        coins = sorted(((c, float(v), float(v) * float(coin_px(c) or 0)) for c, v in hold.items() if float(v or 0) > 0), key=lambda x: -x[2])
        coins = [x for x in coins if x[2] >= 0.01]
        out.append('Wallet: ' + (', '.join('%s %s (≈$%.2f)' % (dec(a), disp(c), u) for c, a, u in coins) if coins else 'nothing worth over a cent'))
    except Exception:
        pass
    try:
        lent = ['%s USDC on %s' % (dec(float(p.get('usdc') or 0) + float(p.get('in_app') or 0)), p.get('name') or k)
                for k, p in (lend_positions(address) or {}).items() if float(p.get('usdc') or 0) + float(p.get('in_app') or 0) >= 0.01]
        lent += ['%s %s on %s' % (dec(x['lent']), x['coin'], x['name']) for x in other_lent(address)]
        out.append('Lent: ' + (', '.join(lent) if lent else 'nothing'))
    except Exception:
        pass
    try:
        r = lend_cli('perp-positions', address, timeout=40)
        if r.get('has'):
            ps = ['%s %s %gx, size %s, PnL $%.2f, liquidation %s' % (p['symbol'], p['side'], float(p.get('leverage') or 0), dec(p['size']), float(p.get('pnl') or 0),
                                                                     fmt_px(p['liquidation']) if p.get('liquidation') else '?') for p in (r.get('positions') or [])]
            out.append('Perps: %s USDC free, equity $%.2f%s' % (dec(float(r.get('free') or 0)), float(r.get('equity') or 0), ('; positions: ' + '; '.join(ps)) if ps else '; no positions'))
        else:
            out.append('Perps: no account yet')
    except Exception:
        pass
    return '\n'.join('- ' + x for x in out)


PLAIN_COIN_WORDS = {'stablecoin': 'USDC', 'stablecoins': 'USDC', 'stable': 'USDC', 'stables': 'USDC', 'dollars': 'USDC', 'dollar': 'USDC',
                    'usd': 'USDC', 'bitcoin': 'BTC', 'ether': 'ETH', 'ethereum': 'ETH', 'solana': 'SOL'}
PLAIN_NOT_COINS = {'send', 'up', 'treat', 'blue', 'fud', 'strat', 'me'}   # coin symbols that are also everyday words


def plain_command(msg):
    """A plain sentence that clearly asks to move money -> the command Kivy's code reads ("move 0.05 of my sui into a stablecoin"
    -> "swap 0.05 SUI to USDC"). Used when the AI may not see the wallet (Settings › Privacy), so the card comes from code
    (2026-10-27). None when anything is unclear: the AI answers as before."""
    low = ' '.join(str(msg or '').lower().split())
    rc = re.search(r'\b(0x[0-9a-f]{64}|[a-z0-9-]+\.sui)\b', low)
    rest = low.replace(rc.group(1), ' ') if rc else low
    am = re.search(r'(?<![\w.])(\d+(?:\.\d+)?|all|half|everything)(?![\w.])', rest)
    if not am:
        return None
    amt = am.group(1)
    syms = {k.lower(): k for k in ecoins()}
    coins = []
    for w in re.findall(r'[a-z][a-z0-9.]*', rest):
        w = w.rstrip('.')
        c = PLAIN_COIN_WORDS.get(w) or (syms.get(w) if w not in PLAIN_NOT_COINS else None)
        if c and c not in coins:
            coins.append(c)
    if not coins:
        return None
    proto = re.search(r'\b(navi|suilend|scallop|ember|alphalend)\b', rest)
    on = (' on ' + proto.group(1)) if proto else ''
    if rc and re.search(r'\b(send|pay|transfer|give)\b', rest):
        return 'send %s %s to %s' % (amt, coins[0], rc.group(1))
    if re.search(r'\b(withdraw|take out|pull out|take back|get back)\b', rest) and len(coins) == 1:
        return 'withdraw %s %s%s' % (amt, coins[0], (' from ' + proto.group(1)) if proto else '')
    if re.search(r'\b(lend|deposit|supply|earn|to work|interest|yield)\b', rest) and len(coins) == 1:
        return 'lend %s %s%s' % (amt, coins[0], on)
    if len(coins) == 2 and re.search(r'\b(swap|convert|move|switch|change|exchange|turn|sell|buy|put)\b', rest):
        a, b = (coins[1], coins[0]) if re.search(r'\bbuy\b', rest) and re.search(r'\bwith\b', rest) else (coins[0], coins[1])
        return 'swap %s %s to %s' % (amt, a, b)
    return None


def ai_reply(msg, history, address=None):
    """The AI's answer, with one guard: it may never say a transaction is planned or ready to approve unless a real card
    came with it — approval buttons only ever come from Kivy's own cards (2026-10-21: "nothing happened")."""
    out = _ai_reply(msg, history, address)
    if out and not out.get('plan') and not out.get('widget') and AI_FAKE_PLAN.search(out.get('text') or '') \
            and not re.search(r"\b(?:can't|cannot|can not|won't|unable|not able)\b", out.get('text') or '', re.I):   # a refusal isn't a fake plan (2026-10-23)
        out = dict(out, text='I haven\'t prepared a transaction for that yet. Say it in one line and I\'ll show the card to approve — '
                              'e.g. "send 5 USDC to 0x…" or "lend all my USDC on NAVI".')
    if out and isinstance(out.get('text'), str):
        out = dict(out, text=fix_coin_names(out['text']))
    return out


_COIN_FIX = {'t': 0.0, 'pats': []}


def fix_coin_names(text):
    """Coin names exactly as Kivy writes them in AI answers ("USD sUI", "usdsui", "Vsui" -> USDsui, vSUI; 2026-10-24).
    Mixed-case names only (USDsui, vSUI, haSUI, sSUI, xBTC…): an all-caps pair like "USD SUI" is left alone — it may be two coins."""
    if time.time() - _COIN_FIX['t'] > 600 or not _COIN_FIX['pats']:
        names = set(COINS())
        try:
            names |= {{'USDSUI': 'USDsui'}.get(r['token'], r['token']) for r in lend_rates_all() if r.get('token')}
        except Exception:
            pass
        pats = []
        for n in sorted(names, key=len, reverse=True):
            if n == n.upper() or n == n.lower() or not re.match(r'^[A-Za-z]{3,10}$', n):
                continue                                  # plain all-caps tickers are written right already
            m = re.match(r'^([a-z]+)([A-Z]+)$|^([A-Z]+)([a-z]+)$', n)
            if m:                                         # "USD" + "sui": also matches a stray space between the parts, unless both are caps
                a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
                pats.append((re.compile(r'(?<![A-Za-z0-9])%s(?![A-Za-z0-9])' % re.escape(n), re.I), n))
                pats.append((re.compile(r'(?<![A-Za-z0-9])%s\s+(?!(?-i:%s)(?![A-Za-z0-9]))%s(?![A-Za-z0-9])' % (re.escape(a), re.escape(b.upper()), re.escape(b)), re.I), n))
            else:
                pats.append((re.compile(r'(?<![A-Za-z0-9])%s(?![A-Za-z0-9])' % re.escape(n), re.I), n))
        _COIN_FIX.update(t=time.time(), pats=pats)
    for rx, n in _COIN_FIX['pats']:
        text = rx.sub(n, text)
    return text


def _ai_reply(msg, history, address=None):
    """The AI answers; Kivy's own functions run every tool it asks for."""
    if BRAIN is None:
        return None
    now = time.time()
    _RATE[:] = [t for t in _RATE if now - t < 60]
    vid = str(getattr(XW, 'vid', '') or '')
    mine = _RATE_V.setdefault(vid, [])
    mine[:] = [t for t in mine if now - t < 60]
    today = time.strftime('%Y-%m-%d', time.gmtime(now))
    if _RATE_DAY['day'] != today:
        _RATE_DAY.update(day=today, n=0)
    if len(_RATE) >= AI_PER_MIN or (vid and len(mine) >= AI_PER_VISITOR_MIN) or _RATE_DAY['n'] >= AI_PER_DAY \
            or not ip_quota('ai', getattr(XW, 'ip', None)):
        return {'text': AI_BUSY, 'busy': True}          # honest, instead of the help menu (2026-10-23)
    _RATE.append(now)
    _RATE_DAY['n'] += 1
    if vid:
        mine.append(now)
    if len(_RATE_V) > 5000:
        _RATE_V.clear()
    k = K()

    def tool_prices(a):
        px = prices()
        want = [x.upper() for x in (a.get('assets') or [])] or k['assets']
        return {'prices_usd': {x: px.get(x) for x in want if x in k['assets']}}

    def num(v, coin=None):
        try:
            return float(v)
        except Exception:
            w = word_amount(str(v).strip(), str(coin or '').upper(), hold())
            return w if w is not None else 0.0

    _h = {}

    def hold():
        if address and 'b' not in _h:
            _h['b'] = all_balances(address)
        return _h.get('b')

    def tool_balances(a):
        if not address:
            return {'connected': False, 'message': k['no_wallet']}
        b = balances(address) or {}                 # Kivy's coins; the listed others are added just below
        px = prices()
        found = {disp(sym): {'amount': dec(amt), 'usd': round(amt * px[sym], 2) if sym in px else None}
                 for sym, amt in b.items() if amt > 0}
        for x in other_balances(address):            # USDsui, vSUI… held outside Kivy's coin list
            found[x['sym']] = {'amount': dec(x['amount']), 'usd': round(x['amount'] * x['px'], 2) if x['px'] else None}
        d = defi_positions(address)
        lent = [{'market': x['market'], 'coin': x['coin'], 'amount': dec(x['amount']), 'usd': round(x['usd'], 2),
                 'yearly_rate_pct': round(x['apr'] or 0, 2)} for x in d['lent']]
        try:                                         # open perps positions and resting orders too
            pp = lend_cli('perp-positions', address, timeout=40)
        except Exception:
            pp = {}
        perp_pos = [{'market': x['symbol'], 'side': x['side'], 'leverage': round(float(x.get('leverage') or 0), 1), 'size': dec(x['size']),
                     'entry': dec(x['entry']), 'liquidation': dec(x['liquidation']), 'pnl_usd': round(float(x.get('pnl') or 0), 4)}
                    for x in pp.get('positions') or []]
        perp_ord = [{'market': g['symbol'], 'side': o['side'], 'type': 'limit', 'size': dec(o['size']), 'price': dec(o.get('price')),
                     'value_usd': round(float(o['size']) * float(o.get('price') or 0), 2), 'px': float(o.get('price') or 0)}
                    for g in (pp.get('positions') or []) + (pp.get('orders') or []) for o in g.get('orders') or []]
        if not found and not lent and d.get('perps', 0) < 0.01 and not perp_pos and not perp_ord:   # the owner: an empty wallet is just "You have 0 USDC."
            _h['exact'] = 'You have 0 USDC.'
            return {'connected': True, 'found': {}, 'reply_exactly': 'You have 0 USDC.'}
        # the balance answer is built here, not by the AI: exact numbers, nothing dropped, total at the bottom
        usd = lambda v: ('-$' if v < 0 else '$') + '{:,.2f}'.format(abs(v))
        lines, w_usd = [], 0.0
        for sym, f in found.items():
            w_usd += f['usd'] or 0
            lines.append('• %s: %s%s' % (sym, f['amount'], ' (≈%s)' % usd(f['usd']) if f['usd'] is not None else ''))
        lent_usd = sum(float(x['usd']) for x in lent)
        for x in lent:
            lines.append('• Lent: %s %s on %s, %s%% a year%s' % (x['amount'], x['coin'], x['market'], x['yearly_rate_pct'],
                                                                 '' if x['coin'] == 'USDC' else ' (≈%s)' % usd(x['usd'])))
        eq = float(pp.get('equity') or 0) if pp.get('has') else float(d.get('perps') or 0)
        if pp.get('has') or d.get('perps', 0) >= 0.01:
            lines.append('• Perps account: %s equity (%s USDC free)' % (usd(eq), dec(float(pp.get('free') or d.get('perps') or 0))))
        if len(perp_pos) <= 3:
            for x in perp_pos:
                lines.append('• Position: %s %s %gx · PnL %s%s' % (x['market'], x['side'], x['leverage'], '+' if x['pnl_usd'] >= 0 else '', usd(x['pnl_usd'])))
        else:
            tp = sum(x['pnl_usd'] for x in perp_pos)
            lines.append('• %d positions (%s) · PnL %s%s' % (len(perp_pos), ', '.join(x['market'] for x in perp_pos), '+' if tp >= 0 else '', usd(tp)))
        if len(perp_ord) <= 2:
            for o in perp_ord:
                lines.append('• Order: %s %s %s @ %s (≈%s)' % (o['market'], o['side'], o['size'], usd(o['px']), usd(o['value_usd'])))
        else:
            lines.append('• %d open orders (%s) · ≈%s total' % (len(perp_ord), ', '.join(sorted({o['market'] for o in perp_ord})), usd(sum(o['value_usd'] for o in perp_ord))))
        if d['rewards_usd'] > 0:                        # tiny ones too, as <$0.01 (2026-10-25)
            lines.append('• Rewards to claim: %s' % usd_small(d['rewards_usd']))
        total = w_usd + lent_usd + eq
        parts = [('wallet %s' % usd(w_usd)) if w_usd else '', ('lending %s' % usd(lent_usd)) if lent_usd else '', ('perps %s' % usd(eq)) if eq else '']
        summary = '\n'.join(lines) + '\n\nTotal ≈ %s%s.' % (usd(total), (' (' + ' + '.join(x for x in parts if x) + ')') if sum(1 for x in parts if x) > 1 else '')
        if re.search(r'balance|holding|portfolio|net ?worth|total value|how much (?:do )?i (?:have|own)|what do i (?:have|own)|^\\W*my (?:wallet|funds|assets)\\W*$', msg, re.I):
            _h['exact'] = summary
        return {'connected': True, 'summary': summary, 'in_wallet': found, 'lent_defi_positions': lent,
                'rewards_to_claim_usd': usd_small(d['rewards_usd']),
                'aftermath_perps_collateral_usdc': dec(d.get('perps', 0)),
                'perps_open_positions': perp_pos or 'none', 'perps_open_orders': perp_ord or 'none',
                'note': 'If they asked for their balance, reply with summary exactly. Otherwise use what you need. Tell the user what is in the wallet AND their DeFi positions (coins lent, with market and yearly rate), '
                        'and mention rewards to claim whenever there are any (say <$0.01 when tiny), plus perps collateral, open perps positions (market, side, leverage, PnL) '
                        'and open orders (market, side, size @ price, and value_usd as ≈$; if more than 2, just say how many, which markets and their total ≈$ value). Do not list coins they have none of.'}

    def show_card(a):
        # the same live boxes the typed shortcuts show; the AI only picks which one
        c, sym = str(a.get('card') or '').lower(), str(a.get('symbol') or '')
        if c == 'trade':                              # the trade panel, filled in: deposit + order go in ONE transaction
            sym = chart_symbol(sym)
            if not sym:
                return {'error': 'unknown market'}
            mx = next((r.get('max_lev') for r in perp_market_list() if r['symbol'] == sym), None) or 10
            lv = str(a.get('leverage') or 'max').lower()
            lev = mx if lv in ('max', '') else max(1, min(float(re.sub(r'[^0-9.]', '', lv) or mx), mx))
            try:
                usd = float(a.get('usd') or 0)
            except Exception:
                usd = 0
            mg = str(a.get('margin') or '').lower().strip()
            if mg in ('max', 'all', 'everything'):     # all the user's free USDC: wallet + free perps balance
                try:
                    margin = float(perp_account(address, fresh=True).get('collateral') or 0)   # perps balance only, never the wallet
                except Exception:
                    margin = 0
            else:
                try:
                    margin = float(re.sub(r'[^0-9.]', '', mg) or 0)
                except Exception:
                    margin = 0
            otype = 'limit' if str(a.get('order_type') or '').lower() == 'limit' else 'market'
            try:
                lpx = float(re.sub(r'[^0-9.]', '', str(a.get('price') or '')) or 0)
            except Exception:
                lpx = 0
            if not usd and margin:                    # margin from the user's money -> the biggest order it can open
                usd = margin * 0.97 / (1.05 / lev + 0.0009)
            usd = round(usd, 2)
            if not usd:                                 # no size: never a bare chart — ask how much, with quick picks
                sd = 'short' if str(a.get('side') or '').lower() in ('short', 'sell') else 'long'
                opts = ['%s %s $5 %gx' % (sd, sym, lev), '%s %s $10 %gx' % (sd, sym, lev), '%s %s with max usdc at %gx' % (sd, sym, lev),
                        '%s market (label: See the %s market first)' % (sym, sym)]
                return {'error': 'size missing', 'note': 'The user did not give an order size. Do NOT show a chart. Call ask_choice with these '
                        'options (labels can be friendlier): %s%s, and ask in one line how big the order should be.'
                        % ('; '.join(opts), (' — they asked for a limit at %s, so keep that in each option' % lpx) if lpx else '')}
            side = 'short' if str(a.get('side') or '').lower() in ('short', 'sell', '1') else 'long'
            w = widget_reply('%s %s $%s %gx' % (side, sym, usd, lev) if usd else '%s %s %gx' % (side, sym, lev), address, history)
            if not w or not w.get('widget'):
                return {'error': 'trade panel unavailable'}
            cw = w['widget'] if w['widget'].get('type') == 'chart' else (w['widget'].get('chart') or w['widget'])
            if otype == 'limit' and lpx > 0:
                cw['trade'].update({'type': 'limit', 'price': lpx})
            cf = perp_confirm(address, cw)
            if (cf is not cw and (cf.get('deposit') or 0) > 0.000001) or (mg in ('max', 'all', 'everything') and margin <= 0.000001):
                da = deposit_ask(address, cf.get('deposit') if cf is not cw else None)
                _h['widget'] = da['widget']
                return {'shown': 'perps deposit card', 'perps_short': True, 'note': da['text'] + ' Say exactly this, briefly. No order was prepared.'}
            _h['widget'] = cf
            return {'shown': 'trade panel', 'side': side, 'symbol': sym, 'order_type': otype, 'limit_price': lpx or None,
                    'size_usd': usd, 'leverage': lev, 'market_max_leverage': mx,
                    'note': 'The order uses only USDC already in the perps account. If the card says short, tell the user to deposit '
                            'the amount they are willing to risk first (show_card perps_deposit). Do not repeat the numbers at length.'}
        ask = {'markets': 'market', 'perps': 'perps', 'positions': 'positions', 'lending': 'lend', 'lending_withdraw': 'withdraw',
               'rewards': 'claim rewards', 'perps_deposit': 'deposit to perps', 'perps_withdraw': 'withdraw from perps'}.get(c)
        if c == 'chart':
            ask = ('chart ' + chart_symbol(sym)) if chart_symbol(sym) else None
        if not ask:
            return {'error': 'unknown card or market'}
        try:
            w = widget_reply(ask, address, history)
        except Exception:
            w = None
        if not w or not w.get('widget'):
            return ({'error': 'card unavailable: the read behind it failed just now. Tell the user it did not load and to try again in a '
                              'moment. Do NOT describe a slider, card or amount picker.'} if not w else {'note': w.get('text')})
        _h['widget'] = w['widget']
        return {'shown': c + (' ' + sym.upper() if sym else ''), 'note': 'The card is shown under your reply; do not repeat its numbers. Tables start sorted by 24h volume; the user can tap a column (e.g. 24h) to sort.'}

    def ask_choice(a):
        # the AI's "did you mean…?" — buttons that send a plain chat message (the same as the user typing it)
        opts = []
        for o in (a.get('options') or [])[:4]:
            if not isinstance(o, dict):
                continue
            lbl, m = str(o.get('label') or '').strip()[:60], str(o.get('message') or '').strip()[:80]
            if lbl and m and not re.search(r'0x[0-9a-fA-F]{6,}|https?://', m):
                opts.append({'label': lbl, 'sub': '', 'ask': m})
        if len(opts) < 2:
            return {'error': 'give 2-4 options'}
        _h['widget'] = {'type': 'choose', 'options': opts}
        return {'shown': len(opts), 'note': 'The options are shown as buttons under your reply; ask the question in one line and do not list them again.'}

    tools = {
        'ask_choice': ask_choice,
        'get_balances': (lambda a: {'note': 'The wallet IS connected, but the user turned off "Let the AI see my wallet" in Settings › Privacy. '
                                             'Never guess a balance and never ask them to connect. Say in one line that wallet sharing with the AI is off, '
                                             'and that their balance is at the top right (or they can turn sharing back on in Settings › Privacy). '
                                             'If they asked to swap, lend, send or withdraw, STILL call that plan tool: Kivy checks their balance '
                                             'itself and shows them the card, without telling you their numbers.'}) if getattr(XW, 'ai_wallet', True) is False else tool_balances,
        'get_prices': tool_prices,
        'plan_swap': lambda a: plan('swap', num(a.get('amount'), a.get('from_asset')), str(a.get('from_asset', '')).upper(),
                                    str(a.get('to_asset', '')).upper(), holdings=hold()),
        'plan_lend': lambda a: (lend_card(lend_token(a.get('asset')), a.get('protocol'), address)
                                if lend_token(a.get('asset')) not in (None, 'USDC') else
                                plan('lend', num(a.get('amount'), a.get('asset') or 'USDC'), str(a.get('asset') or 'USDC').upper(),
                                     holdings=hold(), protocol=a.get('protocol'))),
        'plan_send': lambda a: plan('send', num(a.get('amount'), a.get('asset')), str(a.get('asset', '')).upper(),
                                    recipient=str(a.get('recipient', '')), holdings=hold(), sender=address),
        'plan_withdraw': lambda a: (widget_reply('withdraw %s%s' % (lend_token(a.get('asset')), (' from %s' % a['protocol']) if a.get('protocol') else ''), address)
                                    if lend_token(a.get('asset')) not in (None, 'USDC') else None) or plan('withdraw', 0.0 if str(a.get('amount')).lower() in WORD_AMOUNTS else num(a.get('amount')),
                                        str(a.get('asset') or 'USDC').upper(), protocol=a.get('protocol'),
                                        lent=lend_positions(address) if address else {},
                                        all_=str(a.get('amount')).lower() in ('all', 'max', 'everything')),
        'get_lending': lambda a: {'rates': coin_rates(a.get('asset')), 'collateral_by_market': collat_ai(),
                                  'your_positions': ('not shared with the AI (Settings › Privacy) — show the lending card instead'
                                                     if getattr(XW, 'ai_wallet', True) is False else lend_positions_all(address) if address else 'no wallet connected')},
        'show_card': lambda a: show_card(a),
        'get_transaction': lambda a: _h.setdefault('tx', tx_detail(str(a.get('digest') or '').strip() or last_digest(history) or latest_digest(address))),
    }
    def keep_card(f):                                # a tool that answers with a card (lend / withdraw / swap-then-lend): show it
        def g(a):
            r = f(a)
            if isinstance(r, dict) and isinstance(r.get('widget'), dict) and not _h.get('widget'):
                _h['widget'] = r['widget']
            return r
        return g
    tools = {n: keep_card(f) for n, f in tools.items()}
    if getattr(XW, 'ai_wallet', True) is False:      # Settings › Privacy off: cards still reach the user, but the AI gets no amounts or
        def private(f):                               # balances from plan / card / transaction tools (audit nv #11)
            def g(a):
                r = f(a)
                out = {'card_shown': bool(_h.get('widget') or (isinstance(r, dict) and (r.get('widget') or r.get('card') or r.get('plan')))),
                       'note': 'The user turned off "Let the AI see my wallet": the details are on the card for the user, not for you. '
                               'Say in one line what the card does; never state or guess a balance or amount from their wallet.'}
                if isinstance(r, dict) and r.get('plan'):
                    out['_plan'] = r['plan']           # the card, lifted by the brain and never sent to the model
                if isinstance(r, dict) and r.get('error'):
                    out['error'] = re.sub(r'\d[\d,.]*', '…', str(r['error']))[:200]   # the reason, without their numbers
                return out
            return g
        tools = {n: (f if n in ('ask_choice', 'get_balances', 'get_prices', 'get_lending') else private(f)) for n, f in tools.items()}
    # Kivy never invents an amount ("bitcoin" alone became "swap ALL your SUI"). A plan needs a number or
    # all/half/max in the user's own latest message — otherwise the model is told to ask instead.
    said_amount = re.search(r'\d|\b(all|half|max|everything|entire|whole)\b', msg, re.I)
    for name in ('plan_swap', 'plan_lend', 'plan_send'):
        tools[name] = (lambda f: lambda a: f(a) if said_amount else {
            'error': 'The user has not said an amount. Do NOT guess and never default to all of a coin. Ask how much they want, '
                     'and if it is unclear whether they mean spot (swap) or perps (long/short), ask that too.'})(tools[name])
    try:
        for name in [n for n in tools if n != 'get_balances']:   # remember if anything besides a balance check ran
            tools[name] = (lambda f: lambda a: (_h.__setitem__('other', True), f(a))[1])(tools[name])
        spotish = re.search(r'\b(?:lend\w*|deposit\w*|withdr\w*|swap\w*|send\w*|transfer\w*|balances?|wallet|apy|apr|yield|earn\w*|rewards?|dust|stak\w*|suilend|navi|ember|fees?|gas)\b', msg, re.I)
        tradeish = re.search(r'\b(?:perps?|long|short|lever\w*|margin|positions?|trad\w*|stocks?|shares?|futures|markets?|tickers?|liquidat\w*|funding|tp|sl|stop|chart|\d+\s*x)\b', msg, re.I)
        try:                                          # every Aftermath perps market, by category, so the AI never says one isn't there —
            if spotish and not tradeish:              # only on questions that could be about trading (~1k tokens saved otherwise, W-01)
                raise StopIteration
            cats = {}
            for r in perp_market_list():
                cats.setdefault(r.get('category') or 'Other', []).append(r['symbol'])
            k['perp_markets'] = cats
        except Exception:
            pass
        if address and getattr(XW, 'ai_wallet', True):   # Kivy's memory of this wallet (public chain facts; off with Settings › Privacy)
            try:
                k = dict(k, _memory=memory_text(wallet_memory(address)))
            except Exception:
                pass
            try:                                         # what they hold right now, so the AI answers with their numbers instead of asking (2026-10-23)
                k = dict(k, _snapshot=wallet_snapshot(address))
                tools.pop('get_balances', None)          # the snapshot IS the balance: no second AI round to fetch it again (W-01)
            except Exception:
                pass
        out = BRAIN.chat(msg, history, k, tools, wallet_connected=bool(address), allow=address or '')
        url = (_h.get('tx') or {}).get('explorer')
        if out and url and url not in (out.get('text') or ''):
            out['text'] = (out.get('text') or '').rstrip() + '\n\nView on SuiVision: ' + url
        if out and _h.get('widget') and not out.get('plan'):
            out['widget'] = _h['widget']       # the box the AI asked for, under its reply
        # safety net: a complete trade request (side + market + size) always comes with its order card, even if the model forgot it
        if out and address and not out.get('widget') and not out.get('plan'):
            try:
                sm = re.search(r'\b(long|short|buy|sell)\b', msg, re.I)
                zm = re.search(r'(?:\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:\$|usd\b|dollars?))', msg, re.I)
                lm = re.search(r'(\d+(?:\.\d+)?)\s*x\b', msg, re.I)
                coin = next((chart_symbol(t) for t in re.findall(r'[A-Za-z&][A-Za-z0-9&]{1,9}', msg)   # US500, JP225 too
                             if t.lower() not in ('long', 'short', 'buy', 'sell', 'with', 'at', 'kivy', 'can', 'you', 'open', 'leverage', 'usd', 'usdc', 'the', 'and', 'for', 'max', 'please')
                             and chart_symbol(t)), None)
                if sm and zm and coin:
                    side = 'short' if sm.group(1).lower() in ('short', 'sell') else 'long'
                    w = widget_reply('%s %s $%s%s' % (side, coin, zm.group(1) or zm.group(2), (' %sx' % lm.group(1)) if lm else ''), address, history)
                    if w and w.get('widget'):
                        out['widget'] = w['widget']
            except Exception:
                pass
        # safety net: a trading request with an empty perps account always comes with the deposit card
        if out and address and not out.get('widget') and not out.get('plan') and re.search(
                r'\b(long|short|leverage|limit order|open (?:a )?(?:position|trade)|perps? (?:order|trade)|\d+\s*x)\b', msg, re.I):
            try:
                if float(perp_account(address, fresh=True).get('collateral') or 0) < 1:
                    out['widget'] = (perp_widget('deposit to perps', address) or {}).get('widget')
            except Exception:
                pass
        if out and _h.get('exact') and not out.get('plan') and not _h.get('other') and not _h.get('widget'):
            out['text'] = _h['exact']          # empty wallet: exactly this, no extra words
        return out
    except Exception as exc:
        print('kivy: AI error %s' % str(exc)[:120], flush=True)
        return None


KIVY_DIARY = KIVY_HOME / 'diary.md'
_DIARY_SEEN = set()


# ── After-the-fact check: did the signed transaction do what Kivy's card promised? (2026-10-20) ──
# When Kivy builds a transaction the SERVER remembers its plan (the same limits the pre-sign check uses, plus the
# minimum the user should receive). After signing, the browser only hands back the digest and the plan's id; the
# server reads the real transaction from the chain and compares. Verdict -> Kivy's tx log (✅ / ⚠️) + data/checks.jsonl.
class IntentStore(dict):
    """Kivy's plans, also kept on disk (data/intents.db, 24 h): a restart or a flood that pushes a plan out of memory no longer
    leaves check 2 without it (red-team 2026-10-24). Memory keeps 2 h; a plan missing from memory is read back from disk.
    Changing a field of a stored plan needs .save(iid)."""
    DB = HERE / 'data' / 'intents.db'

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        try:
            import sqlite3
            with self._lock, sqlite3.connect(self.DB) as c:
                c.execute('CREATE TABLE IF NOT EXISTS intents(iid TEXT PRIMARY KEY, at REAL, data TEXT)')
                for iid, data in c.execute('SELECT iid, data FROM intents WHERE at > ?', (time.time() - 7200,)):
                    super().__setitem__(iid, json.loads(data))
        except Exception as exc:
            print('kivy intents: disk store unavailable (%s)' % exc, flush=True)

    def _db(self, sql, args=()):
        import sqlite3
        try:
            with self._lock, sqlite3.connect(self.DB, timeout=5) as c:
                return c.execute(sql, args).fetchall()
        except Exception as exc:
            print('kivy intents: %s' % str(exc)[:120], flush=True)
            return []

    def __setitem__(self, iid, it):
        super().__setitem__(iid, it)
        self.save(iid)

    def save(self, iid):
        it = dict.get(self, iid)
        if it is not None:
            self._db('INSERT OR REPLACE INTO intents(iid, at, data) VALUES(?,?,?)', (iid, float(it.get('at') or time.time()), json.dumps(it, default=str)))

    def get(self, iid, default=None):
        it = dict.get(self, iid)
        if it is None and re.match(r'^[0-9a-f]{16}$', str(iid or '')):
            rows = self._db('SELECT data FROM intents WHERE iid = ? AND at > ?', (iid, time.time() - 86400))
            if rows:
                it = json.loads(rows[0][0])
                super().__setitem__(iid, it)            # back in memory (not written again)
        return it if it is not None else default

    def prune_disk(self):
        self._db('DELETE FROM intents WHERE at < ?', (time.time() - 86400,))


_INTENTS = IntentStore()
CHECKS = Path(__file__).resolve().parent / 'data' / 'checks.jsonl'
TX_BUILD = {'/api/tx/dust', '/api/tx/swap', '/api/tx/check-send', '/api/tx/sendmany', '/api/tx/lend', '/api/tx/swaplend', '/api/tx/withdrawswap', '/api/tx/perp', '/api/tx/compose'}
OTHER_SHARE_MAX = 0.01                                # others (pools, protocol fee wallets) may get at most 1% of what was paid


def intent_actions(path, req, resp):
    """The steps of a transaction Kivy built, for the numbers: [{service, coin, amount, usd}] (one PTB can hold several)."""
    def act(service, coin, amount):
        try:
            a = float(amount)
        except Exception:
            a = 0.0
        c = ckey(str(coin or '')) or str(coin or '')
        px = 1.0 if c in ('USDC', 'USDsui', 'USDT') else (coin_px(c) or 0)
        return {'service': service, 'coin': c, 'amount': a, 'usd': round(a * px, 6) if math.isfinite(a) else 0}
    k = path.rsplit('/', 1)[-1]
    try:
        if k == 'swap':
            return [act('swap', req.get('from'), req.get('amount'))]
        if k == 'dust':                               # one swap per dust coin (2026-10-25)
            return [act('swap', l.get('sym'), l.get('amount')) for l in (resp.get('legs') or [])]
        if k == 'check-send':
            items = req.get('items') or [{'coin': req.get('coin'), 'amount': req.get('amount')}]
            return [act('send', x.get('coin'), x.get('amount')) for x in items]
        if k == 'sendmany':                           # every withdrawal and every transfer in the multi-send
            return [act('withdraw' if p['op'] == 'withdraw' else 'send', p.get('coin'), p.get('amount')) for p in (resp.get('plan') or [])]
        if k == 'lend':
            a = str(req.get('action') or 'deposit')
            svc = {'deposit': 'lend', 'withdraw': 'withdraw', 'claim': 'claim'}.get(a, a)
            amt = resp.get('amount') if str(req.get('amount')) == 'all' else req.get('amount')
            if a == 'claim':                              # a claim's amount comes from the request, not the chain: never volume (audit nv #10)
                amt = 0
            return [act(svc, req.get('coin') or 'USDC', amt or 0)]
        if k == 'swaplend':
            return [act('swap', req.get('from'), req.get('amount')), act('lend', req.get('to'), resp.get('expected_out') or 0)]
        if k == 'withdrawswap':
            out = [act('withdraw', req.get('coin') or 'USDC', resp.get('amount') or req.get('amount') or 0)]
            if req.get('to') and str(req.get('to')).lower() != str(req.get('coin') or 'USDC').lower():
                out.append(act('swap', req.get('coin') or 'USDC', resp.get('amount') or 0))
            if resp.get('lend_to'):
                out.append(act('lend', req.get('to') or req.get('coin'), resp.get('expected_out') or resp.get('amount') or 0))
            return out
        if k == 'compose':                            # follow the coin through the plan: start -> swaps -> sinks (a 'rest' sink takes what's left)
            plan = resp.get('plan') or []
            out, cur, have = [], None, 0.0
            for p in plan:
                op = p.get('op')
                if op == 'dust':                       # the sweep at the start: one swap per dust coin, then the coin in hand
                    out += [act('swap', l.get('sym'), l.get('amount')) for l in (p.get('legs') or [])]
                    cur, have = p.get('coin'), float(p.get('min') or p.get('amount') or 0)
                    continue
                if op in ('wallet', 'withdraw', 'perps_out'):
                    more = op == 'withdraw' and out and out[-1]['service'] == 'withdraw' and cur == (p.get('coin') or 'USDC')
                    cur, have = p.get('coin') or 'USDC', (have if more else 0.0) + float(p.get('amount') or 0)   # several markets: they add up
                    if op != 'wallet':
                        out.append(act('withdraw' if op == 'withdraw' else 'perps_withdraw', cur, have))
                elif op == 'plus':
                    have += float(p.get('amount') or 0)
                elif op == 'swap' and p.get('in') is not None:   # one piece of a split: its own input; the start coin stays in hand
                    out.append(act('swap', cur, float(p['in'])))
                elif op == 'swap':
                    out.append(act('swap', cur, have))
                    cur, have = p.get('to') or cur, float(p.get('min') or 0)
            fixed = sum(float(p.get('amount') or 0) for p in plan if p.get('op') in ('lend', 'send', 'keep', 'perps') and p.get('amount') is not None)
            for p in plan:
                op = p.get('op')
                if op in ('lend', 'send', 'perps'):
                    amt = float(p['amount']) if p.get('amount') is not None else max(have - fixed, 0)
                    out.append(act({'lend': 'lend', 'send': 'send', 'perps': 'perps_deposit'}[op], p.get('coin') or cur, amt))
            return out
        if k == 'perp':
            a, o = str(req.get('action') or ''), req.get('order') or {}
            if a == 'order':
                return [{'service': 'perps_open', 'coin': str(o.get('symbol') or ''), 'amount': float(o.get('usd') or 0), 'usd': float(o.get('usd') or 0)}]
            if a == 'close':
                pv = resp.get('preview') or {}
                usd = float(pv.get('order_value') or 0) if isinstance(pv, dict) else 0.0
                return [{'service': 'perps_close', 'coin': str(o.get('symbol') or ''), 'amount': usd, 'usd': usd}]
            if a in ('deposit', 'withdraw', 'margin'):
                amt = req.get('amount') if a != 'margin' else o.get('amount')
                return [act('perps_' + a, 'USDC', resp.get('amount') or amt or 0)]
            if a in ('tpsl', 'cancel', 'open'):
                return [{'service': {'open': 'perps_account'}.get(a, 'perps_' + a), 'coin': str(o.get('symbol') or ''), 'amount': 0, 'usd': 0}]
    except Exception:
        pass
    return []


KIVY_PKGS_FILE = Path('/opt/kivy/kivy_pkgs.json')   # the Move packages a Kivy transaction may call (audit V01); the page enforces the same list


def kivy_pkgs():
    try:
        return {normaddr(p) for p in json.loads(KIVY_PKGS_FILE.read_text())['packages']}
    except Exception:
        return set()


def normaddr(a):
    try:
        return '0x%064x' % int(str(a), 16)
    except Exception:
        return str(a).lower()


def pkg_watch(path, tx):
    """Every package outside the allowlist that a built transaction calls is logged; returns the ones the page would refuse.
    Same rule as the page: an unlisted 'router' step inside a swap is the aggregator's route (trusted, 2026-10-25);
    anything else is refused here too, before the card goes out (review F-04)."""
    bad = []
    try:
        t = json.loads(tx) if isinstance(tx, str) else (tx or {})
        for c in t.get('commands') or []:            # never publish or upgrade a program in a user's transaction (audit nv #2)
            if isinstance(c, dict) and ({'Publish', 'Upgrade'} & set(c) or c.get('$kind') in ('Publish', 'Upgrade')):
                bad.append('it publishes or upgrades a program')
        allow = kivy_pkgs()
        calls = [c.get('MoveCall') for c in t.get('commands') or [] if isinstance(c, dict) and c.get('MoveCall')]
        swapping = any(mc.get('module') == 'router' and normaddr(mc.get('package')) in allow for mc in calls)   # Aftermath's allowlisted router only (S-01)
        for mc in calls:
            if normaddr(mc.get('package')) not in allow:
                with open(HERE / 'data' / 'unknown_pkgs.log', 'a') as f:
                    f.write('%s %s %s::%s::%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), path, normaddr(mc.get('package')), mc.get('module'), mc.get('function')))
                if not (swapping and mc.get('module') == 'router'):
                    bad.append('%s…::%s' % (normaddr(mc.get('package'))[:10], mc.get('module')))
    except Exception:
        pass
    return bad


def intent_label(path, req, resp):
    """What the Activity tab calls a transaction, written by Kivy from the plan it built — never text from the request
    (red-team 2026-10-24: /api/tx/log took any label, so anyone could write into a user's Activity, greeting and AI notes).
    Same wording the page used to send."""
    k = path.rsplit('/', 1)[-1]
    nm = lambda m: LEND_MKTS.get(str(m or ''), str(m or ''))
    c = lambda x: disp(ckey(str(x or '')) or lend_token(str(x or '')) or 'USDC')
    try:
        if k == 'compose':
            def step(p):
                op, a = p.get('op'), ('%s ' % dec(p['amount'])) if p.get('amount') is not None else ''
                if op == 'wallet':
                    return 'Take %s%s from your wallet' % (a, c(p.get('coin')))
                if op == 'withdraw':
                    return 'Withdraw %s%s from %s' % (a, c(p.get('coin')), nm(p.get('market')))
                if op == 'plus':
                    return 'Add %s%s from your wallet' % (a, c(p.get('coin')))
                if op == 'perps_out':
                    return 'Withdraw %sUSDC from your perps account' % a
                if op == 'dust':
                    return 'Sweep %d dust coins into %s' % (len(p.get('legs') or []), c(p.get('coin')))
                if op == 'swap':
                    return 'Swap %s to %s' % (c(p.get('from')), c(p.get('to')))
                if op == 'lend':
                    return 'Lend %s%s on %s' % (a, c(p.get('coin')), nm(p.get('market')))
                if op == 'send':
                    to = str(p.get('to') or '')
                    return 'Send %s%s to %s' % (a, c(p.get('coin')), (to[:6] + '…' + to[-4:]) if ADDR_RE.match(to) else 'an address')
                if op == 'keep':
                    return 'Keep %s%s in your wallet' % (a, c(p.get('coin')))
                if op == 'perps':
                    return 'Deposit %sUSDC into your perps account' % a
                return None
            return 'chain ' + ' → '.join(x for x in (step(p) for p in resp.get('plan') or []) if x)
        if k == 'perp':
            a, o = str(req.get('action') or ''), req.get('order') or {}
            sym = re.sub(r'[^A-Za-z0-9-]', '', str(o.get('symbol') or ''))[:20]
            if a == 'order':
                return 'Perps %s %s%s' % ('limit' if str(o.get('type') or '') == 'limit' else 'market', 'short ' if o.get('side') else 'long ', sym)
            return 'Perps %s %s' % (re.sub(r'[^a-z]', '', a)[:12], sym)
        if k == 'withdrawswap':
            if resp.get('lend_to'):
                return 'move %s %s→%s → %s' % (nm(req.get('protocol')), c(req.get('coin')), c(req.get('to') or req.get('coin')), nm(resp.get('lend_to')))
            return 'withdrawswap %s %s→%s' % (nm(req.get('protocol')), c(req.get('coin')), c(req.get('to')))
        if k == 'swaplend':
            return 'swaplend %s %s→%s' % (nm(req.get('protocol') or req.get('market')), c(req.get('from')), c(req.get('to')))
        if k == 'lend':
            return 'lend %s %s' % ({'deposit': 'deposit', 'withdraw': 'withdraw', 'claim': 'claim'}.get(str(req.get('action') or ''), 'deposit'), nm(req.get('protocol')))
        if k == 'swap':
            return 'swap %s→%s' % (c(req.get('from')), c(req.get('to')))
        if k in ('check-send', 'sendmany'):
            return 'send'
        if k == 'dust':
            return 'dust sweep %d coins→%s' % (len(resp.get('legs') or []), c(req.get('to')))
    except Exception:
        pass
    return k


def remember_intent(path, req, resp):
    """Store the plan behind a transaction Kivy just built; returns its id (kept 2 hours)."""
    now = time.time()
    for k in [k for k, v in list(_INTENTS.items()) if now - v['at'] > 7200]:
        _INTENTS.pop(k, None)
    g = resp.get('guard') or {}
    it = {'at': now, 'kind': 'move' if resp.get('lend_to') else path.rsplit('/', 1)[-1], 'address': str(req.get('address') or '').lower(), 'guard': g,
          'protocol': str(req.get('protocol') or ''), 'action': str(req.get('action') or ''), 'min_in': {}}
    if resp.get('router') in ('Aftermath', 'Cetus', 'Bluefin 7k'):
        it['router'] = resp['router']                 # which swap router built it (backups since 2026-10-26) — kept, not shown on the card
    try:                                              # the minimum the card promised (swap / withdraw-then-swap)
        slip = float(K()['slippage_cap_pct']) / 100
        if it['kind'] == 'swap' and resp.get('min_out') and resp.get('coin_out'):
            c = cinfo(resp['coin_out'])
            it['min_in'][c['type']] = int(float(resp['min_out']) * 10 ** int(c['decimals']) * 0.999)
        if it['kind'] == 'withdrawswap' and resp.get('expected_out') and resp.get('to'):
            c = cinfo(resp['to'])
            it['min_in'][c['type']] = int(float(resp['expected_out']) * (1 - slip) * 10 ** int(c['decimals']) * 0.999)
    except Exception:
        pass
    if it['kind'] == 'move':
        it['swap'] = bool(req.get('to')) and str(req.get('to')).lower() != str(req.get('coin') or '').lower()
        it['lend_to'] = str(req.get('lend_to') or '')
    if it['kind'] == 'compose':
        it['sinks'] = resp.get('check_sinks') or []
        it['final_type'] = resp.get('final_type')
        it['src_op'] = ((resp.get('plan') or [{}])[0] or {}).get('op')
        try:                                          # how much passes through the chain's swaps: a router fee is measured against this (2026-10-23)
            dec = next((int(v['decimals']) for v in COINS().values() if v['type'] == it['final_type']), None)
            mins = [float(p.get('min') or 0) for p in (resp.get('plan') or []) if p.get('op') in ('swap', 'dust')]
            if dec is not None and mins:
                it['volume'] = {it['final_type']: int(max(mins) * 10 ** dec)}
            tys = {p['coin']: (p['type'], int(p.get('dec') or 0)) for p in (resp.get('plan') or []) if p.get('type')}
            if any(p.get('in') is not None for p in (resp.get('plan') or [])):    # a split: each coin's own swap
                it['volume'] = {tys[p['to']][0]: int(float(p.get('min') or 0) * 10 ** tys[p['to']][1]) for p in resp['plan'] if p.get('op') == 'swap' and p.get('to') in tys}
        except Exception:
            pass
        try:                                          # the coins the plan TAKES IN count too: a swap's input that was withdrawn inside the transaction
            ec = ecoins()                             # never touches the wallet, so a 0.0001% router fee read as 1% of the leftover dust (2026-10-26)
            vol = it.get('volume') or {}
            for p in resp.get('plan') or []:
                c = ec.get(p.get('coin') or '') or {}
                if p.get('op') in ('wallet', 'withdraw', 'plus', 'perps_out') and c.get('type') and p.get('amount'):
                    vol[c['type']] = vol.get(c['type'], 0) + int(float(p['amount']) * 10 ** int(c['decimals']))
            if vol:
                it['volume'] = vol
        except Exception:
            pass
    if it['kind'] == 'dust' and resp.get('to_type'):  # the router fee is measured against what the sweep delivers
        it['volume'] = {resp['to_type']: int(resp.get('min_units') or 0)}
    if it['kind'] in ('move', 'swaplend'):           # the coin that must go INTO the market, not to the wallet
        try:
            tc = req.get('to') or req.get('coin')
            ty = (cinfo(ckey(tc) or tc)['type'] if (ckey(tc) or tc) in ecoins() else None)
            it['lent_type'] = ty
            it['lent_units'] = int(float(resp.get('expected_out') or resp.get('amount') or 0) * 10 ** int(cinfo(ckey(tc) or tc)['decimals'])) if ty else 0
            if ty and it['lent_units']:
                it['volume'] = {ty: it['lent_units']}
            it['lend_to'] = it.get('lend_to') or str(req.get('protocol') or '')
        except Exception:
            pass
    it['ip'] = getattr(XW, 'ip', None)               # the visitor who built it: only they may report on it (review 2026-10-24)
    it['actions'] = intent_actions(path, req, resp)
    it['label'] = intent_label(path, req, resp)[:300]
    vol = {k: int(v) for k, v in (it.get('volume') or {}).items()}       # the most of each coin this plan moves (audit V01: the
    for k, v in (it.get('min_in') or {}).items():                         # browser caps what any unapproved address may get by it)
        vol[k] = max(vol.get(k, 0), int(v))
    for a in it['actions']:
        try:
            c = cinfo(a['coin'])
            vol[c['type']] = max(vol.get(c['type'], 0), int(float(a['amount']) * 10 ** int(c['decimals'])))
        except Exception:
            pass
    it['vol'] = vol
    it['fee_sui'] = int((g.get('fee') or {}).get('mist') or 0) / 1e9   # Kivy's flat fee in this transaction (verified on chain by the guard)
    exp = []                                          # what each lend should put into its market (S-06: check 2 only saw the call)
    try:
        if it['kind'] == 'compose':
            exp = [{'mk': k['market'], 'type': it.get('final_type'), 'amount': float(k['expect'])} for k in it.get('sinks') or []
                   if k.get('op') == 'lend' and k.get('expect') and it.get('final_type')]
        elif it['kind'] in ('move', 'swaplend') and it.get('lent_type') and it.get('lent_units') and it.get('lend_to'):
            exp = [{'mk': it['lend_to'], 'type': it['lent_type'], 'amount': it['lent_units'] / 10 ** int(cinfo(ckey(req.get('to') or req.get('coin')) or req.get('to') or req.get('coin'))['decimals'])}]
        elif it['kind'] == 'lend' and it.get('action') == 'deposit' and float(req.get('amount') or 0) > 0:
            c = 'USDC' if str(req.get('coin') or 'USDC').upper() == 'USDC' else lend_token(str(req.get('coin')))
            exp = [{'mk': it['protocol'], 'type': cinfo(c)['type'], 'amount': float(req['amount'])}]
    except Exception:
        exp = []
    iid = secrets.token_hex(8)
    _INTENTS[iid] = it
    if exp:
        it['lend_expect'] = exp
        def snap():                                   # the market positions BEFORE the user signs, read in the background
            try:
                rows = lend_positions_rows(it['address'])
                it['lend_before'] = {'%s|%s' % (r['id'], lend_ntype(r.get('type') or cinfo('USDC')['type'])): float(r['lent']) for r in rows}
                it['lend_before_at'] = time.time()
                _INTENTS.save(iid)
            except Exception:
                pass
        import threading
        th = threading.Thread(target=snap, daemon=True)
        th.start()
        th.join(timeout=5)                            # wait (usually 1-3 s) so the reading is from BEFORE the wallet can sign — a fast
    return iid                                        # signer beat it on the first live test (2026-10-25); after 5 s it carries on alone


def lend_ntype(t):
    return re.sub(r'^0x0*', '', str(t).split('::')[0].lower()) + '::' + '::'.join(str(t).split('::')[1:])


def lend_amount_check(intent, res, digest):
    """After check 2: did each lend's market really receive (about) what was planned? Positions after vs the reading taken
    before signing; short by more than 2% = a problem (S-06). Skipped, with a note, when the reading isn't usable."""
    exp = (intent or {}).get('lend_expect')
    if not exp or res.get('ok') is not True:
        return
    before, at = intent.get('lend_before'), intent.get('lend_before_at')
    try:                                              # the chain's time to the millisecond (whole seconds skipped a reading done 1.4 s before)
        tu = str(tx_detail(digest).get('time_utc') or '')
        ms = re.search(r'\.(\d+)', tu)
        t_tx = calendar.timegm(time.strptime(tu[:19], '%Y-%m-%dT%H:%M:%S')) + (float('0.' + ms.group(1)) if ms else 0.0)
    except Exception:
        t_tx = None
    if before is None or at is None or t_tx is None or at >= t_tx:
        res.setdefault('notes', []).append('lent amount not checked (no reading from before the transaction)')
        return
    try:
        after = {'%s|%s' % (r['id'], lend_ntype(r.get('type') or cinfo('USDC')['type'])): float(r['lent']) for r in lend_positions_rows(intent['address'])}
    except Exception:
        res.setdefault('notes', []).append('lent amount not checked (positions not readable)')
        return
    want = {}
    for e in exp:
        k = '%s|%s' % (e['mk'], lend_ntype(e['type']))
        want[k] = want.get(k, 0.0) + float(e['amount'])
    for _ in range(3):                                # vault shares show up late (NAVI vaults took >10 s, live 2026-10-25): up to 3 more reads, 10 s apart
        if all(k in after for k in want):
            break
        time.sleep(10)
        try:
            after = {'%s|%s' % (r['id'], lend_ntype(r.get('type') or cinfo('USDC')['type'])): float(r['lent']) for r in lend_positions_rows(intent['address'])}
        except Exception:
            pass
    for k, amt in want.items():
        mk = LEND_MKTS.get(k.split('|')[0], k.split('|')[0])
        if k not in after:                            # that market isn't in the reading at all: say so, never guess a shortfall
            res.setdefault('notes', []).append('lent amount on %s not checked (market not in the positions reading)' % mk)
            continue
        got = after[k] - before.get(k, 0.0)
        if got < amt * 0.98 - 1e-9:
            res.setdefault('problems', []).append('only %s of the planned %s reached %s' % (dec(max(got, 0), 6), dec(amt, 6), mk))
            res['ok'] = False
        else:
            res.setdefault('notes', []).append('lent %s on %s ✓' % (dec(got, 6), mk))


def check_tx(digest, intent):
    """Compare the transaction on chain with the plan. Returns {'ok', 'problems', 'notes'}."""
    f = lend_cli('tx-facts', digest, timeout=60)
    if not f.get('ok'):
        raise RuntimeError(f.get('error') or 'chain read failed')
    norm = lambda a: re.sub(r'^0x0*', '', str(a or '').lower())
    ntype = lambda t: re.sub(r'^0x0*', '', str(t).split('::')[0].lower()) + '::' + '::'.join(str(t).split('::')[1:])
    problems, notes = [], []
    me = norm((intent or {}).get('address') or f.get('sender'))
    if not f.get('success'):
        problems.append('the transaction failed on chain (%s)' % (f.get('error') or '?'))
    if intent and norm(f.get('sender')) != me:
        problems.append('it was signed by %s, not the wallet the plan was for' % f.get('sender'))
    g = (intent or {}).get('guard') or {}
    out = {ntype(k): int(v) for k, v in (g.get('out') or {}).items()}
    sui = ntype('0x2::sui::SUI')
    out[sui] = out.get(sui, 0) + int(g.get('gas') or 100_000_000)
    need = {ntype(k): int(v) for k, v in (g.get('in') or {}).items()}
    for k, v in ((intent or {}).get('min_in') or {}).items():
        need[ntype(k)] = max(need.get(ntype(k), 0), int(v))
    paid, got, others = {}, {}, {}
    for b in f.get('balance_changes') or []:
        t, v = ntype(b['coin_type']), int(b['amount'])
        if norm(b['address']) == me:
            (paid if v < 0 else got)[t] = (paid if v < 0 else got).get(t, 0) + abs(v)
        elif v > 0:
            others.setdefault(norm(b['address']), {})[t] = others.get(norm(b['address']), {}).get(t, 0) + v
    sym = lambda t: t.split('::')[-1]
    credited = {k: dict(v) for k, v in others.items()}   # what each address received in full: the payment checks read this (a chain's send
                                                          # step pops its recipient from `others`, and the pay check then saw 0 — live test 2026-10-25)

    def units(t, v):                                  # 786 -> '0.000786' for USDC (it printed raw units)
        d = next((int(c['decimals']) for c in COINS().values() if ntype(c['type']) == t), None)
        return ('%.9f' % (v / 10 ** d)).rstrip('0').rstrip('.') if d is not None else str(v)
    if intent:
        for t, v in paid.items():
            if g.get('ctokens') and '::reserve::CToken<' in t:
                continue
            if v > out.get(t, 0):
                problems.append('it took %s %s from the wallet, the plan allowed %s' % (v, sym(t), out.get(t, 0)))
        for t, v in need.items():
            if got.get(t, 0) < v:
                problems.append('the wallet received %s %s, the plan promised at least %s' % (got.get(t, 0), sym(t), v))
        for k in (intent.get('sinks') or []):        # a chain's sends: the recipient must get at least the planned minimum
            if k['op'] == 'send':
                to, pt = norm(k['to']), ntype(intent.get('final_type') or '')
                if (others.get(to) or {}).get(pt, 0) < int(k.get('min') or 1):
                    problems.append('%s… got %s %s, the plan said at least %s' % (to[:8], (others.get(to) or {}).get(pt, 0), sym(pt), k.get('min')))
                others.pop(to, None)
        pays = g.get('pay') or []
        pays = pays if isinstance(pays, list) else [pays]        # several coins to one address (one transaction)
        for pay in pays:
            to, pt = norm(pay.get('address')), ntype(pay.get('coin_type'))
            if (credited.get(to) or {}).get(pt, 0) < int(pay.get('amount') or 0):
                problems.append('the recipient got %s %s, the plan said %s' % ((credited.get(to) or {}).get(pt, 0), sym(pt), pay.get('amount')))
        for pay in pays:
            others.pop(norm(pay.get('address')), None)
    rev = norm(((K().get('revenue') or {}).get('address')) or '')
    via_agg = pure_agg_swap(f.get('calls'))       # a PURE swap through a trusted aggregator (review pre-launch H1, 2026-10-28)
    for who, coins in others.items():                 # pools and protocol fee wallets: small, else a problem
        for t, v in coins.items():
            # measured against the most of this coin the plan moved — not only what the wallet itself paid or kept
            # (a chain's swap output never touches the wallet: a 0.02% router fee read as 4% of the leftover, 2026-10-23)
            vol = {ntype(k): int(v) for k, v in ((intent or {}).get('volume') or {}).items()}
            base = max(paid.get(t, 0), got.get(t, 0), out.get(t, 0), need.get(t, 0), vol.get(t, 0))
            if who == rev and rev:
                notes.append('Kivy fee %s %s' % (units(t, v), sym(t))); continue
            if base and v > base * OTHER_SHARE_MAX:
                problems.append('another address (%s…) received %s %s' % (who[:8], units(t, v), sym(t)))
            elif not base and v > 0 and not via_agg and who not in {norm(x) for x in ROUTER_FEE_WALLETS_SRV}:   # a coin the plan never mentions, to anyone but the router's fee wallet (audit nv #1)
                problems.append('another address (%s…) received %s %s that the plan never mentions' % (who[:8], units(t, v), sym(t)))
            elif not base and v > 0:                     # a coin only the route touched (trusted, 2026-10-25): noted so it stays visible
                notes.append('route paid %s %s to %s…' % (units(t, v), sym(t), who[:8]))
    for o in f.get('moved') or []:
        if norm(o['from']) == me and norm(o['to']) != me:
            problems.append('one of the wallet\'s objects went to %s' % o['to'])
    want = {'swap': 'router::', 'swaplend': 'router::', 'withdrawswap': 'router::', 'dust': 'router::'}.get((intent or {}).get('kind'))
    if (intent or {}).get('kind') == 'move' and intent.get('swap'):
        want = 'router::'
    proto = (intent or {}).get('protocol') or ''
    if (intent or {}).get('kind') == 'lend' and (intent or {}).get('action') in ('deposit', 'withdraw'):
        want = 'lending_market::' if proto.startswith('suilend') else None
    if want and not any(m.startswith(want) for m in f.get('modules') or []):
        problems.append('the planned protocol (%s) was not called' % want.rstrip(':'))
    if (intent or {}).get('kind') == 'compose':
        mods = f.get('modules') or []
        if intent.get('src_op') == 'perps_out' and not any(m.startswith('account::withdraw_collateral') for m in mods):
            problems.append('nothing was withdrawn from the perps account')
        if intent.get('src_op') == 'dust' and not any(m.startswith('router::') for m in mods):
            problems.append('the dust was not swapped (no router call)')
        for k in intent.get('sinks') or []:
            if k['op'] == 'lend':
                lt = k['market']
                dep = ('lending_market::deposit' if lt.startswith('suilend') else 'navi_vault::deposit' if lt in VAULTS or lt in SUI_VAULT_MKTS
                       else 'incentive_v3::')
                if not any(m.startswith(dep) for m in mods):
                    problems.append('nothing was lent on %s (no deposit call)' % LEND_MKTS.get(lt, lt))
            elif k['op'] == 'perps' and not any(m.startswith('account::deposit_collateral') for m in mods):
                problems.append('nothing was deposited into the perps account')
            elif k['op'] == 'keep' and intent.get('final_type') and got.get(ntype(intent['final_type']), 0) < 1:
                problems.append('the coin you kept never arrived in the wallet')
    if (intent or {}).get('kind') in ('move', 'swaplend') and intent.get('lend_to'):
        lt = intent['lend_to']
        dep = ('lending_market::deposit' if lt.startswith('suilend') else 'navi_vault::deposit' if lt in VAULTS or lt in SUI_VAULT_MKTS
               else 'incentive_v3::')
        if not any(m.startswith(dep) for m in f.get('modules') or []):
            problems.append('nothing was lent on %s (no deposit call)' % LEND_MKTS.get(lt, lt))
        ty, units = intent.get('lent_type'), int(intent.get('lent_units') or 0)
        if ty and units and got.get(ntype(ty), 0) > max(units * 0.01, 1):
            problems.append('%s %s went to the wallet instead of being lent' % (got.get(ntype(ty), 0), sym(ntype(ty))))
    if not intent:
        notes.append('no plan on record (server restarted?) — checked the basics only')
    moves = []                                        # what really moved, for the stats pipeline
    for b in f.get('balance_changes') or []:
        try:
            sy, amt = human_units(b['coin_type'], abs(int(b['amount'])))
            moves.append({'who': who_is(b['address'], me), 'coin': sy, 'amt': ('+' if int(b['amount']) > 0 else '−') + amt})
        except Exception:
            pass
    return {'ok': not problems, 'problems': problems, 'notes': notes, 'moves': moves[:12], 'status': 'SUCCESS' if f.get('success') else 'FAILED'}


_VERIFYING = set()                                   # digests being checked now: one checker each, a few at a time (audit V09)
_VERIFY_SLOTS = threading.BoundedSemaphore(4)


def human_units(t, v):
    """Raw coin units -> (symbol, plain amount) for the stats pipeline."""
    nt = lambda x: re.sub(r'^0x0*', '0x', str(x or '').lower())
    for sym, c in list(ecoins().items()):
        if nt(c['type']) == nt(t):
            return sym, ('%.9f' % (int(v) / 10 ** int(c['decimals']))).rstrip('0').rstrip('.')
    return str(t).split('::')[-1], str(v)


def who_is(addr, me):
    """'you' / 'Kivy' (treasury) / a short address, for the stats pipeline."""
    a = str(addr or '').lower()
    rev = str((K().get('revenue') or {}).get('address') or '').lower()
    nt = lambda x: re.sub(r'^0x0*', '', x)
    return 'you' if me and nt(a) == nt(str(me).lower()) else 'Kivy' if rev and nt(a) == nt(rev) else a[:6] + '…' + a[-4:]


def pipe_plan(it):
    """Kivy's plan in one line: the steps (service, amount, coin) and its fee."""
    steps = [' '.join(x for x in (a.get('service', '').replace('_', ' '), ('%g' % float(a['amount'])) if a.get('amount') else '', a.get('coin') or '') if x)
             for a in (it.get('actions') or [])]
    return {'steps': steps or [it.get('kind') or '?'], 'fee_sui': it.get('fee_sui') or 0, **({'router': it['router']} if it.get('router') else {})}


def unlogged_scan():
    """Every 10 min: a plan the user signed (or whose wallet reply got lost) but that the page never reported — tab closed,
    phone locked — is looked up on chain by its sender and checked like any other, so no signed transaction misses check 2.
    Nothing found: noted on the plan's row. Candidates that don't match the plan: an alert line for review_checks (2026-10-23)."""
    while True:
        time.sleep(600)
        scan_once()


def scan_once(min_age=600):
    q = 'query($a: SuiAddress!) { transactions(last: 10, filter: {sentAddress: $a}) { nodes { digest effects { timestamp } } } }'
    if True:
        for iid, it in list(_INTENTS.items()):
            try:
                t0 = it.get('signed_at')
                if not t0 or it.get('logged') or it.get('scanned') or time.time() - t0 < min_age or not ADDR_RE.match(it.get('address') or ''):
                    continue
                it['scanned'] = True
                _INTENTS.save(iid)
                nodes = ((gql(q, {'a': it['address']}).get('transactions') or {}).get('nodes')) or []
                cand = [n['digest'] for n in nodes if n.get('effects') and
                        calendar.timegm(time.strptime(n['effects']['timestamp'][:19], '%Y-%m-%dT%H:%M:%S')) >= t0 - 60]
                hit = None
                for dg in cand:
                    try:
                        if check_tx(dg, it).get('ok'):
                            hit = dg; break
                    except Exception:
                        pass
                if hit:
                    it['logged'] = hit
                    _INTENTS.save(iid)
                    kivy_diary_add(hit, '%s · found by the scan (the page never reported it)' % it.get('kind'))
                    verify_later(hit, iid)
                elif cand:
                    with open(CHECKS, 'a') as fh:
                        fh.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'digest': cand[-1], 'kind': it.get('kind'),
                                             'address': it.get('address'), 'ok': False,
                                             'problems': ['a plan was signed but never reported, and none of the wallet\'s %d transactions since then matches it' % len(cand)]}) + '\n')
                else:
                    metrics.pipe(iid, chain={'status': 'not found', 'moves': []})
            except Exception:
                pass


_VERIFY_WAIT = []                                     # checks that found the queue full: run as soon as a slot frees (audit nv #9)


def verify_later(digest, iid):
    """After a signed transaction: wait for it on chain, check it, record the verdict (never blocks the user)."""
    key = (digest, iid or '')                         # per plan: a check without a plan can't block the one with it (review 2026-10-24)
    if key in _VERIFYING:
        return
    if len(_VERIFYING) >= 32:                         # never silent: kivy_watch alerts on this line (review 2026-10-24)
        print('kivy check dropped: %s… (%d checks waiting)' % (digest[:10], len(_VERIFYING)), flush=True)
        if len(_VERIFY_WAIT) < 500 and (digest, iid) not in _VERIFY_WAIT:
            _VERIFY_WAIT.append((digest, iid))       # delayed, never lost
        return
    _VERIFYING.add(key)

    def run():
        with _VERIFY_SLOTS:
            try:
                work()
            finally:
                _VERIFYING.discard(key)
                if _VERIFY_WAIT:
                    try:
                        verify_later(*_VERIFY_WAIT.pop(0))
                    except IndexError:
                        pass

    def work():
        intent = _INTENTS.get(iid) if iid else None
        res = None
        for i in range(6):
            try:
                res = check_tx(digest, intent); break
            except Exception as exc:
                res = {'ok': None, 'problems': [], 'notes': ['could not read it yet: %s' % str(exc)[:80]]}
                time.sleep(5)
        try:
            lend_amount_check(intent, res, digest)    # S-06: the amount that reached each lending market, not just the call
        except Exception:
            pass
        try:                                          # Kivy's numbers: one PTB, its steps
            dd = tx_detail(digest)
            if dd and dd.get('status'):
                sender = str(dd.get('sender') or '').lower()
                try:
                    sent_at = calendar.timegm(time.strptime(str(dd.get('time_utc') or '')[:19], '%Y-%m-%dT%H:%M:%S'))
                except Exception:
                    sent_at = None
                # verified = THIS card's transaction: its wallet sent it, check 2 passed, it came after the card was built, and no
                # other digest already claimed the card — an old or unrelated transaction can't inflate the numbers (review F-03)
                ver = bool(intent) and sender == str(intent.get('address') or '').lower() and res.get('ok') is True \
                    and sent_at is not None and sent_at >= float(intent.get('at') or 0) - 120 and intent.get('verified_digest') in (None, digest)
                if ver and intent.get('verified_digest') != digest:
                    intent['verified_digest'] = digest
                    _INTENTS.save(iid)
                metrics.ptb(digest, sender, str(dd['status']).upper(), (intent or {}).get('kind') or 'unknown', (intent or {}).get('actions') if ver else [], ver,
                            gas=float(dd.get('fee_sui') or 0), fee=(intent or {}).get('fee_sui', 0) if ver else 0)
        except Exception:
            pass
        try:                                          # stats pipeline: the real result and check 2 on this plan's row
            metrics.pipe(iid, digest=digest, chain={'status': res.get('status'), 'moves': res.get('moves') or []},
                         check2={'ok': res.get('ok'), 'problems': res.get('problems') or [], 'notes': res.get('notes') or []})
        except Exception:
            pass
        rec = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'digest': digest, 'kind': (intent or {}).get('kind'),
               'address': (intent or {}).get('address'), **({'router': intent['router']} if (intent or {}).get('router') else {}), **res}
        try:
            with open(CHECKS, 'a') as fh:
                fh.write(json.dumps(rec) + '\n')
        except Exception:
            pass
        mark = (' · check ⚠️ ' + '; '.join(res['problems'])[:200] if res['ok'] is False else ' · check ? (chain not readable)' if not res['ok']
                else ' · check ✅' if intent else ' · check ? (no Kivy plan to compare)')   # unknown plan is never a green check (audit V10)
        for _ in range(6):                            # the log line may land a moment after us
            try:
                import fcntl
                with open(KIVY_DIARY, 'r+') as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    text = fh.read()
                    key = '[%s…]' % digest[:10]
                    i = text.find(key)
                    if i >= 0:
                        e = text.find('\n', i)
                        line_start = text.rfind('\n', 0, i) + 1
                        head = text[line_start:i]
                        if ' · check ? (no Kivy plan to compare)' in head and intent:   # the plan's own check wins
                            head = head.replace(' · check ? (no Kivy plan to compare)', '')
                        if ' · check ' not in head:
                            text = text[:line_start] + head.rstrip().rstrip('·').rstrip() + mark + ' · ' + text[i:]
                            fh.seek(0); fh.truncate(); fh.write(text)
                        return
            except Exception:
                pass
            time.sleep(5)
    import threading
    threading.Thread(target=run, daemon=True).start()


def kivy_diary_add(digest, label=''):
    """Kivy's own diary: one line per on-chain transaction made through Kivy, newest on top under its UTC date.
    Every fact comes from the chain (status, sender, coins, fee, time) — the page only supplies the digest and a label."""
    import fcntl
    if digest in _DIARY_SEEN and not label:
        return 'seen'
    if digest in _DIARY_SEEN or label:               # logged before without a plan: Kivy's own label replaces 'transaction'
        clean = re.sub(r'[^A-Za-z0-9 _.,:@$%/+()→≈…\-]', '', str(label))[:300]
        with open(KIVY_DIARY, 'a+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0); text = f.read()
            if digest[:10] + '…]' in text:
                _DIARY_SEEN.add(digest)
                new = re.sub(r'(?m)^(- \*\*[^*]+\*\* — \S+ · )transaction( · [^\n]*\[%s…\])' % re.escape(digest[:10]), lambda m: m.group(1) + clean + m.group(2), text, count=1)
                if new != text and clean:
                    f.seek(0); f.truncate(); f.write(new)
                return 'seen'
    d = tx_detail(digest)
    if not d or d.get('error') or not d.get('status'):
        return 'not on chain yet'
    sender = d.get('sender') or ''
    short = lambda a: (a[:6] + '…' + a[-4:]) if a and len(a) > 12 else (a or '?')
    moves = '; '.join('%s %s %s' % (m['amount'], m['coin'], ('→ ' + short(m['to'])) if m.get('to') and m.get('to') != sender
                                                       else ('out' if m.get('from') == sender else 'in'))
                      for m in (d.get('moves') or [])[:6]) or 'no coin moves'
    t = str(d.get('time_utc') or '')
    day, hm = (t[:10], t[11:16]) if len(t) >= 16 else (time.strftime('%Y-%m-%d', time.gmtime()), time.strftime('%H:%M', time.gmtime()))
    ok = '✅' if str(d['status']).upper() == 'SUCCESS' else '❌ ' + str(d['status'])
    label = re.sub(r'[^A-Za-z0-9 _.,:@$%/+()→≈…\-]', '', str(label or ''))[:300] or 'transaction'   # full step list (Activity detail, 2026-10-21); never '·' (it splits the line)
    line = '- **%s UTC** — %s · %s · %s · fee %s SUI · %s · [%s…](%s)' % (hm, short(sender), label, moves, d.get('fee_sui'), ok, digest[:10], d.get('explorer'))
    with open(KIVY_DIARY, 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0); text = f.read()
        if digest[:10] + '…]' in text:
            _DIARY_SEEN.add(digest); return 'seen'
        head = '# Kivy diary — on-chain transactions made through Kivy (newest on top)\n'
        body = text[len(head):] if text.startswith(head) else text
        sec = '## %s\n' % day
        body = body.replace(sec, sec + line + '\n', 1) if sec in body else sec + line + '\n\n' + body.lstrip('\n')
        # keep dates newest-first
        parts = re.split(r'(?m)^(?=## \d{4}-\d{2}-\d{2}\n)', body)
        parts = sorted([x for x in parts if x.strip()], key=lambda x: x[3:13], reverse=True)
        f.seek(0); f.truncate(); f.write(head + '\n' + ''.join(x if x.endswith('\n\n') else x.rstrip('\n') + '\n\n' for x in parts))
    _DIARY_SEEN.add(digest)
    return 'logged'


SPOT_NAME = {'BTC': 'xBTC'}                   # what the spot coin is really called on Sui
SPOT_IN = {'XBTC': 'BTC'}                     # typed / AI names -> Kivy's internal key


def disp(sym):
    return SPOT_NAME.get(sym, sym)


def disp_text(t):
    """Spot texts only: say xBTC, never plain BTC."""
    return re.sub(r'(?<![A-Za-z])BTC(?![A-Za-z])', 'xBTC', t) if isinstance(t, str) else t


def cap_usd():
    """Kivy's $ limit per swap / send / lend, or None when off (knowledge/kivy.json cap_usd: 0). 2026-10-23: no limit —
    the user approves every transaction in their own wallet."""
    try:
        v = float(K().get('cap_usd') or 0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def ui_cap():
    """The cap the cards use for sliders and Max: unlimited when there is none."""
    return cap_usd() or 1e12


_KC = {'m': None, 'v': None}
_PAGE = {}                                           # the built index page + its ETag, keyed on the files' mtimes


def K():
    """Re-read only when the file changes, so editing it still changes Kivy without a restart (it was parsed ~100 times a
    request — review W-06). A shallow copy each call: callers may add keys to it."""
    m = KNOWLEDGE.stat().st_mtime_ns
    if _KC['m'] != m:
        _KC['v'], _KC['m'] = json.loads(KNOWLEDGE.read_text()), m
    return dict(_KC['v'])


def sign(msg):
    return hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()


def token():
    exp = str(int(time.time()) + TTL)
    return exp + '.' + sign(exp)


def valid(tok):
    try:
        exp, sig = tok.split('.', 1)
        return hmac.compare_digest(sig, sign(exp)) and int(exp) > time.time()
    except Exception:
        return False


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


TATUM_KEY = HERE / 'data' / 'tatum.key'
_TATUM = {'skip': {}, 'ip': None, 'ip_t': 0.0}


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
    import math
    places = max(0, sig - 1 - int(math.floor(math.log10(abs(x)))))
    t = (('{:,.%df}' if commas else '{:.%df}') % min(places, 12)).format(x)
    return t.rstrip('0').rstrip('.') if '.' in t else t


def NET():
    return K().get('network') or 'mainnet'


def COINS():
    k = K()
    return k['coins_testnet'] if NET() == 'testnet' else k['coins']


# Every coin Suilend or NAVI lists (lending versions like USDsui / vSUI, other reserves, reward coins like BLUE):
# Kivy can swap, send and show these too (2026-10-20). A protocol listing is the filter — airdropped junk
# tokens are never listed, so they stay invisible and refused. Kivy's own coins keep their names (BTC = xBTC).
_ECO = {'t': 0, 'v': None}
ECO_ALIAS = {'XBTC': 'BTC', 'SUIETH': 'ETH', 'USDSUI': 'USDsui'}


def ecoins():
    """{SYM: {'type', 'decimals', 'icon'}} — Kivy's coins first, then every listed coin. Cached an hour."""
    if _ECO['v'] and time.time() - _ECO['t'] < 3600:
        return _ECO['v']
    norm = lambda t: re.sub(r'^0x0*', '0x', str(t or '').lower())
    v = {sym: {'type': c['type'], 'decimals': int(c['decimals']), 'icon': None} for sym, c in COINS().items()}
    types, names = {norm(c['type']) for c in v.values()}, {x.lower() for x in v}
    rows = []
    if NET() == 'mainnet':
        try:
            rows = lend_cli('known-coins', timeout=120).get('coins') or []
        except Exception:
            rows = []
    icons = Path(__file__).resolve().parent / 'static' / 'icons'
    for c in rows:
        sym = ECO_ALIAS.get(str(c.get('sym')).upper(), str(c.get('sym')))
        if not re.match(r'^[A-Za-z][A-Za-z0-9.]{1,11}$', sym) or re.search(r'points?$|^lp\b', sym, re.I):
            continue                                     # LP shares and points tokens aren't coins to trade
        if norm(c.get('type')) in types or sym.lower() in names or not re.match(r'^0x[0-9a-fA-F]+::\w+::\w+$', str(c.get('type'))):
            continue                                     # first listing wins (NAVI has two different WBTC)
        types.add(norm(c['type'])); names.add(sym.lower())
        icon = next(('/icons/%s.%s' % (sym, e) for e in ('png', 'svg') if (icons / ('%s.%s' % (sym, e))).exists()), None)
        v[sym] = {'type': c['type'], 'decimals': int(c['dec']), 'icon': icon}
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


def cinfo(sym):
    return COINS().get(sym) or ecoins()[sym]


def coin_icon(sym):
    """Kivy's own icon file for a coin ('/icons/USDsui.png'), or None."""
    k = ckey(sym) or sym
    icons = Path(__file__).resolve().parent / 'static' / 'icons'
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


def guard(out=None, recv=None, ctokens=False, pay=None, gas=GUARD_GAS_MIST):
    """What the approved action may do to the user's wallet. The browser simulates the final transaction
    and refuses to sign if it spends more than `out`, receives less than `recv`, spends other coins,
    or moves the user's objects to someone else (audit T11: a successful simulation is not enough)."""
    return {'out': out or {}, 'in': recv or {}, 'ctokens': ctokens, 'pay': pay, 'gas': str(gas)}


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
    d = json.load(urllib.request.urlopen('http://127.0.0.1:8799/%s?%s' % (cmd, q), timeout=timeout))
    if not d.get('ok'):
        raise RuntimeError(str(d.get('error'))[:200])
    return d


def rpc(method, params, timeout=15):
    """Sui JSON-RPC `method` on kivy.json's rpc — only a fallback now: GraphQL comes first (publicnode 503 and
    Mysten retiring JSON-RPC, 2026-10-23). Returns `result`."""
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    req = urllib.request.Request(K()['rpc'], data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'kivy/1'})
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    if 'error' in d and 'result' not in d:
        raise ValueError(str(d['error'])[:200])
    return d.get('result')


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
    norm = lambda t: re.sub(r'^0x0*', '0x', str(t or '').lower())
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
        a = perp_account(address)
        out['perps'] = float(a.get('collateral') or 0) if a.get('has') else 0.0
    except Exception:
        out['perps'] = 0.0
    return out


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


def _amt(v, coin, address):
    try:
        return float(v)
    except ValueError:
        w = word_amount(v, ckey(coin) or coin, all_balances(address) if address else None)
        return w if w is not None else 0.0


def reply(text, address=None):
    """Plain-rule order reader. Returns {'text', 'plan'?}. Never executes."""
    t = ' '.join(text.strip().split())
    low = t.lower()
    num = r'(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?|all|max|everything|half|a\s+quarter|a\s+third)'
    asset = r'([a-zA-Z]+)'
    m = (re.match(r'^(?:swap|convert|trade)\s+' + num + r'\s+' + asset + r'\s+(?:to|for|into)\s+' + asset, low)
         or re.match(r'^(?:buy)\s+' + asset + r'\s+with\s+' + num + r'\s+' + asset, low))
    if m and low.startswith('buy'):
        m = (m.group(2), m.group(3), m.group(1))
    elif m:
        m = (m.group(1), m.group(2), m.group(3))
    if m:
        a, b = m[1].upper(), m[2].upper()
        amt = _amt(m[0], a, address)
        return plan('swap', amt, a, b, holdings=all_balances(address) if address else None)
    m = re.match(r'^(?:send|transfer|pay)\s+' + num + r'\s+' + asset + r'\s+to\s+(.+)$', t, re.I)
    if m:   # 'to this wallet : 0x…' / 'to my friend alice.sui': take the address or name wherever it is
        tail = m.group(3)
        found = (re.search(r'0x[0-9a-fA-F]{64}\b', tail) or re.search(r'@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b', tail, re.I)
                 or re.search(r'@[a-z0-9-]+', tail, re.I) or re.match(r'(\S+)', tail))
        return plan('send', _amt(m.group(1), m.group(2).upper(), address), m.group(2).upper(), recipient=found.group(0), sender=address,
                    holdings=all_balances(address) if address else None)
    pro = market_in(low)
    # lending is USDC only, so the coin may be left out: 'lend all', 'lend half my usdc', 'withdraw everything'
    filler = r'(?:\s+(?:of\s+)?(?:my|the|it))?'
    # only a real coin name counts ('lend all SUI' -> USDC-only reply); filler like 'amount', 'money', 'of it' means USDC
    lend_coin = lambda g: g.upper() if g and g.upper() in K()['assets'] else 'USDC'
    m = re.match(r'^(?:lend|deposit|supply)\s+' + num + filler + r'(?:\s+([a-z]+))?\b', low)
    if m:
        c = lend_coin(m.group(2))
        return plan('lend', _amt(m.group(1), c, address), c,
                    holdings=balances(address) if address else None, protocol=pro)
    m = re.match(r'^(?:withdraw|redeem)\s+' + num + filler + r'(?:\s+([a-z]+))?\b', low)
    if m:
        if not address:
            return {'text': K()['no_wallet']}
        w = m.group(1)
        return plan('withdraw', 0.0 if w in WORD_AMOUNTS else float(w), lend_coin(m.group(2)), protocol=pro,
                    lent=lend_positions(address), all_=w in ('all', 'max', 'everything'))
    k = K()
    # what can you do?
    if re.search(r"\bwhat (can|do) (you|u|kivy) do\b|\bwhat are you\b|\bcapabilit|\bhow can you help\b", low):
        return {'text': ('Here is what I can do on Sui, with your own wallet:\n'
                         '• Swap — e.g. "swap 5 SUI to USDC". I find the best price; your wallet approves it.\n'
                         '• Send — e.g. "send 1 USDC to name.sui". I show the full address first; your wallet approves it.\n'
                         '• Any coin Suilend or NAVI lists (USDsui, vSUI, BLUE…) can be swapped and sent too.\n'
                         '• Lend / withdraw — any coin Suilend or NAVI lists; "swap USDC to USDsui then lend" and "withdraw USDsui as USDC" are one approval each.\n'
                         '• Perps — long or short with leverage on Aftermath (market, limit, stop, scale, TWAP); your wallet approves each order.\n'
                         '• Prices and your balances — connect a wallet with the icon at the bottom left.\n\n'
                         'Coins: %s.%s I never hold your keys, and nothing moves without your yes.')
                        % (', '.join(k['assets']), (' Swaps, sends and lending up to about $%g per order; perps have no cap.' % cap_usd()) if cap_usd() else '')}
    # which coins can I use? ("what assets can I play with", "which tokens do you support")
    if re.search(r'\b(assets?|coins?|tokens?)\b', low) and re.search(r'\b(what|which|available|support(ed)?|can i|play)\b', low):
        px = prices()
        rows = ['• %s  $%s' % (x, ('%.4f' % px[x]) if x in px else '—') for x in k['assets']]
        lines = '\n'.join(rows)
        example = k['assets'][2] if len(k['assets']) > 2 else 'USDC'
        return {'text': ('Kivy can play with these %d coins on Sui: %s\n%s\n\n'
                         'Swap any of them into another, send them, or lend them. '
                         '%sTry: "swap 5 SUI to %s".')
                        % (len(k['assets']), ', '.join(k['assets']), lines, ('Each swap, send or loan is up to about $%g. ' % cap_usd()) if cap_usd() else '', example)}
    # price questions: "SUI price", "price of DEEP", "how much is WAL"
    if re.search(r'\b(price|prices|how much is|worth|rate of)\b', low):
        px = prices()
        asked = [a for a in k['assets'] if re.search(r'\b%s\b' % a.lower(), low)] or k['assets']
        rows = ['• %s  $%s' % (a, ('%.4f' % px[a]) if a in px else '—') for a in asked]
        return {'text': k['price_intro'] + '\n' + '\n'.join(rows)}
    if re.search(r'\b(balance|balances|wallet|holdings?|how much do i have)\b', low):
        return {'text': balances_text(address) if address else k['no_wallet']}
    if re.search(r'\b(rules?|policy|limits?|cap)\b', low):
        rules = [r.replace('{cap}', '%g' % (cap_usd() or 0)).replace('{assets}', ', '.join(k['assets']))
                  .replace('{slippage}', '%g' % k['slippage_cap_pct']) for r in k['rules'] if cap_usd() or '{cap}' not in r]   # no limit: no limit rule
        return {'text': 'The rules I work under:\n' + '\n'.join('• ' + r for r in rules)}
    ans = faq(low, k)
    if ans:
        return {'text': ans}
    return {'text': k['help']}


def faq(low, k):
    """Best FAQ answer from the knowledge file, by whole-word/phrase key matches."""
    best, score = None, 0
    words = ' ' + re.sub(r'[^a-z0-9$% -]+', ' ', low) + ' '
    for item in k.get('faq') or []:
        hits = sum(len(key) for key in item['keys'] if ' %s ' % key in words)
        if hits > score:
            best, score = item['answer'], hits
    if best and '{kivy_fee}' in best:
        kf = kivy_fee()                                  # the real fee, always said (it said nothing once the % fees went to 0, 2026-10-23)
        best = best.replace('{kivy_fee}', (' Kivy\'s own fee: simple transactions and all sends are free; a multi-step transaction (several actions in one approval) '
                                           'includes %g SUI, shown on its card. Asking me is free.' % kf['sui']) if kf else ' Kivy itself charges no fee.')
    if best == '@help':
        return k['help']
    if best == '@assets':
        return ('I work with these coins: %s — plus every coin Suilend or NAVI lists (USDsui, vSUI, haSUI, sSUI, LBTC, wBTC, BLUE, '
                'NAVX…): swap, send, lend, and withdraw as another coin in one approval. Anything unlisted I refuse.') % ', '.join(k['assets'])
    return best


_RCP = {}


def recipient_check(q, sender=''):
    """The Send card's live check of a recipient (2026-10-23): 'ok' (a .sui name that resolves, or an address that has
    been used), 'new' (a valid address with no activity yet — allowed: new wallets are real), 'bad' (unknown name, not an
    address, the sender itself). Cached one minute."""
    q = str(q or '').strip()
    key = (q.lower(), sender.lower())
    hit = _RCP.get(key)
    if hit and time.time() - hit[0] < 60:               # a minute: a name that moves shows its new address fast (audit nv #6)
        return hit[1]
    looks_name = bool(re.search(r'\.sui$|^@', q, re.I))
    to = resolve_recipient(q)
    if not to:
        out = {'state': 'bad', 'why': "No such .sui name" if looks_name else 'Not a valid address or .sui name'}
    elif sender and to == sender.lower():
        out = {'state': 'bad', 'address': to, 'why': 'That is the sending account itself'}
    else:
        used = None
        try:
            d = gql('query($a: SuiAddress!) { transactions(first: 1, filter: {affectedAddress: $a}) { nodes { digest } } }', {'a': to})
            used = bool(((d.get('transactions') or {}).get('nodes') or []))
        except Exception:
            pass
        out = {'state': 'ok' if (used or looks_name or used is None) else 'new', 'address': to, 'name': q if looks_name else None}
    if len(_RCP) > 2000:
        _RCP.clear()
    _RCP[key] = (time.time(), out)
    return out


def resolve_recipient(r):
    """0x address as-is; a SuiNS name (alice.sui / @alice) via GraphQL (JSON-RPC fallback). None if invalid."""
    r = (r or '').strip()
    if ADDR_RE.match(r):
        return r.lower()
    name = r.lower()
    if name.startswith('@'):
        name = name[1:] + '.sui'
    if not re.match(r'^[a-z0-9-]+(\.[a-z0-9-]+)*\.sui$', name):
        return None
    try:
        try:                                             # GraphQL first, gRPC (via kivy-lend) as the fallback on mainnet
            d = gql('query($n: String!) { address(name: $n) { address } }', {'n': name})
            out = (d.get('address') or {}).get('address')
        except Exception:
            if NET() != 'mainnet':
                raise
            out = None
        if not out and NET() == 'mainnet':
            try:
                out = grpc_backup('grpc-name', '0x0', name if name.endswith('.sui') else name.lstrip('@') + '.sui', timeout=10).get('address')
            except Exception:
                out = None
        return out.lower() if out and ADDR_RE.match(out) else None
    except Exception:
        return None


GASLESS = {'USDC'}          # Sui gasless stablecoin transfers (protocol 125, May 2026); Kivy's coins only
GASLESS_MIN = 0.01


def gasless_send(coin, amt):
    return NET() == 'mainnet' and coin in GASLESS and amt >= GASLESS_MIN


LEND_DIR = KIVY_HOME / 'lend'
PROTOS = {'suilend': 'Suilend Main Market', 'suilend-ember': 'Suilend Ember', 'suilend-matrixdock': 'Suilend Matrixdock',
          'navi': 'NAVI', 'navi-highyield': 'NAVI High Yield', 'navi-prime': 'NAVI Prime'}
VAULTS = {'navi-highyield', 'navi-prime'}
# most specific first: 'suilend ember' must not stop at 'suilend'
MARKET_ALIASES = [('suilend-matrixdock', r'matri\w*(?:\s*do[ckx]\w*)?'), ('suilend-ember', r'emb[ae]r'),
                  ('navi-highyield', r'high\s*yield'), ('navi-prime', r'prime'),
                  ('navi', r'navi'), ('suilend', r'suilend\s+main'), ('suilend', r'suilend'), ('suilend', r'main')]


def market_in(text):
    for key, pat in MARKET_ALIASES:
        if re.search(r'\b' + pat + r'\b', text, re.I):
            return key
    return None


def market_only(text):
    """The market the message names when it names nothing else ('suilend', 'lend on navi prime')."""
    t = re.sub(r'^\s*(?:lend\s+(?:on|with|in|via|to|into)\s+|use\s+|deposit\s+(?:on|in|into|to)\s+)', '', text.strip(), flags=re.I)
    t = re.sub(r'[.!?]+$', '', t).strip().lower()
    t = re.sub(r'\b(usdc|market|vault|the)\b', ' ', t)
    key = market_in(t)
    if not key:
        return None
    left = t
    for k, pat in MARKET_ALIASES:
        left = re.sub(r'\b' + pat + r'\b', ' ', left, flags=re.I)
    return key if not left.strip() else None
_LEND = {'rates': (0, None), 'pos': {}}


_CLI_TTL = {'perp-positions': 15, 'rewards': 30}      # per-wallet reads one chat or panel asked for 2-3 times (W-03)
_CLI_CACHE = {}


def lend_cli(*args, timeout=150):
    """Ask Kivy's lending builder (kivy-lend.service, localhost only). It never holds a key. A wallet's perps positions and
    rewards are reused for 15 / 30 s — never after that wallet signs a transaction (mark_dirty)."""
    from urllib.parse import urlencode
    import copy
    cmd, rest = args[0], [str(x) for x in args[1:]]
    key = (cmd, tuple(rest)) if cmd in _CLI_TTL and len(rest) == 1 else None
    if key:
        hit = _CLI_CACHE.get(key)
        if hit and time.time() - hit[0] < _CLI_TTL[cmd] and hit[0] > _DIRTY.get(rest[0].lower(), 0):
            return copy.deepcopy(hit[1])
    url = 'http://127.0.0.1:8799/%s?%s' % (cmd, urlencode([('a', x) for x in rest]))
    with urllib.request.urlopen(url, timeout=timeout) as r:
        out = json.load(r)
    if key and isinstance(out, dict) and out.get('ok') is not False:
        if len(_CLI_CACHE) > 5000:
            _CLI_CACHE.clear()
        _CLI_CACHE[key] = (time.time(), copy.deepcopy(out))
    return out


def lend_rates():
    t, v = _LEND['rates']
    if v and time.time() - t < 300:
        return v
    out = lend_cli('rates').get('rates') or []
    v = {r['market']: r for r in out if 'market' in r and 'total_apr' in r}
    _LEND['rates'] = (time.time(), v)
    return v


def lend_collateral():
    """{market: {'coins': [{sym, icon, ltv, liq, usd}], 'n'} | {'vault': True, 'risk'}} — what borrowers post in each market,
    biggest first. A lender's risk is that collateral: if it falls faster than liquidations sell it, lenders take the loss
    (2026-10-23). Read-only; cached 10 min."""
    t, v = _LEND.get('collat') or (0, None)
    if v and time.time() - t < 600:
        return v
    v = lend_cli('collateral').get('markets') or {}
    for m in v.values():
        for c in m.get('coins') or []:
            c['icon'] = coin_icon(c['sym']) or c.get('icon')     # Kivy's own icon when it has one
    _LEND['collat'] = (time.time(), v)
    return v


LEND_WORD_RE = re.compile(r'\b(?:lend\w*|ember|matrix\s*dock|suilend|navi|vaults?|collateral)\b', re.I)   # lending words, not finance terms (APR, APY, yield); a bare 'market' could be perps
LEND_MKT_NAMES = [('suilend-ember', r'\bember\b'), ('suilend-matrixdock', r'\bmatrix\s*dock\b'), ('navi-highyield', r'\bhigh\s*yield\b'),
                  ('navi-prime', r'\bprime\b'), ('suilend', r'\bsuilend(?:\s+main)?\b'), ('navi', r'\bnavi(?:\s+main)?\b')]


def lend_ctx(msg, history):
    """Is this question about the LENDING markets? Its own words, or a lending table just shown (not the perps markets)."""
    if LEND_WORD_RE.search(msg):
        return True
    if re.search(r'\bmarkets?\b', msg, re.I):
        last = next((h.get('text') or h.get('content') or '' for h in reversed(history or []) if (h.get('role') in ('assistant', 'kivy', 'bot'))), '')
        return bool(re.search(r'lending market', str(last), re.I))
    return False


def lend_risk_reply(msg, address, history):
    """"What's the difference between the markets?", "which is safest?", "why does Ember pay more?": answered from the
    markets' REAL collateral, never from the AI's memory (it once said Ember takes DEEP and WAL — it takes only eEARN)."""
    col = lend_collateral()
    if not col:
        return None
    named = [k for k, rx in LEND_MKT_NAMES if re.search(rx, msg, re.I)]
    if 'suilend' in named and any(k.startswith('suilend-') for k in named) and not re.search(r'\bmain\b', msg, re.I):
        named.remove('suilend')                      # "suilend ember" names Ember, not Suilend Main
    if 'navi' in named and any(k.startswith('navi-') for k in named) and not re.search(r'\bmain\b', msg, re.I):
        named.remove('navi')                         # "navi prime" names the Prime vault, not NAVI's main market
    keys = named if len(named) >= 1 else ['suilend-ember', 'suilend-matrixdock', 'suilend', 'navi']
    lines = []
    for k in keys:
        c = col.get(k) or {}
        nm = LEND_MKTS.get(k, k)
        if c.get('vault'):
            lines.append('• %s: a curator vault — its collateral isn\'t published.' % nm)
            continue
        coins = c.get('coins') or []
        if not coins:
            continue
        top = [x['sym'] for x in coins[:5]]
        lines.append('• %s: %s' % (nm, ('only ' + ' and '.join(top)) if len(coins) <= 2 else '%d coins — %s and more' % (len(coins), ', '.join(top))) + '.')
    if not named and any(col.get(k, {}).get('vault') for k in ('navi-highyield', 'navi-prime')):
        lines.append('• NAVI High Yield and Prime: curator vaults — their collateral isn\'t published.')
    head = ('No lending market is risk-free. ' if re.search(r'\bsafe', msg, re.I) else '') + \
        ('What matters is the collateral borrowers post there' if len(named) == 1 else 'The difference is the collateral borrowers post in each market') + \
        ' — that is the lender\'s risk: if it falls faster than it can be liquidated, lenders can lose part of their deposit.'
    tail = 'One or two niche coins means concentrated risk, usually paid with a higher rate; many large coins spread it. The Collateral column shows it for every market.'
    out = {'text': head + '\n' + '\n'.join(lines) + '\n' + tail}
    try:
        t = widget_reply('lending markets', address, history)
        if t and t.get('widget'):
            out['widget'] = t['widget']
    except Exception:
        pass
    return out


def collat_ai():
    """For the AI: each market's collateral (biggest first, with LTV / liquidation %), so it can explain the risk difference."""
    try:
        out = {}
        for k, m in lend_collateral().items():
            name = LEND_MKTS.get(k, k)
            out[name] = 'curator vault: a curator decides where it lends; its collateral is not published' if m.get('vault') else \
                [('%s (LTV %s%%, liquidation %s%%, $%s posted)' % (c['sym'], c['ltv'], c['liq'], format(c['usd'], ','))) for c in (m.get('coins') or [])[:8]]
        return out
    except Exception:
        return 'not available right now'


def collat_row(mkt):
    """The table's Collateral cell: every collateral coin, biggest first, or a curator vault (no list published)."""
    try:
        c = lend_collateral().get(mkt) or {}
    except Exception:
        return None
    if c.get('vault'):
        return {'vault': True, 'risk': c.get('risk')}
    coins = c.get('coins') or []
    return {'coins': [{'sym': x['sym'], 'icon': x.get('icon')} for x in coins]} if coins else None   # all of them: the cell shows a few, the panel all


def lend_rates_all():
    """Yield of every Kivy coin on Suilend (3 markets) and NAVI (+ its USDC vaults). Read-only; cached 5 min."""
    t, v = _LEND.get('rates_all') or (0, None)
    if v and time.time() - t < 300:
        return v
    v = [r for r in (lend_cli('rates-all').get('rates') or []) if (r.get('market') in PROTOS or r.get('vault')) and r.get('asset')]
    icons = Path(__file__).resolve().parent / 'static' / 'icons'
    for r in v:                                          # LSTs (vSUI, haSUI, sSUI): the icon Kivy keeps locally
        tok = r.get('token')
        r['icon'] = next(('/icons/%s.%s' % (tok, e) for e in ('png', 'svg') if tok and re.match(r'^[A-Za-z]{2,12}$', tok) and (icons / ('%s.%s' % (tok, e))).exists()), None)
    _LEND['rates_all'] = (time.time(), v)
    return v


# Every coin each market lists can be lent, not just USDC (2026-10-20). PROTOS stays the USDC list;
# NAVI's SUI vaults are SUI-only markets.
SUI_VAULT_MKTS = {'navi-sui-yield': 'NAVI SUI High Yield', 'navi-sui-prime': 'NAVI SUI Prime'}
LEND_MKTS = dict(PROTOS, **SUI_VAULT_MKTS)
# loose on purpose (2026-10-20: "swap usdc to usdsui then lend all of it in best amrket" fell through to the AI)
SWAPLEND_RE = re.compile(r'\b(?:swap|convert|change|exchange)\s+(?:([\d.]+|all|half|max)\s+)?(?:(?:my|of|the)\s+)*([A-Za-z]{2,10})\s+(?:to|for|into)\s+'
                         r'([A-Za-z]{2,10})\b(.*?\b(?:lend|supply|deposit|earn)\b(.*))$', re.I)
WCOIN_RE = re.compile(r'^\s*(?:withdraw|redeem|take\s+(?:back|out))\s+(?:([\d.]+|all|half|max)\s+)?(?:(?:my|of|the)\s+)*([A-Za-z]{2,10})'
                      r'(?:\s+(?:from|on|in|at)\s+(.+?))?\s*[?.!]*\s*$', re.I)


def lend_token(word):
    """A coin Kivy can lend, spelled the way the markets list it ('vsui' -> 'vSUI', 'xbtc' -> 'BTC'); None if not one."""
    w = str(word or '').strip()
    up = SPOT_IN.get(w.upper(), w.upper())
    if up in K()['assets']:
        return up
    try:
        toks = [r['token'] for r in lend_rates_all() if r.get('token')]
    except Exception:
        return None
    hit = next((t for t in toks if t == w), None) or next((t for t in toks if t.lower() == w.lower()), None)
    return {'USDSUI': 'USDsui'}.get(hit, hit) if hit else None


def lend_rows_for(coin):
    """Rate rows for one coin, best first. A Kivy coin's own rows (SUI, not vSUI), or one version's rows."""
    rows = lend_rates_all()
    if coin in K()['assets']:
        out = [r for r in rows if r['asset'] == coin and not r.get('token')]
    else:
        out = [r for r in rows if str(r.get('token') or '').lower() == coin.lower()]
        exact = {r['market'] for r in out if r.get('token') == coin}
        out = [r for r in out if r.get('token') == coin or r['market'] not in exact]
    return sorted((r for r in out if not r.get('rate_only')), key=lambda r: -float(r['total_apr']))   # Volo: rates only


def lend_pick(rows, where, coin):
    """The market row a message names ('on navi prime'), or the best one when it names none (or says 'best')."""
    w = (where or '').strip().lower()
    if not w or re.search(r'\bbest\b|\bhighest\b|\btop\b', w):
        fam = re.search(r'\b(suilend|navi)\b', w)            # "the best Suilend market": the best one of that protocol
        pick = [r for r in rows if r['market'].startswith(fam.group(1)) and not r.get('rate_only')] if fam else []
        return (pick or rows or [None])[0]
    for r in rows:
        if w in (LEND_MKTS.get(r['market'], '').lower(), str(r.get('name') or '').lower()):
            return r
    key = market_in(w)
    if coin == 'SUI':                                      # SUI's vaults share their names with the USDC vaults
        key = {'navi-highyield': 'navi-sui-yield', 'navi-prime': 'navi-sui-prime'}.get(key, key)
    return next((r for r in rows if r['market'] == key), None)


def coin_px(coin):
    """USD price for the cap check: a Kivy coin's own, or (vSUI, LBTC, USDsui…) its base coin's."""
    px = prices()
    c = SPOT_IN.get(coin, coin)
    if px.get(c):
        return float(px[c])
    try:
        t = cinfo(ckey(coin) or coin)['type']
        own = token_prices([t], max_age=600).get(t)
        if own:
            return float(own)
    except Exception:
        pass
    try:
        base = next((r['asset'] for r in lend_rates_all() if r.get('token') and r['token'].lower() == coin.lower()), None)
    except Exception:
        base = None
    return float(px.get(base) or 0) or None


def coin_change(coin):
    """24h price move, % — Bluefin's for Kivy's coins; a staked or bridged coin (vSUI, LBTC) moves with its base coin; a dollar coin is 0."""
    ch = _PX.get('c') or {}
    c = SPOT_IN.get(coin, coin)
    if c in ch:
        return ch[c]
    if c == 'USDC' or re.search(r'usd', c, re.I):
        return 0.0
    try:
        base = next((r['asset'] for r in lend_rates_all() if r.get('token') and r['token'].lower() == coin.lower()), None)
    except Exception:
        base = None
    return ch.get(base)


def spendable(coin, holdings):
    """What the wallet can put in of a Kivy coin (a little SUI kept back for gas)."""
    have = float((holdings or {}).get(coin) or 0)
    return max(0.0, have - max(float(K()['gas_sui']) * 3, 0.05) - fee_reserve()) if coin == 'SUI' else have   # room for gas (+ Kivy's fee)


def spend_check(coin, amt, address):
    """Cap and balance for coin Kivy is about to spend (lend, or swap then lend). None = fine, else why not."""
    if not (math.isfinite(amt) and amt > 0):
        return 'The amount has to be more than zero.'
    p, cap = coin_px(coin), cap_usd()
    if cap and not p:
        return "I can't get a live %s price right now, so I can't check the $%.0f cap. Try again in a minute." % (coin, cap)
    if cap and amt * p > cap * 1.0001:
        return "That's about $%.2f, over my $%.0f cap. Try a smaller amount." % (amt * p, cap)
    hb = dict(all_balances(address) or {})
    for k, v in xw_extra(address).items():            # money an earlier step of a cross-account plan sends here
        hb[k] = float(hb.get(k) or 0) + float(v)
    if amt > spendable(coin, hb) + 1e-12:
        if coin == 'SUI':
            return ('You can use up to %s SUI — Kivy keeps a little SUI (%s) in your wallet for the network fee%s. Try %s or less.'
                    % (dec(spendable(coin, hb)), dec(0.05 + fee_reserve()), (' and Kivy\'s %s SUI fee' % dec(fee_reserve())) if fee_reserve() else '', dec(spendable(coin, hb))))
        return 'Your wallet has %s %s, not enough for %s.' % (dec(float(hb.get(coin) or 0)), coin, dec(amt))
    return None


def lend_card(coin, where=None, address=None, amt=None, src=None):
    """Lend any listed coin: a slider in that coin, or — when the wallet has none of it — swap another coin
    into it and lend, in one transaction (2026-10-20)."""
    rows = lend_rows_for(coin)
    if not rows:
        return {'text': "%s isn't listed on Suilend or NAVI right now." % coin}
    r = lend_pick(rows, where, coin)
    if not r:
        return {'text': '%s is not lendable on %s. It is on: %s.' % (coin, where, ', '.join(LEND_MKTS.get(x['market'], x['name']) for x in rows))}
    if not address:
        return {'text': K()['no_wallet']}
    key, name, apr = r['market'], LEND_MKTS.get(r['market'], r['name']), round(float(r['total_apr']), 2)
    if r.get('token') and coin not in K()['assets']:
        coin = {'USDSUI': 'USDsui'}.get(r['token'], r['token'])       # one spelling on screen; the builder matches either
    hold, cap = all_balances(address) or {}, ui_cap()
    px = {c: (prices().get(c) or coin_px(c) or 0) for c in hold if float(hold.get(c) or 0) > 0}
    p = coin_px(coin) or 0
    have = spendable(coin, hold)
    if have * p >= 0.01 and not src:
        return {'text': 'Slide to choose how much %s to lend on %s.' % (disp(coin), name),
                'widget': {'type': 'lend', 'coin': coin, 'protocol': key, 'name': name, 'apr': apr, 'px': p, 'lend_fee_pct': rev_lend_pct(),
                           'balance': have, 'max': round(min(have, cap / p) if p else have, 9), 'cap': cap, 'preset': amt or 0}}
    srcs = {c: {'bal': spendable(c, hold), 'px': float(px.get(c) or 0)} for c in px
            if c != coin and float(px.get(c) or 0) > 0 and spendable(c, hold) * float(px.get(c) or 0) >= 0.01}
    if not srcs:
        return {'text': 'You have no %s, and no other coin to swap into it. Deposit some first (tap your balance for your address).' % disp(coin)}
    fam = r['asset'] if r['asset'] != coin else None
    pick = src if src in srcs else fam if fam in srcs else 'USDC' if 'USDC' in srcs else max(srcs, key=lambda c: srcs[c]['bal'] * srcs[c]['px'])
    return {'text': ('You have no %s, so Kivy swaps your %s into it and lends it on %s in one transaction: one approval, and if any step fails nothing moves.'
                     % (disp(coin), disp(pick), name)) if not src else
                    'Swap %s to %s, then lend it on %s, in one transaction: one approval.' % (disp(pick), disp(coin), name),
            'widget': {'type': 'swaplend', 'to': coin, 'from': pick, 'protocol': key, 'name': name, 'apr': apr, 'px_to': p, 'fee_pct': rev_fee_pct(), 'lend_fee_pct': rev_lend_pct(),
                       'sources': srcs, 'cap': cap, 'preset': amt or 0}}


_DIRTY = {}             # address -> when Kivy last saw it sign a transaction (its positions/balances changed)


def mark_dirty(addr):
    """A transaction from this wallet landed: drop its cached positions and balances so the next read is new."""
    a = str(addr or '').lower()
    if a:
        _DIRTY[a] = time.time()
        for k in [k for k in _BAL if a in (k if isinstance(k, tuple) else (k,))]:
            _BAL.pop(k, None)


def pos_cached(hit, addr, fresh):
    """A cached position read is good for 30 s (10 s when the answer decides an amount, e.g. a withdrawal), and never
    once the wallet has signed a transaction since (scale: 200k users a day would otherwise mean ~7M chain calls)."""
    return bool(hit) and hit[0] > _DIRTY.get(str(addr or '').lower(), 0) and time.time() - hit[0] < (10 if fresh else 30)


_POS_LOCKS = {}


def lend_positions_all(addr, fresh=False):
    """[{market, token, asset, type, amount}] — every coin this wallet has lent. Cached 30 s. One read at a time per wallet:
    the wallet panel asks for USDC and other coins at once, and both are served by the same read (W-04)."""
    hit = _LEND.setdefault('posall', {}).get(addr)
    if pos_cached(hit, addr, fresh):
        return hit[1]
    lock = _POS_LOCKS.setdefault(addr, threading.Lock())
    if len(_POS_LOCKS) > 5000:
        _POS_LOCKS.clear()
    with lock:
        hit = _LEND['posall'].get(addr)              # someone else just read it while we waited
        if hit and time.time() - hit[0] < 5 and hit[0] > _DIRTY.get(str(addr or '').lower(), 0):
            return hit[1]
        v = lend_cli('positions-all', addr).get('positions') or []
        _LEND['posall'][addr] = (time.time(), v)
    return v


def other_lent(addr, fresh=False):
    """Lent coins other than USDC, worth at least a cent, as withdraw rows."""
    out = []
    try:
        for x in lend_positions_all(addr, fresh):
            tok = {'USDSUI': 'USDsui'}.get(x.get('token'), x.get('token'))
            if not tok or tok == 'USDC' or x['market'] not in LEND_MKTS:
                continue
            p = token_prices([x['type']]).get(x['type']) or coin_px(tok) or 0
            if float(x['amount']) * p < 0.01:
                continue
            apr = next((float(r['total_apr']) for r in lend_rows_for(tok) if r['market'] == x['market']), 0)
            out.append({'id': x['market'], 'name': LEND_MKTS[x['market']], 'coin': tok, 'type': x.get('type'), 'px': p, 'total': round(apr, 2), 'icon': coin_icon(tok),
                        'lent': round(float(x['amount']), 9), 'ask': 'withdraw %s from %s' % (tok, LEND_MKTS[x['market']])})
    except Exception:
        pass
    return out


def to_opts(coin):
    return [coin] + [a for a in K()['assets'] if a != coin]


# ── Move: withdraw a lent coin, swap it if needed, and lend it on another market — ONE transaction (2026-10-20) ──
MOVE_RE = re.compile(r'^\s*(?:withdraw|take|pull|move|shift)\s+(?:(all|half|max|[\d.]+)\s+(?:of\s+)?)?(?:(?:my|the)\s+)?([A-Za-z]{2,10})\s+'
                     r'(?:out\s+)?from\s+(.+?)\s*(?:,|\band\b|\bthen\b|&|\bto\b)\s*(.*)$', re.I)


def move_parse(msg):
    """{'coin', 'amount', 'src', 'to', 'where'} for "withdraw X from A (and swap it to Y) then lend it on B" or "move X from A to B"."""
    m = MOVE_RE.match(msg)
    if not m or re.search(r'\bwallet\b', m.group(3), re.I) or CHAIN_PERPS.search(msg):
        return None                                   # from the wallet, or into perps: the chain reader handles it
    rest = m.group(4)
    lend = re.search(r'\b(?:lend|supply|deposit|put|park|earn)\b(?:\s+(?:it|them|that|all|everything))*(?:\s+(?:in|on|at|to|into|with)\s+(.+?))?\s*[?.!]*\s*$', rest, re.I)
    plain_to = re.match(r'^\s*(.+?)\s*[?.!]*\s*$', rest) if re.match(r'^\s*move|^\s*shift', msg, re.I) or ' to ' in (' ' + m.group(0)[len(m.group(1) or '') :]) else None
    if not lend and not (re.match(r'^\s*(?:move|shift)\b', msg, re.I) and plain_to and market_in(plain_to.group(1))):
        return None                                   # no lending step: not a move (withdraw / withdraw-as handle it)
    coin = lend_token(m.group(2))
    if not coin:
        return None
    sw = re.search(r'\b(?:swap|convert|change|turn)\b(?:\s+\w+){0,3}?\s+(?:to|into)\s+([A-Za-z]{2,10})\b|\bas\s+([A-Za-z]{2,10})\s*[?.!]*\s*$', rest, re.I)
    if sw and sw.group(2):
        rest = rest[:sw.start()]
    sg = (sw.group(1) or sw.group(2)) if sw else None
    to = (ckey(sg) or lend_token(sg)) if sg else coin
    if not lend and sw and sw.group(2):
        plain_to = re.match(r'^\s*(.+?)\s*[?.!]*\s*$', rest)
    where = (lend.group(1) if lend else plain_to.group(1)) or ''
    return {'coin': coin, 'amount': m.group(1), 'src': m.group(3), 'to': to, 'where': where}


def move_card(mv, address):
    coin, to = mv['coin'], mv['to']
    if not to:
        return {'text': "I can't swap into that coin. Which coin should it become?"}
    mine = [r for r in lend_positions_rows(address) if r['coin'].lower() == coin.lower()]
    src = mv['src'].strip().lower()
    fam = re.match(r'^(suilend|navi)\b(?!\s+(?:main|ember|matrix|high|prime|sui))', src)
    if fam:
        mine = [r for r in mine if r['id'].split('-')[0] == fam.group(1)]
    else:
        k = chain_market(src, coin)[0] or market_in(src)
        if k:
            mine = [r for r in mine if r['id'] == k]       # never fall back to another position (it picked NAVI main for 'NAVI SUI High Yield')
    if not mine:
        return {'text': 'You have no %s lent on %s through Kivy.' % (disp(coin), mv['src'])}
    row = max(mine, key=lambda r: r['lent'] * (r.get('px') or 0))
    rows = [r for r in lend_rows_for(to) if not (r['market'] == row['id'] and to == coin)]
    w = mv['where'].lower()
    wf = re.search(r'\b(suilend|navi)\b', w)
    spec = market_in(w) if re.search(r'\b(main|ember|matrix|high\s*yield|prime)\b', w) else None
    if to == 'SUI' and spec:
        spec = {'navi-highyield': 'navi-sui-yield', 'navi-prime': 'navi-sui-prime'}.get(spec, spec)
    pick = next((r for r in rows if r['market'] == spec), None) if spec else \
        next((r for r in rows if not wf or r['market'].split('-')[0] == wf.group(1)), None)
    if not pick:
        if not rows:                                  # the only market for it is where it already is
            return {'text': '%s can only be lent on %s, where it already is. To move it, swap it into another coin, e.g. "move my %s from %s to navi as USDC".'
                            % (disp(to), row['name'], disp(coin), mv['src'])}
        return {'text': '%s is not lendable on %s. It is on: %s.' % (disp(to), mv['where'] or 'that market',
                ', '.join(LEND_MKTS.get(r['market'], r['name']) for r in rows))}
    tgt = pick['market']
    card = withdraw_card(row, to if to != coin else None)
    wd = card['widget']
    wd.update(lend_to=tgt, lend_name=LEND_MKTS.get(tgt, pick['name']), lend_apr=round(float(pick['total_apr']), 2), to=to,
              lend_fee_pct=rev_lend_pct(), to_opts=[to])
    if mv['amount'] and mv['amount'].lower() in ('half',):
        wd['preset'] = round(row['lent'] / 2, 9)
    elif mv['amount'] and re.match(r'^[\d.]+$', mv['amount']):
        wd['preset'] = float(mv['amount'])
    else:
        wd['preset'] = row['lent']                      # "all of it" / no amount: the whole position
    steps = ['withdraw %s from %s' % (disp(coin), row['name'])] + (['swap it to %s' % disp(to)] if to != coin else []) + \
            ['lend it on %s at %.2f%% a year' % (wd['lend_name'], wd['lend_apr'])]
    card['text'] = 'Move in one approval: %s. If any step fails, nothing happens.' % ', '.join(steps)
    return card


# ── Chains: several steps in ONE transaction (2026-10-20: "can kivy handle several complex txn steps") ──
# A message like "use 5 SUI from my wallet, swap to USDC, lend half on NAVI and send the rest to bob.sui" becomes a
# spec for the builder's compose engine: one source (wallet coin or a withdrawal), up to 2 swaps, then 1-4 final steps
# (lend / send / keep) with amounts (a number, half / 75%, or the rest). Gaps are filled from the user's wallet when
# there is exactly one sensible answer; otherwise Kivy asks. Borrowing, perps and unknown coins are refused.
CHAIN_SPLIT = re.compile(r'\s*(?:;|:|,|\band\s+then\b|\bthen\b|\bafter\s+that\b|\band\b(?=\s+(?:then\s+)?(?:lend(?:ing)?|send(?:ing)?|swap(?:ping)?|deposit(?:ing)?|put|park(?:ing)?|stick|keep|leave|sell|buy|turn|convert|move|add|withdraw|claim|transfer|pay|give|use|lend|forward)\b)|(?<=\w)(?<!\bto)(?<!\bnot)(?<!\bthen)(?<!\bthat)(?<!\bthe)(?<!\bthis)(?<!\ba)(?<!\bmy)(?<!\beach)\s+(?=(?:swap|convert|deposit|lend|supply|park|put|send|keep|stake|transfer|forward)\s))\s*', re.I)   # "withdraw ssui from suilend swap all to vsui" (2026-10-21)
# withdraw also reads "remove unused margin" and "take my free margin out" (2026-10-23)
CHAIN_VERB = [('withdraw', r'withdr[ae]w\w*|pull|redeem|remove|free\s+up|cash\s+out|take\s+out|take\s+\S+(?:\s+\S+){0,2}\s+out|get\s+(?:\S+\s+){0,4}back'), ('move', r'move|shift|rebalance'),
              ('claim', r'claim|harvest'), ('send', r'send|transfer|pay|give|forward'), ('lend', r'lend(?:ing)?|deposit(?:ing)?|put|park(?:ing)?|stick|add|supply|stake'),
              ('keep', r'keep|leave|hold'), ('swap', r'swap|convert|sell|buy|turn|exchange|change|get'), ('use', r'use|take|grab|spend')]
CHAIN_REFUSE = re.compile(r'\b(borrow\w*|loan\s+against|leverage\w*|\d+(?:\.\d+)?x\b|long|short|open\s+(?:a\s+)?(?:\w+\s+)?(?:position|trade|perpetual\w*))\b', re.I)
CHAIN_BOT = re.compile(r'\b(?:bots?|trading\s+bot|auto[- ]?trad\w*|copy[- ]?trad\w*|dca|every\s+(?:day|week|hour|month)|daily|weekly|hourly|schedul\w*|recurring)\b', re.I)
CHAIN_PERPS = re.compile(r'\b(aftermath|perps?|perpetuals?|margin|collateral|futures\s+account)\b', re.I)
MKT_PHRASES = [('navi-sui-yield', r'navi\s+sui\s+high\s*yield|sui\s+high\s*yield'), ('navi-sui-prime', r'navi\s+sui\s+prime|sui\s+prime'),
               ('navi-highyield', r'(?:navi\s+)?high\s*yield'), ('navi-prime', r'(?:navi\s+)?prim(?:e)?'), ('suilend-ember', r'(?:suilend\s+)?ember'),
               ('suilend-matrixdock', r'(?:suilend\s+)?matrix\w*'), ('suilend', r'suilend\s+main(?:\s+market)?'), ('navi', r'navi\s+main'),
               ('fam:suilend', r'suilend'), ('fam:navi', r'navi')]


def chain_market(text, coin=None):
    """('key', None) for a named market, ('fam:navi', ..) for a protocol, ('best', ..) for 'best/somewhere good', else (None, text_wo)."""
    t = text.lower()
    hits = []
    for i, (key, pat) in enumerate(MKT_PHRASES):
        m = re.search(r'\b(?:%s)\b' % pat, t)
        if m:
            hits.append((m.start(), i, key, m))
    if hits:
        _, _, key, m = min(hits)                     # the first market named; the most specific pattern on a tie
        if coin == 'SUI' and key in ('navi-highyield', 'navi-prime'):
            key = {'navi-highyield': 'navi-sui-yield', 'navi-prime': 'navi-sui-prime'}[key]
        return key, text[:m.start()] + ' ' + text[m.end():]
    if re.search(r'\b(best|highest|somewhere\s+good|good\s+(?:place|market|yield)|top)\b', t):
        return 'best', text
    if re.search(r'\bthere\b', t):
        return 'there', text
    return None, text


def chain_coins(text):
    """Coins named in a clause (market names already cut out), plus words that look like unsupported coin tickers."""
    found, bad = [], []
    for w in re.findall(r'[A-Za-z][A-Za-z0-9.]*', text):
        w = w.rstrip('.')
        lw = w.lower()
        if lw in ('navi', 'suilend', 'ember', 'prime', 'main', 'matrixdock', 'high', 'yield', 'aftermath', 'sui.', 'apr', 'apy', 'tvl', 'ptb'):
            continue                                 # market / protocol words, not coins
        if lw in INTENT_STOP or lw in VERB_OF or lw in INTENT_FORMS or lw in ('wallet', 'proceeds', 'output', 'rest', 'remaining', 'other',
                                                                           'half', 'liquid', 'sui.', 'go', 'one', 'deposit', 'market', 'friend',
                                                                           'rewards', 'reward', 'coin', 'swap', 'router', 'whatever', 'comes'):
            continue
        k = COIN_NAMES.get(lw) or (ckey(w) if (len(lw) >= 3 or w.isupper()) else None)
        if k:
            if k not in found:
                found.append(k)
        elif w.isupper() and 3 <= len(w) <= 14 and not re.match(r'^0X', w):
            bad.append(w)                            # looks like a ticker Kivy doesn't know (DOGE, NOTAREALCOIN)
    return found, bad


def chain_amount(text, coin=None):
    """('units', n) | ('frac', f) | ('rest', None) | ('all', None) | (None, None) — amounts as a user writes them."""
    t = text.lower()
    if re.search(r'\b(the\s+)?(rest|remaining|remainder|leftover|other\s+half|what\'?s\s+left)\b', t):
        n = re.search(r'(?<![\w.])(\d+(?:\.\d+)?)(?![\w.%])', re.sub(r'0x[0-9a-f]+', ' ', t))
        return 'rest', (float(n.group(1)) if n else None)   # "the remaining 10": what they expect is left
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', t)
    if m:
        return 'frac', float(m.group(1)) / 100
    if re.search(r'\bhalf\b', t):
        return 'frac', 0.5
    fr = re.search(r'(?<![\w./])([1-9])\s*/\s*([2-9])(?![\w./])', t)      # "1/2", "2/3" (review multi-step scenarios 2026-10-25)
    if fr and int(fr.group(1)) < int(fr.group(2)):
        return 'frac', int(fr.group(1)) / int(fr.group(2))
    if re.search(r'\b(?:a|one)\s+third\b', t):
        return 'frac', 1 / 3
    if re.search(r'\b(?:a|one)\s+quarter\b', t):
        return 'frac', 0.25
    m = re.search(r'(?<![\w.])(\d+(?:\.\d+)?)(?![\w.%])', re.sub(r'0x[0-9a-f]+', ' ', t))
    if m:
        return 'units', float(m.group(1))
    if re.search(r'\b(all|everything|whole|max)\b', t):
        return 'all', None
    return None, None


# "use 0.4 usdc from my wallet: 0.1 to 0x…, 0.1 to 0x…, the rest on ember" — items of a list start with an amount and
# say no verb: a recipient makes it a send, a market a lend (2026-10-25)
LIST_ITEM_RE = re.compile(r'^\s*(?:(?:the\s+)?(?:rest|remainder|other\s+half)|half|a\s+(?:third|quarter)|\$?\d+(?:\.\d+)?\s*%?'
                          r'(?:\s+(?:of\s+)?(?:my\s+)?[A-Za-z][A-Za-z0-9]{1,9})?)\s+(?:to|on|in|into|at)\b', re.I)
_CRCPT = r'0x[0-9a-fA-F]{64}|@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b|@[a-z0-9-]{2,}'


def chain_sends(c):
    """The sends of one clause: [(kind, amt, who, words)] — "send 0.2 usdc to A and 0.1 usdc to B" pairs each recipient with
    the words before it; "0.1 usdc each to A and B" gives every recipient the one amount (2026-10-25)."""
    ms = list(re.finditer(_CRCPT, c, re.I))
    if not ms:
        return []
    if len(ms) == 1 or re.search(r'\beach\b', c, re.I):
        body = re.sub(_CRCPT, ' ', c, flags=re.I)
        kind, amt = chain_amount(body)
        return [(kind, amt, m.group(0), body) for m in ms]
    out, pos = [], 0
    for m in ms:
        seg = c[pos:m.start()]
        kind, amt = chain_amount(seg)
        out.append((kind, amt, m.group(0), seg))
        pos = m.end()
    return out


INPROTO_RE = re.compile(r'\b(?:change|swap|convert|switch|turn|rotate|flip|exchange)\b', re.I)
MKT_EXACT = {'navi': 'navi main', 'suilend': 'suilend main'}


def inproto(msg, address):
    """"change vsui to hasui in navi": a LENT coin swapped into another and lent back on the same market, as one chain
    (2026-10-21). The rewritten chain message, a 'which one?' reply, or None when it isn't that."""
    verb = INPROTO_RE.search(msg)
    if not address or QUESTION_RE.search(msg) or re.search(r'\b(?:withdraw|deposit|lend|park|put|send|pull|move|take\s+out|then)\b', msg, re.I) \
            or re.search(r'\b(?:%s)\b' % dict(CHAIN_VERB)['withdraw'], msg, re.I) or re.search(r'\bout\s+of\b|\bboth\b|\beverywhere\b', msg, re.I):
        return None                                  # a question, or already spelled out as steps ("take all my usdc out of…"): the chain reader has it
    mk, rest = chain_market(msg)
    if mk in ('best', 'there') or not (verb or (mk and re.search(r'\b(?:to|into|for)\b', rest, re.I))):
        return None                                  # "half my navi main sui to usdc" needs no verb
    if CHAIN_PERPS.search(msg) or re.search(_CRCPT, msg, re.I):
        return None                                  # perps margin or a recipient is involved: the chain reader's job ("free up my margin…", 2026-10-23; sends 2026-10-25)
    if CHAIN_REFUSE.search(msg) or BORROW_RE.search(msg):
        return {'text': 'Kivy never borrows, loops or uses leverage on lending. I can swap the lent coin into another and lend it back on '
                        'the same market — without the leverage part.'} if mk else None
    coins = []
    for w in re.findall(r'[A-Za-z][A-Za-z0-9]*', rest):
        c = ckey(w) if w.lower() not in ('it', 'my', 'me', 'to', 'in', 'on', 'of', 'all', 'half', 'max') else None
        if c and c not in coins:
            coins.append(c)
    if len(coins) != 2:
        return None
    x, y = coins
    if not mk and float((all_balances(address) or {}).get(x) or 0) > 0:
        return None                                  # no market named and the wallet has it: a normal wallet swap
    rows = [r for r in lend_positions_rows(address) if r['coin'].lower() == x.lower()
            and (not mk or r['id'] == mk or (mk.startswith('fam:') and r['id'].split('-')[0] == mk[4:]))]
    if not rows:                                     # not lent there: say so when a market was named, else the wallet cards handle it
        return {'text': 'You have no %s lent on %s through Kivy.' % (disp(x), LEND_MKTS.get(mk, mk.replace('fam:', '').title()
                                                                                       .replace('Navi', 'NAVI')))} if mk else None
    am = re.search(r'\b(all|everything|max|half|\d+(?:\.\d+)?)\b', rest, re.I)
    amt = am.group(1) if am else 'all'
    ys = lend_rows_for(y)                            # markets that take the new coin, best rate first
    def dest(r):
        """Lend it back on the same market — or, when that market doesn't list the new coin, the best one of the same protocol
        ("swap my usdc in suilend to deep in suilend": Ember has no DEEP, Suilend Main does — 2026-10-24)."""
        if any(z['market'] == r['id'] for z in ys):
            return r['id']
        if mk and not mk.startswith('fam:'):         # a market named exactly ("in ember"): never swap it for another quietly
            return None
        fam = mk[4:] if mk else r['id'].split('-')[0]
        return next((z['market'] for z in ys if z['market'].split('-')[0] == fam), None)
    line = lambda r: 'withdraw %s %s from %s, swap it to %s, then deposit into %s' % (amt, x, MKT_EXACT.get(r['id'], r['name']), y,
                                                                                     MKT_EXACT.get(dest(r), LEND_MKTS.get(dest(r), dest(r))))
    if mk and rows and len(rows) == 1 and not dest(rows[0]):   # no market of that protocol takes it: say where it can go
        r0 = rows[0]
        opts = [{'label': 'Swap to %s, lend it on %s' % (disp(y), LEND_MKTS.get(z['market'], z['name'])), 'sub': '%.2f%% a year' % float(z.get('total_apr') or 0),
                 'ask': 'withdraw %s %s from %s, swap it to %s, then deposit into %s' % (amt, x, MKT_EXACT.get(r0['id'], r0['name']), y, MKT_EXACT.get(z['market'], LEND_MKTS.get(z['market'], z['market'])))}
                for z in ys[:2]]
        opts.append({'label': 'Withdraw to my wallet as %s' % disp(y), 'sub': 'from %s' % r0['name'],
                     'ask': 'withdraw %s %s from %s, swap it to %s, then keep it in my wallet' % (amt, x, MKT_EXACT.get(r0['id'], r0['name']), y)})
        return {'text': ('No %s market lists %s.' % (mk[4:].title().replace('Navi', 'NAVI'), disp(y)) if mk.startswith('fam:') else
                         '%s doesn\'t list %s.' % (r0['name'], disp(y))) + ' Where should it go?',
                'widget': {'type': 'choose', 'options': opts}}
    if not mk:                                       # no market named and none in the wallet: keep it lent, or take it out? (ask)
        opts = [{'label': 'Swap to %s, keep it lent on %s' % (disp(y), r['name']), 'sub': '%s %s lent' % (dec(r['lent']), disp(x)), 'ask': line(r)}
                for r in rows]
        opts += [{'label': 'Withdraw to my wallet as %s' % disp(y), 'sub': 'from %s' % r['name'],
                  'ask': 'withdraw %s %s from %s, swap it to %s, then keep it in my wallet' % (amt, x, MKT_EXACT.get(r['id'], r['name']), y)} for r in rows]
        return {'text': 'Your %s is lent, not in your wallet. What should I do?' % disp(x), 'widget': {'type': 'choose', 'options': opts}}
    if len(rows) > 1:
        return {'text': 'You have %s lent on %d %s markets. Which one should I switch to %s?' % (disp(x), len(rows), mk.replace('fam:', '').title()
                                                                                               if mk and mk.startswith('fam:') else 'lending', disp(y)),
                'widget': {'type': 'choose', 'options': [{'label': '%s on %s' % (disp(x), r['name']), 'sub': '%s lent' % dec(r['lent']), 'ask': line(r)}
                                                         for r in rows]}}
    return line(rows[0])


CHAIN_VOCAB = ['split', 'divide', 'spread', 'half', 'halves', 'evenly', 'equally', 'between', 'across', 'withdraw', 'swap', 'convert', 'lend',
               'deposit', 'supply', 'stake', 'park', 'then', 'into', 'from', 'rest', 'remaining', 'those', 'these', 'them', 'that', 'best', 'market',
               'markets', 'yield', 'suilend', 'navi', 'ember', 'matrixdock', 'prime', 'main', 'high', 'vault', 'wallet', 'send', 'keep', 'perps',
               'margin', 'aftermath', 'each', 'with', 'also', 'plus', 'lent', 'every', 'everything', 'all', 'and', 'the', 'my', 'it', 'to', 'on', 'in']


REAL_WORD_TYPOS = {'spilt': 'split', 'spit': 'split', 'slit': 'split',
                   'dsut': 'dust', 'duts': 'dust', 'dustt': 'dust', 'swep': 'sweep', 'sweeep': 'sweep', 'sweap': 'sweep', 'swepp': 'sweep'}   # dust/sweep misspellings, listed: a fuzzy match turned 'just' into 'dust'   # real words that are typos in a money request


def fix_chain_typos(msg):
    """Near-misses of the words a multi-step request uses ("splt hlaf hlaf into suilnd and nvai", "sulend", "usdcc", "ssiu"),
    corrected — chain wording, market names and coin names only, never other words (2026-10-24, ripple of the split fix)."""
    try:
        coin_names = {c.lower(): c for c in COINS()}
        coin_names.update({{'USDSUI': 'USDsui'}.get(r['token'], r['token']).lower(): {'USDSUI': 'USDsui'}.get(r['token'], r['token'])
                           for r in lend_rates_all() if r.get('token')})
    except Exception:
        coin_names = {}
    def fix(m):
        w = m.group(0)
        lw = w.lower()
        if lw in REAL_WORD_TYPOS:
            return REAL_WORD_TYPOS[lw]
        if len(lw) < 4 or lw in NEVER_FIX or lw in REAL_COINS or lw in CHAIN_VOCAB or lw in coin_names or ckey(w) or lend_token(w) or lw in ENGLISH:
            return w                                      # a real English word ("what", "lending") is never "corrected"
        v = difflib.get_close_matches(lw, CHAIN_VOCAB, n=1, cutoff=0.75)
        if v:
            return v[0]
        c = difflib.get_close_matches(lw, list(coin_names), n=1, cutoff=0.75)
        return coin_names[c[0]] if c else w
    return re.sub(r'[A-Za-z]+', fix, msg)


def typo_chain(msg, address):
    """A multi-step request with typos: read it with the typos fixed, and say how it was read (the card shows every step anyway)."""
    n = fix_chain_typos(msg)
    if n == msg:
        return None
    try:                                             # a typo gets exactly what the fixed words get (2026-10-25: "withdaw $0.50 from ember")
        r = widget_reply(n, address)
    except Exception:
        r = None
    if r and isinstance(r.get('widget'), dict):
        r['text'] = 'Read as: "%s". %s' % (n, r.get('text') or '')
        return r
    r = chain_parse(n, address)
    if r and isinstance(r.get('widget'), dict) and r['widget'].get('type') == 'chain':
        r['text'] = 'Read as: "%s". %s' % (n, r.get('text') or '')
        return r
    return None


SPLIT_RE = re.compile(r'\b(?:split|divide|spread)\b(?:\s+(?:it|them|that|this|those|the\s+\w+))?'
                      r'\s*(?P<how>half\s*(?:/|and|-|,)?\s*half|50\s*/\s*50|50-50|evenly|equally|in\s+(?:half|two|three)|(?P<p1>\d{1,2})\s*(?:/|-|:|\s)\s*(?P<p2>\d{1,2})(?:\s*(?:/|-|:|\s)\s*(?P<p3>\d{1,2}))?)?'
                      r'\s*(?:,?\s*(?:and\s+)?(?:deposit|lend|put|park|supply|stake)\w*)?\s*(?:it\s+|them\s+)?'
                      r'(?:in\s*to|into|between|across|on|in|to)\s+(?P<tgt>.+?)\s*[.!]*$', re.I)


def split_rewrite(msg):
    """"…then split half half deposit into suilend and navi best market", "split it 50/50 between suilend and navi",
    "split 30/70 between ember and navi": the same as "lend half on suilend and the rest on navi" (2026-10-24)."""
    m = SPLIT_RE.search(msg)
    if not m:
        return msg
    tgt = re.sub(r'\b(?:best|top|highest)\s+(?:yield\s+|rate\s+)?markets?\b|\bmarkets?\b|\b(?:best|top|highest)\b', ' ', m.group('tgt'), flags=re.I)
    parts = [x.strip() for x in re.split(r'\s*(?:,|&|\band\b|\bplus\b)\s*', tgt) if x.strip()]
    if not 2 <= len(parts) <= 3 or not all(chain_market(x)[0] for x in parts):
        return msg
    if m.group('p1'):
        ws = [int(m.group('p1')), int(m.group('p2'))] + ([int(m.group('p3'))] if m.group('p3') else [])
        if len(ws) != len(parts) or sum(ws) != 100:
            return msg
    else:
        ws = [round(100 / len(parts), 4)] * len(parts)
    share = lambda w: 'half' if abs(w - 50) < 1e-9 else '%g%%' % w
    lines = ['lend %s on %s' % (share(w), x) for w, x in zip(ws[:-1], parts[:-1])]
    out = ', '.join(lines) + ' and lend the rest on %s' % parts[-1]
    return msg[:m.start()] + out


_FW = r'[A-Za-z][A-Za-z0-9]{1,9}'
_FLIST = r'%s(?:\s*(?:,|&|\+|\band\b|\bplus\b)\s*(?:and\s+)?%s){1,4}' % (_FW, _FW)
_FSH = r'\d{1,3}(?:\.\d+)?\s*%\s*each|each\s+\d{1,3}(?:\.\d+)?\s*%|(?:a\s+)?(?:half|third|quarter)\s+each|equally|evenly|in\s+equal\s+(?:parts|shares|amounts)|(?:\d{1,3}\s*/\s*){1,4}\d{1,3}'
FAN_RE = re.compile(r'(?:\b(?P<verb>split|divide|spread|swap|convert|turn|change)\b(?P<obj>(?:\s+(?!(?:in)?to\b|equally\b|evenly\b|\d{1,3}\s*%%)[\w.$]+){0,4}?)\s+)?'
                    r'(?P<sh>%s)?\s*(?:(?:in)?to|as)\s+(?P<list>%s)(?:\s+(?P<sh2>%s))?' % (_FSH, _FLIST, _FSH), re.I)
_FPART = r'(\d{1,3}(?:\.\d+)?\s*%%|half|a\s+third|a\s+quarter|(?:the\s+)?rest)\s+(?:of\s+it\s+)?(?:in)?to\s+(%s)' % _FW   # "half to USDC"
_FPART_NC = re.sub(r'\((?!\?)', '(?:', _FPART)
FAN2_RE = re.compile(r'%s(?:\s*(?:,|\band\b|&)\s*(?:and\s+)?%s){1,4}' % (_FPART_NC, _FPART_NC), re.I)


def fanout_parse(msg, address):
    """One coin into several coins in ONE transaction (2026-10-24: "withdraw hasui from navi, then 25% each into USDC,
    USDsui, SUI and deep then deposit into suilend"): the start (a withdrawal or a wallet coin), a share per coin, and where
    each lands (lent on the named protocol's best market for that coin, or kept). None when the message is not one."""
    if getattr(XW, 'fan_depth', 0):
        return None
    tok = lambda w: COIN_NAMES.get(w.lower()) or ckey(w) or lend_token(w)
    m, legs = FAN_RE.search(msg), None
    if m and (m.group('sh') or m.group('sh2') or (m.group('verb') or '').lower() in ('split', 'divide', 'spread')):
        coins, end = [], m.end()
        for wm in re.finditer(_FW, m.group('list')):     # coins only: "… and SUI and lend on navi" stops before "lend"
            if wm.group(0).lower() in ('and', 'plus'):
                continue
            if not tok(wm.group(0)) or wm.group(0).lower() in VERB_OF or wm.group(0).lower() in INTENT_STOP:   # 'send' is also a coin
                end = m.start('list') + wm.start()
                break
            coins.append(tok(wm.group(0)))
        if len(set(coins)) != len(coins) or len(coins) < 2:
            return None
        sh = (m.group('sh') or m.group('sh2') or 'equally').lower()
        n = len(coins)
        if '/' in sh:
            ws = [float(x) for x in re.findall(r'\d+', sh)]
            if len(ws) != n or abs(sum(ws) - 100) > 1e-6:
                return {'text': 'Give one share per coin that adds up to 100, e.g. "%s into %s".' % ('/'.join(['%g' % (100.0 / n)] * n), ', '.join(disp(c) for c in coins))}
            fr = [w / 100 for w in ws]
        elif '%' in sh:
            each = float(re.search(r'\d+(?:\.\d+)?', sh).group(0)) / 100
            if abs(each * n - 1) > 1e-6:
                return {'text': '%g%% each of %d coins is %g%%, not all of it. Say "equally", or give each share, e.g. "%s into %s".'
                                % (each * 100, n, each * n * 100, '/'.join(['%g' % (100.0 / n)] * n), ', '.join(disp(c) for c in coins))}
            fr = [each] * n
        else:
            fr = [1.0 / n] * n
        legs, start = list(zip(coins, fr)), m.start()
    else:
        m = FAN2_RE.search(msg)
        if not m:
            return None
        parts = [(a.lower(), tok(c)) for a, c in re.findall(_FPART, m.group(0), re.I)]
        if len(parts) < 2 or not all(c for _, c in parts) or len({c for _, c in parts}) != len(parts):
            return None
        val = lambda a: 0.5 if a == 'half' else 1 / 3 if 'third' in a else 0.25 if 'quarter' in a else None if 'rest' in a else float(a.rstrip('% ')) / 100
        fixed = [val(a) for a, _ in parts]
        if sum(1 for x in fixed if x is None) > 1:
            return {'text': 'Only one coin can take "the rest" — give the others a share.'}
        tot = sum(x for x in fixed if x is not None)
        if tot > 1 + 1e-6:
            return {'text': 'Those shares add up to more than everything.'}
        fr = [x if x is not None else 1 - tot for x in fixed]
        if None not in fixed and abs(tot - 1) > 1e-6:
            return {'text': 'Those shares add up to %g%%, not all of it. Say where the rest goes, e.g. "… and the rest to %s".' % (tot * 100, disp(parts[-1][1]))}
        legs, start, end = [(c, f) for (_, c), f in zip(parts, fr)], m.start(), m.end()
    if len(legs) > 4:                                 # the builder's limit: 4 final steps per transaction
        return {'text': 'At most 4 coins in one split — that keeps it to one transaction. Split into 4 now and swap the rest in a second step.'}
    head = re.sub(r'(?i)[\s,;:]*(?:\b(?:and|then|after\s+that|swap|convert|change|turn|exchange|split|divide|it)\b[\s,]*)*$', '', msg[:start]).strip()   # "…, swap half to X and half to Y": "swap" is the split's own verb
    obj = (m.group('obj') or '').strip() if 'obj' in m.groupdict() else ''
    if not head:
        if not obj or obj.lower() in ('it', 'them', 'that', 'everything'):
            return None
        head = 'use %s' % obj
    tail = msg[end:]
    if re.search(r'\b(?:send|transfer|pay|give|forward)\b|0x[0-9a-fA-F]{6,}|\.sui\b', tail, re.I):
        return {'text': 'Each coin of a split can be lent or kept in your wallet — sending isn\'t part of a split yet. Send it as its own step.'}
    lend = re.search(r'\b(?:lend|deposit|put|park|supply|stake|earn)\w*\b', tail, re.I)
    mk = (chain_market(tail)[0] or 'best') if lend else 'best'
    XW.fan_depth = 1
    try:
        r = chain_parse(head + ', then keep it in my wallet', address)
    finally:
        XW.fan_depth = 0
    if r is None:
        return None
    w = r.get('widget') or {}
    if w.get('type') == 'choose':                      # "which one should I start from?": each choice keeps the split
        for o in w.get('options') or []:
            if isinstance(o.get('ask'), str):
                o['ask'] = re.sub(r',\s*then keep it in my wallet\s*$', '', o['ask']) + ', then ' + msg[start:]
        return r
    if w.get('type') != 'chain':
        return r                                     # "you have no haSUI lent", …
    spec0 = w['spec']
    if spec0.get('swaps'):
        return {'text': 'Split it straight from the start coin, without a swap first — e.g. "withdraw my USDC from NAVI, then 50%% each into SUI and DEEP".'}
    src = dict(spec0['source'])
    coin = 'USDC' if src.get('op') == 'perps_out' else src.get('coin')
    est = float(w['steps'][-1].get('q') or 0)
    steps = w['steps'][:-1]
    per = {}                                         # "lend the SUI on navi and the DEEP on suilend" / "keep the DEEP": each coin its own place
    for to, _ in legs:                               # (review 2026-10-25: the split read one market for all coins and lent both on NAVI)
        names = [x for x, v in COIN_NAMES.items() if v == to] + [to.lower(), disp(to).lower()]
        alt = '|'.join(sorted({re.escape(x) for x in names if x}, key=len, reverse=True))
        pm = re.search(r'\b(?:the\s+)?(?:%s)\b\s+(?:on|in|into|at|to|with)\s+(.+?)(?=\s*(?:,|;|\band\b|\bthen\b|$))' % alt, tail, re.I)
        if pm and chain_market(pm.group(1))[0]:
            per[to] = ('lend', chain_market(pm.group(1))[0])
        elif re.search(r'\bkeep\s+(?:the\s+)?(?:%s)\b' % alt, tail, re.I):
            per[to] = ('keep', None)
    sinks, bad = [], []
    for i, (to, f) in enumerate(legs):
        lend_i, mk_i = (per[to][0] == 'lend', per[to][1]) if to in per else (bool(lend), mk)
        k = {'op': 'lend' if lend_i else 'keep', 'to': to, 'amount': {'rest': True} if i == len(legs) - 1 else {'frac': round(f, 6)}}
        if lend_i:
            rows = lend_rows_for(to)
            if mk_i.startswith('fam:'):
                rows = [x for x in rows if x['market'].split('-')[0] == mk_i[4:]]
            elif mk_i not in ('best', 'there'):
                rows = [x for x in rows if x['market'] == mk_i]
            if not rows:
                bad.append(to)
                continue
            k['market'], k['apr'] = rows[0]['market'], round(float(rows[0]['total_apr']), 2)
        sinks.append(k)
    if bad:
        where = '; '.join('%s: %s' % (disp(c), ', '.join(LEND_MKTS.get(x['market'], x['name']) for x in lend_rows_for(c)) or 'nowhere Kivy supports') for c in bad)
        wname = LEND_MKTS.get(mk) or (mk[4:].title().replace('Navi', 'NAVI') if mk.startswith('fam:') else 'that market')
        return {'text': '%s can\'t be lent on %s. Where each can be lent — %s.' % (' and '.join(disp(c) for c in bad), wname, where)}
    for (to, f), k in zip(legs, sinks):
        part = est * f
        if to != coin:
            try:
                out = float(swap_quote(coin, to, part)['out']) if part > 0 else 0
            except Exception:
                p1, p2 = coin_px(coin) or 0, coin_px(to) or 0
                out = part * p1 / p2 if p2 else 0
            sp = round(slip_for(coin, to) * 100, 3)
            steps.append({'coin': to, 'title': 'Swap %s %s to %s' % (dec(part), disp(coin), disp(to)), 'q': part,
                          'tt': 'Swap {q} %s to %s' % (disp(coin), disp(to)), 'sub': '≈ %s %s (max %g%% slippage)' % (dec(out), disp(to), sp)})
        else:
            out = part
        if k['op'] == 'lend':
            nm = LEND_MKTS.get(k['market'], k['market'])
            steps.append({'coin': to, 'proto': k['market'], 'title': 'Lend ≈ %s %s on %s' % (dec(out), disp(to), nm), 'q': out,
                          'tt': 'Lend ≈ {q} %s on %s' % (disp(to), nm), 'sub': '%.2f%% a year' % k['apr'] + (' · Kivy fee %g%%' % rev_lend_pct() if rev_lend_pct() else '')})
        else:
            steps.append({'coin': to, 'title': 'Keep ≈ %s %s in your wallet' % (dec(out), disp(to)), 'q': out, 'tt': 'Keep ≈ {q} %s in your wallet' % disp(to)})
    for st in steps:
        st['icon'] = coin_icon(st.get('coin'))
    spec = {'source': src, 'swaps': [], 'sinks': [{x: k[x] for x in ('op', 'to', 'amount', 'market') if x in k} for k in sinks]}
    return {'text': 'Here is the plan: %d steps in one transaction — you approve once, and if any step fails nothing happens.' % len(steps),
            'widget': dict({'type': 'chain', 'steps': steps, 'spec': spec, 'fee_pct': rev_fee_pct() if any(to != coin for to, _ in legs) else 0},
                           **({'pick': w['pick']} if w.get('pick') else {}))}


def chain_parse(msg, address):
    """A chain spec + card, a question, a refusal, or None when the message is not a multi-step chain. Every card is checked
    against the message first: nothing the user named may be missing from it (chain_coverage)."""
    if SEND_BRIDGE_RE.search(msg) and CHAIN_SPLIT.search(msg):
        return {'text': 'Kivy works on Sui only — it can\'t bridge to another chain. I can do the Sui part on its own if you ask for it without the bridge.'}
    r = _chain_parse_staked(msg, address)
    return chain_coverage(msg, r)


_MKT_WORDS = (('navi', 'NAVI'), ('suilend', 'Suilend'), ('ember', 'Ember'), ('matrixdock', 'Matrixdock'), ('matrix', 'Matrixdock'),
              ('prime', 'Prime'), ('high yield', 'High Yield'), ('highyield', 'High Yield'))


def chain_coverage(msg, r):
    """A multi-step card must carry EVERYTHING the user named — each coin, market, address and the perps account. the reviewer's
    60 multi-step scenarios (2026-10-25) found six cards that silently dropped a part (a 3rd swap, a bridge, a 2nd coin's
    withdrawal, a perps leg, a SUI lend, a send-to-address). A dropped part = no card: say what can't fit."""
    w = (r or {}).get('widget') if isinstance(r, dict) else None
    if not isinstance(w, dict) or w.get('type') != 'chain':
        return r
    text = ' '.join('%s %s' % (st.get('title') or '', st.get('sub') or '') for st in w.get('steps') or []).lower()
    body = re.sub(r'0x[0-9a-fA-F]{6,}|(?<![\w.])@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b', ' ', msg, flags=re.I)
    body = re.sub(r'(?<=[A-Za-z0-9%)])\.\s+(?=[A-Za-z])', ', ', body)   # read it the way the reader does: sentences are steps
    body = STAKED_ASK.sub(' staked ', body)          # "the best staked sui" became sSUI/vSUI/haSUI in the plan
    missing = []
    for wd in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', body):
        c = ckey(wd) or lend_token(wd)
        if c and wd.lower() not in ('my', 'all', 'it', 'to', 'on', 'in', 'of', 'and', 'the', 'max', 'half', 'rest', 'usd', 'dollars', 'dollar',
                                   'up', 'out', 'back', 'more', 'any', 'one', 'two', 'free', 'go', 'get', 'put', 'add', 'pay') \
                and not re.search(r'(?<![a-z0-9])(?:%s|%s)(?![a-z0-9])' % (re.escape(disp(c).lower()), re.escape(c.lower())), text) and disp(c) not in missing:
            missing.append(disp(c))                  # whole words: "sui" inside "Suilend" once hid a dropped SUI withdrawal
    for word, name in _MKT_WORDS:
        if re.search(r'\b%s\b' % word, body, re.I) and name.lower() not in text and name not in missing:
            missing.append(name)
    for a in re.findall(r'0x[0-9a-fA-F]{64}', msg):
        if a.lower() not in text and (a[:6] + '…' + a[-4:]).lower() not in text and a.lower()[:10] not in text:
            missing.append('the transfer to %s…%s' % (a[:6], a[-4:]))
    for n in re.findall(r'(?<![\w.])@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b', msg, re.I):
        if n.lower() not in text:
            missing.append('the transfer to %s' % n)
    if CHAIN_PERPS.search(body) and 'perps' not in text:
        missing.append('the perps account')
    # each clause that names a market is served by a step OF ITS KIND: a lend clause by a lend there, a withdraw clause by a
    # withdrawal from there (a "High Yield" withdrawal once satisfied a dropped "half into NAVI SUI High Yield" lend)
    spec = w.get('spec') or {}
    src_m = {(spec.get('source') or {}).get('market')} | {x.get('market') for x in (spec.get('source') or {}).get('more') or []}
    sink_m = {k.get('market') for k in spec.get('sinks') or [] if k.get('op') == 'lend'}
    same = lambda want, have: any(h and (h == want or (want.startswith('fam:') and h.split('-')[0] == want[4:])) for h in have)
    parts = [q for p0 in CHAIN_SPLIT.split(body) for q in re.split(r'\band\b(?!\s+(?:ember|navi|suilend|matrixdock|prime|high)\b)', p0 or '', flags=re.I)]
    for part in parts:                               # "put half into X and the rest as margin": each half checked on its own
        part = (part or '').strip()
        mk = chain_market(part)[0] if part else None
        if not mk or mk in ('best', 'there') or CHAIN_PERPS.search(part) or re.search(r'\b(?:move|shift|rebalance|switch)\b', part, re.I):
            continue                                 # a move names both its markets; the perps account has its own check above
        name = LEND_MKTS.get(mk) or mk.replace('fam:', '').title().replace('Navi', 'NAVI')
        lendv = re.search(r'\b(?:lend\w*|deposit\w*|put|park\w*|supply|stick)\b', part, re.I)   # a lend verb decides first ("put the withdrawn funds into X")
        if not lendv and re.search(r'\b(?:withdr\w*|take|pull|remove|get|cash|from|out\s+of)\b', part, re.I):
            if not same(mk, src_m) and name not in missing:
                missing.append('the withdrawal from %s' % name)
        elif lendv or re.search(r'\b(?:into|on|in)\b', part, re.I):
            if not same(mk, sink_m) and name not in missing:
                missing.append('the deposit on %s' % name)
    if not missing:
        return r
    return {'text': "I can't fit all of that into one transaction — this part isn't in the plan: %s. One transaction takes one starting coin, "
                    "at most 2 swaps, and each split part can be lent or kept. Split it into two requests, or say it differently." % ', '.join(missing[:5])}


def _chain_parse_staked(msg, address):
    if STAKED_ASK.search(msg) and not STAKED_NAMED.search(msg) and not re.search(r'\bunstak', msg, re.I):
        lend_at = re.search(r'\b(?:lend|deposit|put|park|supply)\w*\b(.*)$', msg, re.I)   # "…and lend it on suilend": best staked SUI THERE
        mk = _mk_of(lend_at.group(1)) if lend_at else None
        ranked = staked_ranked(mk)
        where = LEND_MKTS.get(ranked[0][1]['market'], ranked[0][1]['market']) if ranked and mk else None
        ranked = ranked or staked_ranked()
        if ranked:                                    # "swap my sui to the best staked sui and lend it": was read as plain SUI (2026-10-25)
            r = _chain_parse(STAKED_ASK.sub(ranked[0][0], msg, count=1), address)
            if r and r.get('text') and isinstance(r.get('widget'), dict):
                r['text'] = staked_line(ranked, where) + ' ' + r['text']
            return r
    return _chain_parse(msg, address)


def _chain_parse(msg, address):
    msg = re.sub(r'(?<=[A-Za-z0-9%)])\.\s+(?=[A-Za-z])', ', ', msg)   # "Withdraw … . Swap to SUI. Lend …": sentences are steps (review 2026-10-25)
    msg = re.sub(r'\s\+\s*(?=\$?\d)', ' plus ', msg)                # "+ 0.4 usdc from my wallet" = a top-up
    msg = split_rewrite(msg)
    fo = fanout_parse(msg, address)                  # one coin into several coins, each lent or kept (2026-10-24)
    if fo is not None:
        return fo
    # "from suilend, navi and ember": a list of SOURCE markets, not separate steps (the comma would split it)
    for _ in range(3):
        msg = re.sub(r'(?i)(\b(?:from|out\s+of)\s+(?:both\s+)?(?:[a-z]+\s+)?(?:suilend|navi|ember|matrixdock)(?:\s+(?:main|prime|ember|market))?)\s*,\s*(?=(?:suilend|navi|ember|matrixdock)\b)',
                     r'\1 and ', msg)
    for _ in range(5):                                # "lend 0.4 on prime, 0.3 on high yield, 0.2 on ember": every item, not just the first
        new = re.sub(r'(?i)\b(lend|deposit|park|put|supply)\b([^,;]*?)(?:\band\s+|,\s*)(half|the\s+rest|rest|a\s+third|a\s+quarter|\d+(?:\.\d+)?\s*%?)(\s+[a-z]{2,10})?\s+(on|in|into|at)\b',
                     lambda m: re.sub(r'\s{2,}', ' ', '%s%s and %s %s%s %s' % (m.group(1), m.group(2), m.group(1), m.group(3), m.group(4) or '', m.group(5))), msg)
        if new == msg:                                # (a comma glued "prime" + "and" into "primeand", misreading the market — a reviewer 2026-10-25)
            break
        msg = new   # "…and 0.2 sui on suilend", "…, 0.2 on suilend" too (2026-10-25)
    msg = re.sub(r'\s*:\s*(?=0x[0-9a-fA-F]{4,})', ' ', msg)   # "to this wallet: 0x…" — not a step break
    for _ in range(4):                                # "0.1 usdc each to A, B, C and D": a comma between two recipients is a list, not a step (2026-10-25)
        msg = re.sub(r'(?i)(%s)\s*,\s*(?=(?:and\s+)?(?:%s))' % (_CRCPT, _CRCPT), r'\1 and ', msg)
    # "send half my sui to bob.sui and lend the rest": the start is ALL of it, half one way, the rest the other (2026-10-23)
    msg = re.sub(r'(?i)^\s*(send|lend|deposit|put|swap|move)\s+(half|a\s+third|a\s+quarter|\d+(?:\.\d+)?\s*%)\s+(?:of\s+)?my\s+([a-z]+)\b(?=.*\b(?:the\s+rest|remainder|what(?:\'s|\s+is)\s+left)\b)',
                 r'take all my \3, \1 \2', msg)
    clauses = [c.strip() for c in CHAIN_SPLIT.split(msg) if c and c.strip()]
    verbs = []
    for c in clauses:
        hit = None
        for v, pat in CHAIN_VERB:
            m = re.search(r'\b(?:%s)\b' % pat, c, re.I)
            if m and (hit is None or m.start() < hit[1]):
                hit = (v, m.start())
        if hit and hit[0] == 'use' and CHAIN_PERPS.search(c) and not re.search(r'\b(?:wallet|deposit\w*|lend\w*|put|park|add|as)\b', c, re.I):
            hit = ('withdraw', hit[1])                # "grab the leftover margin in my perps account": margin is the source
        if not hit and LIST_ITEM_RE.match(c):         # "0.1 to 0x…" / "the rest on ember": list items say no verb (2026-10-25)
            if re.search(_CRCPT, c, re.I):
                hit = ('send', 0)
            elif chain_market(c)[0] not in (None, 'there'):
                hit = ('lend', 0)
        verbs.append(hit[0] if hit else None)
    trade_at = [i for i, c in enumerate(clauses) if re.search(r'\b(?:long|short|ape)\b|\bopen\s+(?:a\s+)?(?:\w+\s+)?position\b', c, re.I)]
    perpish = re.search(r'\b(?:perps?|leverage|margin|\d+(?:\.\d+)?\s*x)\b', msg, re.I)
    side_only = lambda c: perpish and re.match(r'^\s*(?:(?:market|limit)\s+)?(?:buy|sell)(?:\s+order)?\s*[?.!]*\s*$', c, re.I)   # "…, market buy" = the order side
    joined = re.search(r'\b(?:withdraw|swap|convert|send|move|take)\b.+\b(?:and|then|&)\s+(?:then\s+)?(?:go\s+|open\s+(?:a\s+)?)?(?:long|short|ape)\b', msg, re.I)
    if joined or trade_at and any(v in ('withdraw', 'swap', 'move', 'send') for i, v in enumerate(verbs) if v and i not in trade_at and not side_only(clauses[i])):   # a money step AND a trade step
        return {'text': 'In a multi-step move Kivy can deposit margin into your perps account, but not open a position, use leverage '
                        'or borrow. Do the deposit here, then open the position from the perps card.'}
    verbs = [None if v and side_only(c) else v for c, v in zip(clauses, verbs)]
    verbs = ['swap' if not v and DUST_RE.search(c) else v for c, v in zip(clauses, verbs)]   # "sweep my dust, then …": the dust is the start
    ops = [v for v in verbs if v]
    single_move = len(ops) == 1 and ops[0] == 'move' and (re.search(r'\bfrom\b.+\b(?:to|into)\b', msg, re.I)
                                                         or re.search(r'\b(?:margin|perps?|aftermath|collateral)\b.*\b(?:to|into)\s+\w', msg, re.I))
    multi_from = len(ops) == 1 and ops[0] == 'withdraw' and (
        re.search(r'\b(?:both|everywhere|every\s+market|all\s+(?:my\s+)?(?:markets|protocols|places))\b', msg, re.I)
        or len(re.findall(r'\b(?:suilend|navi|ember|matrixdock)\b', msg, re.I)) >= 2)   # "withdraw USDC from suilend and navi": one approval, home
    if len(ops) < 2 and not single_move and not multi_from:
        return None                                  # one step: the normal cards handle it
    if CHAIN_BOT.search(msg):                         # "…then start a trading bot with all the SUI" (2026-10-21)
        return {'text': 'Kivy doesn\'t run bots, schedules or repeating trades — every step needs your own approval. '
                        'I can do the money steps once, here, if you drop that part.'}
    if CHAIN_REFUSE.search(msg):
        return {'text': 'In a multi-step move Kivy can deposit margin into your perps account, but not open a position, use leverage '
                        'or borrow. Do the deposit here, then open the position from the perps card.'}
    if address is None:
        return {'text': K()['no_wallet']}
    if not ADDR_RE.match(address or ''):
        return None
    lent = lend_positions_rows(address)
    hold = dict(all_balances(address) or {})
    for k, v in xw_extra(address).items():            # money an earlier step of a cross-account plan sends here
        hold[k] = float(hold.get(k) or 0) + float(v)
    src, swaps, sinks, hand, last_mkt = None, [], [], None, None
    for c, v in zip(clauses, verbs):
        if src is None and DUST_RE.search(c):         # "swap all my dust as USDC then deposit into suilend" (2026-10-25): the sweep starts the chain
            if NET() != 'mainnet':
                return {'text': 'Dust sweeps need the swap router, which is mainnet-only.'}
            m = DUST_TO_RE.search(c)
            to = ((ckey(m.group(1)) or lend_token(m.group(1))) if m else None) or 'SUI'
            dust, _ = dust_coins(address, to)
            if not dust:
                return {'text': 'No dust: every coin Kivy recognises in your wallet is worth $0.01 or more (SUI is kept for gas).'}
            px0 = coin_px(to) or 0
            src = {'op': 'dust', 'coin': to, 'amount': 'all', 'px': px0, 'types': [x['type'] for x in dust], 'syms': [x['sym'] for x in dust],
                   'est': sum(x['usd'] for x in dust) / px0 * 0.98 if px0 else 0}   # the router's real quote comes with the transaction
            hand = to
            continue
        if src is None and not v:                    # "I've got 80 USDC from my wallet:" — the start, said without an action word
            mw = re.search(r'(\d+(?:\.\d+)?)\s+([A-Za-z]+)', c)
            wc = (ckey(mw.group(2)) or COIN_NAMES.get(mw.group(2).lower())) if mw else None
            if wc and re.search(r"\bmy\s+wallet\b|\bi\s*(?:have|'ve\s+got|got)\b", c, re.I):
                have = spendable(wc, hold)
                if float(mw.group(1)) > have + 1e-9:
                    return {'text': 'Your wallet has %s %s, not %s.' % (dec(have), wc, mw.group(1))}
                src = {'op': 'wallet', 'coin': wc, 'amount': float(mw.group(1)), 'est': float(mw.group(1)), 'px': coin_px(wc) or 0}
                hand = wc
            continue
        if not v:
            continue
        if re.search(r'\b(?:leave|keep)\b.*\b(?:deposited|where\s+it\s+is|in\s+place|lent|there)\b', c, re.I):
            continue                                 # "leave the other half deposited": nothing to do
        if src is None and v in ('lend', 'send', 'keep', 'swap') and re.search(r"\b(?:from|in|of)\s+my\s+wallet\b|\bi\s*(?:have|'ve\s+got|got)\b", c, re.I):
            mw = re.search(r'(\d+(?:\.\d+)?)\s+([A-Za-z]+)', c)
            wc = (ckey(mw.group(2)) or COIN_NAMES.get(mw.group(2).lower())) if mw else None
            if wc and v != 'swap':                   # "I've got 80 USDC from my wallet: lend half…" — the start, then this step
                have = spendable(wc, hold)
                if float(mw.group(1)) > have + 1e-9:
                    return {'text': 'Your wallet has %s %s, not %s.' % (dec(have), wc, mw.group(1))}
                src = {'op': 'wallet', 'coin': wc, 'amount': float(mw.group(1)), 'est': float(mw.group(1)), 'px': coin_px(wc) or 0}
                hand = wc
                c = c[mw.end():]
        mk, rest_txt = chain_market(c, hand)
        coins, bad = chain_coins(rest_txt)
        if bad:
            return {'text': '%s isn\'t a coin Kivy supports. I work with SUI, USDC, DEEP, WAL, BTC, ETH, SOL and the coins Suilend or NAVI list.' % bad[0]}
        # the amount is read only BEFORE a wallet top-up ("withdraw usdc from aftermath plus 5 usdc from my wallet": not 5)
        own_txt = re.split(r'\b(?:plus|including|incl\.?|together\s+with|along\s+with|as\s+well\s+as|and\s+also)\b', rest_txt, maxsplit=1, flags=re.I)[0]
        kind, amt = chain_amount(own_txt)
        # "withdraw some usdc…": no amount — the card opens with its amount picker (2026-10-24; it used to ask "how much?")
        if v == 'claim':                             # the claim is its own transaction: offer it, and say the next step
            return {'text': 'Claiming is its own transaction, so it can\'t be one step of a chain yet. Claim first; then ask me to swap or lend what arrived.',
                    'widget': {'type': 'choose', 'options': [{'label': 'Claim my rewards', 'sub': 'step 1 of 2', 'ask': 'claim my rewards'}]}}
        if src is None and v in ('lend', 'send') and coins and kind == 'units':   # "lend 2 USDC … send the remaining 10 USDC": from the wallet
            total, rest_all = 0.0, False
            for c2, v2 in zip(clauses, verbs):
                if v2 == 'send' and len(chain_sends(c2)) > 1:   # several recipients in one clause: every send counts (2026-10-25)
                    for k2, a2, _, _ in chain_sends(c2):
                        if k2 == 'rest':
                            rest_all = True
                        elif k2 == 'units':
                            total += a2
                        elif k2 is None:
                            return {'text': 'Give each recipient an amount, e.g. "0.2 %s to A and 0.1 %s to B", or "0.1 %s each to A and B".' % ((disp(coins[0]),) * 3)}
                        else:
                            return {'text': 'How much in total should I take from your wallet?'}
                    continue
                if v2 in ('lend', 'send', 'keep'):
                    n2 = re.search(r'(\d+(?:\.\d+)?)', re.sub(r'0x[0-9a-fA-F]+', ' ', c2))
                    if not n2 and re.search(r'\b(?:the\s+rest|rest\s+of|remaining|what(?:\'s|\s+is)\s+left|everything\s+else)\b', c2, re.I):
                        rest_all = True                  # "send 1 usdc to x and lend the rest of my usdc": the rest of the wallet (2026-10-24)
                        continue
                    if not n2:
                        return {'text': 'How much in total should I take from your wallet?'}
                    total += float(n2.group(1))
            wc = coins[0]
            have = spendable(wc, hold)
            if rest_all:
                if have <= total + 1e-9:
                    where = [r for r in lend_positions_rows(address) if r['coin'].lower() == wc.lower()]
                    return {'text': 'Your wallet has %s %s, so there is no rest to lend.%s' % (dec(have), disp(wc), (
                        ' Your %s is lent (%s) — withdraw it first, e.g. "withdraw %s %s from %s and send it to …".' % (
                            disp(wc), ', '.join('%s %s' % (r['name'], dec(r['lent'], 6)) for r in where), dec(total), wc, where[0]['name'])) if where else '')}
                total = have
            if total > have + 1e-9:
                return {'text': 'Your wallet has %s %s — not enough for %s. Try smaller amounts.' % (dec(have), disp(wc), dec(total))}
            total = round(total, 9)                   # 0.4 + 0.3 + 0.2 + 0.1 is 0.9999999999999999 in floats: the start was 1 unit short (review 2026-10-25)
            src = {'op': 'wallet', 'coin': wc, 'amount': total, 'est': total, 'px': coin_px(wc) or 0}
            hand = wc
        if v in ('withdraw', 'move') or (v == 'use' and mk and mk not in ('best', 'there') and re.search(r'\b(from|out\s+of)\b', c, re.I)):
            if src:
                return {'text': 'One starting coin per transaction, please. Split it into two requests.'}
            frm = mk
            if v == 'move':                          # "move X from A to B": A is the source, B the lending target
                m2 = re.search(r'\b(?:from|on|in|out\s+of)\s+(.+?)\s+(?:to|into|over\s+to)\s+(.+?)$', c, re.I)
                mas = re.search(r'\bas\s+([A-Za-z]{2,10})\s*[?.!]*\s*$', c, re.I)          # "… to navi as USDC": swap on the way
                if mas and (ckey(mas.group(1)) or COIN_NAMES.get(mas.group(1).lower())):
                    move_to_coin = ckey(mas.group(1)) or COIN_NAMES.get(mas.group(1).lower())
                    c = c[:mas.start()]
                    m2 = re.search(r'\b(?:from|on|in|out\s+of)\s+(.+?)\s+(?:to|into|over\s+to)\s+(.+?)$', c, re.I)
                else:
                    move_to_coin = None
                if m2:
                    frm = chain_market(m2.group(1))[0]
                    to_mk = chain_market(m2.group(2))[0]
                    if to_mk:
                        sinks.append({'op': 'lend', 'mk': to_mk, 'amount': ('rest', None)})
                    elif CHAIN_PERPS.search(m2.group(2)):      # "move my usdc from suilend main to my perps account"
                        sinks.append({'op': 'perps', 'amount': ('rest', None)})
                else:                                # "move my margin to navi": from the perps account
                    m3 = re.search(r'^(.*?)\b(?:to|into|over\s+to)\s+(.+?)$', c, re.I)
                    if m3 and CHAIN_PERPS.search(m3.group(1)) and chain_market(m3.group(2))[0]:
                        sinks.append({'op': 'lend', 'mk': chain_market(m3.group(2))[0], 'amount': ('rest', None)})
                        m2 = m3
            pz = m2.group(1) if v == 'move' and m2 else re.split(r'\b(?:to|into|over\s+to)\b', c, maxsplit=1, flags=re.I)[0]   # the source part only
            if CHAIN_PERPS.search(pz) and not re.search(r'\b(?:suilend|navi|ember|matrixdock|prime|high\s*yield|lent|lending)\b', pz, re.I):
                acct = perp_account(address, fresh=True)
                if not acct.get('has'):
                    return {'text': 'You don\'t have an Aftermath perps account, so there is no margin to withdraw.'}
                free = float(acct.get('collateral') or 0)
                if free <= 0:
                    return {'text': 'Your Aftermath perps account has no free USDC to withdraw.'}
                a = 'all' if kind in (None, 'all', 'rest') else (round(free * amt, 6) if kind == 'frac' else amt)
                if a != 'all' and a > free + 1e-9:
                    return {'text': 'Your perps account has %s USDC free — less than %s.' % (dec(free), dec(a))}
                src = {'op': 'perps_out', 'coin': 'USDC', 'amount': a, 'est': free if a == 'all' else a, 'px': 1.0}
                hand, last_mkt = 'USDC', None
                if v == 'move' and locals().get('move_to_coin') and move_to_coin != 'USDC':
                    swaps.append({'to': move_to_coin}); hand = move_to_coin
                continue
            coin = coins[0] if coins else None
            # "withdraw USDC from both suilend and navi" / "from suilend, navi and ember" / "from everywhere": several withdrawals
            # of ONE coin into one transaction (2026-10-24: the NAVI half was silently dropped)
            frms, t_ = [], pz
            for _ in range(4):
                k_, t_ = chain_market(t_)
                if not k_ or k_ in ('best', 'there'):
                    break
                frms.append(k_)
            every = re.search(r'\b(?:everywhere|every\s+market|all\s+(?:my\s+)?(?:markets|protocols|places|positions))\b', pz, re.I)
            if (len(frms) >= 2 or every) and coin:
                hit = lambda r, f: r['id'] == f or (f.startswith('fam:') and r['id'].split('-')[0] == f[4:])
                many = [r for r in lent if r['coin'].lower() == coin.lower() and (every or any(hit(r, f) for f in frms))]
                if len(many) >= 2:
                    if kind == 'units':
                        return {'text': 'From several markets at once, say "all" or a share ("half") — not one number.'}
                    each = lambda r: 'all' if kind in (None, 'all', 'rest') else round(r['lent'] * amt, 9)
                    r0 = many[0]
                    src = {'op': 'withdraw', 'market': r0['id'], 'coin': r0['coin'], 'amount': each(r0),
                           'est': sum(r['lent'] if each(r) == 'all' else each(r) for r in many), 'px': r0.get('px') or coin_px(r0['coin']) or 0,
                           'more': [{'market': r['id'], 'coin': r['coin'], 'amount': each(r), 'est': r['lent'] if each(r) == 'all' else each(r)} for r in many[1:]],
                           'first_est': r0['lent'] if each(r0) == 'all' else each(r0)}
                    hand, last_mkt = r0['coin'], None
                    if v == 'move' and move_to_coin and move_to_coin != hand:
                        swaps.append({'to': move_to_coin}); hand = move_to_coin
                    continue
            cands = [r for r in lent if (not coin or r['coin'].lower() == coin.lower())
                     and (not frm or frm == 'best' or (frm.startswith('fam:') and r['id'].split('-')[0] == frm[4:]) or r['id'] == frm)]
            if not cands:
                return {'text': 'You have no %s lent%s through Kivy.' % (disp(coin) if coin else 'coins', (' on %s' % LEND_MKTS.get(frm, frm.replace('fam:', '').title())) if frm and frm != 'best' else '')}
            if len(cands) > 1:                        # several positions fit: ask, each choice re-sends the whole chain made precise
                after = msg[msg.find(c) + len(c):].lstrip(' ,;') if c in msg else ''
                aw = 'all' if kind in (None, 'all') else (dec(amt) if kind == 'units' else 'half')
                return {'text': 'Which one should I start from?',
                        'widget': {'type': 'choose', 'options': [{'label': '%s on %s' % (r['coin'], r['name']), 'sub': '%s lent' % dec(r['lent']),
                                                                  'ask': ('withdraw %s %s from %s, %s' % (aw, r['coin'], r['name'], after)).strip(' ,')}
                                                                 for r in cands[:4]]}}
            r = cands[0]
            a = 'all' if kind in (None, 'all', 'rest') else (round(r['lent'] * amt, 9) if kind == 'frac' else amt)
            if a != 'all' and a > r['lent'] + 1e-9:
                return {'text': 'You have %s %s lent on %s — that is less than %s.' % (dec(r['lent']), r['coin'], r['name'], dec(a))}
            src = {'op': 'withdraw', 'market': r['id'], 'coin': r['coin'], 'amount': a,
                   'est': r['lent'] if a == 'all' else a, 'px': r.get('px') or coin_px(r['coin']) or 0,
                   'pick': kind is None, 'max': r['lent']}     # no amount said: the card's picker starts at all (2026-10-24)
            hand, last_mkt = r['coin'], r['id']
            if v == 'move' and move_to_coin and move_to_coin != hand:
                swaps.append({'to': move_to_coin}); hand = move_to_coin
            continue
        if v in ('use', 'swap') and src is None:     # the starting coin comes from the wallet
            tgt = None
            mb = re.search(r'\b(?:buy|get)\s+(?:some\s+)?([A-Za-z]+)\b', c, re.I)          # "to buy USDC", "to get vSUI", "buy X with Y"
            mt = re.search(r'\b(?:to|into|for)\s+(?:some\s+)?([A-Za-z]+)\b', c, re.I)
            if mb and (ckey(mb.group(1)) or COIN_NAMES.get(mb.group(1).lower())):
                tgt = ckey(mb.group(1)) or COIN_NAMES.get(mb.group(1).lower())
            elif mt and (ckey(mt.group(1)) or COIN_NAMES.get(mt.group(1).lower())):
                tgt = ckey(mt.group(1)) or COIN_NAMES.get(mt.group(1).lower())
            srcs = [x for x in coins if x != tgt]
            if not srcs and kind == 'units':          # "use 6 from my wallet": the only coin they hold
                held = [k for k, v in hold.items() if float(v or 0) * float(coin_px(k) or 0) >= 0.02 and k != tgt]
                if len(held) == 1:
                    srcs = held
            if len(srcs) > 1:
                return {'text': 'One starting coin per transaction, please — which one: %s?' % ' or '.join(srcs)}
            if not srcs:
                return None if v == 'swap' and not tgt else {'text': 'Which coin should I start with, and how much?'}
            coin = srcs[0]
            # "swap THAT usdc to deep…" with no USDC in the wallet but USDC lent: start from the lent coin (2026-10-24)
            mine = [r for r in lent if r['coin'].lower() == coin.lower()]
            if mine and not re.search(r'\bwallet\b', c, re.I) and (spendable(coin, hold) * float(coin_px(coin) or 0) < 0.01 or re.search(r'\b(?:that|lent)\b', c, re.I)):
                if len(mine) > 1:
                    after = msg[msg.find(c) + len(c):].lstrip(' ,;') if c in msg else ''
                    tail = re.sub(r'^.*?\b(?:to|into|for)\s+', 'swap it to ', c, count=1, flags=re.I) if tgt else ''
                    return {'text': 'Your %s is lent on %d markets. Which one should I use?' % (disp(coin), len(mine)),
                            'widget': {'type': 'choose', 'options': [{'label': '%s on %s' % (disp(coin), r['name']), 'sub': '%s lent' % dec(r['lent']),
                                                                      'ask': ', '.join(x for x in ('withdraw all %s from %s' % (coin, r['name']), tail, after) if x)} for r in mine]}}
                r = mine[0]
                a = 'all' if kind in (None, 'all', 'rest') else (round(r['lent'] * amt, 9) if kind == 'frac' else amt)
                if a != 'all' and a > r['lent'] + 1e-9:
                    return {'text': 'You have %s %s lent on %s — that is less than %s.' % (dec(r['lent']), r['coin'], r['name'], dec(a))}
                src = {'op': 'withdraw', 'market': r['id'], 'coin': r['coin'], 'amount': a,
                       'est': r['lent'] if a == 'all' else a, 'px': r.get('px') or coin_px(r['coin']) or 0, 'pick': kind is None, 'max': r['lent']}
                hand, last_mkt = r['coin'], r['id']
                if tgt and tgt != hand:
                    swaps.append({'to': tgt}); hand = tgt
                continue
            if kind is None and re.search(r'\b(?:the\s+rest|remainder|what(?:\'s|\s+is)\s+left)\b', msg, re.I):
                kind, amt = 'all', 1.0                   # "take my sui, send half … and lend the rest": all of it (2026-10-23)
            pick = False
            if kind in (None, 'rest') or (kind == 'frac' and not re.search(r'\bhalf\b|%', c)):
                kind, amt, pick = 'all', 1.0, True       # no amount said: the card opens at the max with an amount picker (2026-10-24)
            have = spendable(coin, hold)
            a = have * amt if kind == 'frac' else (have if kind == 'all' else amt)
            src = {'op': 'wallet', 'coin': coin, 'amount': round(a, 9), 'est': a, 'px': coin_px(coin) or 0, 'pick': pick, 'max': round(have, 9)}
            hand = coin
            if tgt and tgt != coin:
                swaps.append({'to': tgt}); hand = tgt
            continue
        if src is None:
            return None                              # a chain must start with a coin; not ours
        if v == 'swap':
            mt = re.search(r'\b(?:to|into|for)\s+(?:some\s+)?([A-Za-z]+)\b', c, re.I)
            mb = re.search(r'\bbuy\s+([A-Za-z]+)\b', c, re.I)
            word = (mb.group(1) if mb else None) or (mt.group(1) if mt else None) or (coins[-1] if coins else None)
            tgt = (ckey(word) or COIN_NAMES.get(str(word).lower())) if word else None
            if not tgt:
                return {'text': 'Which coin should I swap it into?'}
            if tgt != hand:
                if len(swaps) >= 2:
                    return {'text': 'At most two swaps in one transaction — please split it.'}
                swaps.append({'to': tgt}); hand = tgt
            continue
        if (v == 'lend' or (v == 'use' and src)) and CHAIN_PERPS.search(c):   # "deposit into aftermath as margin", "use it as margin"
            sinks.append({'op': 'perps', 'amount': (kind, amt) if kind in ('frac', 'units') else ('rest', None)})
            continue
        if v == 'lend':
            sinks.append({'op': 'lend', 'mk': mk or 'best', 'amount': (kind, amt)})
            continue
        if v == 'send':
            sends = chain_sends(c)
            if not sends:
                return {'text': 'Which address or .sui name should I send it to? (I always show the full address before you approve.)'}
            if len(sends) == 1:
                sends = [(kind, amt, sends[0][2], '')]   # one recipient: the clause's own amount, market words cut out
            elif re.search(r'\beach\b', c, re.I) and sends[0][0] not in ('units', 'frac'):
                return {'text': '"Each" needs an amount, e.g. "send 0.1 %s each to %s".' % (disp(hand), ' and '.join(x[2] if not ADDR_RE.match(x[2]) else x[2][:6] + '…' + x[2][-4:] for x in sends))}
            for sk, sa, who, seg in sends:            # "send 0.2 usdc to A and 0.1 usdc to B": one step per recipient (2026-10-25)
                if sk is None and len(sends) > 1:     # (one recipient with no amount takes the rest, as before)
                    return {'text': 'Give each recipient an amount, e.g. "0.2 %s to A and 0.1 %s to B", or "0.1 %s each to A and B".' % ((disp(hand),) * 3)}
                oc = [x for x in chain_coins(seg)[0] if x != hand]
                if oc:
                    return {'text': 'Every send in one plan is in the coin in hand here (%s). Send the %s in its own request.' % (disp(hand), disp(oc[0]))}
                to = resolve_recipient(who)
                if not to:
                    return {'text': 'I couldn\'t find %s. Give a full 0x… address or a .sui name that exists.' % who}
                if to == address.lower():
                    return {'text': '%s…%s is your own wallet — sending there moves nothing. Remove it and ask again.' % (address[:6], address[-4:])}
                sinks.append({'op': 'send', 'to': to, 'name': who, 'amount': (sk, sa)})
            continue
        if v == 'keep':
            sinks.append({'op': 'keep', 'amount': (kind if kind else 'rest', amt)})
            continue
    if src is None:
        return None
    if len(sinks) > 4:
        return {'text': 'A plan can end in at most 4 steps — split it into two.'}
    if (src['op'] == 'perps_out' or src.get('more')) and not sinks:
        sinks.append({'op': 'keep', 'amount': ('rest', None)})       # "withdraw from aftermath and swap to sui" / "…from suilend and navi": it comes home
    if not sinks and len(swaps) <= 1:
        return None                                  # withdraw-then-swap / a plain swap: their own cards (with a slider) do it
    if not sinks:
        sinks.append({'op': 'keep', 'amount': ('rest', None)})
    # amounts: "all"/none on the last step = the rest; a lone step takes everything
    if len(sinks) == 1:
        sinks[0]['amount'] = ('rest', None)
    for i, k in enumerate(sinks):
        if k['amount'][0] in (None, 'all') and i == len(sinks) - 1:
            k['amount'] = ('rest', None)
        elif k['amount'][0] in (None, 'all'):
            return {'text': 'How should I split it between the steps? For example "lend half … and send the rest …".'}
    if src and src.get('op') == 'perps_out' and any(k['op'] == 'perps' for k in sinks):
        return {'text': 'That USDC is already in your perps account.'}
    if any(k['op'] == 'perps' for k in sinks):
        if sum(1 for k in sinks if k['op'] == 'perps') > 1:
            return {'text': 'One perps deposit per transaction, please.'}
        if hand != 'USDC':
            if len(swaps) >= 2:
                return {'text': 'Perps margin is USDC, and that would need a third swap. Please split it.'}
            swaps.append({'to': 'USDC'}); hand = 'USDC'      # margin is USDC: swap to it first
        pk = next(k for k in sinks if k['op'] == 'perps')
        later = sinks[sinks.index(pk) + 1:]
        others = [k for k in sinks if k is not pk]
        if pk['amount'][0] == 'rest' and later:             # "deposit it into perps, then lend": nothing would be left to lend
            return {'text': 'Once it is in your perps account there is nothing left for the next step. Say how to split it, '
                            'e.g. "lend half on NAVI and put the rest into perps".'}
        if pk['amount'][0] == 'frac':                         # "half into perps, lend the rest": the perps step takes the rest instead
            rest = [k for k in others if k['amount'][0] == 'rest']
            if rest:
                fixed = sum(k['amount'][1] for k in others if k['amount'][0] == 'frac')
                rest[0]['amount'] = ('frac', round(max(1 - pk['amount'][1] - fixed, 0), 6))
                if rest[0]['amount'][1] <= 0:
                    return {'text': 'Those shares add up to more than everything.'}
            elif others:
                return {'text': 'Say where the rest goes, e.g. "half into perps and lend the rest on NAVI".'}
        elif pk['amount'][0] == 'units' and others:
            return {'text': 'With other steps, give the perps deposit as a share ("half") or make it the last step ("the rest into perps").'}
        sinks = others + [pk]                                  # Aftermath appends its deposit, so it goes last and takes the rest
        if pk['amount'][0] == 'units' and src and src.get('op') == 'wallet' and not swaps and abs(float(src['amount']) - pk['amount'][1]) > 1e-9:
            return {'text': 'Start with exactly the amount for perps, e.g. "take %s USDC from my wallet and put it into perps".' % dec(pk['amount'][1])}
        pk['amount'] = ('rest', None)
        try:
            if not perp_account(address).get('has'):
                return {'text': 'You don\'t have an Aftermath perps account yet. Say "open perps account" first, then ask again.'}
        except Exception:
            pass
    if sum(1 for k in sinks if k['amount'][0] == 'rest') > 1:
        return {'text': 'Only one step can take "the rest" — give the others an amount.'}
    # "…including my remaining USDC in wallet": add the wallet's own coins of the same kind (2026-10-21)
    pm = re.search(r'\b(including|incl\.?|plus|together\s+with|along\s+with|as\s+well\s+as|and\s+also|also\s+(?:add|use|put))\b(.{0,50}?)\bwallet\b', msg, re.I) \
        or re.search(r'\b(and|with)\s+((?:all\s+)?(?:of\s+)?(?:my|the)\s+(?:remain\w*\s+|rest\s+of\s+|other\s+|leftover\s+|wallet\s+)?\w*\s*(?:in|from)\s+(?:my\s+)?)wallet\b', msg, re.I)
    if pm and src['op'] == 'dust':
        return {'text': 'Sweep the dust on its own first, then add wallet coins in a second request.'}
    if pm and src['op'] == 'wallet':
        return {'text': 'That\'s your wallet twice — say it as one amount, e.g. "use 15 %s from my wallet, then …".' % disp(src['coin'])}
    if pm and src['op'] in ('withdraw', 'perps_out') and src.get('pick') and re.search(r'\bsome\b', msg, re.I):   # "withdraw some … plus 5 from my wallet": a picker can't show both parts
        return {'text': 'How much should I start with? Say a number, "half" or "all".'}
    if pm and src['op'] in ('withdraw', 'perps_out'):
        after = re.match(r'\s*([A-Za-z]{2,10})', msg[pm.end():])
        said = [ckey(w) for w in re.findall(r'[A-Za-z]{2,10}', pm.group(2)) + ([after.group(1)] if after else []) if ckey(w)]
        if re.search(r'\bsome\b', pm.group(2), re.I) and not re.search(r'\d', pm.group(2)):
            have0 = spendable(src['coin'], hold)
            return {'text': 'How much %s should I add from your wallet? You have %s.' % (disp(src['coin']), dec(have0)),
                    'widget': {'type': 'choose', 'options': [{'label': l, 'ask': re.sub(r'(?i)\bsome\b', v, msg, count=1)}
                                                             for l, v in (('Half (%s)' % dec(have0 / 2), dec(have0 / 2)), ('All (%s)' % dec(have0), 'all'))]}}
        if said and said[0] != src['coin']:
            return {'text': 'I can add your wallet\'s %s only if it\'s the same coin as the withdrawal (%s). Swap it first, or say it as two steps.' % (disp(said[0]), disp(src['coin']))}
        have = spendable(src['coin'], hold)
        nm = re.search(r'(\d+(?:\.\d+)?)', pm.group(2))
        plus = float(nm.group(1)) if nm else have
        if plus <= 0:
            return {'text': 'Your wallet has no %s to add.' % disp(src['coin'])}
        if plus > have + 1e-9:
            return {'text': 'Your wallet has %s %s — less than %s.' % (dec(have), disp(src['coin']), dec(plus))}
        why = spend_check(src['coin'], plus, address)
        if why:
            return {'text': why}
        src['plus'] = round(plus, 9)
        src['est'] = float(src['est'] or 0) + plus
    # estimates for the card: the swap router's real quote for each swap, the same route the transaction will use (2026-10-24:
    # a price-based guess minus 0.3% showed 9.922 USDsui for 9.951 USDC, then 9.949 arrived). Prices only if the router can't answer.
    est, px, cur = src['est'], src['px'], src['coin']
    for s in swaps:
        p2 = coin_px(s['to']) or 0
        try:
            s['est'] = float(swap_quote(cur, s['to'], est)['out']) if est > 0 else 0
        except Exception:
            s['est'] = est * px / p2 if p2 else 0
        est, px, cur = s['est'], p2, s['to']
    for k in sinks:
        if k['op'] == 'lend':
            rows = lend_rows_for(hand)
            m = k['mk']
            if m == 'there':
                m = last_mkt
            if m and m.startswith('fam:'):
                rows = [r for r in rows if r['market'].split('-')[0] == m[4:]]
                m = rows[0]['market'] if rows else None
            elif m == 'best':
                m = rows[0]['market'] if rows else None
            row = next((r for r in rows if r['market'] == m), None)
            if not row:
                where_ok = [LEND_MKTS.get(r['market'], r['name']) for r in lend_rows_for(hand)]
                if src['op'] == 'withdraw' and hand == src['coin'] and len(lend_rows_for(hand)) == 1 and lend_rows_for(hand)[0]['market'] == src['market']:
                    return {'text': '%s can only be lent on %s, where it already is. To move it, swap it on the way, e.g. "... as USDC".' % (disp(hand), where_ok[0])}
                # name the market, and offer the ones that do list it — the same request, that market swapped in (2026-10-24)
                want = k['mk'] if k['mk'] not in ('there', 'best') else (m or '')
                wname = LEND_MKTS.get(want) or (want[4:].title().replace('Navi', 'NAVI') if want.startswith('fam:') else 'that market')
                pat = dict(MKT_PHRASES).get(want)
                spans = list(re.finditer(r'\b(?:%s)\b' % pat, msg, re.I)) if pat else []
                def _res(x):                                  # where another lend step of this request goes
                    mk2 = x.get('mk') or ''
                    if mk2.startswith('fam:'):
                        return next((r['market'] for r in lend_rows_for(hand) if r['market'].split('-')[0] == mk2[4:]), None)
                    return mk2
                used = {_res(x) for x in sinks if x is not k and x.get('op') == 'lend'}
                opts = []
                if spans:
                    sp = spans[-1]                            # the last mention: where the money goes, not where it comes from
                    for r in [r for r in lend_rows_for(hand) if r['market'] not in used][:3]:
                        nm = MKT_EXACT.get(r['market'], LEND_MKTS.get(r['market'], r['name']))
                        opts.append({'label': 'Use %s instead' % LEND_MKTS.get(r['market'], r['name']), 'sub': '%.2f%% a year' % float(r.get('total_apr') or 0),
                                     'ask': msg[:sp.start()] + nm + msg[sp.end():]})
                return {'text': '%s can\'t be lent on %s. It can be lent on: %s.' % (disp(hand), wname, ', '.join(where_ok) or 'no market Kivy supports'),
                        'widget': {'type': 'choose', 'options': opts} if opts else None}
            if src['op'] == 'withdraw' and row['market'] == src['market'] and hand == src['coin'] and not src.get('more') \
                    and len([x for x in sinks if x.get('op') in ('lend', 'send', 'keep', 'perps')]) == 1:   # part of a split may stay where it is
                return {'text': 'That is where your %s already is.' % disp(hand)}
            k['market'], k['apr'] = row['market'], round(float(row['total_apr']), 2)
    fixed = 0.0
    for k in sinks:
        kd, a = k['amount']
        k['est'] = est * a if kd == 'frac' else a if kd == 'units' else None
        fixed += k['est'] or 0
    if fixed > est * 1.0001:
        return {'text': 'Those amounts add up to %s %s, but you\'d have about %s. Try smaller amounts.' % (dec(fixed), hand, dec(est))}
    for k in sinks:
        if k['est'] is None:
            k['est'] = max(est - fixed, 0)
            said = k['amount'][1] if k['amount'][0] == 'rest' else None
            if said and abs(k['est'] - said) > max(said * 0.02, 1e-6):   # "send the remaining 10" but only 3 would be left
                return {'text': 'After the other steps you would have about %s %s left, not %s. Check the amounts.' % (dec(k['est']), disp(hand), dec(said))}
    # limits (testing phase): what leaves the wallet at the start, and every send to someone else
    if src['op'] == 'wallet':
        why = spend_check(src['coin'], src['amount'], address)
        if why:
            return {'text': why}
    for k in sinks:
        if k['op'] == 'send' and cap_usd() and (k['est'] or 0) * (px or 0) > cap_usd() * 1.0001:
            return {'text': "That send is about $%.2f, over my $%.0f limit per send. Try a smaller amount." % (k['est'] * px, cap_usd())}
    spec = {'source': dict({k: src[k] for k in ('op', 'coin', 'amount', 'market', 'plus', 'types') if k in src},
                           **({'more': [{k: x[k] for k in ('market', 'coin', 'amount')} for x in src['more']]} if src.get('more') else {})),
            'swaps': [{'to': s['to']} for s in swaps],
            'sinks': [dict({'op': k['op'], 'amount': ({'rest': True} if k['amount'][0] == 'rest' else
                                                      {'frac': k['amount'][1]} if k['amount'][0] == 'frac' else {'units': k['amount'][1]})},
                           **({'market': k['market']} if k['op'] == 'lend' else {}), **({'to': k['to']} if k['op'] == 'send' else {}))
                      for k in sinks]}
    return chain_card(spec, src, swaps, sinks, hand)


def chain_card(spec, src, swaps, sinks, hand):
    """The step-by-step card: what happens, in order, with estimates. One approval for all of it."""
    steps = []
    if src['op'] == 'wallet':
        steps.append({'icon': 'wallet', 'coin': src['coin'], 'title': 'Take %s %s from your wallet' % (dec(src['amount']), disp(src['coin'])),
                      'q': src['amount'], 'tt': 'Take {q} %s from your wallet' % disp(src['coin'])})
    elif src['op'] == 'dust':
        steps.append({'icon': 'swap', 'coin': src['coin'], 'title': 'Sweep %d dust coin%s into %s' % (len(src['types']), '' if len(src['types']) == 1 else 's', disp(src['coin'])),
                      'sub': '≈ %s %s · %s' % (dec(src['est']), disp(src['coin']), ', '.join(src.get('syms') or []))})
    elif src['op'] == 'perps_out':
        steps.append({'icon': 'perps', 'coin': 'USDC', 'proto': 'aftermath', 'title': 'Withdraw %s USDC from your perps account' % ('all your free' if src['amount'] == 'all' else dec(src['amount'])),
                      'sub': '≈ %s USDC · Aftermath perps margin' % dec(src['est'])})
    elif src.get('more'):                              # several markets, one coin: a withdraw step each
        for x in [{'market': src['market'], 'amount': src['amount'], 'est': src.get('first_est')}] + src['more']:
            steps.append({'icon': 'withdraw', 'coin': src['coin'], 'proto': x['market'],
                          'title': 'Withdraw %s %s from %s' % ('all your' if x['amount'] == 'all' else dec(x['amount']), disp(src['coin']), LEND_MKTS.get(x['market'], x['market'])),
                          'sub': '≈ %s %s' % (dec(x.get('est') or 0), disp(src['coin']))})
    else:
        steps.append({'icon': 'withdraw', 'coin': src['coin'], 'title': 'Withdraw %s %s from %s' % ('all your' if src['amount'] == 'all' else dec(src['amount']),
                                                                                          disp(src['coin']), LEND_MKTS.get(src['market'], src['market'])),
                      'sub': '≈ %s %s' % (dec(src['est']), disp(src['coin'])),
                      'q': src['est'], 'tt': 'Withdraw {q} %s from %s' % (disp(src['coin']), LEND_MKTS.get(src['market'], src['market'])),
                      'ts': '≈ {q} %s' % disp(src['coin'])})
    if src.get('plus'):
        steps.append({'icon': 'wallet', 'coin': src['coin'], 'title': 'Add %s %s from your wallet' % (dec(src['plus']), disp(src['coin'])),
                      'sub': 'together ≈ %s %s' % (dec(src['est']), disp(src['coin']))})
    prev = src['coin']
    for s in swaps:
        sp = round(slip_for(prev, s['to']) * 100, 3)
        steps.append({'icon': 'swap', 'coin': s['to'], 'title': 'Swap %s to %s' % (disp(prev), disp(s['to'])), 'sub': '≈ %s %s (max %g%% slippage)' % (dec(s['est']), disp(s['to']), sp),
                      'q': s['est'], 'ts': '≈ {q} %s (max %g%% slippage)' % (disp(s['to']), sp)})
        prev = s['to']
    for k in sinks:
        amt = '≈ %s %s' % (dec(k['est']), disp(hand))
        if k['op'] == 'lend':
            steps.append({'icon': 'lend', 'coin': hand, 'proto': k['market'], 'title': 'Lend %s on %s' % (amt, LEND_MKTS.get(k['market'], k['market'])),
                          'q': k['est'], 'tt': 'Lend ≈ {q} %s on %s' % (disp(hand), LEND_MKTS.get(k['market'], k['market'])),
                          'sub': '%.2f%% a year' % k['apr'] + (' · Kivy fee %g%%' % rev_lend_pct() if rev_lend_pct() else '')})
        elif k['op'] == 'perps':
            steps.append({'icon': 'perps', 'coin': 'USDC', 'proto': 'aftermath', 'title': 'Deposit %s into your perps account' % amt,
                          'q': k['est'], 'tt': 'Deposit ≈ {q} %s into your perps account' % disp(hand),
                          'sub': 'Aftermath perps margin — no position is opened'})
        elif k['op'] == 'send':
            nm = (k['name'][:6] + '…' + k['name'][-4:]) if ADDR_RE.match(k['name']) else k['name']      # the full address goes below
            steps.append({'icon': 'send', 'coin': hand, 'title': 'Send %s to %s' % (amt, nm), 'sub': 'Full address: ' + k['to'], 'full': True,
                          'q': k['est'], 'tt': 'Send ≈ {q} %s to %s' % (disp(hand), nm)})
        else:
            steps.append({'icon': 'keep', 'coin': hand, 'title': 'Keep %s in your wallet' % amt, 'q': k['est'], 'tt': 'Keep ≈ {q} %s in your wallet' % disp(hand)})
    for st in steps:                                 # every coin's own icon (haSUI, LBTC, …), not just Kivy's seven
        st['icon'] = coin_icon(st.get('coin'))
    n = len(steps)
    return {'text': 'Here is the plan: %d steps in one transaction — you approve once, and if any step fails nothing happens.' % n,
            'widget': dict({'type': 'chain', 'steps': steps, 'spec': spec, 'fee_pct': rev_fee_pct() if swaps else 0},
                           **({'pick': {'coin': src['coin'], 'max': src['max'], 'base': src['est'] if src['amount'] == 'all' else src['amount'],
                                        'px': src.get('px') or 0, 'all': src['op'] == 'withdraw'}}
                              if src.get('pick') and src.get('max') and all(k['amount'][0] != 'units' for k in sinks) else {}))}


def lend_post(path, body, timeout=300):
    """POST to Kivy's builder (localhost only). A swap route that a DEX on it rejects in the test run (MoveAbort in a
    'router' module — the 0x7de5… adapter flakes now and then) gets ONE retry with a fresh route (2026-10-25)."""
    def once():
        req = urllib.request.Request('http://127.0.0.1:8799' + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    out = once()
    if isinstance(out, dict) and out.get('ok') is False and re.search(r'MoveAbort.*router::|router::assert', str(out.get('error') or ''))             and '(tried ' not in str(out.get('error')):   # the builder already tried every router (2026-10-26): no second round
        print('kivy route retry: %s %s' % (path, str(out.get('error'))[:120]), flush=True)
        out = once()
    return out


def lend_positions_rows(address):
    """Every coin lent (USDC + others), as withdraw rows."""
    pos = lend_positions(address, fresh=True)
    rows = [{'id': p, 'name': PROTOS[p], 'total': round(apr_of(p) or 0, 2), 'lent': round(v, 6), 'coin': 'USDC', 'px': 1.0, 'icon': '/icons/USDC.svg'}
            for p, v in ((p, float((pos.get(p) or {}).get('usdc') or 0)) for p in PROTOS) if v >= 0.01]
    return rows + other_lent(address, fresh=True)


def wd_preset(card, msg):
    """"withdraw ALL my usdc from navi": the slider starts at the whole position (it started at 0 — live test 2026-10-25); "half" at half."""
    w = card.get('widget') or {}
    if w.get('type') == 'withdraw' and not w.get('preset'):
        if re.search(r'\b(?:all|everything|max|entire|whole|full)\b', msg, re.I):
            w['preset'] = w.get('max') or 0
        elif re.search(r'\bhalf\b', msg, re.I):
            w['preset'] = round(float(w.get('max') or 0) / 2, 9)
        elif not re.search(r'\d|\b(?:some|part|bit|little)\b', msg, re.I):
            w['preset'] = w.get('max') or 0           # no amount = all of it (KIVYSOUL: "A withdrawal with no amount = all of it")
        else:                                         # "withdraw 0.05 sui from navi sui high yield": the typed amount of that coin (2026-10-27)
            m = re.search(r'(?<![\d.$])(\d+(?:\.\d+)?)\s*(?:%s)\b(?!\s*%%)' % re.escape(str(w.get('coin') or '')), msg, re.I)
            if m and 0 < float(m.group(1)) <= float(w.get('max') or 0) + 1e-9:
                w['preset'] = float(m.group(1))
    return card


def withdraw_card(row, to=None):
    to = to if to in to_opts(row['coin']) else row['coin']
    return {'text': 'Slide to choose how much %s to withdraw from %s%s.' % (disp(row['coin']), row['name'],
                                                                           (', then swap it to %s — one approval' % disp(to)) if to != row['coin'] else ''),
            'widget': {'type': 'withdraw', 'coin': row['coin'], 'protocol': row['id'], 'name': row['name'], 'apr': row['total'],
                       'px': row['px'], 'balance': row['lent'], 'max': row['lent'], 'to': to, 'to_opts': to_opts(row['coin']), 'fee_pct': rev_fee_pct()}}


def lend_positions(addr, fresh=False):
    """{market: {market, name, usdc}} — USDC lent per market, from the same read as every other coin: positions-all computes
    USDC with the same function as 'positions' (verified on a live NAVI deposit 2026-10-25) — one chain scan, not two (W-04)."""
    hit = _LEND['pos'].get(addr)
    if pos_cached(hit, addr, fresh):
        return hit[1]
    try:
        rows = lend_positions_all(addr, fresh)
    except Exception:                                    # one retry: these reads fail now and then (RPC hiccups)
        time.sleep(1)
        rows = lend_positions_all(addr, True)
    v = {p: {'market': p, 'name': PROTOS[p], 'usdc': round(sum(float(r.get('amount') or 0) for r in rows if r.get('market') == p and str(r.get('token')).upper() == 'USDC'), 9)}
         for p in PROTOS}
    _LEND['pos'][addr] = (time.time(), v)
    return v


def best_proto():
    try:
        r = lend_rates()
        return max(PROTOS, key=lambda p: float((r.get(p) or {}).get('total_apr') or 0))
    except Exception:
        return 'navi'


def apr_of(p):
    try:
        return float((lend_rates().get(p) or {}).get('total_apr') or 0)
    except Exception:
        return None


WORD_AMOUNTS = {'all': 1.0, 'max': 1.0, 'everything': 1.0, 'entire': 1.0, 'half': 0.5, 'a quarter': 0.25, 'quarter': 0.25, 'a third': 1 / 3, 'third': 1 / 3}


def word_amount(word, coin, holdings):
    """'all BTC' -> the wallet's whole BTC (SUI keeps a little back for gas). None if unknown."""
    w = ' '.join(str(word).lower().split())
    pc = re.match(r'^(\d+(?:\.\d+)?)\s*%$', w)          # "10%" of the coin (2026-10-25: it was read as 10 coins)
    share = (float(pc.group(1)) / 100 if 0 < float(pc.group(1)) <= 100 else None) if pc else WORD_AMOUNTS.get(w)
    if share is None or holdings is None:
        return None
    have = float(holdings.get(coin) or 0.0)
    if coin == 'SUI':
        have = max(0.0, have - max(float(K()['gas_sui']) * 3, 0.01) - fee_reserve())
    return have * share


def plan(kind, amt, a, b=None, holdings=None, recipient=None, protocol=None, lent=None, all_=False, sender=None):
    k = K()
    ASSETS, CAP_USD, VENUES = k['assets'], cap_usd(), k['venues']
    GAS_SUI, SLIPPAGE_CAP = float(k['gas_sui']), float(k['slippage_cap_pct'])
    a, b = SPOT_IN.get(a, a), SPOT_IN.get(b, b) if b else b
    a, b = ckey(a) or a, (ckey(b) or b) if b else b       # 'USDSUI' -> 'USDsui'
    bad = [x for x in (a, b) if x and x not in ASSETS and not (kind in ('swap', 'send') and x in ecoins())]
    if bad:
        return {'text': "I can't do that: %s isn't on my allowed list (%s)."
                        % (', '.join(bad), ', '.join(ASSETS))}
    if kind in ('lend', 'withdraw'):
        if a != 'USDC':                                    # other coins use the lending card
            return {'text': 'Say "lend %s" to open its lending card (best market first), or tap it in "lending markets".' % a}
        protocol = (protocol or '').lower() or None
        if protocol and protocol not in PROTOS:
            protocol = market_in(protocol)
            if not protocol:
                return {'text': 'I can lend USDC on: %s.' % ', '.join(PROTOS.values())}
        if kind == 'lend' and not protocol:
            protocol = best_proto()
        if kind == 'withdraw':
            lent = lent or {}
            have_lent = {p: float((lent.get(p) or {}).get('usdc') or 0) for p in PROTOS}
            have_lent = {p: (v if v >= 0.01 else 0.0) for p, v in have_lent.items()}   # ignore rounding dust
            if not protocol:
                protocol = max(have_lent, key=have_lent.get)
            if have_lent.get(protocol, 0) <= 0:
                app = float((lent.get(protocol) or {}).get('in_app') or 0)
                return {'text': ('You have no USDC in %s through Kivy.' % PROTOS[protocol])
                                + (' Your %s USDC in a Suilend account is withdrawn in the Suilend app.' % dec(app) if app else '')}
            if all_:
                amt = have_lent[protocol]
            elif amt > have_lent[protocol] + 1e-9:
                return {'text': 'You have %s USDC lent on %s — try that much or less.' % (dec(have_lent[protocol]), PROTOS[protocol])}
    if kind == 'swap' and a == b:
        return {'text': 'Swapping %s to %s does nothing — pick two different coins.' % (a, b)}
    if not math.isfinite(amt):
        return {'text': 'That amount is not a number I can use.'}
    if amt <= 0:
        if holdings is not None and kind in ('swap', 'send', 'lend') and not float(holdings.get(a) or 0):
            return {'text': 'You have 0 %s, so there is nothing to %s.' % (a, kind)}
        return {'text': 'The amount has to be more than zero.'}
    px = prices()
    p_a = px.get(a) or (coin_px(a) if a not in ASSETS else None)
    if CAP_USD and kind != 'withdraw' and not (isinstance(p_a, (int, float)) and math.isfinite(p_a) and p_a > 0):
        return {'text': "I can't get a live %s price right now, so I can't check the $%.0f cap. Try again in a minute." % (a, CAP_USD)}
    usd = amt * p_a if p_a else None
    if CAP_USD and usd is not None and usd > CAP_USD and kind != 'withdraw':   # taking your own money back is never capped
        return {'text': "That's about $%.2f, over my $%.0f cap. I won't plan it — try a smaller amount."
                        % (usd, CAP_USD)}
    gas_usd = GAS_SUI * px.get('SUI', 1.0)
    have = None
    to_addr = None
    if kind == 'send':
        to_addr = resolve_recipient(recipient)
        if not to_addr:
            return {'text': "I couldn't read that recipient. Give a full 0x… address (64 characters) or a .sui name."}
        if sender and to_addr.lower() == sender.lower():   # never a card that sends to the sending account itself (2026-10-21)
            accts = [x for x in xw_accounts(sender) if x['a'].lower() != sender.lower()]
            return {'text': 'That address is this account (%s…%s) — sending there moves nothing.%s' % (sender[:6], sender[-4:],
                            (' To send from your other account %s…%s instead, switch to it in your wallet and ask again.' % (accts[0]['a'][:6], accts[0]['a'][-4:]))
                            if accts else ' Paste the address you want to send to.')}
    if holdings is not None and kind in ('swap', 'lend', 'send'):
        have = float(holdings.get(a) or 0.0)
        spare = have - ((max(GAS_SUI * 3, 0.01) + fee_reserve()) if a == 'SUI' else 0.0)     # keep a little SUI for gas (+ Kivy's fee)
        if amt > spare + 1e-9:                         # float room: "all" = balance − 0.01 must pass (0.912597 − 0.01 = 0.90259699…)
            return {'text': 'Your wallet has %s %s%s, not enough for %s. Try a smaller amount.'
                            % (dec(have), a, ' (a little SUI is kept back for gas)' if a == 'SUI' else '', dec(amt))}
    held = ('Your wallet holds %s %s — enough ✓' % (dec(have), a)) if have is not None else None
    if kind == 'swap':
        p_b = px.get(b) or coin_px(b)
        out = (usd / p_b) if (usd is not None and p_b) else None
        steps = [held or 'Check the wallet holds %s %s' % (dec(amt), a),
                 'Get a route %s → %s from the %s' % (a, b, VENUES['swap']),
                 'Swap %s %s for about %s %s (refuse if slippage > %g%%)'
                 % (dec(amt), a, dec(out) if out else '?', b, SLIPPAGE_CAP),
                 'Confirm the %s arrived and report the real amount' % b]
        risks = ['Price can move before the swap lands — capped at %g%% slippage' % SLIPPAGE_CAP,
                 'Router fees are included in the quote, not on top']
        title = 'Swap %s %s → %s' % (dec(amt), a, b)
    elif kind == 'send':
        steps = [held or 'Check the wallet holds %s %s' % (dec(amt), a),
                 'Send %s %s to %s' % (dec(amt), a, to_addr),
                 'Confirm it left the wallet and report the transaction']
        risks = ['A transfer cannot be undone: check the full address above, character by character',
                 'Sending to an exchange? Make sure it supports %s on the Sui network' % a]
        title = 'Send %s %s' % (dec(amt), a)
        if recipient and not ADDR_RE.match(recipient.strip()):
            title += ' to %s' % recipient.strip()
    elif kind == 'lend':
        apr = apr_of(protocol)
        steps = [held or 'Check the wallet holds %s %s' % (dec(amt), a),
                 'Deposit %s %s into %s%s' % (dec(amt), a, PROTOS[protocol], (' — earns about %.2f%% a year' % apr) if apr else ''),
                 'Supply only: Kivy never borrows. Withdraw any time']
        risks = ['Rates change with demand', 'If %s were hacked, deposits could be lost' % PROTOS[protocol]]
        title = 'Lend %s %s on %s' % (dec(amt), a, PROTOS[protocol])
    else:
        steps = ['You have %s USDC lent on %s' % (dec(float((lent.get(protocol) or {}).get('usdc') or 0)), PROTOS[protocol]),
                 'Withdraw %s %s back to your wallet' % ('all' if all_ else dec(amt), a)]
        risks = ['A pool near full use may not return everything at once']
        title = 'Withdraw %s %s from %s' % ('all' if all_ else dec(amt), a, PROTOS[protocol])
    action = None
    if kind == 'swap':
        action = {'kind': 'swap', 'from': a, 'to': b, 'amount': amt, 'label': 'Approve the swap in your wallet'}
    elif kind == 'send':
        action = {'kind': 'send', 'coin': a, 'amount': amt, 'to': to_addr, 'label': 'Approve the transfer in your wallet'}
    elif kind in ('lend', 'withdraw'):
        action = {'kind': 'lend', 'protocol': protocol, 'name': PROTOS[protocol], 'action': 'deposit' if kind == 'lend' else 'withdraw',
                  'amount': 'all' if all_ else amt,
                  'label': 'Approve the %s in your wallet' % ('deposit' if kind == 'lend' else 'withdrawal')}
    title, steps = disp_text(title), [disp_text(x) for x in steps]
    rows = plan_rows(kind, a, b, amt, to_addr, protocol, all_, usd, px, lent)
    return {'text': k['plan_intro'],
            'plan': {'title': title, 'value_usd': round(usd, 2) if usd is not None else None,
                     'amount': None if all_ else dec(amt), 'coin': disp(a),
                     'action': action, 'to_full': to_addr, 'rows': rows,
                     'venue': (('%s · %.2f%% a year' % (PROTOS[protocol], apr_of(protocol) or 0)) if kind in ('lend', 'withdraw')
                               else VENUES.get(kind, 'Sui network')), 'steps': steps, 'risks': risks,
                     'cost': 'Free' if kind == 'send' and gasless_send(a, amt)
                             else 'Est. %s SUI' % dec(GAS_SUI) + (' + Kivy fee %g%%' % rev_fee_pct() if kind == 'swap' and rev_fee_pct() else '')
                                  + (' + Kivy fee %g%%' % rev_lend_pct() if kind == 'lend' and rev_lend_pct() else ''),
                     'status': k['plan_status']}}


def plan_rows(kind, a, b, amt, to_addr, protocol, all_, usd, px, lent):
    """The card's icon rows (2026-10-22: every confirm card in the icon style of the multi-step card)."""
    sh = lambda x: '%s…%s' % (x[:6], x[-4:])
    row = lambda c, t, sub='', proto=None: {'coin': disp(c), 'icon': coin_icon(c), 'title': disp_text(t), 'sub': sub, 'proto': proto}
    try:
        if kind == 'swap':
            p_b = px.get(b) or coin_px(b)
            out = (usd / p_b) if (usd is not None and p_b) else None
            return [row(a, 'Pay %s %s' % (dec(amt), a), ('≈$%.2f' % usd) if usd is not None else ''),
                    row(b, 'Receive ≈ %s %s' % (dec(out), b) if out else 'Receive %s' % b,
                        'at most %g%% slippage' % float(K()['slippage_cap_pct']))]
        if kind == 'send':
            return [row(a, 'Send %s %s' % (dec(amt), a), 'to %s' % sh(to_addr or ''))]
        if kind == 'lend':
            apr = apr_of(protocol)
            return [row(a, 'Lend %s %s on %s' % (dec(amt), a, PROTOS[protocol]), ('%.2f%% a year' % apr) if apr else '', protocol)]
        if kind == 'withdraw':
            have = float(((lent or {}).get(protocol) or {}).get('usdc') or 0)
            return [row(a, 'Withdraw %s %s from %s' % ('all' if all_ else dec(amt), a, PROTOS[protocol]),
                        'back to your wallet' + (' · %s lent now' % dec(have) if have else ''), protocol)]
    except Exception:
        pass
    return None


_RCPT = r'(0x[0-9a-fA-F]{64}|@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b|@[a-z0-9-]{2,})'


def send_multi_parse(msg, rcpts, address):
    """Several recipients in one message, each with an amount (or one amount "each"): one Send card, ONE transaction.
    None when the words don't pair every recipient with an amount — the caller then asks which one."""
    segs = re.findall(r'(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+to\s+' + _RCPT, msg, re.I)
    each = re.search(r'(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:each\s+to|to\s+each\s+of)\s+(.+)$', msg, re.I)
    if each:
        segs = [(each.group(1), each.group(2), r) for r in rcpts]
    if len(segs) != len(rcpts) or len(segs) < 2:
        return None
    hold = dict(all_balances(address) or {})
    lines, need = [], {}
    for amt, coin, who in segs:
        c = ckey(coin) or lend_token(coin)
        if not c:
            return {'text': "%s isn't a coin I can send. Check the name and ask again." % coin}
        to = resolve_recipient(who)
        if not to:
            return {'text': 'I couldn\'t find %s. Give a full 0x… address or a .sui name that exists.' % who}
        if to.lower() == address.lower():
            return {'text': 'One of those is your own wallet (%s…%s) — sending there moves nothing. Remove it and ask again.' % (address[:6], address[-4:])}
        a = float(amt)
        if not a > 0:
            return {'text': 'Every transfer needs an amount above zero.'}
        need[c] = need.get(c, 0.0) + a
        lines.append({'source': 'wallet', 'coin': c, 'amount': a, 'to': to, 'name': who})
    if len(lines) > 12:
        return {'text': 'Send 1 to 12 transfers at a time — split it into two requests.'}
    for c, a in need.items():                        # all the transfers of one coin together must fit (SUI keeps a little for gas)
        have = spendable(c, hold)
        if a > have + 1e-9:
            return {'text': 'Those transfers need %s %s in total, but your wallet has %s%s.' % (dec(a), disp(c), dec(have), ' (a little SUI is kept for gas)' if c == 'SUI' else '')}
    usd = sum(l['amount'] * (1.0 if l['coin'] in ('USDC', 'USDsui') else float(coin_px(l['coin']) or 0)) for l in lines)
    if cap_usd() and usd > cap_usd() + 0.005:
        return {'text': "That's about $%.2f in total, over my $%.0f cap. Try smaller amounts." % (usd, cap_usd())}
    sh = lambda a: a if not ADDR_RE.match(a) else a[:6] + '…' + a[-4:]
    k = K()
    return {'text': 'Here\'s the plan: %d transfers in one transaction — you approve once, and if any part fails nothing is sent.' % len(lines),
            'plan': {'title': disp_text('Send ' + ' + '.join('%s %s to %s' % (dec(l['amount']), l['coin'], sh(l['name'] if not ADDR_RE.match(l['name']) else l['to'])) for l in lines)),
                     'value_usd': round(usd, 2), 'amount': None, 'coin': None,
                     'action': {'kind': 'sendmany', 'lines': [{'source': 'wallet', 'coin': l['coin'], 'amount': l['amount'], 'to': l['to']} for l in lines],
                                'label': 'Approve the transfers in your wallet'},
                     'venue': 'Sui network · one transaction',
                     'to_list': ['%s · %s %s' % (l['to'], dec(l['amount']), disp(l['coin'])) for l in lines],   # every full address, shown above Approve
                     'steps': [disp_text('Send %s %s to %s' % (dec(l['amount']), l['coin'], l['to'])) for l in lines] +
                              ['All in one transaction: one approval, and if any part fails nothing is sent'],
                     'risks': ['A transfer cannot be undone: check every full address above, character by character',
                               'Sending to an exchange? Make sure it supports the coin on the Sui network'],
                     'cost': 'Est. %s SUI' % dec(float(k['gas_sui'])), 'status': k['plan_status']}}


def plan_send_many(items, recipient, holdings, sender):
    """Several coins to one recipient in ONE transaction, one approval (2026-10-22: 'send several assets at the same time').
    items = [(coin, amount)]. Each coin passes the single-send checks; the $ cap applies to the total."""
    k = K()
    parts, usd = [], 0.0
    for c, amt in items:
        one = plan('send', amt, c, recipient=recipient, holdings=holdings, sender=sender)
        if not one.get('plan'):
            return one
        parts.append(one['plan'])
        usd += one['plan'].get('value_usd') or 0.0
    if cap_usd() and usd > cap_usd() + 0.005:
        return {'text': "That's about $%.2f in total, over my $%.0f cap. I won't plan it — send one coin at a time or a smaller amount." % (usd, cap_usd())}
    to_addr = parts[0]['to_full']
    what = ' + '.join('%s %s' % (dec(a), disp(c)) for c, a in items)
    title = disp_text('Send %s' % what)
    if recipient and not ADDR_RE.match(recipient.strip()):
        title += ' to %s' % recipient.strip()
    return {'text': 'Here\'s the plan: every coin in one transaction. Nothing happens until you approve it in your wallet — once.',
            'plan': {'title': title, 'value_usd': round(usd, 2), 'amount': None, 'coin': None,
                     'action': {'kind': 'send', 'items': [{'coin': c, 'amount': a} for c, a in items], 'to': to_addr,
                                'label': 'Approve the transfer in your wallet'},
                     'to_full': to_addr, 'venue': 'Sui network · one transaction',
                     'rows': [r for p in parts for r in (p.get('rows') or [])],
                     'steps': [disp_text('Send %s %s to %s' % (dec(a), c, to_addr)) for c, a in items] +
                              ['All in one transaction: one approval, and if any part fails nothing is sent'],
                     'risks': ['A transfer cannot be undone: check the full address above, character by character',
                               'Sending to an exchange? Make sure it supports each of these coins on the Sui network'],
                     'cost': 'Est. %s SUI' % dec(float(k['gas_sui'])), 'status': k['plan_status']}}


DIGEST_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def explorer_tx(digest):
    return ('https://suivision.xyz/txblock/' if NET() == 'mainnet' else 'https://%s.suivision.xyz/txblock/' % NET()) + digest


ROUTER_FEE_WALLETS_SRV = {'0x1fb0c0a6790860fc83c8f26f6fa089868ad94de9468348f98dfb4cdf89a53777'}   # Aftermath router's fee wallet (the page trusts every route-only coin, 2026-10-25)
# the swap aggregators' entry programs: Aftermath router, Cetus aggregator v3, Bluefin 7k. 2026-10-28: "hop from dex agg
# (aftermath, cetus, bluefin swap aggregator) is ok, where it go" — a coin only the route touches may go to any address
# (a pool's own fee wallet, e.g. MAGMA in an Aftermath AMM pool); the user's own coins keep the 1% limit and every receive check
# A route-only coin may go anywhere ONLY in a pure aggregator swap: an aggregator is called and nothing outside a router
# pulls value out of a protocol (review pre-launch H1, 2026-10-28: with 'any call to an aggregator', a crafted transaction
# could add a lending withdraw / perps collateral withdraw and send that coin — which never touches the wallet — to anyone).
# Framework (0x1, 0x2) helpers don't count. False positives only fall back to the strict rule.
OUTFLOW_FN = re.compile(r'withdraw|redeem|borrow|claim|collateral|unstake|liquidat', re.I)


def pure_agg_swap(calls):
    n = lambda a: re.sub(r'^0x0*', '', str(a or '').lower())
    cs = [str(c).split('::') for c in calls or [] if str(c).count('::') >= 2]
    agg = any(n(c[0]) in {n(x) for x in AGG_PKGS_SRV} for c in cs)
    pull = any(OUTFLOW_FN.search(c[2]) and c[1] != 'router' and n(c[0]) not in ('1', '2') for c in cs)
    return bool(agg and not pull)


AGG_PKGS_SRV = {'0x7de5de8d75a8f4e42cdd3c018f788bc9b9ebf2d3d61dcfe9d2136f17f077afd5', '0xffd4058af7d6f6d66c335930cced8c91e38ec943e2392232aee27e8127e684e9', '0x62412b7268c35f3808336aee57a52836501f40b8ba5d936f8ad275e672befd04'}


def slip_for(a, b):
    """Swap slippage (a fraction) for the pair a -> b: the tightest tier that holds BOTH coins (stablecoins 0.05%, SUI and its
    staked versions 0.1%, big coins 0.2%), else Kivy's ceiling (0.5%). 2026-10-24: "as low as possible". Same rule in lend.ts."""
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


def tx_detail(digest):
    """What a transaction did, read from the chain: who sent what to whom, fee, time."""
    if not DIGEST_RE.match(digest or ''):
        return {'error': 'That does not look like a transaction id.'}
    q = ('query($d: String!) { transaction(digest: $d) { digest sender { address } effects { status timestamp '
         'balanceChanges { nodes { owner { address } coinType { repr } amount } } '
         'gasEffects { gasSummary { computationCost storageCost storageRebate } } } } }')
    t = (gql(q, {'d': digest}) or {}).get('transaction')
    if not t or not t.get('effects'):
        return {'error': 'Not found on the chain yet — it can take a few seconds.'}
    e = t['effects']
    norm = lambda x: re.sub(r'^0x0*', '0x', x or '')
    syms = {norm(v['type']): (sym, int(v['decimals'])) for sym, v in ecoins().items()}   # every coin Kivy knows (USDsui has 6 decimals, not the default 9 — 2026-10-26)
    sender = (t.get('sender') or {}).get('address')
    g = e.get('gasEffects', {}).get('gasSummary') or {}
    fee_mist = int(g.get('computationCost') or 0) + int(g.get('storageCost') or 0) - int(g.get('storageRebate') or 0)
    by_coin = {}
    for n in (e.get('balanceChanges') or {}).get('nodes') or []:
        ct = norm(n['coinType']['repr'])
        sym, dp = syms.get(ct, (ct.split('::')[-1], 9))
        if '::reserve::CToken<' in ct:
            sym, dp = 'Suilend receipt (cUSDC)' if ct.rstrip('>').endswith('::usdc::USDC') else 'Suilend receipt', 6
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
        for a, x in outs:
            moves.append({'coin': disp(sym), 'amount': dec(x, 9, False), 'from': a, 'to': ins[0][0] if len(ins) == 1 else None})
        if not outs:
            for a, x in ins:
                moves.append({'coin': disp(sym), 'amount': dec(x, 9, False), 'from': None, 'to': a})
    return {'digest': t['digest'], 'status': e.get('status'), 'time_utc': e.get('timestamp'), 'sender': sender,
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


def fee_reserve():
    """Extra SUI to keep back when spending 'all' SUI. Kivy's fee is only on multi-step transactions now (2026-10-22), and a
    simple swap/send/lend pays none — the reserve stays at gas alone. (Multi-step plans leave room inside the builder.)"""
    return 0.0


def ref_code():
    c = str((K().get('revenue') or {}).get('aftermath_ref_code') or '').strip()
    return c if re.match(r'^[A-Za-z0-9_-]{2,40}$', c) else ''


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
    kind = json.loads(post('v1/transactions/trade', {'walletAddress': addr, 'completeRoute': route, 'slippage': slip}))['txKind']
    res = {'tx_kind': kind, 'expected_out': out, 'min_out': out * (1 - slip), 'coin_out': b, 'fee_pct': rev_fee_pct(), 'router': 'Aftermath'}
    # the transaction is run here first; when Aftermath's route fails in that run, the same swap is built with Cetus, then
    # Bluefin 7k (same coins, amount and slippage; their programs are on the allowlist) — 2026-10-26: "A, add bluefin agg too"
    try:
        chk = lend_post('/swapcheck', {'address': addr, 'kind': kind}, timeout=60)
        if chk.get('ok') is False and chk.get('route_fail'):
            alt = lend_post('/swapalt', {'address': addr, 'from': ca['type'], 'to': cb['type'], 'amount': base, 'slip': slip,
                                         'min': str(base_units(min_out, cb['decimals'])) if min_out else '0'}, timeout=90)   # the approved minimum, enforced on chain by the backup too (audit nv #5)
            if alt.get('ok'):
                d = 10 ** int(cb['decimals'])
                res.update(tx_kind=alt['tx_kind'], expected_out=int(alt['expected_base']) / d, min_out=int(alt['min_base']) / d, router=alt.get('router'))
            else:
                print('kivy swap backup failed: %s' % str(alt.get('error'))[:200], flush=True)
    except Exception as exc:                          # the check is extra: if it can't run, the card works as before
        print('kivy swap check skipped: %s' % str(exc)[:160], flush=True)
    return res


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
    from urllib.parse import quote
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
            'volume_24h': next((r['volume'] for r in perp_market_list() if r['symbol'] == sym), None)}


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


# ── Changing market by name (2026-10-21: "open korean market" got a vague answer) ──
# Full names for every market we are sure of; a ticker always works too. Groups ask with choices.
MARKET_NAMES = {
    'bitcoin': 'BTC', 'btc': 'BTC', 'xbtc': 'BTC', 'ethereum': 'ETH', 'ether': 'ETH', 'solana': 'SOL', 'sui': 'SUI', 'near protocol': 'NEAR',
    'zcash': 'ZEC', 'monero': 'XMR', 'pump fun': 'PUMP', 'pumpfun': 'PUMP', 'uniswap': 'UNI', 'ripple': 'XRP', 'hyperliquid': 'HYPE',
    'monad': 'MON', 'quant': 'QNT', 'curve': 'CRV', 'curve dao': 'CRV', 'spx6900': 'SPX', 'worldcoin': 'WLD', 'world coin': 'WLD',
    'litecoin': 'LTC', 'basic attention token': 'BAT', 'binance coin': 'BNB', 'bnb chain': 'BNB',
    'korean won': 'USDKRW', 'won': 'USDKRW', 'usd krw': 'USDKRW', 'dollar won': 'USDKRW', 'krw': 'USDKRW',
    'brazilian real': 'USDBRL', 'usd brl': 'USDBRL', 'dollar real': 'USDBRL', 'brl': 'USDBRL',
    'google': 'GOOGL', 'alphabet': 'GOOGL', 'nvidia': 'NVDA', 'tesla': 'TSLA', 'sandisk': 'SNDK', 'sk hynix': 'SKHYNIX', 'hynix': 'SKHYNIX',
    'circle': 'CRCL', 'samsung': 'SAMSUNG', 'samsung electronics': 'SAMSUNG', 'micron': 'MU', 'intel': 'INTC', 'marvell': 'MRVL',
    'iovance': 'IOVA', 'byd': 'BYD', 'eli lilly': 'LLY', 'lilly': 'LLY', 'apple': 'AAPL', 'meta': 'META', 'facebook': 'META',
    'amc entertainment': 'AMC', 'xiaomi': 'XIAOMI', 'bank of china': 'BOC',
    'silver': 'XAG', 'xag': 'XAG', 'gold': 'XAUT', 'xau': 'XAUT', 'tether gold': 'XAUT', 'brent': 'BRENT', 'brent crude': 'BRENT', 'brent oil': 'BRENT',
    'wti': 'WTI', 'wti crude': 'WTI', 'wti oil': 'WTI', 'us oil': 'WTI',
    's&p 500': 'US500', 's&p': 'US500', 'sp500': 'US500', 'sp 500': 'US500', 'sandp 500': 'US500', 'us 500': 'US500',
    'nasdaq': 'US100', 'nasdaq 100': 'US100', 'ndx': 'US100', 'us 100': 'US100', 'us tech 100': 'US100',
    'nifty': 'INDIA50', 'nifty 50': 'INDIA50', 'india 50': 'INDIA50', 'kospi': 'KR200', 'kospi 200': 'KR200', 'korea 200': 'KR200',
    'nikkei': 'JP225', 'nikkei 225': 'JP225', 'japan 225': 'JP225',
    # from a reviewer list (2026-10-21)
    'micron technology': 'MU', 'marvell technology': 'MRVL', 'iovance biotherapeutics': 'IOVA', 'meta platforms': 'META', 'west texas intermediate': 'WTI',
    's&p500': 'US500', 's and p 500': 'US500', 'standard and poors 500': 'US500', 'nasdaq100': 'US100', 'nifty50': 'INDIA50', 'spx 6900': 'SPX',
    'pumpfun ': 'PUMP', 'pump.fun': 'PUMP', 'deepbook': 'DEEP', 'walrus': 'WAL',
}
_COINWORDS = set(COIN_NAMES) | {a.lower() for a in (K().get('assets') or [])} | {'btc', 'xbtc', 'sui', 'eth', 'sol', 'usdc', 'usd'}
INTENT_FORMS |= {w for k in MARKET_NAMES for w in k.split() if w not in _COINWORDS} | {'krw', 'brl', 'kospi', 'nikkei', 'nifty'}   # market names are never typos (coin words stay coins)
MARKET_DESC = {'KR200': 'Korea 200 index (KOSPI 200)', 'JP225': 'Japan 225 index (Nikkei)', 'INDIA50': 'India 50 index (Nifty)', 'US500': 'S&P 500 index',
               'US100': 'Nasdaq 100 index', 'USDKRW': 'US dollar / Korean won', 'USDBRL': 'US dollar / Brazilian real', 'SAMSUNG': 'Samsung Electronics',
               'SKHYNIX': 'SK Hynix', 'XAUT': 'Gold', 'XAG': 'Silver', 'WTI': 'WTI crude oil', 'BRENT': 'Brent crude oil', 'BYD': 'BYD',
               'XIAOMI': 'Xiaomi', 'BOC': 'Bank of China', 'NVDA': 'Nvidia', 'AAPL': 'Apple', 'TSLA': 'Tesla', 'GOOGL': 'Alphabet (Google)',
               'META': 'Meta', 'MU': 'Micron', 'INTC': 'Intel', 'MRVL': 'Marvell', 'SNDK': 'SanDisk', 'LLY': 'Eli Lilly', 'CRCL': 'Circle',
               'BTC': 'Bitcoin', 'ETH': 'Ethereum', 'SOL': 'Solana', 'SUI': 'Sui', 'XRP': 'XRP', 'BNB': 'BNB', 'HYPE': 'Hyperliquid',
               'PUMP': 'Pump.fun', 'SPX': 'SPX6900 (memecoin)', 'LTC': 'Litecoin', 'XMR': 'Monero', 'ZEC': 'Zcash', 'UNI': 'Uniswap', 'NEAR': 'NEAR'}
MARKET_GROUPS = [   # (words, what it is, symbols — or a category to take the busiest from)
    (r'(?:us|u\.s\.|american)\s+ind(?:ex|exes|ices)', 'US index', ['US500', 'US100']),
    (r'asia\w*\s+ind(?:ex|exes|ices)', 'Asian index', ['KR200', 'JP225', 'INDIA50']),
    (r'asia\w*(?:\s+stocks?)?', 'Asian', ['KR200', 'JP225', 'INDIA50', 'SAMSUNG', 'SKHYNIX', 'BYD', 'XIAOMI', 'BOC']),
    (r'korea\w*|kr|kospi\s+market', 'Korea', ['KR200', 'SAMSUNG', 'SKHYNIX', 'USDKRW']),
    (r'japan\w*|jp', 'Japan', ['JP225']), (r'india\w*', 'India', ['INDIA50']),
    (r'chin\w*|hong\s*kong', 'China', ['BYD', 'XIAOMI', 'BOC']), (r'brazil\w*', 'Brazil', ['USDBRL']),
    (r'(?:us|u\.s\.|american|wall\s+street)(?:\s+stocks?)?', 'US', ['US500', 'US100', 'NVDA', 'AAPL', 'TSLA', 'GOOGL', 'META']),
    (r'chips?|semi\w*|memory|chip\s*makers?', 'chip makers', ['NVDA', 'SAMSUNG', 'SKHYNIX', 'MU', 'INTC', 'MRVL', 'SNDK']),
    (r'(?:precious\s+)?metals?|gold\s+(?:and|&)\s+silver', 'metals', ['XAUT', 'XAG']),
    (r'oil|crude|energy', 'oil', ['WTI', 'BRENT']), (r'forex|fx|currenc\w*', 'currencies', ['USDKRW', 'USDBRL']),
    (r'(?:an?\s+)?ind(?:ex|exes|ices)', 'index', ['US500', 'US100', 'INDIA50', 'KR200', 'JP225']),
    (r'spot', 'spot', 'spot'),
    (r'commodit\w*', 'commodities', ['XAUT', 'XAG', 'WTI', 'BRENT']),
    (r'meme\s*(?:coins?)?|memecoins?', 'memecoins', ['PUMP', 'SPX']),
    (r'stocks?|equit\w*|shares', 'stocks', 'Stocks'), (r'crypto\w*|coins?|tokens?', 'crypto', 'Crypto'),
]
MARKET_NOT = re.compile(r'\b(?:wallet|lend\w*|chat|position|positions|orders?|history|account|settings?|swap|send|deposit|withdraw|long|short|earn|rewards?|activity|balance)\b', re.I)
MARKET_SWITCH_RE = re.compile(r"^\s*(?:kivy[,\s]+)?(?:(?:can|could)\s+you\s+)?(?:please\s+|pls\s+)?(?:i\s+(?:want|wanna|would\s+like)\s+(?:to\s+)?)?(?:let'?s\s+)?"
                              r"(?:open|show(?:\s+me)?|go\s+to|switch(?:\s+(?:the\s+)?market)?\s+to|change(?:\s+(?:the\s+)?market)?\s+to|change\s+to|move\s+to|take\s+me\s+to|jump\s+to|load|"
                              r"pull\s+up|bring\s+up|view|see|trade|look\s+at)\s+(.+?)\s*[?.!]*\s*$", re.I)
MARKET_FILLER = re.compile(r"\b(?:the|a|an|market|markets|chart|charts|perps?|perpetuals?|futures|page|terminal|please|pls|trading|price|index|stock|shares|share|now|instead|one)\b"
                           r"|[-/](?:usdc|usdt|usd|perp)\b|\b(?:usdc|usdt)\b", re.I)


def market_find(text, strict=False):
    """('one', SYMBOL) / ('group', label, [symbols]) / None, from what the user called the market."""
    raw = str(text or '').strip()
    t = re.sub(r'\s+', ' ', MARKET_FILLER.sub(' ', raw.lower().replace('-perp', ' ').replace('/', ' '))).strip()
    t = re.sub(r'\b(?:spot)\b', '', t).strip()
    if len(raw) > 60:
        return None
    g = re.sub(r'\s+', ' ', re.sub(r'\b(?:the|please|pls|perps?|perpetuals?|trading|page|show|me|all|some)\b|[?.!]', ' ', raw.lower())).strip()
    g = re.sub(r'\s+(?:market|markets|ones?)$', '', g)
    parts = [x for x in re.split(r'\s+or\s+|\s*,\s*', g) if x]
    if len(parts) > 1:                               # "apple or tesla": both, as choices
        found = [market_find(x) for x in parts]
        if all(x and x[0] == 'one' for x in found):
            return ('group', 'two', [x[1] for x in found])
    for words, label, syms in MARKET_GROUPS:
        if re.fullmatch(r'(?:%s)(?:\s+(?:market|markets|stocks?|ones?))?' % words, g, re.I) or re.fullmatch(r'(?:%s)' % words, t, re.I):
            if syms == 'spot':
                return ('spot', [a for a in (K().get('assets') or []) if a.upper() != 'USDC'])
            if isinstance(syms, str):
                rows = sorted([r for r in perp_market_list() if r.get('category') == syms], key=lambda r: -float(r.get('volume') or 0))
                lead = ['BTC', 'ETH', 'SOL', 'SUI'] if syms == 'Crypto' else []
                syms = (lead + [r['symbol'] for r in rows if r['symbol'] not in lead])[:8]
            return ('group', label, syms) if len(syms) > 1 else ('one', syms[0])
    key = re.sub(r'[^a-z0-9& ]', '', t)
    if not key:
        return None
    if key in MARKET_NAMES and not (strict and key in _COINWORDS):
        return ('one', MARKET_NAMES[key])
    if strict:
        return None                                  # a message with no verb: only full names ("kospi") and groups, never tickers or typos
    sym = chart_symbol(key.replace(' ', ''))
    if sym:
        return ('one', sym)
    if key.upper() in [a.upper() for a in (K().get('assets') or []) if a.upper() != 'USDC']:
        return ('one', key.upper())                  # a spot-only coin (DEEP, WAL)
    import difflib                                   # 'samsnug', 'etherium', 'nvdia'
    names = list(MARKET_NAMES) + [m.lower() for m in af_markets()]
    hit = difflib.get_close_matches(key, names, n=1, cutoff=0.8)
    if hit:
        return ('one', MARKET_NAMES.get(hit[0]) or hit[0].upper())
    return None


def market_choices(label, syms, address=None):
    rows = {r['symbol']: r for r in perp_market_list()}
    opts = []
    for sm in syms:
        r = rows.get(sm)
        if not r:
            continue
        ch = r.get('change')
        opts.append({'label': '%s — %s' % (sm, MARKET_DESC.get(sm, sm)), 'ask': 'open %s' % sm,
                     'sub': ('$%s' % ('{:,.6g}'.format(float(r['price']))) if r.get('price') else '') +
                            (' · %+.2f%% 24h' % float(ch) if ch is not None else '') + (' · up to %sx' % r['max_lev'] if r.get('max_lev') else '')})
    return {'text': 'Kivy has these %s markets. Which one should I open?' % label, 'widget': {'type': 'choose', 'options': opts}}


def market_switch(msg, address, history=None):
    """"open korean market" / "switch to nvidia" / "change market to gold" / "BTC-PERP": open it, or offer the choices."""
    sp = re.match(r'^\s*(?:buy|sell)\s+([A-Za-z]{2,10})\s+(?:page|market|screen)\s*[?.!]*\s*$', msg, re.I)
    if sp:
        msg = 'open %s spot' % sp.group(1)
    m = MARKET_SWITCH_RE.match(msg)
    target = m.group(1) if m else None
    if not target:
        bare = re.sub(r'[?.!]+$', '', msg.strip())
        if len(bare.split()) > 4 or not re.search(r'[A-Za-z]', bare) or QUESTION_RE.search(msg) or '?' in msg:
            return None
        f = market_find(bare, strict=not re.search(r'\bperps?\b|-perp\b|^[A-Za-z0-9]{2,12}/usdc?$', bare, re.I))   # "btc perp", "BTC-PERP", "BTC/USDC"                        # bare: only names and groups ("kospi", "korean market"), never a bare ticker (other cards ask about those)
        if not f or (f[0] == 'one' and bare.lower() == f[1].lower()):
            return None
        target = bare
    if MARKET_NOT.search(target):
        return None                                  # "open my positions", "open a long": not a market
    f = market_find(target)
    if not f and re.search(r'\d', target):
        return None                                  # "open 10 sui": an amount, not a market name
    if not f:
        if not m or not re.search(r'\b(?:market|stock|coin|token|index|perps?|chart)\b|^[A-Za-z&]{2,12}$', target, re.I):
            return None
        return {'text': '%s isn\'t one of Kivy\'s markets. Kivy trades %d perps markets on Aftermath: crypto, US and Asian stocks, the S&P 500, '
                        'Nasdaq, KOSPI, Nikkei and Nifty indexes, gold, silver, oil, and USD/KRW and USD/BRL. Pick a group:' % (target.strip().title(), len(perp_market_list())),
                'widget': {'type': 'choose', 'options': [{'label': l, 'ask': 'open %s markets' % w} for l, w in
                                                         (('Crypto', 'crypto'), ('Stocks', 'stocks'), ('Indexes', 'index'), ('Gold, silver & oil', 'commodities'), ('Currencies', 'forex'))]}}
    if f[0] == 'group':
        return market_choices(f[1], f[2], address) if f[1] != 'two' else dict(market_choices('', f[2], address), text='Which one should I open?')
    if f[0] == 'spot':
        return {'text': 'Spot markets swap real coins in your wallet. Which one should I open?',
                'widget': {'type': 'choose', 'options': [{'label': '%s — spot' % disp(c), 'sub': 'buy or sell against USDC', 'ask': 'open %s spot' % c} for c in f[1]]}}
    mv = market_overview(f[1], address)
    if not mv:
        return None
    if m and re.match(r'(?i)\s*(?:\S+\s+){0,6}?trade\b', msg) and mv['perp'] and mv['spot'] and not re.search(r'\b(?:spot|perps?|perpetuals?)\b', msg, re.I):
        return None                                  # "trade btc": perps or spot? the trade card asks
    mv['venue'] = 'spot' if (re.search(r'\bspot\b|\bbuy\b.*\bpage\b', msg, re.I) and mv['spot']) or not mv['perp'] else 'perp'
    return {'text': 'Opening the %s trading page.' % ((mv['spot_name'] + ' spot') if mv['venue'] == 'spot' else (mv['perp'] + ' perps')), 'widget': mv}


# ── Sending, read without the AI (2026-10-21: "Send all fund to this wallet : 0x…" was read as the coin FUD) ──
SEND_VERB_RE = re.compile(r'^\s*(?:pls\s+|please\s+|can\s+you\s+|i\s+(?:want|wanna|need)\s+(?:to\s+)?)?(?:send|sned|snd|transfer|trasnfer|tranfer|trnasfer|pay|give|move)\b', re.I)
SEND_SWEEP_RE = re.compile(r'\b(?:all|every|whole|entire)\s+(?:of\s+)?(?:my\s+|the\s+)?(?:funds?|money|assets?|coins?|tokens?|balances?|wallet|crypto|holdings|stuff)\b|\beverything\b|\bmy\s+whole\s+wallet\b', re.I)
SEND_SCHED_RE = re.compile(r'\b(?:every|daily|weekly|monthly|hourly|recurring|repeat\w*|schedul\w*|automatic\w*|each\s+(?:day|week|month)|tomorrow|tonight|later|at\s+\d{1,2}(?::\d\d)?\s*(?:am|pm)?)\b', re.I)
SEND_BRIDGE_RE = re.compile(r'\b(?:bridge|ethereum\s+(?:network|chain|mainnet|wallet)|erc-?20|arbitrum|optimism|polygon|bsc|bnb\s+chain|base\s+chain|solana\s+(?:network|wallet|chain)|evm|tron|trc-?20|btc\s+network|bitcoin\s+(?:network|wallet|address)|another\s+chain|other\s+chain)\b', re.I)
SEND_CHAIN_WORDS = re.compile(r'\b(?:navi|suilend|ember|matrixdock|aftermath|perps?|margin|lend\w*|lent|deposit\w*|withdraw\w*|swap\w*|convert)\b', re.I)


def send_followup(msg, address, history):
    """The answer to a send question Kivy just asked ("Who should receive 1.15 USDC?" -> "0x…"): finish that send."""
    last = next((str(h.get('text') or '') for h in reversed(history or []) if isinstance(h, dict) and h.get('role') == 'model'), '')
    t = msg.strip().strip('.')
    m = re.match(r'^Who should receive ([\d.,]+) ([A-Za-z][\w.]*)\?', last)
    if m and re.fullmatch(r'(?:(?:to|send\s+(?:it\s+)?to|it\'?s|here|this|address)\s*:?\s*)?(0x[0-9a-fA-F]+|@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui|@[a-z0-9-]+)', t, re.I):
        to = re.search(r'0x[0-9a-fA-F]+|@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui|@[a-z0-9-]+', t, re.I).group(0)
        return send_reply('send %s %s to %s' % (m.group(1).replace(',', ''), m.group(2), to), address, history)
    m = re.match(r'^How much ([A-Za-z][\w.]*) should I send(?: to (\S+?))?\?', last)
    if m and re.fullmatch(r'(?:\$\s*)?\d+(?:\.\d+)?\s*%?|all|max|half|everything', t, re.I):
        return send_reply('send %s %s%s' % (t, m.group(1), (' to ' + m.group(2)) if m.group(2) else ''), address, history)
    m = re.match(r'^Which coin should I send (\S+) of\?', last)
    if m and ckey(t):
        return send_reply('send %s %s' % (m.group(1), ckey(t)), address, history)
    return None


def send_reply(msg, address, history=None):
    """One send card (plan) when the request is clear; a question with choices when a piece is missing; a plain no when it can't be done."""
    if history and not SEND_VERB_RE.match(msg):
        f = send_followup(msg, address, history)
        if f:
            return f
    if not SEND_VERB_RE.match(msg) or QUESTION_RE.search(msg):
        return None
    if re.search(r'[,;]\s*(?:and\s+|then\s+)?(?:lend|deposit|swap|keep|put|park|supply)\b', msg, re.I):
        return None                                      # "send 25% to bob, lend the rest": a plan of steps, not a plain send (2026-10-23)
    if re.match(r'^\s*(?:send|transfer)(?:\s+(?:money|funds?|coins?|tokens?|assets?|crypto))?\s*[.!?]*\s*$', msg, re.I):
        # bare "send": the Send card — pick what (wallet or lent), how much, to whom; several at once (2026-10-23)
        return {'text': 'Pick what to send and who gets it.' if address else K()['no_wallet'], 'widget': {'type': 'sendcard'} if address else None}
    low = msg.lower()
    if re.match(r'^\s*move\b', low) and not re.search(r'0x[0-9a-f]{6,}|\b[a-z0-9-]+\.sui\b|@[a-z0-9-]+', low):
        return None                                  # "move my usdc to navi": a lending move, not a send
    if SEND_CHAIN_WORDS.search(msg) and len(re.findall(r'\b(?:then|and)\b', low)):
        return None                                  # several steps: the chain reader
    if SEND_SCHED_RE.search(msg):
        return {'text': 'Kivy sends once, right when you approve it in your wallet — it can\'t schedule or repeat transfers. Say the send you want now, e.g. "send 5 USDC to alice.sui".'}
    if SEND_BRIDGE_RE.search(msg):
        return {'text': 'Kivy only sends on the Sui network — it can\'t bridge to another chain. Send to a Sui address (0x followed by 64 characters) or a name.sui.'}
    fr = re.search(r'\bfrom\s+(0x[0-9a-fA-F]+|[a-z0-9-]+\.sui|@[a-z0-9-]+)', msg, re.I)
    if fr and (not address or fr.group(1).lower() != address.lower()):
        return {'text': 'Kivy can only send from the wallet you connected, and your wallet asks you to approve it. It can\'t move funds out of someone else\'s wallet.'}
    if not address:
        return {'text': K()['no_wallet']}
    # who to
    hexes = re.findall(r'0x[0-9a-fA-F]+', msg)
    name = re.search(r'(?<![\w.])@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b|(?<![\w@])@[a-z0-9-]{2,}\b', msg, re.I)
    btc = re.search(r'\b(?:bc1[a-z0-9]{20,}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b', msg)
    to_raw, to = None, None
    names_all = re.findall(r'(?<![\w.])@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b|(?<![\w@])@[a-z0-9-]{2,}\b', msg, re.I)
    rcpts = list(dict.fromkeys([h.lower() for h in hexes] + [n.lower() for n in names_all]))
    if len(rcpts) > 1 and not re.search(r'\bor\b', msg, re.I):   # "1 usdc to A and 2 usdc to B" / "1 usdc each to A and B": ONE transaction (2026-10-25)
        multi = send_multi_parse(msg, rcpts, address)
        if multi:
            return multi
    if len(rcpts) > 1:                               # "to bob.sui or @bob": which one?
        base = msg
        for x in rcpts:
            base = re.sub(re.escape(x), ' ', base, flags=re.I)
        base = re.sub(r'\s+(?:or|and|,)\s*$|\s+to\s*(?:(?:or|and|,)\s*)*$', '', re.sub(r'\s+', ' ', base).strip())
        return {'text': 'You named %d recipients — which one should get it?' % len(rcpts),
                'widget': {'type': 'choose', 'options': [{'label': x if not x.startswith('0x') else x[:10] + '…' + x[-4:], 'ask': '%s to %s' % (base, x)} for x in rcpts]}}
    if hexes:
        to_raw = max(hexes, key=len)
        n = len(to_raw) - 2
        if n == 40:
            return {'text': '%s… is an Ethereum-style address (40 characters). Kivy only sends on Sui: a Sui address is 0x followed by 64 characters.' % to_raw[:10]}
        if n != 64:
            return {'text': 'That address isn\'t complete: a Sui address is 0x followed by 64 characters, and this one has %d. Copy it again from the wallet that will receive it.' % n}
        to = to_raw.lower()
    elif name:
        to_raw = name.group(0)
        to = resolve_recipient(to_raw)
        if not to:
            return {'text': 'I couldn\'t find %s on Sui. Check the spelling, or paste the full address (0x followed by 64 characters).' % to_raw}
    elif btc:
        return {'text': 'That looks like a Bitcoin address. Kivy only sends on the Sui network, to a Sui address (0x followed by 64 characters) or a name.sui.'}
    else:                                            # "to my other wallet", "to my slush wallet": one of the user's own accounts (2026-10-21)
        accts = xw_accounts(address)
        mine = [r for r in xw_refs(msg, accts, address) if (r[2] == 'other' or (isinstance(r[2], int) and accts[r[2]]['a'].lower() != address.lower()))]
        if mine or XW_OTHER_RE.search(msg) or re.search(r'\b(?:my\s+)?(?:other|second|2nd|another)\s+(?:connected\s+)?(?:wallet|account)\b', msg, re.I):
            others = [x for x in accts if x['a'].lower() != address.lower()]
            pick = [accts[r[2]] for r in mine if isinstance(r[2], int)]
            if not pick and len(others) == 1:
                pick = others
            if not pick and not others:
                return {'text': 'Kivy can only see the account your wallet is set to (%s…%s). To send to your other wallet, paste its full address '
                                '(0x followed by 64 characters).' % (address[:6], address[-4:])}
            if not pick:
                base = re.sub(r'(?i)\b(?:my\s+)?(?:other|second|2nd|another)\s+(?:connected\s+)?(?:wallet|account)(?:\s+that\s+(?:is\s+)?connected)?\b', '%s', msg)
                return {'text': 'Which of your accounts should get it?', 'widget': {'type': 'choose', 'options': [
                    {'label': '%s…%s' % (x['a'][:6], x['a'][-4:]) + ((' · ' + x['w']) if x.get('w') else ''), 'ask': (base % x['a']) if '%s' in base else msg + ' to ' + x['a']} for x in others]}}
            to_raw = to = pick[0]['a'].lower()
    if not to_raw and re.match(r'^\s*(?:\w+\s+){0,3}?(?:pay|give|move)\b', low) and not re.search(r'\d', msg):
        return None                                  # "give me the sui chart": not a send without someone or an amount
    if to and to == address.lower():
        return {'text': 'That\'s your own wallet — sending there would only cost gas. Paste the address you want to send to.'}
    rest = msg.replace(to_raw, ' ') if to_raw else msg
    # which coin(s), how much
    hold = all_balances(address) or {}
    words = [w for w in re.findall(r'[A-Za-z][A-Za-z0-9]*', rest) if w.lower() not in INTENT_STOP | {'send', 'sned', 'snd', 'transfer', 'pay', 'give', 'move',
             'wallet', 'this', 'that', 'address', 'friend', 'fund', 'funds', 'money', 'everything', 'max', 'half', 'worth', 'of', 'dollars', 'usd', 'please', 'pls'}]
    coins = []
    for w in words:
        c = ckey(w)
        if c and c not in coins:
            coins.append(c)
    am = re.search(r'\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:usd|dollars?|bucks)\b(?!\s*c)', rest, re.I)
    usd = float(am.group(1) or am.group(2)) if am else None
    num = None if usd is not None else re.search(r'(?<![\w.])(\d+(?:\.\d+)?)(?:\s*(%|percent))?', rest)
    pct = float(num.group(1)) / 100 if num and num.group(2) else None
    qty = float(num.group(1)) if num and not num.group(2) else None
    word = re.search(r'\b(all|max|everything|entire|half)\b', rest, re.I)
    held = [c for c in sorted(hold, key=lambda c: -(float(hold[c]) * (coin_px(c) or 0))) if float(hold.get(c) or 0) * (coin_px(c) or 0) >= 0.01]
    tail = (' to %s' % to_raw) if to_raw else ''
    disp_amt = lambda c: dec(word_amount('all', c, hold) or 0)
    if not coins and (SEND_SWEEP_RE.search(rest) or (word and qty is None and usd is None and pct is None)):
        if not held:
            return {'text': 'Your wallet has nothing to send yet.'}
        share = 'half' if word and word.group(1).lower() == 'half' else 'all'
        if to and len(held) > 1:                    # every coin, one transaction, one approval (2026-10-22)
            many = plan_send_many([(c, word_amount(share if c != 'SUI' or share == 'half' else 'max', c, hold) or 0) for c in held], to_raw, hold, address)
            if many.get('plan'):
                return many
            return {'text': many.get('text', '') + ' Pick a coin to send on its own:',
                    'widget': {'type': 'choose', 'options': [{'label': 'Send %s %s' % (share, disp(c)), 'sub': '%s %s' % (dec(word_amount(share, c, hold) or 0), disp(c)),
                                                              'ask': 'send %s %s%s' % ('max' if c == 'SUI' and share == 'all' else share, c, tail)} for c in held]}}
        return {'text': ('I\'ll send them one coin at a time — each is its own approval in your wallet%s. Which first?' % (' (a little SUI stays for gas)' if 'SUI' in held else ''))
                        + ('' if to else ' Then tell me who to send to.'),
                'widget': {'type': 'choose', 'options': [{'label': 'Send all %s' % disp(c), 'sub': '%s %s' % (disp_amt(c), disp(c)),
                                                          'ask': 'send %s %s%s' % ('max' if c == 'SUI' else 'all', c, tail)} for c in held]}}
    if not coins:
        if qty is None and usd is None and pct is None:
            if to and held:                          # "send to @bob": which coin?
                return {'text': 'What should I send to %s?' % to_raw,
                        'widget': {'type': 'choose', 'options': [{'label': 'Send %s' % disp(c), 'sub': 'you have %s' % dec(float(hold[c])),
                                                                  'ask': 'send %s%s' % (c, tail)} for c in held]}}
            return None                              # not a send we can read ("send it", "pay attention")
        amt_txt = ('$%g' % usd) if usd is not None else ('%g%%' % (pct * 100)) if pct is not None else '%g' % qty
        return {'text': 'Which coin should I send %s of?' % amt_txt,
                'widget': {'type': 'choose', 'options': [{'label': '%s %s' % (amt_txt, disp(c)), 'sub': 'you have %s' % dec(float(hold[c])),
                                                          'ask': 'send %s %s%s' % (amt_txt, c, tail)} for c in held
                                                         if qty is None or float(hold.get(c) or 0) >= qty] or
                           [{'label': 'Send USDC', 'ask': 'send %s USDC%s' % (amt_txt, tail)}]}}
    if len(coins) > 1:                               # "send 5 usdc and 2 sui": each with its own amount, one approval each
        pairs = {}
        for a_, w_ in re.findall(r'(\d+(?:\.\d+)?|all|half|max)\s+(?:of\s+)?(?:my\s+|the\s+)?([A-Za-z][A-Za-z0-9]*)', rest, re.I):
            if ckey(w_) and ckey(w_) not in pairs:
                pairs[ckey(w_)] = a_
        if to and all(c in pairs for c in coins):   # "send 1 sui and 5 usdc to X": both coins, ONE transaction, one approval (2026-10-25)
            items = [(c, (word_amount('max' if c == 'SUI' and pairs[c].lower() == 'all' else pairs[c], c, hold) or 0) if pairs[c].lower() in WORD_AMOUNTS else float(pairs[c]))
                     for c in coins]
            many = plan_send_many(items, to_raw, hold, address)
            if many.get('plan'):
                return many
            if many.get('text'):
                return many                          # e.g. not enough of one coin: say so, never split it silently
        return {'text': 'One coin per send — each is its own approval. Which first?',
                'widget': {'type': 'choose', 'options': [{'label': 'Send %s %s' % (pairs.get(c, ''), disp(c)), 'ask': 'send %s %s%s' % (pairs.get(c) or (word.group(1) if word else ''), c, tail)} for c in coins]}}
    c = coins[0]
    have = float(hold.get(c) or 0)
    if have <= 0:
        return {'text': 'You don\'t have any %s in this wallet to send.' % disp(c)}
    if qty is None and usd is None and pct is None and not word:
        opts = [('A quarter', 0.25), ('Half', 0.5), ('All' if c != 'SUI' else 'Max (keeps gas)', 1.0)]
        return {'text': 'How much %s should I send%s? You have %s.' % (disp(c), (' to %s' % to_raw) if to_raw else '', dec(have)),
                'widget': {'type': 'choose', 'options': [{'label': l, 'sub': '%s %s' % (dec((word_amount('all', c, hold) or 0) * f), disp(c)),
                                                          'ask': 'send %s %s%s' % (dec((word_amount('all', c, hold) or 0) * f) if f < 1 else ('max' if c == 'SUI' else 'all'), c, tail)}
                                                         for l, f in opts]}}
    if word:
        amt = word_amount(word.group(1), c, hold) or 0
    elif pct is not None:
        amt = (word_amount('all', c, hold) or 0) * pct
    elif usd is not None:
        px = coin_px(c) or (1.0 if c in ('USDC', 'USDsui') else None)
        if not px:
            return {'text': 'I can\'t price %s right now — say the amount in %s instead.' % (disp(c), disp(c))}
        amt = usd / px
    else:
        amt = qty
    if not amt or amt <= 0:
        return {'text': 'There isn\'t enough %s to send after keeping a little for gas.' % disp(c) if c == 'SUI' else 'Nothing to send.'}
    if have < amt <= have * 1.001 + 1e-9:            # a shown, rounded balance ("1.15874" for 1.158737): it means all of it
        amt = word_amount('all', c, hold) or have
    if amt > have + 1e-9:
        # not a dead end: if another coin covers it, offer to swap first (2026-10-23)
        short = amt - have
        alt = sorted(((k, spendable(k, hold) * float(coin_px(k) or 0)) for k in hold if k != c and float(hold.get(k) or 0) > 0), key=lambda x: -x[1])
        pc = float(coin_px(c) or 0)
        if alt and pc and alt[0][1] >= short * pc * 1.02:
            k = alt[0][0]
            need = short * pc * 1.02 / float(coin_px(k) or 1)
            return {'text': 'You have %s %s — %s short of %s. You could swap a little %s to cover it first.' % (dec(have), disp(c), dec(short), dec(amt), disp(k)),
                    'widget': {'type': 'choose', 'options': [{'label': 'Swap %s %s to %s' % (dec(need), disp(k), disp(c)), 'sub': 'then ask to send again',
                                                              'ask': 'swap %s %s to %s' % (plain_num(round(need, 6)), k, c)},
                                                             {'label': 'Send %s %s instead' % (dec(have), disp(c)), 'sub': 'what you have', 'ask': re.sub(r'\b\d+(?:\.\d+)?\b', plain_num(have), msg, count=1)}]}} \
                if have > 0 else {'text': "You don't have any %s yet. You could swap some %s to %s first." % (disp(c), disp(k), disp(c)),
                                  'widget': {'type': 'choose', 'options': [{'label': 'Swap %s %s to %s' % (dec(need), disp(k), disp(c)), 'sub': 'then ask to send again',
                                                                            'ask': 'swap %s %s to %s' % (plain_num(round(need, 6)), k, c)}]}}
        other = sum(v for _, v in alt)
        return {'text': 'You have %s %s — less than %s.%s' % (dec(have), disp(c), dec(amt),
                ((' Everything else in your wallet is worth about $%.2f, so that would not cover it either.' % other) if other >= 0.01 else ''))}
    if not to:
        return {'text': 'Who should receive %s %s? Paste their Sui address (0x followed by 64 characters) or a name.sui.' % (dec(amt), disp(c))}
    return plan('send', amt, c, recipient=to_raw, holdings=hold, sender=address)


# ── One sentence, several of the user's own accounts (2026-10-21) ──
# A Sui transaction has ONE sender, so the sentence is cut into legs: one leg per account, each its own approval, run in
# order (a leg starts after the one before it landed). Each leg is planned by chain_parse for that account; money a leg
# sends to the next account is counted as that account's "incoming" while planning.
XW = threading.local()           # per request: XW.accounts = [{'a': address, 'w': wallet name, 'n': [nicknames]}], XW.extra
XW_ATOMIC_RE = re.compile(r'\b(?:in\s+(?:one|a\s+single|the\s+same|1)\s+(?:transaction|txn?|tx|approval|signature|go)|atomic\w*|all\s+at\s+once|same\s+transaction|one\s+signature)\b', re.I)
XW_OTHER_RE = re.compile(r'\bmy\s+other\s+(?:wallet|account)\b', re.I)


def xw_accounts(address):
    acc = [x for x in (getattr(XW, 'accounts', None) or []) if isinstance(x, dict) and ADDR_RE.match(str(x.get('a') or ''))][:10]
    if address and not any(x['a'].lower() == address.lower() for x in acc):
        acc = [{'a': address, 'w': ''}] + acc
    return acc


def xw_extra(address):
    return (getattr(XW, 'extra', None) or {}).get((address or '').lower()) or {}


def xw_refs(text, accts, active):
    """[(start, end, account index or 'other' or 'outsider:<x>', preposition)] — where the sentence names an account."""
    out = []
    low = text.lower()
    pre = lambda i: (re.search(r'\b(from|to|into|in|on|using|with|via|over\s+to)\s+(?:(?:my|the|our|this|that)\s+)?(?:(?:other\s+)?(?:address|wallet|account|acct)\s*:?\s*)?$', low[max(0, i - 40):i])
                     or [None, ''])[1]           # "to this address: 0x0262…" counts as "to" (2026-10-21)
    for h in re.finditer(r'0x[0-9a-f]{4,64}\b', low):
        hits = [i for i, x in enumerate(accts) if x['a'].lower().startswith(h.group(0))]
        if len(hits) == 1:
            out.append((h.start(), h.end(), hits[0], pre(h.start())))
        elif not hits and len(h.group(0)) == 66:
            out.append((h.start(), h.end(), 'outsider:' + h.group(0), pre(h.start())))
    for i, x in enumerate(accts):
        names = [str(n).lower() for n in (x.get('n') or []) if n] + ([x['w'].lower().split()[0]] if x.get('w') else [])
        for n in names:
            for m in re.finditer(r'\b(?:my\s+)?%s(?:\'s)?(?:\s+(?:wallet|account|acct))?\b' % re.escape(n), low):
                if not any(a <= m.start() < b for a, b, _, _ in out):
                    out.append((m.start(), m.end(), i, pre(m.start())))
    for m in XW_OTHER_RE.finditer(low):
        out.append((m.start(), m.end(), 'other', pre(m.start())))
    me = next((i for i, x in enumerate(accts) if x['a'].lower() == (active or '').lower()), None)
    for m in re.finditer(r'\b(?:my|this|the\s+current)\s+(?:wallet|account)\b(?!\s+(?:a|b|c)\b)', low):
        if me is not None and not any(a <= m.start() < b for a, b, _, _ in out) and pre(m.start()):
            out.append((m.start(), m.end(), me, pre(m.start())))
    import difflib                                   # "slsuh", "nigthly": a wallet name with a typo
    wn = {x['w'].lower().split()[0]: i for i, x in enumerate(accts) if x.get('w')}
    for m in re.finditer(r'\b[a-z]{4,}\b', low):
        if any(a <= m.start() < b for a, b, _, _ in out) or m.group(0) in wn:
            continue
        hit = difflib.get_close_matches(m.group(0), list(wn), n=1, cutoff=0.75)
        if hit and m.group(0) not in INTENT_STOP and not ckey(m.group(0)):
            out.append((m.start(), m.end(), wn[hit[0]], pre(m.start())))
    for m in re.finditer(r'\bfrom\s+(@[a-z0-9-]+|[a-z0-9-]+\.sui)\b', low):
        out.append((m.start(1), m.end(1), 'outsider:' + m.group(1), 'from'))
    return sorted(out)


def xw_handoff(widget):
    """What a leg ends up sending on: (coin, estimated amount) from its last step."""
    for st in reversed((widget or {}).get('steps') or []):
        m = re.search(r'≈\s*([\d,]+(?:\.\d+)?)\s+([A-Za-z]+)', (st.get('title') or '') + ' ' + (st.get('sub') or ''))
        if m:
            return ckey(m.group(2)) or m.group(2), float(m.group(1).replace(',', ''))
    return None, None


def xw_parse(msg, address, history=None):
    msg = re.sub(r'\s*:\s*(?=0x[0-9a-fA-F]{4,})', ' ', msg)   # "to this address: 0x…" — the colon is not a step break
    accts = xw_accounts(address)
    if len(accts) < 2 or not address:
        return None
    refs = xw_refs(msg, accts, address)
    me = next(i for i, x in enumerate(accts) if x['a'].lower() == address.lower())
    out_from = [r for r in refs if isinstance(r[2], str) and r[2].startswith('outsider:') and r[3] in ('from', 'using', 'with', 'in', 'on')]
    if out_from and any(isinstance(r[2], int) or r[2] == 'other' for r in refs):
        return {'text': 'Kivy can only move money from accounts you connected and approve yourself. %s isn\'t one of them — connect it (wallet → + Account) and say it again.' % out_from[0][2][9:][:12]}
    if re.search(r'\b(?:both|each|all\s+(?:my|the))\s+(?:of\s+)?(?:my\s+)?(?:wallets|accounts)\b|\bbetween\s+(?:both|my|the)\s+(?:wallets|accounts)\b|\bthrough\s+each\b', msg, re.I) \
            and not re.search(r'\bfrom\b.+\bto\b', msg, re.I):
        others = [i for i in range(len(accts)) if i != me][:2]
        return {'text': 'Which account goes first — where does the money start, and where should it end up?',
                'widget': {'type': 'choose', 'options': [{'label': 'Start in %s → %s' % (accts[x]['w'] or accts[x]['a'][:6], accts[y]['w'] or accts[y]['a'][:6]),
                                                          'ask': re.sub(r'(?i)\b(?:use\s+)?(?:both|each|all\s+(?:my|the))\s+(?:of\s+)?(?:my\s+)?(?:wallets|accounts)\b|\bbetween\s+(?:both|my|the)\s+(?:wallets|accounts)\b|\bthrough\s+each\b', '', msg).strip(' ,')
                                                                 + ' (from %s to %s)' % (accts[x]['a'], accts[y]['a'])}
                                                         for x, y in ((me, others[0]), (others[0], me))]}} if others else None
    own = {r[2] for r in refs if isinstance(r[2], int)}
    if not refs or (own <= {me} and not any(r[2] == 'other' for r in refs)):
        return None                                  # only this account: the normal readers
    if not CHAIN_SPLIT.search(msg) and not re.search(r'\b(?:send|move|transfer|forward|withdraw|swap|lend|deposit|use|take|pull|put|park)\b', msg, re.I):
        return None
    if XW_ATOMIC_RE.search(msg):
        return {'text': 'That can\'t be one transaction: on Sui every transaction has a single sender, so each of your accounts signs its own part. '
                        'I can do it as steps, one approval per account, in order — say it again without "one transaction".'}
    if any(r[2] == 'other' for r in refs):
        others = [i for i in range(len(accts)) if i != me]
        if len(others) > 1:
            return {'text': 'Which of your other accounts?', 'widget': {'type': 'choose', 'options': [
                {'label': accts[i]['a'][:6] + '…' + accts[i]['a'][-4:] + ((' · ' + accts[i]['w']) if accts[i].get('w') else ''),
                 'ask': XW_OTHER_RE.sub('my %s wallet' % accts[i]['a'][:6], msg)} for i in others]}}
        refs = [(a, b, others[0] if r == 'other' else r, p) for a, b, r, p in refs]
    # cut into legs
    legs, cur, clauses = [], me, [c for c in CHAIN_SPLIT.split(msg) if c and c.strip()]
    leg = {'acct': cur, 'parts': [], 'incoming': False}
    for c in clauses:
        rs = [r for r in xw_refs(c, accts, address) if not (isinstance(r[2], str) and r[2].startswith('outsider'))]
        rs = [(a, b, (next(i for i in range(len(accts)) if i != me) if r == 'other' else r), p) for a, b, r, p in rs]
        frm = next((r for r in rs if r[3] in ('from', 'in', 'on', 'using', 'with', 'via') and isinstance(r[2], int)), None)
        to = next((r for r in rs if r[3] in ('to', 'into', 'over to') and isinstance(r[2], int)), None)
        if frm and frm[2] != leg['acct']:
            if leg['parts'] or leg['incoming']:
                legs.append(leg)
            leg = {'acct': frm[2], 'parts': [], 'incoming': False}
        text = c
        for a, b, r, p in sorted(rs, reverse=True):
            if to and (a, b) == (to[0], to[1]):
                text = text[:a] + accts[r]['a'] + text[b:]
            else:                                    # "from my vera wallet" / "in my slush": who signs, not part of the step
                text = re.sub(r'\b(?:from|in|on|using|with|via)\s+(?:my\s+|the\s+)?$', '', text[:a]) + text[b:]
        text = re.sub(r'\s+', ' ', text).strip(' ,')
        if text:
            leg['parts'].append(text)
        if to and to[2] != leg['acct'] and re.search(r'\b(?:send|sned|move|transfer|forward|give|pay|put|bring)\b', c, re.I):
            legs.append(leg)
            leg = {'acct': to[2], 'parts': [], 'incoming': True}
    if leg['parts'] or not legs:
        legs.append(leg)
    if len(legs) < 2 and legs[0]['acct'] == me:
        return None
    # a few wordings the single-account reader doesn't take, rewritten per leg
    for lg in legs:
        a = accts[lg['acct']]['a']
        for k, t in enumerate(lg['parts']):
            m = re.match(r'(?i)^\s*(?:move|take|pull|bring|send)\s+(?:all|everything)\s+(?:(?:my|the)\s+)?(?:(\w+)\s+)?lent\s+(?:on|in|at)\s+(.+?)\s+(?:over\s+)?to\s+(0x[0-9a-f]{64})\b(.*)$', t)
            if m:
                rows = [r for r in lend_positions_rows(a) if chain_market(m.group(2))[0] in (r['id'], 'fam:' + r['id'].split('-')[0])
                        and (not m.group(1) or r['coin'].lower() == m.group(1).lower())]
                coin = rows[0]['coin'] if len(rows) == 1 else (m.group(1) or 'USDC')
                lg['parts'][k:k + 1] = ['withdraw all %s from %s' % (coin, m.group(2)), 'send it to %s%s' % (m.group(3), m.group(4))]
            elif re.match(r'(?i)^\s*(?:take|use|grab)\s+(?:all|everything|\d+(?:\.\d+)?|half)?\b.*\b(?:margin|perps?|aftermath)\b', t):
                lg['parts'][k] = re.sub(r'(?i)^\s*(?:take|use|grab)\b', 'withdraw', t)
    # a leg that starts with "it/that" but isn't where the money went: ask
    for n, lg in enumerate(legs):
        if n and not lg['incoming'] and lg['parts'] and re.match(r'(?i)^\s*\w+\s+(?:it|that|them|those)\b', lg['parts'][0]):
            went = next((accts[l2['acct']] for l2 in reversed(legs[:n]) if l2['incoming']), None)
            here = accts[lg['acct']]
            if went and went['a'] != here['a']:
                return {'text': 'The money goes to %s, but that step is from %s — which do you mean?' % (went.get('w') or went['a'][:6], here.get('w') or here['a'][:6]),
                        'widget': {'type': 'choose', 'options': [
                            {'label': 'Do it from %s (where the money went)' % (went.get('w') or went['a'][:6]), 'ask': msg.replace(here.get('w') or here['a'], went.get('w') or went['a'], 1)},
                            {'label': 'Use %s\'s own funds' % (here.get('w') or here['a'][:6]), 'ask': re.sub(r'(?i)\b(it|that|them|those)\b', 'my USDC', msg, count=1)}]}}
    # plan each leg for its own account
    plans, prev, XW.extra = [], None, {}
    try:
        for n, lg in enumerate(legs, 1):
            a = accts[lg['acct']]['a']
            parts = list(lg['parts'])
            if lg['incoming'] and prev:
                XW.extra = {a.lower(): {prev[0]: prev[1]}}
                if not parts:
                    break                            # the money just arrives there
                more = re.search(r'(\d+(?:\.\d+)?)\s*%s\b' % re.escape(prev[0]), ' '.join(parts), re.I)
                if more and float(more.group(1)) > prev[1] * 1.001:
                    return {'text': 'Step %d uses %s %s, but only about %s %s arrives from the step before. Which do you mean?' % (n, more.group(1), prev[0], dec(prev[1]), prev[0]),
                            'widget': {'type': 'choose', 'options': [
                                {'label': 'Just what arrives (≈ %s %s)' % (dec(prev[1]), prev[0]), 'ask': msg.replace(more.group(0), 'it', 1)},
                                {'label': '%s %s, adding %s\'s own' % (more.group(1), prev[0], accts[lg['acct']].get('w') or a[:6]), 'ask': msg + ' (top up from that account\'s own %s)' % prev[0]}]}}
                parts = ['use %s %s from my wallet' % (dec(prev[1] * 0.995), prev[0])] + parts
            text = ', then '.join(parts)
            r = chain_parse(text, a)
            if r is None:
                r = send_reply(re.sub(r'(?i)^\s*forward\b', 'send', text), a) if re.match(r'^\s*(?:send|transfer|move|give|pay|forward)\b', text, re.I) else None
                if r and (r.get('plan') or {}).get('action'):
                    act = r['plan']['action']
                    r = {'widget': {'type': 'chain', 'steps': [{'title': 'Send ≈ %s %s to %s' % (dec(act['amount']), act['coin'], act['to'][:6] + '…' + act['to'][-4:]), 'sub': 'Full address: ' + act['to'],
                                                                'coin': act['coin'], 'icon': coin_icon(act['coin']) if 'coin_icon' in globals() else None, 'full': True}],
                                    'spec': {'source': {'op': 'wallet', 'coin': act['coin'], 'amount': act['amount']}, 'swaps': [],
                                             'sinks': [{'op': 'send', 'amount': {'rest': True}, 'to': act['to']}]}}}
            if r is None:
                return {'text': 'Step %d (account %s): I couldn\'t read "%s". Say it as one action, e.g. "send 5 USDC to my slush wallet, then lend it on NAVI".' % (n, a[:6] + '…' + a[-4:], text[:80])}
            if (r.get('widget') or {}).get('type') != 'chain':
                r['text'] = 'Step %d (account %s): %s' % (n, a[:6] + '…' + a[-4:], r.get('text') or '')
                return r
            w = r['widget']
            if lg['incoming']:
                w['spec']['source']['incoming'] = True
                if w['steps'] and prev:                  # step 1 of this leg is the money arriving, not a wallet pick
                    w['steps'][0] = dict(w['steps'][0], title='Receive ≈ %s %s from step %d' % (dec(prev[1]), disp(prev[0]), n - 1), sub='then:')
            plans.append({'from': a, 'wallet': accts[lg['acct']].get('w') or '', 'incoming': lg['incoming'], 'steps': w['steps'], 'spec': w['spec']})
            prev = xw_handoff(w)
    finally:
        XW.extra = {}
    if not plans:
        return None
    return {'text': 'Here is the plan: %d step%s across %d of your accounts. Each step is one approval, signed by that account, in this order; '
                    'a step starts only after the one before it has landed. If you stop halfway, what already happened stays done.'
                    % (len(plans), 's' if len(plans) > 1 else '', len({p['from'] for p in plans})),
            'widget': {'type': 'xwallet', 'legs': plans}}


def market_overview(word, address=None):
    """Everything about one coin on both venues, for the market card: which sides exist, names, icon, what the user holds."""
    w = re.sub(r'[^A-Za-z0-9&]', '', str(word or '')).upper()
    psym = chart_symbol(w)
    spot = [a.upper() for a in (K().get('assets') or [])]
    ssym = CHART_ALIASES.get(w, w)
    ssym = SPOT_IN.get(ssym, ssym)
    ssym = ssym if ssym in spot and ssym != 'USDC' else None
    if not psym and not ssym:
        return None
    row = next((r for r in perp_market_list() if r['symbol'] == psym), {}) if psym else {}
    held, pos = None, None
    if address:
        try:
            held = float((balances(address) or {}).get(ssym) or 0) if ssym else None
        except Exception:
            pass
        try:
            pp = lend_cli('perp-positions', address, timeout=40)
            pos = next(({'side': x['side'], 'size': x['size'], 'pnl': x.get('pnl'), 'leverage': x.get('leverage')}
                        for x in pp.get('positions') or [] if x['symbol'] == psym), None) if psym else None
        except Exception:
            pass
    return {'type': 'mktview', 'perp': psym, 'spot': ssym, 'spot_name': disp(ssym) if ssym else None,
            'image': row.get('image') or None, 'max_lev': row.get('max_lev'), 'held': held, 'position': pos, 'connected': bool(address)}


_ACTIVE = {'t': time.time()}                         # last real visitor API request (the watchdog's page loads don't count)


def _warm_markets():
    import threading
    def loop():
        while True:
            if time.time() - _ACTIVE['t'] < 600:          # only while people use Kivy: idle, it cost ~11k Aftermath calls a day (W-02)
                try:
                    perp_market_list(fresh=True)
                except Exception:
                    pass
            time.sleep(15)
    threading.Thread(target=loop, daemon=True, name='kivy-markets').start()


_warm_markets()


LOGIN = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         '<meta name="robots" content="noindex,nofollow"><title>Kivy · sign in</title><style>'
         ':root{--bg:#09090b;--surface:#131316;--line:#26262b;--text:#e7e7ea;--text2:#9a9aa3;--accent:#8b7cf6}'
         '*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);'
         'color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}'
         '.card{width:min(340px,calc(100vw - 32px));background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:26px}'
         'h1{margin:0 0 4px;font:600 20px ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.02em}'
         'p{margin:0 0 18px;color:var(--text2);font-size:13px}input{width:100%;margin:0 0 10px;padding:11px 12px;border-radius:10px;'
         'border:1px solid var(--line);background:#0e0e11;color:var(--text);font:inherit}'
         'button{width:100%;padding:11px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:600 14px system-ui;cursor:pointer}'
         '.err{color:#f87171;font-size:13px;margin:0 0 10px}</style></head><body>'
         '<form class="card" method="post" action="/login"><h1>kivy</h1><p>Sign in to talk to Kivy.</p>{ERR}'
         '<input name="user" placeholder="Username" autocomplete="username">'
         '<input name="pass" type="password" placeholder="Password" autocomplete="current-password">'
         '<button type="submit">Sign in</button></form></body></html>')


# shared budget for expensive public work (audit V11): at most 24 at once, 6 per visitor; beyond that a quick 503 instead of a pile-up
HEAVY = TX_BUILD | {'/api/chart', '/api/spot/chart', '/api/market/view', '/api/market/book', '/api/perp/preview', '/api/swap/quote',
                    '/api/perp/positions', '/api/perp/history', '/api/lend/others', '/api/lend/rewards', '/api/chat',
                    '/api/perp/cancel-preview', '/api/perp/referral', '/api/perp/pro-orders'}   # + review F-02, 2026-10-25
_HEAVY_ALL = threading.BoundedSemaphore(24)
_BUILD_ALL = threading.BoundedSemaphore(16)           # transaction builds have their own slots: slow reads can't starve them (audit nv #13)
_CHAT_ALL = threading.BoundedSemaphore(16)            # chats have their own slots (2026-10-24)
LIGHT = {'/api/terms', '/api/auth/challenge', '/api/tx/detail', '/api/recipient', '/api/tx/labels', '/api/memory', '/api/pipeline',   # per-visitor limit, no slot
         '/api/lend', '/api/balances', '/api/send/assets', '/api/auth/wallet', '/api/chats/save', '/api/chats/delete'}   # + F-02: the wallet panel's reads, sign-in, chat writes
_HEAVY_IP, _HEAVY_LOCK = {}, threading.Lock()


PUBLIC_FLAG = HERE / 'data' / 'public_stats.on'          # present = kivystats.kivelabs.xyz/public is on (switched from that site)
INTERNAL_TOKEN = HERE / 'data' / 'internal.token'        # shared with kivystats (local calls only)
try:
    if not INTERNAL_TOKEN.exists():
        INTERNAL_TOKEN.write_text(secrets.token_hex(32)); os.chmod(INTERNAL_TOKEN, 0o640)
except Exception:
    pass


def admin_wallets():
    """The owner's wallets (data/admins.txt): may open /admin; counted as team in the numbers."""
    try:
        return {x.strip().lower() for x in (HERE / 'data' / 'admins.txt').read_text().split() if x.strip()}
    except Exception:
        return set()


CSP = ("default-src 'self'; script-src 'self' {HASHES}https://static.cloudflareinsights.com; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data: blob: https:; "
       "connect-src 'self' https: wss:; "   # wallets' own code runs in this page and calls their servers (Nightly went silent with a short list, 2026-10-22)
       "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'")
VENDOR = {}                                         # /vendor/<file>: the page's libraries, hosted here at fixed SHA-256 (static/vendor/SHA256SUMS)
try:
    for _ln in (Path(__file__).resolve().parent / 'static' / 'vendor' / 'SHA256SUMS').read_text().splitlines():
        _h, _f = _ln.split()
        VENDOR[_f] = _h
except Exception:
    pass


_CSPC = {'page': None, 'csp': None}
_APPF = {}                                           # /app/ files read once (name + mtime -> bytes)


def csp_for(page):
    """The page's own inline scripts are allowed by their exact SHA-256 and nothing else is (no 'unsafe-inline'): a script
    injected through any text Kivy shows can't run (review F-08, 2026-10-25). Hashed after templating; the same page
    reuses its last header (W-05)."""
    if page is not None and page == _CSPC['page']:
        return _CSPC['csp']
    out = _csp_build(page)
    _CSPC.update(page=page, csp=out)
    return out


def _csp_build(page):
    try:
        txt = page.decode() if isinstance(page, bytes) else str(page)
        hs = ["'sha256-%s'" % base64.b64encode(hashlib.sha256(b.encode()).digest()).decode()
              for b in re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', txt, re.S)]
    except Exception:
        hs = []
    return CSP.replace('{HASHES}', ''.join(h + ' ' for h in hs))


class Handler(BaseHTTPRequestHandler):
    def _real_ip(self):
        """Cloudflare's visitor address; without the header, the peer itself unless it is this box (tests, the tunnel) — so
        limits never vanish if the port is ever reached another way (review F-17, 2026-10-25)."""
        h = self.headers.get('CF-Connecting-IP')
        if h:
            if ':' in h:                                 # IPv6: one home or server holds a whole /64, so limits count the /64 (audit nv #12)
                try:
                    return str(ipaddress.ip_network(h.strip() + '/64', strict=False))
                except ValueError:
                    return h
            return h
        peer = self.client_address[0] if self.client_address else ''
        return None if peer in ('127.0.0.1', '::1', '::ffff:127.0.0.1') else peer

    def _heavy(self, fn):
        path = self.path.split('?', 1)[0]
        if path in LIGHT:                              # quick reads: the per-visitor limit only (red-team 2026-10-24)
            XW.ip = self._real_ip()
            if not ip_quota('heavy', XW.ip):
                return self._send(429, '{"error":"Too many requests — wait a minute and try again."}', 'application/json')
            if self.command == 'GET':                  # the panel's reads look up wallets too: same per-hour wallet limit (F-02)
                from urllib.parse import parse_qs, urlparse
                qa = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
                if ADDR_RE.match(qa) and not ip_quota('wallet', XW.ip, qa):
                    return self._send(429, '{"error":"Too many different wallets from here — try again later."}', 'application/json')
            return fn()
        if path not in HEAVY:
            return fn()
        ip = self._real_ip() or self.client_address[0]
        XW.ip = self._real_ip()                          # the real visitor address, for the abuse limits (None locally)
        if not ip_quota('heavy', XW.ip):
            return self._send(429, '{"error":"Too many requests — wait a minute and try again."}', 'application/json')
        if self.command == 'GET':                      # a wallet looked up: one address may only look up so many different wallets an hour
            from urllib.parse import parse_qs, urlparse
            qa = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            if ADDR_RE.match(qa) and not ip_quota('wallet', XW.ip, qa):
                return self._send(429, '{"error":"Too many different wallets from here — try again later."}', 'application/json')
        with _HEAVY_LOCK:
            if _HEAVY_IP.get(ip, 0) >= 6:
                busy = True
            else:
                busy = False
                _HEAVY_IP[ip] = _HEAVY_IP.get(ip, 0) + 1
        if busy:
            return self._send(429, '{"error":"Too many requests at once — try again in a moment."}', 'application/json')
        try:
            pool = _CHAT_ALL if path == '/api/chat' else _BUILD_ALL if path in TX_BUILD else _HEAVY_ALL   # chats, builds and reads: separate slots
            if not pool.acquire(timeout=8):
                print('kivy busy: %s refused, all slots taken' % path, flush=True)   # kivy_watch alerts on these
                return self._send(503, '{"error":"Kivy is busy right now — try again in a moment."}', 'application/json')
            try:
                return fn()
            finally:
                pool.release()
        finally:
            with _HEAVY_LOCK:
                n = _HEAVY_IP.get(ip, 1) - 1
                if n <= 0:
                    _HEAVY_IP.pop(ip, None)
                else:
                    _HEAVY_IP[ip] = n

    def _plain_http(self):
        """The visitor reached Cloudflare over plain http (launch audit 2026-10-26: http:// served the whole page).
        Cloudflare says so in X-Forwarded-Proto / CF-Visitor; local requests (tests, the tunnel's own checks) carry neither."""
        return (self.headers.get('X-Forwarded-Proto') or '').lower() == 'http' or '"scheme":"http"' in (self.headers.get('CF-Visitor') or '').replace(' ', '')

    def do_GET(self):
        if self._plain_http():                         # every page and API answer goes over https only
            self.send_response(301)
            self.send_header('Location', ORIGIN + self.path)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self._seen()
        return self._heavy(self._do_GET)

    def do_POST(self):
        if self._plain_http():                         # a form or script posting over http is refused, not redirected (the body would be resent in clear)
            return self._send(403, '{"error":"Use https://kivy.kivelabs.xyz"}', 'application/json')
        self._seen()
        return self._heavy(self._do_POST)

    def _seen(self):
        if self.path.startswith('/api/') and self._real_ip():
            _ACTIVE['t'] = time.time()

    timeout = 30                     # socket read/write deadline per request
    server_version = 'kivy'
    sys_version = ''

    def log_message(self, fmt, *args):
        pass

    def _authed(self):
        if PUBLIC:
            return True  # 2026-10-15: 'no need to lock kivy'
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == COOKIE and valid(v):
                return True
        return False

    def _send(self, code, body, ctype='text/html; charset=utf-8', extra=()):
        if code == 200 and getattr(self, 'command', '') == 'POST' and self.path.split('?', 1)[0] in TX_BUILD:
            try:
                o = json.loads(body)
                fee = kivy_fee()
                if fee and len(intent_actions(self.path.split('?', 1)[0], getattr(self, '_req', {}) or {}, o if isinstance(o, dict) else {})) < 2:
                    fee = None                           # only multi-step transactions (PTBs with 2+ actions) pay Kivy's fee (2026-10-22)
                if self.path.split('?', 1)[0] in ('/api/tx/check-send', '/api/tx/sendmany'):
                    fee = None                           # sends are always free, even several in one (2026-10-23)
                if isinstance(o, dict) and o.get('guard') and fee and not o.get('gasless'):
                    g = o['guard']                       # Kivy's flat fee: the browser adds it; this approves exactly it, to exactly us
                    st = '0x2::sui::SUI'
                    g['out'] = dict(g.get('out') or {}); g['out'][st] = str(int(g['out'].get(st) or 0) + int(fee['mist']))
                    pays = g.get('pay') or []
                    pays = pays if isinstance(pays, list) else [pays]
                    g['pay'] = pays + [{'address': fee['address'], 'coin_type': st, 'amount': fee['mist']}]
                    g['fee'] = {'address': fee['address'], 'mist': fee['mist']}
                    body = json.dumps(o)
                if isinstance(o, dict) and o.get('guard'):
                    o['intent_id'] = remember_intent(self.path.split('?', 1)[0], getattr(self, '_req', {}) or {}, o)
                    o['guard']['iid'] = o['intent_id']       # the page reports each check against this plan (stats pipeline)
                    o['guard']['vol'] = {k: str(v) for k, v in (_INTENTS.get(o['intent_id'], {}).get('vol') or {}).items()}
                    tx_for_check = o.get('tx')
                    if not tx_for_check and o.get('tx_kind'):  # a plain swap comes as a BCS kind: the builder lists its calls (S-02)
                        try:
                            kc = lend_post('/kind-calls', {'kind': o['tx_kind']}, timeout=20)
                            tx_for_check = {'commands': [{'MoveCall': c} for c in kc['calls']] + [{o: {}} for o in kc.get('others') or [] if o in ('Publish', 'Upgrade')]} \
                                if kc.get('ok') and kc.get('calls') else None   # non-call commands too (audit nv #2)   # a swap with no calls = not read
                        except Exception:
                            tx_for_check = None
                        if tx_for_check is None:             # can't read it: refused, never waved through unchecked
                            print('kivy refused build: could not list the calls of %s' % self.path.split('?', 1)[0], flush=True)
                    unreadable = bool(o.get('tx_kind')) and not o.get('tx') and tx_for_check is None
                    bad = ['unreadable transaction'] if unreadable else pkg_watch(self.path.split('?', 1)[0], tx_for_check)   # a send has no tx here: the page builds it
                    body = json.dumps(o)
                    if bad:                              # a program Kivy hasn't verified: refused here, not just in the browser (F-04)
                        print('kivy refused build: unlisted %s' % ', '.join(bad), flush=True)
                        code, body = 422, json.dumps({'error': "This transaction would call a program Kivy hasn't verified (%s), so I didn't prepare it." % bad[0]})
            except Exception:
                pass
        if code >= 400 and getattr(self, 'command', '') == 'POST' and self.path.split('?', 1)[0] in TX_BUILD and self._real_ip():
            try:                                       # where Kivy fails: a transaction it could not build
                metrics.failure('build_error', (json.loads(body).get('error') if isinstance(body, str) and body.startswith('{') else str(body))[:200],
                                self.path.split('?', 1)[0].rsplit('/', 1)[-1])
            except Exception:
                pass
        if code >= 400 and self.path.split('?', 1)[0].startswith('/api/') and isinstance(body, str) and body.startswith('{'):
            try:                                       # raw text from a builder, SDK or another service never reaches the user (review F-14)
                o = json.loads(body)
                e = str(o.get('error') or '')
                if RAW_ERR_RE.search(e):
                    print('kivy error shown plainly: %s %s' % (self.path.split('?', 1)[0], e[:300]), flush=True)
                    o['error'] = ("A pool on the swap route refused it in the test run, so nothing was prepared or signed. Tap Try again for a fresh route."
                                  if re.search(r'router::|MoveAbort.*router', e) else   # Aftermath's route: the user approves once more (2026-10-25)
                                  "The protocol refused this in a test run, so nothing was prepared. Try again in a minute, or a smaller amount."
                                  if re.search(r'(?i)abort|simulat|dry.?run|insufficient|balance', e) else
                                  "Kivy couldn't reach a service it needs right now. Nothing was prepared — try again in a minute.")
                    body = json.dumps(o)
            except Exception:
                pass
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Strict-Transport-Security', 'max-age=31536000')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin-allow-popups')   # web wallets that open a popup still talk back
        if ctype.startswith('text/html'):              # scripts only from this site (libraries are hosted here, hashed) — user-harm #1
            self.send_header('Content-Security-Policy', csp_for(data))
        if not any(k.lower() == 'cache-control' for k, _ in extra):
            self.send_header('Cache-Control', 'no-store')
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, where, extra=()):
        self._send(303, '', extra=(('Location', where),) + tuple(extra))

    def _do_GET(self):
        path = self.path.split('?', 1)[0]
        if PUBLIC and path == '/login':
            return self._redirect('/')
        if path == '/login':
            return self._send(200, LOGIN.replace('{ERR}', ''))
        if path == '/logout':
            return self._redirect('/login', (('Set-Cookie', COOKIE + '=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict'),))
        if not self._authed():
            return self._redirect('/login')
        if path == '/api/fee':                          # what the cards show: Kivy's fee and whether referrals are on
            f = kivy_fee()
            tre = str((K().get('revenue') or {}).get('address') or '').lower()   # Kivy's treasury: donations go here (Pricing, 2026-10-23)
            return self._send(200, json.dumps({'sui': f['sui'] if f else 0, 'ref': bool(ref_code()), 'treasury': tre if ADDR_RE.match(tre) else ''}), 'application/json')
        if path.startswith('/internal/'):            # Kivy's numbers for kivystats.kivelabs.xyz (its own login) — local + token only
            tok = INTERNAL_TOKEN.read_text().strip() if INTERNAL_TOKEN.exists() else ''
            if self.headers.get('CF-Connecting-IP') or not tok or not hmac.compare_digest(self.headers.get('X-Internal-Token') or '', tok):
                return self._send(404, 'Not found', 'text/plain')
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            excl = (qs.get('team') or [''])[0] == '0'
            metrics.mark_team(None, admin_wallets())
            if path == '/internal/customers':          # dataset 1 for kivystats: wallets that signed in, and their signed Terms
                if not accounts.CUSTOMERS.exists():
                    return self._send(200, json.dumps({'on': accounts.terms_on(), 'customers': []}), 'application/json')
                import sqlite3
                c = sqlite3.connect('file:%s?mode=ro' % accounts.CUSTOMERS, uri=True)
                ag = {}
                for i, a, v, ts, sig in c.execute('SELECT id, address, version, ts, signature FROM agreements ORDER BY ts'):
                    ag.setdefault(a, []).append({'id': i, 'version': v, 'ts': ts, 'sig': bool(sig)})
                rows = [{'address': a, 'wh': metrics.wh(a), 'first_seen': f, 'last_seen': l, 'sign_ins': n, 'agreements': ag.get(a, [])}
                        for a, f, l, n in c.execute('SELECT address, first_seen, last_seen, sign_ins FROM customers ORDER BY first_seen DESC')]
                return self._send(200, json.dumps({'on': accounts.terms_on(), 'version': accounts.TERMS_VERSION, 'customers': rows}), 'application/json')
            if path == '/internal/metrics':
                try:
                    days = max(7, min(365, int((qs.get('days') or ['30'])[0])))
                except Exception:
                    days = 30
                s = metrics.summary(days, excl)
                s['public_stats'] = PUBLIC_FLAG.exists()
                return self._send(200, json.dumps(s), 'application/json')
            if path == '/internal/public':           # the public page's numbers (users only, no ids)
                return self._send(200, json.dumps(dict(metrics.public_summary(), on=PUBLIC_FLAG.exists())), 'application/json')
            if path == '/internal/public-switch':
                on = (qs.get('on') or [''])[0]
                if on == '1':
                    PUBLIC_FLAG.write_text('on')
                elif on == '0' and PUBLIC_FLAG.exists():
                    PUBLIC_FLAG.unlink()
                return self._send(200, json.dumps({'public_stats': PUBLIC_FLAG.exists()}), 'application/json')
            if path == '/internal/export':
                what = (qs.get('what') or ['ptbs'])[0]
                what = what if what in ('ptbs', 'actions', 'daily', 'snapshots') else 'ptbs'
                import csv, io
                head, rows = metrics.export_rows(what, excl)
                buf = io.StringIO(); w = csv.writer(buf); w.writerow(head); w.writerows(rows)
                return self._send(200, buf.getvalue(), 'text/csv; charset=utf-8',
                                  (('Content-Disposition', 'attachment; filename="kivy-%s-%s.csv"' % (what, metrics.day())),))
            return self._send(404, 'Not found', 'text/plain')
        vm = re.match(r'^/vendor/([A-Za-z0-9.@-]{3,60}\.js)$', path)
        if vm and vm.group(1) in VENDOR:                # served only if the file still matches its recorded hash
            f = Path(__file__).resolve().parent / 'static' / 'vendor' / vm.group(1)
            data = f.read_bytes()
            if hashlib.sha256(data).hexdigest() != VENDOR[vm.group(1)]:
                return self._send(500, 'library changed on disk', 'text/plain')
            return self._send(200, data, 'text/javascript; charset=utf-8', (('Cache-Control', 'public, max-age=31536000, immutable'),))
        am = re.match(r'^/app/([a-z0-9-]{2,40})\.([0-9a-f]{12})\.(js|woff2)$', path)
        if am:                                       # the page's scripts and font (tools/build_page.py): the name carries the content's
            f = Path(__file__).resolve().parent / 'static' / 'app' / am.group(0)[5:]   # SHA-256, so a file changed on disk is never served
            try:
                ck = (f.name, f.stat().st_mtime_ns)
                data = _APPF.get(ck)
                if data is None:
                    data = f.read_bytes()
                    if hashlib.sha256(data).hexdigest()[:12] != am.group(2):
                        return self._send(500, 'file changed on disk', 'text/plain')
                    _APPF.clear() if len(_APPF) > 20 else None
                    _APPF[ck] = data
            except OSError:
                return self._send(404, 'not found', 'text/plain')
            return self._send(200, data, 'text/javascript; charset=utf-8' if am.group(3) == 'js' else 'font/woff2',
                              (('Cache-Control', 'public, max-age=31536000, immutable'),))
        im = re.match(r'^/icons/([A-Za-z]{2,12})\.(png|svg)$', path)
        if im:                                       # coin and protocol icons, stored locally (no hotlinking)
            f = Path(__file__).resolve().parent / 'static' / 'icons' / (im.group(1) + '.' + im.group(2))
            if f.is_file():
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml' if im.group(2) == 'svg' else 'image/png')
                self.send_header('Cache-Control', 'public, max-age=604800')
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'")
                body = f.read_bytes(); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
                return
        icons = {'/favicon.ico': ('favicon.ico', 'image/x-icon'), '/favicon-32.png': ('favicon-32.png', 'image/png'),
                 '/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png'), '/icon-512.png': ('icon-512.png', 'image/png')}
        if path in icons:                       # the Kivy 'k' (Fredoka) as the site icon
            f, ctype = icons[path]
            return self._send(200, (HERE / 'static' / f).read_bytes(), ctype)
        if path == '/api/terms':                       # is the Terms step on, and has this wallet signed the current version?
            from urllib.parse import parse_qs, urlparse
            a = (parse_qs(urlparse(self.path).query).get('address') or [''])[0].lower()
            me = str(accounts.email_from_cookie(self.headers.get('Cookie')) or '').lower()
            # only the signed-in wallet learns its own status: no public "is this wallet a customer" oracle (security audit 2026-10-27)
            return self._send(200, json.dumps({'on': accounts.terms_on(), 'version': accounts.TERMS_VERSION,
                                               'agreed': bool(a and a == me and accounts.agreed(a))}), 'application/json')
        if path in ('/terms', '/privacy'):
            f = HERE / 'legal' / (path[1:] + '.html')
            if not f.is_file():
                return self._send(404, 'not found', 'text/plain')
            return self._send(200, f.read_bytes())
        if path == '/api/me':
            return self._send(200, json.dumps({'email': accounts.email_from_cookie(self.headers.get('Cookie'))}),
                              'application/json')
        if path == '/api/chats':
            email = accounts.email_from_cookie(self.headers.get('Cookie'))
            if not email:
                return self._send(401, '{"error":"sign in with your wallet to keep chats"}', 'application/json')
            return self._send(200, json.dumps(accounts.list_chats(email)), 'application/json')
        if path == '/api/perp/preview':
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            g = lambda k, d='': (q.get(k) or [d])[0]
            addr, sym = g('address'), chart_symbol(g('symbol'))
            try:
                side, usd, lev = int(g('side', '0')), float(g('usd', '0')), float(g('lev', '1'))
            except ValueError:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if not sym or side not in (0, 1) or not (0 < usd <= 100000) or not (1 <= lev <= 100):
                return self._send(400, '{"error":"bad request"}', 'application/json')
            try:
                r = lend_cli('perp-preview', addr if ADDR_RE.match(addr) else '0x0', sym, side, usd, lev)
            except Exception:
                return self._send(502, '{"error":"preview unavailable"}', 'application/json')
            if r.get('ok') and r.get('has_position') and ADDR_RE.match(addr):
                r['pos_lev'] = open_lev(addr, sym)         # the order panel locks its leverage to the open position's (Aftermath Error 1022)
            return self._send(200, json.dumps(r), 'application/json')
        if path == '/api/perp/positions':
            from urllib.parse import parse_qs, urlparse
            addr = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"address"}', 'application/json')
            try:
                r = lend_cli('perp-positions', addr)
                try:                                 # each market's icon, for the wallet tab
                    imgs = {m['symbol']: m.get('image') for m in perp_market_list()}
                    for x in (r.get('positions') or []) + (r.get('orders') or []):
                        x['image'] = imgs.get(x.get('symbol'))
                except Exception:
                    pass
                return self._send(200, json.dumps(r), 'application/json')
            except Exception:
                return self._send(502, '{"error":"positions unavailable"}', 'application/json')
        if path == '/api/spot/chart':
            from urllib.parse import parse_qs, urlparse
            sym = ((parse_qs(urlparse(self.path).query).get('coin') or [''])[0]).upper()
            if sym not in COINS() or sym == 'USDC':
                return self._send(400, '{"error":"coin"}', 'application/json')
            try:
                return self._send(200, json.dumps(spot_chart(sym)), 'application/json')
            except Exception:
                return self._send(502, '{"error":"chart unavailable"}', 'application/json')
        if path == '/api/perp/cancel-preview':      # what cancelling frees + what would go back to the wallet (built + simulated, not signed)
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            addr, sym = (q.get('address') or [''])[0], chart_symbol((q.get('symbol') or [''])[0])
            oid = (q.get('id') or [''])[0]
            if not ADDR_RE.match(addr) or not sym or not re.match(r'^\d{1,60}n?$', oid):
                return self._send(400, '{"error":"bad request"}', 'application/json')
            try:
                r = lend_cli('perp-cancel', addr, 'limit', sym, oid, '1')
            except Exception:
                return self._send(502, '{"error":"preview unavailable"}', 'application/json')
            return self._send(200, json.dumps({'ok': bool(r.get('ok')), 'released': r.get('released'), 'withdrawn': r.get('withdrawn'),
                                               'error': r.get('error')}), 'application/json')
        if path == '/api/market/list':               # the trading page's market picker
            try:
                rows = [{'symbol': r['symbol'], 'image': r.get('image'), 'price': r.get('price'), 'change': r.get('change'), 'category': r.get('category'),
                         'max_lev': r.get('max_lev'), 'funding': r.get('funding'), 'volume': r.get('volume'), 'index': r.get('index'), 'oi_usd': r.get('oi_usd')}
                        for r in perp_market_list()]
            except Exception:
                rows = []
            spot = [{'symbol': c, 'name': disp(c)} for c in [a.upper() for a in (K().get('assets') or [])] if c != 'USDC']
            return self._send(200, json.dumps({'perps': rows, 'spot': spot}), 'application/json')
        if path == '/api/market/view':               # everything the trading page needs for one market (+ the swap box for spot)
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            addr = (q.get('address') or [''])[0]
            addr = addr if ADDR_RE.match(addr) else None
            try:
                mv = market_overview((q.get('symbol') or [''])[0], addr)
                if not mv:
                    return self._send(404, '{"error":"unknown market"}', 'application/json')
                if mv.get('spot') and addr:
                    try:
                        sa = spot_ask('buy %s' % mv['spot'], addr)
                        mv['swap'] = (sa or {}).get('widget')
                        mv['swap_text'] = None if mv['swap'] else (sa or {}).get('text')
                    except Exception:
                        pass
                return self._send(200, json.dumps(mv), 'application/json')
            except Exception:
                return self._send(502, '{"error":"market unavailable"}', 'application/json')
        if path == '/api/market/book':
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            venue, sym = (q.get('venue') or [''])[0], (q.get('symbol') or [''])[0].upper()
            sym = chart_symbol(sym) if venue == 'perp' else (sym if sym in COINS() else None)
            if venue not in ('perp', 'spot') or not sym:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            try:
                return self._send(200, json.dumps(market_book(venue, sym)), 'application/json')
            except Exception:
                return self._send(502, '{"error":"book unavailable"}', 'application/json')
        if path == '/api/swap/quote':
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                a, b = ckey((q.get('from') or [''])[0]), ckey((q.get('to') or [''])[0])
                amt = float((q.get('amount') or ['0'])[0])
                if not a or not b or a == b or not (0 < amt < 1e12):
                    raise ValueError
                return self._send(200, json.dumps(swap_quote(a, b, amt)), 'application/json')
            except ValueError:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            except Exception:
                return self._send(502, '{"error":"no route right now"}', 'application/json')
        if path == '/api/perp/history':
            from urllib.parse import parse_qs, urlparse
            addr = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"address"}', 'application/json')
            try:
                return self._send(200, json.dumps(lend_cli('perp-history', addr)), 'application/json')
            except Exception:
                return self._send(502, '{"error":"history unavailable"}', 'application/json')
        if path == '/api/chart':
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            sym, tf = chart_symbol((q.get('symbol') or [''])[0]), (q.get('tf') or ['15m'])[0]
            if not sym or tf not in TF_SPAN:
                return self._send(400, '{"error":"unknown market"}', 'application/json')
            try:
                from concurrent.futures import ThreadPoolExecutor
                pool = ThreadPoolExecutor(3)             # prices, stats and candles at the same time, not one after another (2026-10-21)
                f_stats = pool.submit(lambda: stale_ok(_CANDLES, ('stats', sym), 15, lambda: chart_stats(sym)))
                f_cand = pool.submit(candles, sym, tf)
                pool.shutdown(wait=False)
                mark, oracle, book = None, None, None
                try:                                     # live prices (every few seconds): mark, oracle (index) and order-book mid
                    hit = _CANDLES.get(('mark', sym))
                    if hit and time.time() - hit[0] < 3:
                        mark, oracle, book = hit[1]
                    else:
                        mp = af_post('markets/prices', {'marketIds': [af_markets()[sym]]}).get('marketsPrices') or []
                        if mp:
                            mark = float(mp[0].get('markPrice') or mp[0].get('basePrice'))
                            oracle = float(mp[0]['basePrice']) if mp[0].get('basePrice') else None
                            book = float(mp[0]['midPrice']) if mp[0].get('midPrice') else None
                        _CANDLES[('mark', sym)] = (time.time(), (mark, oracle, book))
                except Exception:
                    pass
                try:                                     # funding / open interest / 24h volume, refreshed every 15 s
                    stats = dict(f_stats.result())
                    stats['oi_usd'] = stats.pop('oi', 0) * (mark or stats.pop('index', 0) or 0)
                    stats.pop('index', None)
                except Exception:
                    stats = {}
                return self._send(200, json.dumps({'symbol': sym, 'tf': tf, 'market_id': af_markets()[sym], 'candles': f_cand.result(), 'mark': mark,
                                                   'oracle': oracle, 'book': book, 'stats': stats}), 'application/json')
            except Exception:
                return self._send(502, '{"error":"chart data unavailable"}', 'application/json')
        if path == '/api/lend/rewards':
            from urllib.parse import parse_qs, urlparse
            addr = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"address"}', 'application/json')
            try:
                r = lend_cli('rewards', addr)
                icon = lambda sym: (ecoins().get(ckey(sym) or '') or {}).get('icon')
                items = [{'market': x['market'], 'name': LEND_MKTS.get(x['market'], x['market']), 'symbol': x.get('symbol'), 'coin_type': x.get('coinType'),
                          'icon': icon(x.get('symbol')), 'amount': x.get('amount'), 'usd': round(float(x.get('usd') or 0), 6)} for x in (r.get('rewards') or [])]
                return self._send(200, json.dumps({'usd': r.get('usd', 0), 'count': len(items), 'items': items}), 'application/json')
            except Exception:
                return self._send(502, '{"error":"rewards read failed"}', 'application/json')
        if path == '/api/tx/labels':                 # Activity tab: what Kivy called each of these transactions (digests are public)
            from urllib.parse import parse_qs, urlparse
            ds = [d for d in (parse_qs(urlparse(self.path).query).get('d') or [])[:25] if DIGEST_RE.match(d)]
            return self._send(200, json.dumps(tx_labels(ds)), 'application/json')   # public again (2026-10-22: the chain is public anyway)
        if path == '/api/memory':                    # the home screen's "welcome back" line for this wallet
            from urllib.parse import parse_qs, urlparse
            addr = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"address"}', 'application/json')
            try:
                return self._send(200, json.dumps({'welcome': welcome_line(wallet_memory(addr))}), 'application/json')
            except Exception:
                return self._send(200, '{"welcome":""}', 'application/json')
        if path == '/api/recipient':                  # the Send card checks each recipient as it's typed
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            q, frm = (qs.get('q') or [''])[0][:120], (qs.get('from') or [''])[0][:70]
            return self._send(200, json.dumps(recipient_check(q, frm if ADDR_RE.match(frm) else '')), 'application/json')
        if path == '/api/send/assets':               # the Send card: everything this wallet can send — wallet coins and every lent coin
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            addr = (qs.get('address') or [''])[0].lower()
            part = (qs.get('part') or [''])[0]               # 'wallet' = instant (cached balances), 'lent' = the slower lending scan
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"connect a wallet first"}', 'application/json')
            items = []
            try:
                hold = (all_balances(addr) or {}) if part != 'lent' else {}
                for c, v in hold.items():
                    amt = spendable(c, hold) if float(v or 0) > 0 else 0
                    if amt and amt > 0:
                        items.append({'coin': c, 'source': 'wallet', 'label': 'Wallet', 'amount': round(float(amt), 9), 'px': coin_px(c) or 0, 'icon': coin_icon(c)})
            except Exception:
                pass
            try:
                for k, ps in ((lend_positions(addr) or {}) if part != 'wallet' else {}).items():
                    lent = float(ps.get('usdc') or 0) + float(ps.get('in_app') or 0)
                    if lent >= 0.000001:
                        items.append({'coin': 'USDC', 'source': k, 'label': LEND_MKTS.get(k, k), 'amount': round(lent, 6), 'px': 1.0, 'icon': coin_icon('USDC'), 'lent': True})
                for x in (other_lent(addr) if part != 'wallet' else []):
                    if float(x.get('lent') or 0) > 0:
                        items.append({'coin': x['coin'], 'source': x['id'], 'label': x['name'], 'amount': x['lent'], 'px': x.get('px') or 0, 'icon': x.get('icon'), 'lent': True})
            except Exception:
                pass
            items = [i for i in items if i['amount'] * (i['px'] or 0) >= 0.01 or not i['px']]   # no dust
            items.sort(key=lambda i: -(i['amount'] * (i['px'] or 0)))
            return self._send(200, json.dumps({'items': items}), 'application/json')
        if path == '/api/lend/others':               # coins lent other than USDC, for the wallet panel (slower: its own call)
            from urllib.parse import parse_qs, urlparse
            addr = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"address"}', 'application/json')
            icons = {r.get('token'): r.get('icon') for r in lend_rates_all() if r.get('token') and r.get('icon')}
            rows = [dict(x, icon=icons.get(x['coin']) or icons.get(x['coin'].upper())) for x in other_lent(addr, fresh=True)]
            return self._send(200, json.dumps({'rows': rows}), 'application/json')
        if path == '/api/lend':
            from urllib.parse import parse_qs, urlparse
            addr = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            try:
                perp = {}
                if ADDR_RE.match(addr):
                    try:
                        perp = perp_account(addr)
                    except Exception:
                        perp = {}
                return self._send(200, json.dumps({'rates': lend_rates(), 'perp': perp,
                                                   'positions': lend_positions(addr) if ADDR_RE.match(addr) else {}}), 'application/json')
            except Exception:
                return self._send(502, '{"error":"lending read failed"}', 'application/json')
        if path == '/api/tx/detail':
            from urllib.parse import parse_qs, urlparse
            d = (parse_qs(urlparse(self.path).query).get('digest') or [''])[0]
            try:
                return self._send(200, json.dumps(tx_detail(d)), 'application/json')
            except Exception:
                return self._send(502, '{"error":"chain read failed"}', 'application/json')
        if path == '/api/balances':
            from urllib.parse import parse_qs, urlparse
            addr = (parse_qs(urlparse(self.path).query).get('address') or [''])[0]
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"address"}', 'application/json')
            try:
                b = balances(addr)
            except Exception:
                return self._send(502, '{"error":"node"}', 'application/json')
            px = prices()
            ext = other_balances(addr)
            for x in ext:
                x['chg'] = coin_change(x['sym'])
            norm = lambda t: re.sub(r'^0x0*', '0x', str(t).lower())
            return self._send(200, json.dumps({'balances': b, 'prices': {x: px.get(x) for x in b}, 'names': SPOT_NAME,
                                               'changes': {x: coin_change(x) for x in b}, 'extra': ext,
                                               # coin type -> [symbol, decimals]: the Activity tab names what moved
                                               'coins': {norm(c['type']): [s, c['decimals'], c.get('icon') or coin_icon(s)] for s, c in ecoins().items()}}),
                              'application/json')
        if path in ('/', '/index.html'):
            import html as _h
            src = HERE / 'index.html'                  # the minified copy (tools/build_page.py) only when it is at least as new as the source
            mn = HERE / 'index.min.html'
            if mn.exists() and mn.stat().st_mtime_ns >= src.stat().st_mtime_ns:
                src = mn
            key = (str(src), os.stat(src).st_mtime_ns, KNOWLEDGE.stat().st_mtime_ns, KIVY_PKGS_FILE.stat().st_mtime_ns, NET())
            if _PAGE.get('key') == key:                # unchanged files: the page built last time, revalidated not re-sent (W-05)
                hdr = (('ETag', _PAGE['etag']), ('Cache-Control', 'private, no-cache'))
                if self.headers.get('If-None-Match') == _PAGE['etag']:
                    return self._send(304, '', extra=hdr)
                return self._send(200, _PAGE['page'], extra=hdr)
            k = K()
            page = src.read_text()
            page = (page.replace('{{GREETING_TITLE}}', _h.escape(k['greeting_title']))
                        .replace('{{GREETING_TEXT}}', _h.escape(k['greeting_text']))
                        .replace('{{STATUS_CHIP}}', _h.escape(k['status_chip']))
                        .replace('{{NETWORK}}', NET()))
            try:                                       # the page's program allowlist comes from kivy_pkgs.json, the server's own list (F-04), as data
                page = page.replace('</head>', '<script type="application/json" id="kivy-pkgs">%s</script></head>'
                                    % json.dumps(json.loads(KIVY_PKGS_FILE.read_text())['packages']).replace('</', '<\\/'), 1)
            except Exception:
                pass
            page = (page
                        .replace('{{SUGGESTIONS}}', ''.join('<button type="button">%s</button>' % _h.escape(x)
                                                            for x in k['suggestions'])))
            _PAGE.update(key=key, page=page, etag='"%s"' % hashlib.sha256(page.encode()).hexdigest()[:20])
            return self._send(200, page, extra=(('ETag', _PAGE['etag']), ('Cache-Control', 'private, no-cache')))
        return self._send(404, 'not found', 'text/plain')

    def _body(self, limit=4096):
        n = int(self.headers.get('Content-Length') or 0)
        if n < 0 or n > limit:  # history included
            raise ValueError('too large')
        return self.rfile.read(n).decode('utf-8', 'replace')

    def _do_POST(self):
        path = self.path.split('?', 1)[0]
        try:
            raw = self._body(160000 if path == '/api/chats/save' else 24000)
        except ValueError:
            return self._send(413, 'too large', 'text/plain')
        self._req = {}
        if path in TX_BUILD:
            try:
                self._req = json.loads(raw or '{}')
            except Exception:
                self._req = {}
            if accounts.terms_on() and not accounts.agreed((self._req or {}).get('address')):   # no transaction before the Terms are signed
                return self._send(403, json.dumps({'error': "Agree to Kivy's Terms first: tap Agree & sign.", 'terms': True}), 'application/json')
        if path.startswith('/api/auth/') or path.startswith('/api/chats/'):
            origin = self.headers.get('Origin')
            if origin != ORIGIN:                       # cookie-bearing writes: same site only
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw or '{}')
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if path == '/api/auth/challenge':
                try:
                    return self._send(200, json.dumps({'message': accounts.challenge(body.get('address'))}), 'application/json')
                except ValueError as exc:
                    return self._send(400, json.dumps({'error': str(exc)}), 'application/json')
            if path == '/api/auth/wallet':
                try:
                    who = accounts.sign_in_wallet(body.get('address'), body.get('message'), body.get('signature'))
                except Exception as exc:
                    return self._send(401, json.dumps({'error': 'Sign-in failed: %s' % str(exc)[:80]}), 'application/json')
                if accounts.terms_on():                    # the wallet address + signed Terms are kept only once the Terms are live
                    accounts.record_sign_in(who, body.get('message'), body.get('signature'))
                ck = '%s=%s; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=%d' % (
                    accounts.USER_COOKIE, accounts.make_cookie(who), accounts.USER_TTL)
                return self._send(200, json.dumps({'email': who}), 'application/json', (('Set-Cookie', ck),))
            if path == '/api/auth/logout':
                who = accounts.email_from_cookie(self.headers.get('Cookie'))
                if who:
                    accounts.log_out_everywhere(who)       # every device's pass for this account ends now, not just this browser's
                ck = '%s=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0' % accounts.USER_COOKIE
                return self._send(200, '{"ok":true}', 'application/json', (('Set-Cookie', ck),))
            email = accounts.email_from_cookie(self.headers.get('Cookie'))
            if not email:
                return self._send(401, '{"error":"sign in with your wallet to keep chats"}', 'application/json')
            try:
                if path == '/api/chats/save' and looks_secret(json.dumps(body.get('chat') or {})):
                    raise ValueError("a chat with a seed phrase or private key in it isn't saved")
                if path == '/api/chats/save':
                    accounts.save_chat(email, body.get('chat') or {}, body.get('index'))
                elif path == '/api/chats/delete':
                    accounts.delete_chat(email, body.get('id'))
                else:
                    return self._send(404, '{"error":"not found"}', 'application/json')
            except ValueError as exc:
                return self._send(400, json.dumps({'error': str(exc)}), 'application/json')
            return self._send(200, '{"ok":true}', 'application/json')
        if path == '/login':
            from urllib.parse import parse_qs
            f = parse_qs(raw)
            u, pw = (f.get('user') or [''])[0], (f.get('pass') or [''])[0]
            if AUTH_USER and AUTH_PASS and hmac.compare_digest(u, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS):
                return self._redirect('/', (('Set-Cookie', COOKIE + '=' + token() +
                                             '; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=%d' % TTL),))
            time.sleep(1.0)                          # slow down guessing
            return self._send(401, LOGIN.replace('{ERR}', '<div class="err">Wrong username or password.</div>'))
        if path == '/api/tx/swap':
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr = str(body.get('address') or '')
                a, b, amt = str(body.get('from', '')).upper(), str(body.get('to', '')).upper(), float(body.get('amount'))
                seen_min = float(body.get('min_out') or 0)          # the minimum the user saw on the card (audit V02)
                if not (math.isfinite(amt) and math.isfinite(seen_min) and amt > 0 and seen_min >= 0):
                    raise ValueError
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"connect a wallet first"}', 'application/json')
            if NET() != 'mainnet':
                return self._send(422, json.dumps({'error': 'Swaps are not available on testnet yet (the swap router is mainnet-only). Sends work.'}), 'application/json')
            a, b = ckey(a) or a, ckey(b) or b
            chk = plan('swap', amt, a, b, holdings=all_balances(addr))
            if not chk.get('plan'):
                return self._send(422, json.dumps({'error': chk.get('text')}), 'application/json')
            try:
                out = swap_tx(addr, a, b, amt, seen_min)
            except Exception as exc:
                return self._send(502, json.dumps({'error': 'Aftermath router: %s' % str(exc)[:160]}), 'application/json')
            t_in, max_in = guard_units(a, amt)
            cb = cinfo(SPOT_IN.get(b, b))
            if seen_min and float(out.get('expected_out') or 0) < seen_min:   # the route got worse than what was reviewed: ask again
                return self._send(409, json.dumps({'error': 'The price moved: you would now get about %s %s, below the %s you approved. Check the new quote and approve again.'
                                                            % (dec(out.get('expected_out') or 0), disp(b), dec(seen_min))}), 'application/json')
            floor = max(int(math.floor(float(out.get('min_out') or 0) * 10 ** int(cb['decimals']) * 0.999)),
                        base_units(seen_min, cb['decimals']) if seen_min else 0)   # never below what the user approved
            out['guard'] = guard(out={t_in: max_in}, recv={cb['type']: str(max(1, floor))})   # the wallet must receive at least the route's minimum (was 1 unit)
            return self._send(200, json.dumps(out), 'application/json')
        if path == '/api/tx/lend':
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr = str(body.get('address') or '')
                proto, act = str(body.get('protocol') or '').lower(), str(body.get('action') or '')
                amt = body.get('amount')
                amt = 'all' if str(amt) == 'all' else float(amt or 0)
                coin = str(body.get('coin') or 'USDC')
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if act in ('deposit', 'withdraw') and amt != 'all' and not (math.isfinite(amt) and amt >= 0):
                return self._send(400, '{"error":"bad amount"}', 'application/json')
            if act == 'deposit' and amt == 'all':      # a deposit needs a number (it crashed the handler — review F-15)
                return self._send(400, '{"error":"say how much to lend"}', 'application/json')
            usdc = coin.upper() == 'USDC'
            coin = 'USDC' if usdc else lend_token(coin) if re.match(r'^[A-Za-z]{2,10}$', coin) else None
            if not ADDR_RE.match(addr) or not coin or act not in ('deposit', 'withdraw', 'claim') or \
                    not (proto in PROTOS or (proto in LEND_MKTS and not usdc and act != 'claim') or (act == 'claim' and proto in CLAIM_SCOPES)):
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if act == 'deposit':                      # same rules as the plan: cap, balance
                if usdc:
                    chk = plan('lend', float(amt), 'USDC', holdings=balances(addr), protocol=proto)
                    if not chk.get('plan'):
                        return self._send(422, json.dumps({'error': chk.get('text')}), 'application/json')
                else:
                    why = spend_check(coin, float(amt), addr)
                    if why:
                        return self._send(422, json.dumps({'error': why}), 'application/json')
            try:
                out = lend_cli('build', addr, proto, act, amt, *([] if usdc else [coin]))
            except Exception as exc:
                return self._send(502, json.dumps({'error': 'lending builder: %s' % str(exc)[:120]}), 'application/json')
            if not out.get('ok'):
                return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
            _LEND['pos'].pop(addr, None); _LEND.get('posall', {}).pop(addr, None)
            usdc_t = COINS()['USDC']['type']
            if usdc:
                g = guard(out=dict([guard_units('USDC', amt)])) if act == 'deposit' else \
                    guard(recv={usdc_t: '1'}, ctokens=True) if act == 'withdraw' else guard()
            else:                                     # the coin type the builder used; the amount is the one approved here
                ct, dc = str(out.get('coin_type') or ''), int(out.get('decimals') or 0)
                if not re.match(r'^0x[0-9a-fA-F]+::\w+::\w+$', ct):
                    return self._send(502, '{"error":"lending builder: no coin type"}', 'application/json')
                g = guard(out={ct: str(int(math.ceil(float(amt) * 10 ** dc * 1.001)) + 1)}) if act == 'deposit' else \
                    guard(recv={ct: '1'}, ctokens=True)
            return self._send(200, json.dumps({'tx': out['tx'], 'amount': out.get('amount'), 'guard': g}), 'application/json')
        if path == '/api/tx/swaplend':                 # swap one coin into another and lend it: ONE transaction
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr, proto = str(body.get('address') or ''), str(body.get('protocol') or '').lower()
                a, b, amt = str(body.get('from') or '').upper(), str(body.get('to') or ''), float(body.get('amount'))
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            a = ckey(a)
            b = lend_token(b) if re.match(r'^[A-Za-z]{2,10}$', b) else None
            if not ADDR_RE.match(addr) or not a or not b or a == b or proto not in LEND_MKTS:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if NET() != 'mainnet':
                return self._send(422, json.dumps({'error': 'Swaps are mainnet-only.'}), 'application/json')
            why = spend_check(a, amt, addr)
            if why:
                return self._send(422, json.dumps({'error': why}), 'application/json')
            try:
                out = lend_cli('swaplend', addr, proto, a, b, amt)
            except Exception as exc:
                return self._send(502, json.dumps({'error': 'lending builder: %s' % str(exc)[:120]}), 'application/json')
            if not out.get('ok'):
                return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
            t_in, max_in = guard_units(a, amt)
            if out.get('from_type') and re.sub(r'^0x0*', '', str(out['from_type'])) != re.sub(r'^0x0*', '', t_in):
                return self._send(502, '{"error":"lending builder: coin mismatch"}', 'application/json')
            _LEND['pos'].pop(addr, None); _LEND.get('posall', {}).pop(addr, None)
            # only the coin paid in may leave the wallet; the swapped coin goes straight into the market
            return self._send(200, json.dumps({'tx': out['tx'], 'router': out.get('router'), 'guard': guard(out={t_in: max_in}), 'to': b,
                                               'expected_out': out.get('expected_out'), 'min_out': out.get('min_out')}), 'application/json')
        if path == '/api/tx/dust':                     # the dust sweep: every coin under $0.01 into one coin, ONE transaction (2026-10-25)
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr, to = str(body.get('address') or ''), ckey(str(body.get('to') or '')) or lend_token(str(body.get('to') or ''))
                ok_types = {re.sub(r'^0x0*', '0x', str(t).lower()) for t in (body.get('types') or [])[:12]}
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if not ADDR_RE.match(addr) or not to or not ok_types:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if NET() != 'mainnet':
                return self._send(422, json.dumps({'error': 'Dust sweeps need the swap router, which is mainnet-only.'}), 'application/json')
            norm = lambda t: re.sub(r'^0x0*', '0x', str(t or '').lower())
            # never trust the card: only coins that are STILL dust and that the card showed
            dust = [x for x in dust_coins(addr, to)[0] if norm(x['type']) in ok_types]
            if not dust:
                return self._send(422, json.dumps({'error': 'Your dust changed since the card was made — ask again for a fresh one.'}), 'application/json')
            try:
                out = lend_post('/dust', {'address': addr, 'to': to, 'items': [{'sym': x['sym'], 'type': x['type'], 'dec': x['dec']} for x in dust]})
            except Exception as exc:
                return self._send(502, json.dumps({'error': 'dust builder: %s' % str(exc)[:120]}), 'application/json')
            if not out.get('ok') or not out.get('legs'):
                return self._send(422, json.dumps({'error': out.get('error') or 'None of the dust can be swapped right now.'}), 'application/json')
            if norm(out.get('to_type')) != norm(cinfo(to)['type']) or any(norm(l['type']) not in {norm(x['type']) for x in dust} for l in out['legs']):
                return self._send(502, '{"error":"dust builder: coin mismatch"}', 'application/json')
            # each dust coin may leave exactly as much as it holds; the chosen coin must arrive, at least the routes' minimum
            out['guard'] = guard(out={l['type']: str(int(l['units'])) for l in out['legs']},
                                 recv={out['to_type']: str(max(1, int(int(out.get('min_units') or 0) * 0.999)))})
            return self._send(200, json.dumps(out), 'application/json')
        if path == '/api/tx/compose':                  # a chain of steps: ONE transaction (2026-10-20)
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr, spec = str(body.get('address') or ''), body.get('spec') or {}
                src, swaps, sinks = spec.get('source') or {}, list(spec.get('swaps') or []), list(spec.get('sinks') or [])
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            bad = lambda why: self._send(400, json.dumps({'error': why}), 'application/json')
            if not ADDR_RE.match(addr):
                return bad('connect a wallet first')
            # never trust the card: every coin, market, amount and address is checked again here
            if src.get('op') == 'wallet':
                c = ckey(src.get('coin'))
                try:
                    a = float(src.get('amount'))
                except Exception:
                    return bad('bad amount')
                if not c or not (math.isfinite(a) and a > 0):
                    return bad('bad starting coin')
                why = spend_check(c, a, addr)
                if why:
                    return self._send(422, json.dumps({'error': why}), 'application/json')
                src = {'op': 'wallet', 'coin': c, 'amount': a}
            elif src.get('op') == 'withdraw':
                c = 'USDC' if str(src.get('coin')).upper() == 'USDC' else lend_token(str(src.get('coin') or ''))
                a = src.get('amount')
                if not c or src.get('market') not in LEND_MKTS or not (a == 'all' or (isinstance(a, (int, float)) and math.isfinite(a) and a > 0)):
                    return bad('bad withdrawal')
                more = []
                for x in (src.get('more') or [])[:4]:          # more withdrawals of the SAME coin, other markets (2026-10-24)
                    xa = x.get('amount')
                    xc = 'USDC' if str(x.get('coin')).upper() == 'USDC' else lend_token(str(x.get('coin') or ''))
                    if xc != c or x.get('market') not in LEND_MKTS or x.get('market') == src['market'] or x.get('market') in [y['market'] for y in more] \
                            or not (xa == 'all' or (isinstance(xa, (int, float)) and math.isfinite(xa) and xa > 0)):
                        return bad('bad withdrawal')
                    more.append({'market': x['market'], 'coin': c, 'amount': xa})
                dd = int(cinfo(c)['decimals']) if c in ecoins() else 9   # half of a position has more decimals than the coin: round down (review 2026-10-25)
                fl = lambda v: v if v == 'all' else math.floor(float(v) * 10 ** dd) / 10 ** dd
                a = fl(a)
                for x in more:
                    x['amount'] = fl(x['amount'])
                src = dict({'op': 'withdraw', 'coin': c, 'market': src['market'], 'amount': a}, **({'more': more} if more else {}))
            elif src.get('op') == 'perps_out':
                a = src.get('amount')
                if not (a == 'all' or (isinstance(a, (int, float)) and math.isfinite(a) and a > 0)):
                    return bad('bad withdrawal')
                src = {'op': 'perps_out', 'coin': 'USDC', 'amount': a}
            elif src.get('op') == 'dust':              # a sweep starting the chain: the dust is found again here, never taken from the card
                c = ckey(src.get('coin')) or lend_token(str(src.get('coin') or ''))
                shown = {re.sub(r'^0x0*', '0x', str(t).lower()) for t in (src.get('types') or [])}
                items = [{'type': x['type'], 'sym': x['sym'], 'dec': x['dec']} for x in dust_coins(addr, c)[0]
                         if re.sub(r'^0x0*', '0x', x['type'].lower()) in shown] if c else []
                if not items:
                    return bad('no dust left to sweep')
                src = {'op': 'dust', 'coin': c, 'amount': 'all', 'items': items}
            else:
                return bad('a chain starts with a wallet coin or a withdrawal')
            plus = (body.get('spec') or {}).get('source', {}).get('plus')
            if plus is not None and src['op'] in ('withdraw', 'perps_out'):
                if not (isinstance(plus, (int, float)) and math.isfinite(plus) and plus > 0):
                    return bad('bad wallet top-up')
                why = spend_check(src['coin'], float(plus), addr)
                if why:
                    return self._send(422, json.dumps({'error': why}), 'application/json')
                src['plus'] = float(plus)
            if len(swaps) > 2 or any(not (ckey(s.get('to')) or lend_token(str(s.get('to') or ''))) for s in swaps):
                return bad('bad swap')
            swaps = [{'to': ckey(s['to']) or lend_token(s['to'])} for s in swaps]
            if len(sinks) > 4:                          # refuse, never build only the first four (audit V15)
                return bad('A plan can end in at most 4 steps — split it into two.')
            clean = []
            fan = any(k.get('to') and not ADDR_RE.match(str(k['to'])) for k in sinks)   # one coin into several (2026-10-24): each step names its coin
            if fan and (swaps or any(k.get('op') not in ('lend', 'keep') for k in sinks)
                        or any(not (ckey(k.get('to')) or lend_token(str(k.get('to') or ''))) for k in sinks)):
                return bad('A split into several coins can only lend or keep each coin')
            for k in sinks:
                am = k.get('amount') or {}
                am = {'rest': True} if am.get('rest') else {'frac': float(am['frac'])} if 'frac' in am else {'units': float(am['units'])} if 'units' in am else None
                if not am or ('frac' in am and not 0 < am['frac'] <= 1) or ('units' in am and not (math.isfinite(am['units']) and am['units'] > 0)):
                    return bad('bad amount in a step')
                tk = ({'to': ckey(k['to']) or lend_token(k['to'])} if fan else {})
                if k.get('op') == 'lend' and k.get('market') in LEND_MKTS:
                    clean.append(dict({'op': 'lend', 'market': k['market'], 'amount': am}, **tk))
                elif fan and k.get('op') == 'keep':
                    clean.append(dict({'op': 'keep', 'amount': am}, **tk))
                elif k.get('op') == 'send' and ADDR_RE.match(str(k.get('to') or '')):
                    clean.append({'op': 'send', 'to': str(k['to']).lower(), 'amount': am})
                elif k.get('op') == 'keep':
                    clean.append({'op': 'keep', 'amount': am})
                elif k.get('op') == 'perps' and am.get('rest'):
                    clean.append({'op': 'perps', 'amount': am})
                else:
                    return bad('bad final step')
            if not clean:
                return bad('nothing to do with the coin')
            try:
                out = lend_post('/compose', {'address': addr, 'spec': {'source': src, 'swaps': swaps, 'sinks': clean}})
            except Exception as exc:
                return self._send(502, json.dumps({'error': 'lending builder: %s' % str(exc)[:120]}), 'application/json')
            if not out.get('ok'):
                return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
            cplan = out.get('plan') or []
            built = [p['op'] for p in cplan]
            head_ops = [src['op']] + ['withdraw'] * len(src.get('more') or []) + (['plus'] if src.get('plus') else [])
            if fan:                                    # every coin that differs from the start is swapped, then each step lands
                want_ops = head_ops + ['swap' for k in clean if k['to'] != src['coin']] + [k['op'] for k in clean]
                landed = [(p.get('op'), p.get('coin'), p.get('market')) for p in cplan if p.get('op') in ('lend', 'keep')]
                if not out.get('fanout') or landed != [(k['op'], k['to'], k.get('market')) for k in clean]:
                    want_ops = None
            else:
                want_ops = head_ops + ['swap'] * len(swaps) + [k['op'] for k in clean if 'rest' not in k['amount']] + [k['op'] for k in clean if 'rest' in k['amount']]
            if built != want_ops:
                return self._send(502, json.dumps({'error': 'Kivy could not build every step right now, so nothing was prepared. Try again in a minute.'}), 'application/json')
            if src['op'] == 'dust':                    # every leg the builder swept is one of the coins found here (review F-20)
                ok_t = {re.sub(r'^0x0*', '0x', x['type'].lower()) for x in src['items']}
                if not cplan[0].get('legs') or any(re.sub(r'^0x0*', '0x', str(l.get('type')).lower()) not in ok_t for l in cplan[0]['legs']):
                    return self._send(502, json.dumps({'error': 'Kivy could not build every step right now, so nothing was prepared. Try again in a minute.'}), 'application/json')
            fin_t, fin_d = out.get('final_type'), int(out.get('final_dec') or 0)
            fin_c = out.get('final_coin')
            # the price for the send limit: what each send is worth
            last_est = next((p.get('min') for p in reversed(cplan) if p['op'] == 'swap'), None)
            slip = float(K()['slippage_cap_pct']) / 100
            # the most a 'rest' send could carry: a swap can land above its minimum; a top-up adds to the start (audit V05)
            start_amt = sum(float(p.get('amount') or 0) for p in cplan if p['op'] == src['op'])   # every withdrawal of a multi-market start
            top = (float(last_est) / max(1 - slip, 0.5)) if last_est is not None else start_amt + float(src.get('plus') or 0)
            if last_est is None:
                last_est = start_amt + float(src.get('plus') or 0)
            px_fin = coin_px(fin_c) if any(k['op'] in ('send', 'lend') for k in clean) else 1
            if not (isinstance(px_fin, (int, float)) and math.isfinite(px_fin) and px_fin > 0):   # no live price: can't check the send limit, so refuse
                return self._send(422, json.dumps({'error': "I can't get a live %s price right now, so I can't check the send limit. Try again in a minute." % disp(fin_c or '')}), 'application/json')
            fixed = sum((p.get('amount') or 0) for p in cplan if p['op'] in ('lend', 'send', 'keep') and not p.get('rest'))
            check_sinks = []
            for p in cplan:
                if p['op'] not in ('lend', 'send', 'keep', 'perps'):
                    continue
                amt = p.get('amount') if not p.get('rest') else max(float(last_est) - fixed, 0)
                most = p.get('amount') if not p.get('rest') else max(top - fixed, 0)
                if fan:                                # each coin of a split: its own price
                    px_fin = coin_px(p.get('coin')) or 0
                if p['op'] == 'lend' and cap_usd() and float(most) * px_fin > cap_usd() * 1.0001:   # a new deposit obeys the cap too (user-harm #11, H36)
                    return self._send(422, json.dumps({'error': 'That deposit is about $%.2f, over the $%g limit per lend. Try a smaller amount.' % (float(most) * px_fin, float(K()['cap_usd']))}), 'application/json')
                if p['op'] == 'send' and cap_usd() and float(most) * px_fin > cap_usd() * 1.0001:
                    return self._send(422, json.dumps({'error': 'That send is over the $%g limit per send.' % float(K()['cap_usd'])}), 'application/json')
                check_sinks.append({'op': p['op'], 'market': p.get('market'), 'to': p.get('to'),
                                    'expect': float(amt) if p['op'] == 'lend' and not fan else None,   # what should reach the market (S-06)
                                    # a fixed send arrives in full (a send is not a swap: no 5% tolerance — audit V01); a 'rest' send is an estimate
                                    'min': (int(amt * 10 ** fin_d * (0.95 if p.get('rest') else 1)) - (0 if p.get('rest') else 1)) if p['op'] == 'send' else 0})
            _LEND['pos'].pop(addr, None); _LEND.get('posall', {}).pop(addr, None)
            # every send in the plan is checked in the browser BEFORE signing: that address must receive at least its minimum (audit V01)
            pays = [{'address': k['to'], 'coin_type': fin_t, 'amount': str(max(1, k['min']))} for k in check_sinks if k['op'] == 'send' and k.get('to')]
            g = guard(out=dict([guard_units(src['coin'], src['amount'])]) if src['op'] == 'wallet' else
                          {l['type']: str(int(l['units'])) for l in (cplan[0].get('legs') or [])} if src['op'] == 'dust' else
                          (dict([guard_units(src['coin'], src['plus'])]) if src.get('plus') else None),
                      # 'keep' means the coin arrives in the wallet — only true after a withdrawal or a swap; a wallet coin kept
                      # where it is never arrives (that check stopped 'send half, keep the rest' — found in the 2026-10-22 audit tests)
                      recv=({p['type']: '1' for p in cplan if p.get('op') == 'keep' and p.get('type') and (src['op'] != 'wallet' or p.get('coin') != src['coin'])} or None) if fan else
                           {fin_t: '1'} if any(k['op'] == 'keep' for k in clean) and (src['op'] != 'wallet' or swaps) else None,
                      ctokens=src['op'] == 'withdraw', pay=pays or None)
            return self._send(200, json.dumps({'tx': out['tx'], 'router': out.get('router'), 'guard': g, 'plan': cplan, 'final_type': fin_t, 'check_sinks': check_sinks}),
                              'application/json')
        if path == '/api/tx/withdrawswap':             # withdraw a lent coin and swap it: ONE transaction
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr, proto = str(body.get('address') or ''), str(body.get('protocol') or '').lower()
                coin, to = str(body.get('coin') or 'USDC'), ckey(body.get('to'))
                lend_to = str(body.get('lend_to') or '').lower() or None     # a move: lend the result on this market
                amt = body.get('amount')
                amt = 'all' if str(amt) == 'all' else float(amt or 0)
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            coin = 'USDC' if coin.upper() == 'USDC' else lend_token(coin) if re.match(r'^[A-Za-z]{2,10}$', coin) else None
            if lend_to and lend_to not in LEND_MKTS:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if not ADDR_RE.match(addr) or not coin or not to or (to == coin and not lend_to) or proto not in LEND_MKTS or \
                    (amt != 'all' and not (math.isfinite(amt) and amt > 0)) or (coin == 'USDC' and proto not in PROTOS):
                return self._send(400, '{"error":"bad request"}', 'application/json')
            try:
                out = lend_cli('withdrawswap', addr, proto, coin, amt, to, *([lend_to] if lend_to else []))
            except Exception as exc:
                return self._send(502, json.dumps({'error': 'lending builder: %s' % str(exc)[:120]}), 'application/json')
            if not out.get('ok'):
                return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
            if (out.get('lend_to') or None) != lend_to:      # 2026-10-20: an out-of-date builder dropped the lend step
                return self._send(502, json.dumps({'error': 'Kivy could not build the lending step right now, so nothing was prepared. Try again in a minute.'}), 'application/json')
            if not lend_to and re.sub(r'^0x0*', '', str(out.get('to_type'))) != re.sub(r'^0x0*', '', cinfo(to)['type']):
                return self._send(502, '{"error":"lending builder: coin mismatch"}', 'application/json')
            _LEND['pos'].pop(addr, None); _LEND.get('posall', {}).pop(addr, None)
            # nothing may leave the wallet (only gas); the swapped coin must arrive
            # nothing may leave the wallet (only gas); a plain withdraw-and-swap must deliver the coin, a move lends it instead
            if lend_to:                                  # the new deposit obeys the lend cap (user-harm #11, H36); fails closed without a price
                pto = coin_px(to)
                val = float(out.get('expected_out') or out.get('amount') or 0) * (pto or 0)
                if not pto:
                    return self._send(422, json.dumps({'error': "I can't get a live %s price right now, so I can't check the lend limit. Try again in a minute." % disp(to)}), 'application/json')
                if cap_usd() and val > cap_usd() * 1.0001:
                    return self._send(422, json.dumps({'error': 'That deposit is about $%.2f, over the $%g limit per lend. Try a smaller amount.' % (val, float(K()['cap_usd']))}), 'application/json')
            g = guard(ctokens=True) if lend_to else guard(recv={out['to_type']: '1'}, ctokens=True)
            return self._send(200, json.dumps({'tx': out['tx'], 'router': out.get('router'), 'guard': g, 'to': to, 'lend_to': lend_to,
                                               'amount': out.get('amount'), 'expected_out': out.get('expected_out')}), 'application/json')
        if path == '/api/perp/referral':             # link this wallet to Kivy's Aftermath referral code (the user signed the terms message, told on the card)
            if self.headers.get('Origin') != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            code_ = ref_code()
            try:
                b = json.loads(raw)
                addr = str(b.get('address') or '').lower()
                if not code_ or not ADDR_RE.match(addr):
                    return self._send(200, '{"ok":false,"why":"off"}', 'application/json')
                req = urllib.request.Request('https://aftermath.finance/api/referrals/link', data=json.dumps({
                    'walletAddress': addr, 'bytes': str(b.get('bytes') or '')[:200], 'signature': str(b.get('signature') or '')[:400], 'refCode': code_}).encode(),
                    headers={'Content-Type': 'application/json', 'User-Agent': 'kivy'})
                try:
                    r = json.load(urllib.request.urlopen(req, timeout=20))
                    return self._send(200, json.dumps({'ok': True, 'linked': bool(r.get('refCode') or r.get('createdAt'))}), 'application/json')
                except urllib.error.HTTPError as e:   # already linked to a code (or not eligible): fine, nothing else to do
                    print('kivy referral not linked: %d %s' % (e.code, e.read().decode(errors='replace')[:160].replace('\n', ' ')), flush=True)
                    return self._send(200, json.dumps({'ok': False, 'why': 'already linked' if e.code in (400, 409) else 'not linked'}), 'application/json')   # Aftermath's own text stays in the log (launch audit #17)
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
        if path in ('/api/m', '/api/feedback'):      # anonymous numbers + 👍/👎 (2026-10-22)
            if self.headers.get('Origin') != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            if not self._real_ip():  # local scripts and tests never count
                return self._send(200, '{"ok":true,"local":true}', 'application/json')
            if path == '/api/feedback' and not ip_quota('fb', self._real_ip()):
                return self._send(429, '{"error":"too many"}', 'application/json')   # rotating visitor ids can't flood the feedback log
            try:
                b = json.loads(raw)
                vid = str(b.get('vid') or '')
                if path == '/api/feedback':
                    clean = lambda x: '[removed: looked like a seed phrase or key]' if looks_secret(x, 8) else str(x or '')   # never stored or summarised (S-04)
                    ok = metrics.feedback(vid, int(b.get('rating') or 0), clean(b.get('note')), clean(b.get('ask')), clean(b.get('answer')))
                    return self._send(200 if ok else 400, '{"ok":%s}' % ('true' if ok else 'false'), 'application/json')
                ev = str(b.get('ev') or '')
                who = (accounts.email_from_cookie(self.headers.get('Cookie')) or '').lower()
                if who and who in admin_wallets():         # our own browser: marked team (the dashboard can leave it out)
                    metrics.mark_team(vid, admin_wallets())
                if ev == 'visit':
                    metrics.visit(vid)
                if ev in ('visit', 'connect') and ADDR_RE.match(str(b.get('address') or '')):
                    metrics.wallet_seen(str(b['address']), vid=vid)  # stored only as a salted hash; linked to the visitor (activation)
                metrics.event(vid, ev, str(b.get('d') or ''))
                return self._send(200, '{"ok":true}', 'application/json')
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
        if path == '/api/pipeline':                   # the page's checks on one plan: simulation + check 1, then the user's answer
            try:
                d = json.loads(raw or b'{}')
                iid, stage = str(d.get('iid') or ''), str(d.get('stage') or '')
                it = _INTENTS.get(iid)
                # red-team 2026-10-24: anyone could post here and fire our alerts. Now: the Kivy page only, and only the
                # visitor whose request built the plan (same address as at build time)
                if self.headers.get('Origin') != ORIGIN or (it and it.get('ip') and it['ip'] != self._real_ip()):
                    it = None
                if it and stage == 'sign' and str(d.get('result')) in ('signed', 'error') and not it.get('signed_at'):
                    it['signed_at'] = time.time()              # the unlogged-transaction scan follows it up (2026-10-23)
                    _INTENTS.save(iid)
                if it and stage == 'pkg_unlisted' and not it.get('pkg_noted'):   # a swap route used a DEX package not on the allowlist yet (audit V01)
                    pk = [x for x in (str(y) for y in (d.get('pkgs') or [])[:5]) if re.match(r'^0x[0-9a-fA-F]{1,64}(?:::\w{1,64}){0,2}$', x)]   # package ids only, no text
                    it['pkg_noted'] = True
                    if pk:
                        with open(HERE / 'data' / 'unknown_pkgs.log', 'a') as fh:
                            fh.write('%s route %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), ' '.join(pk)))
                if it and self._real_ip() and stage in ('check1', 'sign'):   # only real users' plans (tests never count)
                    clip = lambda x, n: str(x or '')[:n]
                    if stage == 'check1':
                        sim = []
                        for m in (d.get('sim') or [])[:12]:
                            sy, amt = human_units(clip(m.get('t'), 200), abs(int(str(m.get('v') or '0'))))
                            sim.append({'who': who_is(clip(m.get('a'), 70), it.get('address')), 'coin': sy, 'amt': ('−' if str(m.get('v', '')).startswith('-') else '+') + amt})
                        metrics.pipe(iid, it.get('address'), create=True, kind=it.get('kind'), plan=pipe_plan(it), sim=sim,
                                     check1={'ok': bool(d.get('ok')), 'why': clip(d.get('why'), 200)})
                    else:
                        res = clip(d.get('result'), 12)
                        metrics.pipe(iid, sign={'result': res if res in ('signed', 'rejected', 'error') else 'error', 'wallet': clip(d.get('wallet'), 40),
                                                'ms': int(d.get('ms') or 0), 'why': clip(d.get('why'), 120)})
            except Exception:
                pass
            return self._send(200, '{}', 'application/json')
        if path == '/api/wallet/probe':                # what the user's wallet reported while switching accounts (public addresses only)
            if self.headers.get('Origin') != ORIGIN or len(raw or '') > 4000:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                raw_body = json.loads(raw)
                if not isinstance(raw_body, dict):
                    raise ValueError
                # only the known diagnostic fields, short public forms (audit V14)
                txt = lambda v, n=40: re.sub(r'[^\w.:…\- ]', '', str(v))[:n]
                body = {}
                for k in ('ev', 'w', 'v', 'active', 'want', 'err'):
                    if k in raw_body:
                        body[k] = txt(raw_body[k], 120 if k == 'err' else 40)
                for k in ('accs', 'seen', 'res', 'offers', 'f'):
                    if isinstance(raw_body.get(k), list):
                        body[k] = [txt(x) for x in raw_body[k][:12]]
                for k in ('ms', 'ok'):
                    if isinstance(raw_body.get(k), (int, float, bool)):
                        body[k] = raw_body[k]
                f = Path('/opt/kivy/data/wallet_probe.log')
                if not f.exists() or f.stat().st_size < 512000:
                    with open(f, 'a') as fh:
                        fh.write(json.dumps({'t': time.strftime('%H:%M:%S'), 'ua': (self.headers.get('User-Agent') or '')[-60:], 'p': body})[:4200] + '\n')
                return self._send(200, '{}', 'application/json')
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
        if path == '/api/tx/log':
            # red-team 2026-10-24: anyone could post any digest + free text here, which landed in the victim's Activity,
            # greeting and AI notes, and the first post skipped check 2. Now: the Kivy page only, a per-visitor limit, the label
            # is written by Kivy from its own plan for THAT wallet (no plan -> no label), and a real plan always gets its check.
            if self.headers.get('Origin') != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            if not ip_quota('heavy', self._real_ip()):
                return self._send(429, '{"error":"Too many requests — wait a minute and try again."}', 'application/json')
            try:
                body = json.loads(raw)
                dg = str(body.get('digest') or '')
                if not re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', dg):
                    raise ValueError
                iid = str(body.get('intent') or '')
                it = _INTENTS.get(iid) if re.match(r'^[0-9a-f]{16}$', iid) else None
                if it and it.get('logged') and it['logged'] != dg:
                    it = None                            # one plan, one transaction
                if it:
                    for _ in range(3):                   # the indexer can be a few seconds behind the wallet
                        dd = tx_detail(dg)
                        if dd and not dd.get('error'):
                            break
                        time.sleep(2)
                    if not dd or dd.get('error') or str(dd.get('sender') or '').lower() != it.get('address'):
                        it = None                        # the plan was for another wallet
                    elif it.get('ip') and it['ip'] != self._real_ip():
                        it = None                        # only the visitor who built the plan may attach it (audit nv #7)
                    else:
                        try:                             # and only to a transaction signed AFTER the plan was built: a stranger can't
                            tt = calendar.timegm(time.strptime(str(dd.get('time_utc'))[:19], '%Y-%m-%dT%H:%M:%S'))   # pick a past one
                            if tt < it['at'] - 60:
                                it = None
                        except Exception:
                            it = None
                if not it:
                    return self._send(200, json.dumps({'diary': 'no plan'}), 'application/json')
                d = kivy_diary_add(dg, it.get('label'))
                if it:
                    first = not it.get('logged')
                    it['logged'] = dg
                    _INTENTS.save(iid)
                    mark_dirty(it.get('address'))
                    if first:                            # its plan's check, even if someone logged the digest first
                        verify_later(dg, iid)
                # no plan: nothing is checked here — a real Kivy transaction whose report lost its plan is found by the
                # unreported-transaction scan (its plan is on disk); a stranger's digest can't fill the checker (review 2026-10-24)
                return self._send(200, json.dumps({'diary': d}), 'application/json')
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
        if path == '/api/tx/perp':
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr, act = str(body.get('address') or ''), str(body.get('action') or '')
                amt = body.get('amount')
                amt = 'all' if str(amt) == 'all' else float(amt or 0)
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if act in ('deposit', 'withdraw') and amt != 'all' and not (math.isfinite(amt) and amt >= 0):
                return self._send(400, '{"error":"bad amount"}', 'application/json')
            if act == 'deposit' and amt == 'all':      # F-15
                return self._send(400, '{"error":"say how much to deposit"}', 'application/json')
            if not ADDR_RE.match(addr) or act not in ('open', 'deposit', 'withdraw', 'order', 'cancel', 'close', 'margin', 'tpsl'):
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if act == 'margin':                       # add margin to an open position (no cap on perps)
                o = body.get('order') or {}
                sym = chart_symbol(o.get('symbol'))
                try:
                    usd = float(o.get('amount') or 0)
                except Exception:
                    usd = 0
                if not sym or not (math.isfinite(usd) and usd > 0):
                    return self._send(422, json.dumps({'error': 'Add more than 0 USDC of margin.'}), 'application/json')
                try:
                    out = lend_cli('perp-margin', addr, sym, usd)
                except Exception as exc:
                    return self._send(502, json.dumps({'error': 'perps builder: %s' % str(exc)[:120]}), 'application/json')
                if not out.get('ok'):
                    return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
                return self._send(200, json.dumps({'tx': out['tx'], 'guard': guard()}), 'application/json')
            if act == 'tpsl':                         # take-profit / stop-loss on an open position
                o = body.get('order') or {}
                sym = chart_symbol(o.get('symbol'))
                try:
                    sl, tp = float(o.get('sl') or 0), float(o.get('tp') or 0)
                    frac, tp2 = float(o.get('frac') or 1), float(o.get('tp2') or 0)   # TP1 closes frac, TP2 the rest (2026-10-21)
                except Exception:
                    sl = tp = -1; frac, tp2 = 1, 0
                if not sym or not (math.isfinite(sl) and math.isfinite(tp) and sl >= 0 and tp >= 0 and (sl or tp or o.get('tps'))) \
                        or not (math.isfinite(frac) and 0.01 <= frac <= 1) or not (math.isfinite(tp2) and tp2 >= 0):
                    return self._send(400, '{"error":"bad request"}', 'application/json')
                tps = o.get('tps')                       # up to 3 take-profits [[price, share], …] (2026-10-21)
                if tps is not None:
                    try:
                        tps = [[float(x[0]), float(x[1])] for x in tps if float(x[0]) > 0]
                        if len(tps) > 3:                  # refuse, never drop a take-profit (user-harm #7)
                            return self._send(422, json.dumps({'error': 'At most 3 take-profits per position.'}), 'application/json')
                    except Exception:
                        return self._send(400, '{"error":"bad request"}', 'application/json')
                    if any(not (math.isfinite(a) and math.isfinite(b) and 0 < b <= 1) for a, b in tps) or sum(b for _, b in tps) > 1.0001 or not (tps or sl):
                        return self._send(400, '{"error":"bad request"}', 'application/json')
                try:
                    out = lend_cli('perp-tpsl-multi', addr, sym, sl, '-'.join('%sx%s' % (plain_num(a), plain_num(b)) for a, b in tps)) if tps is not None else lend_cli('perp-tpsl', addr, sym, sl, tp, frac, tp2)
                except Exception as exc:
                    return self._send(502, json.dumps({'error': 'perps builder: %s' % str(exc)[:120]}), 'application/json')
                if not out.get('ok'):
                    return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
                return self._send(200, json.dumps({'tx': out['tx'], 'guard': guard()}), 'application/json')
            if act in ('cancel', 'close'):            # managing what's already there: never capped
                o = body.get('order') or {}
                sym = chart_symbol(o.get('symbol'))
                try:
                    if act == 'close':
                        if not sym:
                            raise ValueError('market')
                        slip = float(o.get('slippage') or 1)
                        if not (0.05 <= slip <= 5):
                            raise ValueError('slippage')
                        frac = float(o.get('frac') or 1)
                        if not (0.01 <= frac <= 1):
                            raise ValueError('frac')
                        out = lend_cli('perp-close', addr, sym, slip, frac)
                    else:
                        kind, oid = str(o.get('kind') or ''), str(o.get('id') or '')
                        if kind not in ('limit', 'stop', 'twap') or not re.match(r'^(0x[0-9a-fA-F]{64}|\d{1,60}n?)$', oid) or (kind == 'limit' and not sym):
                            raise ValueError('order')
                        out = lend_cli('perp-cancel', addr, kind, sym or 'BTC', oid, '1' if o.get('withdraw') else '0')
                except ValueError:
                    return self._send(400, '{"error":"bad request"}', 'application/json')
                except Exception as exc:
                    return self._send(502, json.dumps({'error': 'perps builder: %s' % str(exc)[:120]}), 'application/json')
                if not out.get('ok'):
                    return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
                return self._send(200, json.dumps({'tx': out['tx'], 'guard': guard()}), 'application/json')
            if act == 'order':                        # all order types; no size cap on perps
                try:
                    o = body.get('order') or {}
                    sym, side, usd, lev = chart_symbol(o.get('symbol')), int(o.get('side')), float(o.get('usd')), float(o.get('lev'))
                    slip, sl, tp = float(o.get('slippage') or 1), float(o.get('sl') or 0), float(o.get('tp') or 0)
                    typ = str(o.get('type') or 'market')
                    f = lambda k: float(o.get(k) or 0)
                except Exception:
                    return self._send(400, '{"error":"bad order"}', 'application/json')
                if not sym or side not in (0, 1) or not (math.isfinite(usd) and usd > 0) or not (1 <= lev <= 100) or not (0.05 <= slip <= 5) \
                        or not (math.isfinite(sl) and sl >= 0 and math.isfinite(tp) and tp >= 0) \
                        or typ not in ('market', 'limit', 'stop-market', 'stop-limit', 'scale', 'twap'):
                    return self._send(400, '{"error":"bad order"}', 'application/json')
                need = {'limit': ['price'], 'stop-market': ['trigger'], 'stop-limit': ['trigger', 'limit'], 'scale': ['start', 'end', 'count'], 'twap': ['minutes', 'slices']}
                if any(not (math.isfinite(f(k)) and f(k) > 0) for k in need.get(typ, [])):
                    return self._send(400, json.dumps({'error': 'Fill in: ' + ', '.join(need[typ])}), 'application/json')
                if typ not in ('market', 'limit') and (sl or tp):   # the builder can't attach TP/SL to these: refuse, never drop it (audit V03)
                    return self._send(422, json.dumps({'error': 'TP/SL can\'t be attached to a %s order yet. Turn TP/SL off, then set it on the position once it fills (Positions → TP/SL).' % typ.replace('-', ' ')}), 'application/json')
                try:
                    if typ == 'market':
                        out = lend_cli('perp-order', addr, sym, side, usd, lev, slip, sl, tp)
                    elif typ == 'limit':
                        out = lend_cli('perp-limit', addr, sym, side, usd, lev, f('price'), 1 if o.get('post_only') else 0, sl, tp)
                    elif typ in ('stop-market', 'stop-limit'):
                        out = lend_cli('perp-stop', addr, sym, side, usd, lev, f('trigger'), f('limit') if typ == 'stop-limit' else 0)
                    elif typ == 'scale':
                        out = lend_cli('perp-scale', addr, sym, side, usd, lev, f('start'), f('end'), int(f('count')))
                    else:
                        out = lend_cli('perp-twap', addr, sym, side, usd, lev, f('minutes'), int(f('slices')), slip)
                except Exception as exc:
                    return self._send(502, json.dumps({'error': 'perps builder: %s' % str(exc)[:120]}), 'application/json')
                if not out.get('ok'):
                    return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
                _PERP.pop(addr, None)
                return self._send(200, json.dumps({'tx': out['tx'], 'preview': out.get('preview'), 'guard': guard()}), 'application/json')
            if act == 'deposit':                      # balance only (no cap on perps)
                if float(amt) > float((balances(addr) or {}).get('USDC') or 0) + 1e-9:
                    return self._send(422, json.dumps({'error': 'Not enough USDC in your wallet.'}), 'application/json')
            try:
                out = lend_cli('perp', addr, act, amt)
            except Exception as exc:
                return self._send(502, json.dumps({'error': 'perps builder: %s' % str(exc)[:120]}), 'application/json')
            if not out.get('ok'):
                return self._send(422, json.dumps({'error': out.get('error', 'could not build')}), 'application/json')
            _PERP.pop(addr, None)
            g = guard(out=dict([guard_units('USDC', amt)])) if act == 'deposit' else \
                guard(recv={COINS()['USDC']['type']: '1'}) if act == 'withdraw' else guard()
            return self._send(200, json.dumps({'tx': out['tx'], 'amount': out.get('amount'), 'guard': g}), 'application/json')
        if path == '/api/perp/pro-orders':           # stop / TWAP orders: needs the wallet's terms signature
            if self.headers.get('Origin') and self.headers.get('Origin') != ORIGIN:   # cross-site POSTs refused (review S-05)
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                if not ADDR_RE.match(str(body.get('address') or '')):
                    raise ValueError
                req = urllib.request.Request('http://127.0.0.1:8799/perp-pro-orders', data=json.dumps({
                    'address': body['address'], 'bytes': str(body.get('bytes') or '')[:200], 'signature': str(body.get('signature') or '')[:400]}).encode(),
                    headers={'Content-Type': 'application/json'})
                return self._send(200, urllib.request.urlopen(req, timeout=60).read(), 'application/json')
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
        if path == '/api/tx/sendmany':                # the Send card: several coins to several addresses, from the wallet or from lending
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr, lines = str(body.get('address') or '').lower(), body.get('lines')
                if not isinstance(lines, list):
                    raise ValueError
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"connect a wallet first"}', 'application/json')
            if not 1 <= len(lines) <= 12:
                return self._send(422, json.dumps({'error': 'Send 1 to 12 transfers at a time.'}), 'application/json')
            clean = []
            for i, l in enumerate(lines, 1):
                src = str(l.get('source') or 'wallet')
                coin = str(l.get('coin') or '').strip()
                to_in = str(l.get('to') or '').strip()
                amt = l.get('amount')
                if src != 'wallet' and src not in LEND_MKTS:
                    return self._send(422, json.dumps({'error': 'Line %d: unknown market.' % i}), 'application/json')
                if src == 'wallet':
                    coin = ckey(coin) or coin
                    if coin not in ecoins():
                        return self._send(422, json.dumps({'error': 'Line %d: %s is not a coin Kivy can send.' % (i, coin)}), 'application/json')
                if not re.match(r'^[A-Za-z][A-Za-z0-9.]{0,15}$', coin):
                    return self._send(422, json.dumps({'error': 'Line %d: pick a coin.' % i}), 'application/json')
                if amt == 'all' and src == 'wallet':
                    return self._send(422, json.dumps({'error': 'Line %d: enter an amount.' % i}), 'application/json')
                if amt != 'all':
                    try:
                        amt = float(amt)
                        assert math.isfinite(amt) and amt > 0
                    except Exception:
                        return self._send(422, json.dumps({'error': 'Line %d: enter an amount above zero.' % i}), 'application/json')
                if not to_in:
                    return self._send(422, json.dumps({'error': 'Line %d: who should receive it? Paste an address or a .sui name.' % i}), 'application/json')
                to = resolve_recipient(to_in)
                if not to:
                    return self._send(422, json.dumps({'error': 'Line %d: "%s" is not a Sui address or a .sui name I can find.' % (i, to_in[:60])}), 'application/json')
                if to == addr:
                    return self._send(422, json.dumps({'error': 'Line %d: that is the sending account itself — nothing would move.' % i}), 'application/json')
                seen = str(l.get('seen') or '').lower()
                if seen and ADDR_RE.match(seen) and normaddr(seen) != normaddr(to):   # the name moved after the card showed it (audit nv #6)
                    return self._send(422, json.dumps({'error': 'Line %d: %s now points to %s…%s, not the %s…%s shown on the card. Check it again before sending.'
                                                                % (i, to_in[:60], to[:6], to[-4:], seen[:6], seen[-4:])}), 'application/json')
                clean.append({'coin': coin, 'source': src, 'amount': amt if amt == 'all' else plain_num(amt), 'to': to, 'to_in': to_in})
            if sum(1 for c in clean if c['amount'] == 'all') and any(sum(1 for d in clean if d['source'] == c['source'] and d['coin'] == c['coin']) > 1
                                                                        for c in clean if c['amount'] == 'all'):
                return self._send(422, json.dumps({'error': '"All" works when that lent coin goes to one address — enter amounts to split it.'}), 'application/json')
            try:
                out = lend_post('/sendmany', {'address': addr, 'lines': [{k: c[k] for k in ('coin', 'source', 'amount', 'to')} for c in clean]}, timeout=180)
            except Exception:
                return self._send(502, '{"error":"Kivy could not build this send right now. Try again in a minute."}', 'application/json')
            if not out.get('ok'):
                return self._send(422, json.dumps({'error': str(out.get('error') or 'could not build')[:300]}), 'application/json')
            wal, pays = {}, []
            for p in out.get('plan') or []:
                if p['op'] != 'send':
                    continue
                units = int(round(float(p['amount']) * 10 ** int(p['dec'])))
                if p['source'] == 'wallet':
                    wal[p['coin_type']] = str(int(wal.get(p['coin_type'], 0)) + units)
                pays.append({'address': p['to'], 'coin_type': p['coin_type'], 'amount': str(int(units * 0.999) if p.get('rest') else units)})   # a withdrawal can round down a hair
            names = {c['to']: c['to_in'] for c in clean if c['to_in'].lower() != c['to']}
            return self._send(200, json.dumps({'ok': True, 'tx': out['tx'], 'plan': out['plan'], 'names': names,
                                               'guard': guard(out=wal, ctokens=any(c['source'] != 'wallet' for c in clean), pay=pays)}), 'application/json')
        if path == '/api/tx/check-send':
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                addr = str(body.get('address') or '')
                to = str(body.get('to') or '')
                many = body.get('items')
                if many is not None:                   # several coins, one transaction
                    if len(many) > 8:                      # refuse, never send only the first eight (audit V15)
                        return self._send(422, json.dumps({'error': 'At most 8 coins in one send — split it into two.'}), 'application/json')
                    many = [(ckey(str(x['coin']).upper()) or str(x['coin']).upper(), float(x['amount'])) for x in many]
                    if len(many) < 2 or len({c for c, _ in many}) != len(many):
                        raise ValueError
                else:
                    coin, amt = str(body.get('coin', '')).upper(), float(body.get('amount'))
            except Exception:
                return self._send(400, '{"error":"bad request"}', 'application/json')
            if not ADDR_RE.match(addr):
                return self._send(400, '{"error":"connect a wallet first"}', 'application/json')
            if many is not None:
                chk = plan_send_many(many, to, all_balances(addr), addr)
                if not chk.get('plan'):
                    return self._send(422, json.dumps({'error': chk.get('text')}), 'application/json')
                its, out = [], {}
                for c, a in many:
                    ci = cinfo(c)
                    u = str(base_units(a, ci['decimals']))
                    its.append({'coin': c, 'type': ci['type'], 'base_units': u})
                    out[ci['type']] = u
                to_full = chk['plan']['to_full']
                return self._send(200, json.dumps({'ok': True, 'to': to_full, 'items': its,
                                                   'guard': guard(out=out, pay=[{'address': to_full, 'coin_type': x['type'], 'amount': x['base_units']} for x in its])}),
                                  'application/json')
            coin = ckey(coin) or coin
            chk = plan('send', amt, coin, recipient=to, holdings=all_balances(addr), sender=addr)
            if not chk.get('plan'):
                return self._send(422, json.dumps({'error': chk.get('text')}), 'application/json')
            c = cinfo(coin)
            units = str(base_units(amt, c['decimals']))
            return self._send(200, json.dumps({'ok': True, 'to': chk['plan']['to_full'], 'type': c['type'],
                                               'base_units': units, 'gasless': gasless_send(coin, amt),
                                               'guard': guard(out={c['type']: units},
                                                              pay={'address': chk['plan']['to_full'], 'coin_type': c['type'], 'amount': units})}),
                              'application/json')
        if path == '/api/chat':
            if not self._authed():
                return self._send(401, '{"error":"sign in"}', 'application/json')
            origin = self.headers.get('Origin')
            if origin and origin != ORIGIN:          # cross-site POSTs refused
                return self._send(403, '{"error":"origin"}', 'application/json')
            try:
                body = json.loads(raw)
                msg = str(body.get('message') or '')[:500]
                history = body.get('history') if isinstance(body.get('history'), list) else []
                address = str(body.get('address') or '')
                address = address if ADDR_RE.match(address) else None
                XW.accounts = [x for x in (body.get('accounts') or [])[:10] if isinstance(x, dict)]
                XW.owner = (accounts.email_from_cookie(self.headers.get('Cookie')) or '').lower()   # who is signed in (user-harm #9)
                XW.ai_wallet = body.get('ai_wallet') is not False                                     # Settings › Privacy (user-harm #12)
                XW.vid = str(body.get('vid') or '') if not body.get('nt') else None                    # anonymous id for the numbers; None = test browser
                if ADDR_RE.match(str(body.get('address') or '')) and not ip_quota('wallet', getattr(XW, 'ip', None), str(body.get('address'))):
                    return self._send(200, json.dumps({'text': 'Too many different wallets from this connection in the last hour — try again later.', 'ai': False}), 'application/json')
            except Exception:
                return self._send(400, '{"error":"bad json"}', 'application/json')
            if looks_secret(msg):                       # a pasted seed phrase / private key: not parsed, logged, counted or sent to the AI
                return self._send(200, json.dumps({'text': SEED_WARN, 'ai': False}), 'application/json')
            history = [h for h in history if not (isinstance(h, dict) and looks_secret(h.get('text')))]
            msg = tidy_amounts(strip_filler(tidy_wording(msg)))
            out = {'text': ENGLISH_ONLY} if not_english(msg) else None   # English only: say so, never guess
            try:                                         # a typo first ("lend vsiu"), so no exact pattern misreads it
                if out is None and not QUESTION_RE.search(msg):
                    out = typo_chain(msg, address)          # a multi-step request with typos: the card, read with them fixed (2026-10-24)
                if out is None and intent_parse(msg)['fixes'] and not QUESTION_RE.search(msg) and not other_unknown(msg):
                    out = intent_ask(msg, address, history)
            except Exception:
                out = None
            if out is None:
                out = widget_reply(msg, address, history)
            if out is None and not QUESTION_RE.search(msg):
                try:
                    out = intent_ask(msg, address, history)     # unclear job or coin: ask, with choices from their wallet
                except Exception:
                    out = None
            if out is None and address and getattr(XW, 'ai_wallet', True) is False:
                alt = plain_command(msg)                 # sharing off: a plain sentence about moving money gets its card from code
                if alt:
                    try:
                        out = typo_chain(alt, address) or widget_reply(alt, address, history) or intent_ask(alt, address, history)
                    except Exception:
                        out = None
                    if out is not None and not (out.get('plan') or out.get('widget')):
                        out = None
            real = bool(self._real_ip()) and getattr(XW, 'vid', '') is not None
            if out is not None:
                out['ai'] = False
                if real:
                    metrics.event(XW.vid, 'msg', 'kivy')  # a message Kivy understood itself
                try:                                     # where Kivy fails: it had to ask "did you mean…" or said no
                    if not self._real_ip() or getattr(XW, 'vid', '') is None:
                        raise StopIteration               # local scripts and tests never count
                    w = out.get('widget') or {}
                    if w.get('type') == 'choose' and re.match(r'^\s*did you mean', out.get('text') or '', re.I):
                        metrics.failure('typo_question', msg, out.get('text') or '', XW.vid)
                    elif not w and not out.get('plan') and re.search(r"\b(can't|cannot|couldn't|isn't one of|not supported|unsupported|I don't)\b", out.get('text') or '', re.I):
                        metrics.failure('refused', msg, out.get('text') or '', XW.vid)
                except Exception:
                    pass
                try:
                    out['widget'] = choose_icons(out.get('widget'))
                except Exception:
                    pass
                return self._send(200, json.dumps(out), 'application/json')
            try:
                if real:
                    metrics.failure('ai_answer', msg, '', XW.vid)  # no exact route understood it: the AI answered
                    metrics.event(XW.vid, 'msg', 'ai')
            except Exception:
                pass
            out = ai_reply(msg, history, address)
            if out is None:
                out = reply(msg, address)  # AI unavailable: the rule-based answers
                if out.get('text') == K()['help']:     # no rule fits either: say so instead of a bare menu (2026-10-23)
                    out['text'] = "I can't answer questions like that right now. " + out['text']
                out['ai'] = False
            else:
                out['ai'] = True
            try:
                out['widget'] = choose_icons(out.get('widget'))
            except Exception:
                pass
            return self._send(200, json.dumps(out), 'application/json')
        return self._send(404, 'not found', 'text/plain')


def sweep_caches():
    """Every 5 minutes: drop cache entries older than an hour, keep each cache under 5,000 entries (audit W01).
    Only (timestamp, value) entries are touched, so live caching works exactly as before."""
    while True:
        time.sleep(300)
        now = time.time()
        try:
            for d in (_PERP, _MEM, _BAL, _TPX, _QUOTE, _BOOK, _CANDLES, _LEND.get('pos', {}), _LEND.get('posall', {})):
                stamped = [(v[0], k) for k, v in list(d.items()) if isinstance(v, tuple) and v and isinstance(v[0], (int, float))]
                for t, k in stamped:
                    if now - t > 3600:
                        d.pop(k, None)
                if len(d) > 5000:
                    for t, k in sorted(stamped)[:len(d) - 5000]:
                        d.pop(k, None)
            try:
                metrics.snapshot_today()                 # the day's numbers, frozen once (for the deck)
            except Exception:
                pass
            for k in [k for k, v in list(_INTENTS.items()) if now - v.get('at', now) > 7200]:
                _INTENTS.pop(k, None)                    # memory only: the disk copy stays 24 h
            _INTENTS.prune_disk()
            if len(_INTENTS) > 5000:
                for k, _ in sorted(_INTENTS.items(), key=lambda x: x[1].get('at', 0))[:len(_INTENTS) - 5000]:
                    _INTENTS.pop(k, None)
        except Exception:
            pass


if __name__ == '__main__':
    threading.Thread(target=sweep_caches, daemon=True).start()
    threading.Thread(target=unlogged_scan, daemon=True, name='kivy-unlogged').start()
    import threading
    threading.Thread(target=lambda: (time.sleep(20), ecoins()), daemon=True).start()   # load the coin list before the first visitor needs it
    ThreadingHTTPServer.request_queue_size = 256      # waiting connections (Python's default 5 reset ~5% of a burst — load test 2026-10-24)
    ThreadingHTTPServer.daemon_threads = True
    with ThreadingHTTPServer(('127.0.0.1', PORT), Handler) as httpd:
        print('kivy on 127.0.0.1:%d' % PORT, flush=True)
        httpd.serve_forever()
