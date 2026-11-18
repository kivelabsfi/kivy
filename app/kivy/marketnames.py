"""Markets by name: "open korean market" -> the trading page on that market (Section 20).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# market_switch is called by widget_reply and intent_ask (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import difflib
import re
from .config import K, disp
from .intentwords import COIN_NAMES, INTENT_FORMS, QUESTION_RE

market_overview = None     # server.py's (Section 21): set by server.py once it is defined (a module never imports server.py)


# ── Section 20: markets by name, sends, cross-account requests — market_switch, send_reply and xw_parse are all called
#    by widget_reply before the perps and chain readers ──
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
    names = list(MARKET_NAMES) + [m.lower() for m in af_markets()]
    hit = difflib.get_close_matches(key, names, n=1, cutoff=0.8)
    if hit:
        return ('one', MARKET_NAMES.get(hit[0]) or hit[0].upper())
    return None


# A 'which one?' choice card for a group of markets (market_find's 'group' result) with live price and 24h move.
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


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import af_markets, chart_symbol, perp_market_list  # noqa: E402
