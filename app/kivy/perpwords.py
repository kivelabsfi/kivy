"""Perps words in multi-step requests and what Kivy says about them (perps scan 2026-11-14, classes C1-C10). The words people
use for the perps account and for putting money into it, what a trade sentence carries besides the order (a stop-loss, a
second order, another wallet), a few multi-step shapes said in other words, and the answers for parts Kivy can't build.
Pure text work: no chain reads, no server state.

- PERP_WORD / CHAIN_PERPS_PAT: ONE list of the words that mean the perps account, for chat and chains alike (C1: chains
  missed "futures", so "put the rest in futures" was lent on Suilend Ember).
- perps_wording(t): casual ways of saying "deposit it into perps" ("chuck it in perps", "fund my perps account", "top up
  margin", "send it to perps", "-> perps", "buy sui with 3x leverage"), rewritten into the words the readers know.
- trade_extras / extras_say: a stop-loss, take-profit or "close it at $2" typed with the trade (C2: dropped silently).
- order_tpsl / tpsl_words / attach_tpsl / with_tpsl: a stop-loss / take-profit price typed with ONE order goes on its order
  card and is placed in the same transaction (2026-11-15, perps FIXES.md Q1); a price on the wrong side is said, not placed.
- split_orders: a second order in the trade part ("long and short btc", "long sui 3x and short btc 2x") (C8).
- other_wallet: "from my other wallet", "on my ledger wallet": Kivy works with the connected wallet only (C8).
- order_ask: the order as the trade reader takes it ("long SUI $50 3x") — one wording for every next step.
- wallet_plus_market / split_places: "20 from my wallet plus 10 from navi into perps", "split 40 usdc between perps and navi" (C10).
"""
# How it runs: imported by site/server.py — tidy_wording calls perps_wording; deposit_then_trade, close_next and the chain
# reader call the rest. Nothing here runs on its own.
import re
from . import amounts as AM

# The perps account, in every word the chat and the chain reader accept (common typos too). "margin" and "collateral" also
# mean the perps account in a chain ("deposit into margin").
PERP_WORD = r'(?:aftermath(?:\s+(?:perps?|perpetuals?|futures|account))?|perps?|pereps?|preps?|peprs?|perpetuals?|futures|perp\s+account)'
CHAIN_PERPS_PAT = r'\b(aftermath|perps?|pereps?|peprs?|perpetuals?|margin|collateral|futures(?:\s+account)?|perp\s+account)\b'

# A destination that means the perps account ("into my perps", "on aftermath", "to my futures account", "into margin").
_PLACE = (r'(?:(?:my|the|your)\s+)?(?:aftermath\s+)?(?:perps?(?:\s+account)?|perpetuals?|futures(?:\s+account)?|perp\s+account|'
          r'margin(?:\s+account)?|aftermath(?:\s+account)?)\b(?![.-]\w)')   # "aftermath.sui" is a name, not the perps account
_CTX = re.compile(r'\b(?:perps?|perpetuals?|futures|aftermath|margin)\b', re.I)
_OBJ = r'(it|that|them|this|those|it\s+all|all\s+of\s+it|everything|the\s+rest|the\s+usdc|the\s+money|the\s+cash|the\s+lot)'
_CASUAL = r'(?:chuck|throw|toss|shove|dump|send|load|bung|drop|whack|slap|plonk|yeet)'
_SYM = r'([A-Za-z][A-Za-z0-9&]{1,12})'


def perps_wording(t):
    """The request with its casual perps words said the way the readers know them. Text with no perps word comes back
    unchanged, so no other request is touched."""
    if not _CTX.search(t or ''):
        return t
    s = re.sub(r'(?i)\boutta\b', 'out of', t)
    s = re.sub(r'\s*;\s*', ', ', s)                  # "withdraw 20 usdc from navi; deposit to perps; long sui 3x": steps
    # arrows as steps: "10 usdc from navi -> perps -> long sui 3x"
    s = re.sub(r'(?i)\s*(?:->|→|=>)\s*(?=%s)' % _PLACE, ', deposit into ', s)
    s = re.sub(r'(?i)\s*(?:->|→|=>)\s*(?=(?:go\s+|open\s+(?:a\s+)?)?(?:long|short)\b)', ', ', s)
    s = re.sub(r'(?i)^\s*(\$?\d[\d.]*\s*[A-Za-z][A-Za-z0-9]{1,9})\s+(?:from|out\s+of)\s+(?=[^,]+,)', r'withdraw \1 from ', s)   # the start, said without its verb
    # "futures" as a place = the perps account ("deposit into futures", "my futures account")
    s = re.sub(r'(?i)\b((?:in|into|to|onto|on|from|out\s+of)\s+(?:(?:my|the|your)\s+)?(?:aftermath\s+)?)futures\b', r'\1perps', s)
    s = re.sub(r'(?i)\bfutures\s+account\b', 'perps account', s)
    s = re.sub(r'(?i)\s+n\s+(?=(?:go\s+)?(?:long|short)\b)', ' and ', s)       # "throw it on aftermath n long btc 2x"
    # "withdraw 20 usdc from navi to my perp account": a withdrawal, then a deposit
    s = re.sub(r'(?i)(\b(?:withdraw|take|pull|grab|get)\b[^,;]*?\b(?:from|out\s+of)\s+(?:(?!\b(?:and|then|deposit|put|lend|swap|send|into|to)\b)[^,;.])+?)'
               r'\s+(?:in)?to\s+(?=%s)' % _PLACE, r'\1, deposit it into ', s)
    # "chuck it in perps", "send it to perps", "throw 20 usdc on aftermath": a deposit
    s = re.sub(r'(?i)\b%s\s+%s\s+(?:in|into|on|onto|to|over\s+to|in\s+to)\s+(?=%s)' % (_CASUAL, _OBJ, _PLACE), r'deposit \1 into ', s)
    s = re.sub(r'(?i)\b%s\s+(\$?\d[\d.]*(?:\s*usdc)?)\s+(?:in|into|on|onto|to)\s+(?=%s)' % (_CASUAL, _PLACE), r'deposit \1 into ', s)
    # "fund my perps account", "top up margin (with it)": a deposit — not "top up the margin on my sui long" (a position's own margin)
    s = re.sub(r'(?i)\b(?:fund|top\s*up|top\s+off|refill|load\s+up)\s+%s(?:\s+with\s+(it|that|them|the\s+rest|the\s+usdc|\$?\d[\d.]*(?:\s*usdc)?))?'
               r'(?!\s+(?:on|for|of)\s+(?:my|the)\s+\w+(?:\s+(?:long|short|position))?)' % _PLACE,
               lambda m: 'deposit %s into my perps account' % (m.group(1) or 'it'), s)
    if re.search(r'(?i)\bdeposit\w*\b[^,;]*\b(?:into|to|in|on)\s+%s' % _PLACE, s):   # a perps deposit is in the sentence: the order words
        s = re.sub(r'(?i)\bape\s+(?:in(?:to)?\s+|on\s+)?(?!\d)%s(\s+\d+(?:\.\d+)?\s*x)?(?:\s+(long|short))?\b' % _SYM,
                   lambda m: '%s %s%s' % ((m.group(3) or 'long').lower(), m.group(1), m.group(2) or ''), s)
        s = re.sub(r'(?i)\b(buy|sell)\s+%s\s+(?:(?:with|at|using)\s+)?(\d+(?:\.\d+)?)\s*x\b(?:\s+lev\w*)?' % _SYM,
                   lambda m: '%s %s %sx' % ('long' if m.group(1).lower() == 'buy' else 'short', m.group(2), m.group(3)), s)
    return ' '.join(s.split())


# ── what a trade part carries besides the order ──
_EXTRA_RE = re.compile(r'(?:\s*(?:,|\band\b|\bwith\b|\bthen\b|\+|&))*\s*(?:(?:and\s+)?(?:then\s+)?(?:set\s+|add\s+|put\s+)?(?:a\s+|an\s+|my\s+|the\s+)?'
                       r'(?:stop[\s-]*loss|take[\s-]*profit|\bsl\b|\btp\b|trailing\s+stop|stop\b)|close\s+(?:it|that|them|the\s+\w+|out)?\s*(?:at|@|when|if|once)\b).*$', re.I)
_NUM = r'\$?\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(?:dollars?|usd))?'


def trade_extras(trade):
    """(the order part, the extras part or '') — "long sui 3x and set a stop loss at 0.8" -> ("long sui 3x", "set a stop loss at 0.8")."""
    m = _EXTRA_RE.search(trade or '')
    if not m or not m.start():
        return (trade or '').strip(' ,'), ''
    return trade[:m.start()].strip(' ,'), trade[m.start():].strip(' ,')


def _tpsl_match(extras):
    """(stop-loss match, take-profit match, "close it at X" match) in the extras; group(1) of each is the price as typed."""
    num = lambda pat: re.search(r'(?i)(?:%s)\s*(?:at|@|to|of|=|price|level)?\s*(?:at\s+)?%s' % (pat, _NUM), extras)
    cl = re.search(r'(?i)close\s+(?:it|that|them|the\s+\w+|out)?\s*(?:at|@|when\s+(?:it\s+)?(?:hits|reaches|gets\s+to)|if\s+(?:it\s+)?(?:hits|reaches)|once\s+(?:it\s+)?(?:hits|reaches))\s*%s' % _NUM, extras)
    return num(r'stop[\s-]*loss|\bsl\b|\bstop\b'), num(r'take[\s-]*profit|\btp\b'), cl


def extras_say(extras, sym, side, mark=None):
    """The sentence that says what happens to the extras, and the request that sets them once the order has filled — never
    dropped silently (C2). ('', '') when there is nothing to say."""
    if not extras:
        return '', ''
    pos = '%s %s' % (sym, side)
    sl, tp, cl = _tpsl_match(extras)
    fx = lambda m: m.group(1).replace(',', '')
    if cl and not (sl or tp):                        # "close it at 2 dollars": above the price a long is a take-profit, below a stop-loss
        px = float(fx(cl))
        if mark:
            above = px > float(mark)
            is_tp = above if side == 'long' else not above
            tp, sl = (cl, None) if is_tp else (None, cl)
        else:
            ask = 'take profit at %s on my %s' % (fx(cl), pos)
            return (' Closing it at $%s is its own step on the open position (a take-profit or stop-loss at that price): once the order has filled, '
                    'say "%s" (or "stop loss at %s on my %s" if that is below the price).' % (fx(cl), ask, fx(cl), pos)), ask
    if re.search(r'(?i)trailing', extras) and not (sl or tp):
        return ' Trailing stops aren\'t offered — set a stop-loss on the open position instead, e.g. "stop loss at <price> on my %s".' % pos, ''
    parts = (['stop loss at %s' % fx(sl)] if sl else []) + (['take profit at %s' % fx(tp)] if tp else [])
    if not parts:
        return (' The stop-loss / take-profit is its own step on the open position: once the order has filled, say e.g. '
                '"stop loss at <price> on my %s".' % pos), ''
    ask = '%s on my %s' % (' and '.join(parts), pos)
    what = (['your stop-loss at %s' % fx(sl)] if sl else []) + (['your take-profit at %s' % fx(tp)] if tp else [])
    verb = 'are steps of their own' if len(what) > 1 else 'is its own step'
    what = ' and '.join(what)
    return (' %s %s on the open position: once the order has filled, say "%s".' % (what[0].upper() + what[1:], verb, ask)), ask


_ORDER_HEAD = re.compile(r'^\s*(?:go\s+|open\s+(?:a\s+)?)?(?:long|short)\b', re.I)


def tpsl_numbers(extras, side, mark=None):
    """(stop-loss, take-profit) prices typed in the extras, 0.0 when not typed. "close it at 2" is a take-profit above the price
    on a long and a stop-loss below it (the other way on a short); with no price to compare it is neither."""
    sl, tp, cl = _tpsl_match(extras or '')
    f = lambda m: float(m.group(1).replace(',', '')) if m else 0.0
    if cl and not (sl or tp):
        if not mark:
            return 0.0, 0.0
        above = f(cl) > float(mark)
        return (0.0, f(cl)) if (above if side == 'long' else not above) else (f(cl), 0.0)
    s_, t_ = f(sl), f(tp)
    if tpsl_pct(extras, sl) or tpsl_pct(extras, tp):   # "stop loss 10%": 10% from the order's price, never a price of 10 (QA 2026-11-17, RDR-V15)
        if not mark:
            return 0.0, 0.0
        long_ = side == 'long'
        if tpsl_pct(extras, sl):
            s_ = round(float(mark) * (1 - s_ / 100.0) if long_ else float(mark) * (1 + s_ / 100.0), 8)
        if tpsl_pct(extras, tp):
            t_ = round(float(mark) * (1 + t_ / 100.0) if long_ else float(mark) * (1 - t_ / 100.0), 8)
    return s_, t_


def tpsl_pct(extras, m):
    """True when the stop-loss / take-profit number m (a _tpsl_match match) is typed as a percent ("stop loss 10%")."""
    return bool(m) and bool(re.match(r'\s*(?:%|percent\b|per\s?cent\b|pct\b)', (extras or '')[m.end(1):], re.I))


def tpsl_far(extras, side, ref):
    """A stop-loss or take-profit typed as a PRICE that is far from the order's price (a stop more than 50% away, a take-profit
    under a third or over three times the price) — the same rule as the open-position reader: ('stop-loss', 10.0) or None."""
    sl, tp, _ = _tpsl_match(extras or '')
    f = lambda m: float(m.group(1).replace(',', '')) if m else 0.0
    if not ref or re.search(r'(?i)\bexactly\b', extras or ''):
        return None
    if sl and not tpsl_pct(extras, sl) and f(sl) and abs(f(sl) - ref) / ref > 0.5:
        return 'stop-loss', f(sl)
    if tp and not tpsl_pct(extras, tp) and f(tp) and not (1 / 3.0 <= f(tp) / ref <= 3):
        return 'take-profit', f(tp)
    return None


def tpsl_words(extras, side, mark=None):
    """The typed stop-loss / take-profit in the words the order reader takes ("stop loss at 0.8 and take profit at 1.5"), or ''."""
    sl, tp = tpsl_numbers(extras, side, mark)
    return ' and '.join((['stop loss at %s' % _g(sl)] if sl else []) + (['take profit at %s' % _g(tp)] if tp else []))


def order_tpsl(msg):
    """("long sui $10 3x", "stop loss at 0.8") for a message that opens ONE order and types a stop-loss / take-profit / close
    PRICE with it; (msg, '') otherwise (no number, a trailing stop, two orders: the readers answer those as before)."""
    if not _ORDER_HEAD.match(msg or '') or len(re.findall(r'(?i)\b(?:long|short)\b', msg)) != 1:
        return msg, ''
    order, extras = trade_extras(msg)
    if not extras or re.search(r'(?i)\btrailing\b', extras) or not any(_tpsl_match(extras)):
        return msg, ''
    return order, extras


def tpsl_problem(sl, tp, side, ref, liq=0.0):
    """Why this stop-loss / take-profit can't go on a %s order priced at ref ('' when it can)."""
    long_, bad = side == 'long', []
    if sl and (sl >= ref if long_ else sl <= ref):
        bad.append('a stop-loss on a %s has to be %s the price now (%s)' % (side, 'below' if long_ else 'above', _g(ref)))
    elif sl and liq > ref * 0.001 and (sl <= liq if long_ else sl >= liq):
        bad.append('a stop-loss at %s is past the liquidation price (about %s), so it would never trigger' % (_g(sl), _g(liq)))
    if tp and (tp <= ref if long_ else tp >= ref):
        bad.append('a take-profit on a %s has to be %s the price now (%s)' % (side, 'above' if long_ else 'below', _g(ref)))
    return '; '.join(bad)


def tpsl_off_text(bad, sym, side):
    return ' I left the stop-loss / take-profit off this order: %s. Once it has filled, say e.g. "stop loss at <price> on my %s %s".' % (bad, sym, side)


def attach_tpsl(w, extras):
    """Put the typed stop-loss / take-profit on the order card (perpconfirm): the page places it in the order's own transaction.
    Returns the sentence for the reply. A price on the wrong side of the price now, or past liquidation, is left off and said."""
    long_ = not int(w.get('side') or 0)
    side, sym = ('long' if long_ else 'short'), w.get('symbol') or 'the market'
    ref, liq = float(w.get('price') or w.get('mark') or 0), float(w.get('liq') or 0)
    far = tpsl_far(extras, side, ref)
    if far:                                          # "stop loss 10" on ETH at 1,816: a price, or 10%? never placed as typed (RDR-V15)
        return (' I left the %s off this order: %s is far from %s\'s price now (%s). If you meant %s%%, say "%s %s%% on my %s %s" once it has filled; '
                'for that exact price, say "%s at exactly %s on my %s %s".' % (far[0], _g(far[1]), sym, _g(ref), _g(far[1]), far[0].replace('-', ' '), _g(far[1]), sym, side,
                                                                              far[0].replace('-', ' '), _g(far[1]), sym, side))
    sl, tp = tpsl_numbers(extras, side, ref or None)
    if not (sl or tp) or not ref:
        return extras_say(extras, sym, side, ref or None)[0]
    bad = tpsl_problem(sl, tp, side, ref, liq)
    if bad:
        return tpsl_off_text(bad, sym, side)
    if sl:
        w['sl'] = sl
    if tp:
        w['tp'] = tp
    what = ' and '.join((['stop-loss at %s' % _g(sl)] if sl else []) + (['take-profit at %s' % _g(tp)] if tp else []))
    return ' Your %s %s placed with the order, in the same approval.' % (what, 'are' if sl and tp else 'is')


def with_tpsl(out, order, extras):
    """The answer to the order alone (order_tpsl), given back with the typed stop-loss / take-profit: on the order card, carried
    into a choice or next step that leads to the order, or said (never dropped — C2)."""
    if not out:
        return out
    w = out.get('widget') or {}
    side = 'short' if re.search(r'(?i)\bshort\b', order or '') else 'long'
    if w.get('type') == 'perpconfirm':
        return dict(out, text=(out.get('text') or '').rstrip() + attach_tpsl(w, extras))
    words = tpsl_words(extras, side)
    if words and w.get('type') == 'choose' and any(_ORDER_HEAD.match(o.get('ask') or '') for o in w.get('options') or []):
        for o in w['options']:                       # "how big?" choices: each one keeps the stop-loss
            if _ORDER_HEAD.match(o.get('ask') or '') and not re.search(r'(?i)stop|take|profit', o['ask']):
                o['ask'] = o['ask'] + ', ' + words
        return out
    if words and _ORDER_HEAD.match(str(w.get('next') or '')):   # a deposit first: the order that follows it keeps the stop-loss
        w['next'] = w['next'] + ', ' + words
        return dict(out, text=(out.get('text') or '').rstrip() + ' Your %s comes with that order, in the same approval.' % words.replace('stop loss', 'stop-loss').replace('take profit', 'take-profit'))
    sym = w.get('symbol') or 'the market'
    return dict(out, text=(out.get('text') or '').rstrip() + (extras_say(extras, sym, side)[0] or ''))


def split_orders(trade):
    """A trade part with two orders -> the orders one by one ("long sui 3x and short btc 2x", "long and short btc 2x");
    [] when it is one order."""
    t = (trade or '').strip(' ,.!')
    sides = re.findall(r'(?i)\b(long|short)\b', t)
    if len(sides) < 2:
        return []
    both = re.match(r'(?i)^(?:go\s+|open\s+(?:a\s+)?)?(long|short)\s+(?:and|&|\+|or)\s+(long|short)\b\s*(.*)$', t)
    if both:                                         # "long and short btc 2x": the same market both ways
        return [('%s %s' % (both.group(1).lower(), both.group(3))).strip(), ('%s %s' % (both.group(2).lower(), both.group(3))).strip()]
    parts = [x.strip(' ,') for x in re.split(r'(?i)\s*(?:,|;|\band\b|\bthen\b|&|\+)\s*(?=(?:also\s+)?(?:go\s+|open\s+(?:a\s+)?)?(?:\d+(?:\.\d+)?x\s+)?(?:long|short)\b)', t) if x.strip(' ,')]
    return parts if len(parts) >= 2 else []


_WALLET_NAMES = r'other|second|2nd|another|different|ledger|slush|nightly|suiet|phantom|surf|okx|hardware|cold|backup|old|spare'
OTHER_WALLET_RE = re.compile(r'(?i)\b(?:from|on|in|using|via|with|through|out\s+of)\s+(?:my\s+|the\s+|a\s+)?(?:(?P<w>%s)\s+(?:wallet|account)|(?P<l>ledger))\b(?!\s*(?:0x|@|[\w-]+\.sui))'
                             % _WALLET_NAMES)


def other_wallet(msg, own=()):
    """The words naming another wallet as a place the money is or goes ("from my other wallet", "on my ledger wallet"), or ''.
    own: names of the connected accounts (their wallet apps, their nicknames) — "from my slush wallet" is no other wallet when
    the connected one IS Slush."""
    for m in OTHER_WALLET_RE.finditer(msg or ''):
        if (m.group('w') or m.group('l')).lower() not in {str(x).lower() for x in own}:
            return m.group(0).strip()
    return ''


def order_ask(trade, sym, maxlev=None):
    """The order as the trade reader takes it, from a trade part and its market: "long SUI $50 3x", "short BTC 25x". maxlev is
    the market's own limit, used when the user said "max leverage"."""
    side = re.search(r'(?i)\b(long|short)\b', trade or '')
    if not side:
        return ''
    t2 = re.sub(r'\b%s\b' % re.escape(sym), ' ', trade, flags=re.I)
    lev = re.search(r'(\d+(?:\.\d+)?)\s*x\b', t2) or re.search(r'(?i)(\d+(?:\.\d+)?)\s+times\b', t2)
    maxl = re.search(r'(?i)\b(?:max\w*|highest|full)\s+lev\w*|\blev\w*\s+(?:at\s+)?max\b', t2)
    size = AM.usd_first(t2)                          # the $ size typed — never the limit price ("long sui 3x at $0.8" became a $0.8 order;
    ask = '%s %s' % (side.group(1).lower(), sym)     # round 2, 2026-11-16)
    if size:
        ask += ' $%s' % size
    if lev:
        ask += ' %sx' % lev.group(1)
    elif maxl and maxlev:
        ask += ' %gx' % float(maxlev)
    if AM.limit_price(t2):                           # the limit price typed stays with the order
        ask += ' at %s' % AM.limit_price(t2)
    return ask


def lev_of(ask):
    """The leverage in an order ask, or None."""
    m = re.search(r'(\d+(?:\.\d+)?)x\b', ask or '')
    return float(m.group(1)) if m else None


# ── multi-step shapes said in other words (C10) ──
_MKT = r'(?:the\s+)?(?:(?:navi|suilend)(?:\s+(?:main(?:\s+market)?|prime|high\s*yield|ember|matrixdock|sui\s+(?:prime|high\s*yield)))?|ember|matrixdock|prime|high\s*yield)'


def wallet_plus_market(msg):
    """"deposit 20 usdc from my wallet plus 10 from navi into perps" -> "withdraw 10 usdc from navi plus 20 usdc from my wallet,
    deposit it into perps" (the chain reader reads a withdrawal with a wallet top-up). The message unchanged when it isn't one."""
    m = re.match(r'(?i)^\s*(deposit|put|lend|move|add|send)\s+(\d[\d.]*)\s*([A-Za-z][A-Za-z0-9]{1,9})\s+from\s+(?:my\s+)?wallet\s+(?:plus|and|\+|&)\s+(\d[\d.]*)'
                 r'(?:\s*\3)?\s+from\s+(%s)\s+((?:in|into|to|on|onto)\s+.+?)\s*[.!]*$' % _MKT, msg or '')
    if not m:
        return msg
    verb = 'deposit' if m.group(1).lower() in ('deposit', 'put', 'add', 'move') else m.group(1).lower()
    return 'withdraw %s %s from %s plus %s %s from my wallet, %s it %s' % (m.group(4), m.group(3), m.group(5), m.group(2), m.group(3), verb, m.group(6))


def split_places(msg, is_market):
    """"split 40 usdc between perps and navi" -> "take 40 usdc from my wallet, lend half on navi and deposit the rest into perps".
    is_market(text) says whether a part names a lending market. The message unchanged when it isn't one."""
    m = re.match(r'(?i)^\s*(?:split|divide|spread)\s+(\d[\d.]*)\s*([A-Za-z][A-Za-z0-9]{1,9})\s+(?:evenly\s+|equally\s+|50\s*/\s*50\s+)?(?:between|across|over)\s+(.+?)\s*[.!]*$', msg or '')
    if not m:
        return msg
    parts = [x.strip() for x in re.split(r'(?i)\s*(?:,|&|\band\b|\bplus\b)\s*', m.group(3)) if x.strip()]
    perps = [x for x in parts if re.match(r'(?i)^%s$' % _PLACE, x)]
    lends = [x for x in parts if x not in perps and is_market(x)]
    if len(parts) != 2 or len(perps) != 1 or len(lends) != 1:
        return msg
    return 'take %s %s from my wallet, lend half on %s and deposit the rest into perps' % (m.group(1), m.group(2), lends[0])


# ── replies for a deposit followed by a trade (deposit_then_trade in server.py) ──
_SAY_EG = 'withdraw 10 USDC from NAVI, deposit to perps and long SUI 3x'
_TRADE_TAIL_RE = re.compile(r'(?i)^(?P<head>.+?)\s*(?:,\s*(?:and\s+)?(?:then\s+)?|\band\s+then\s+|\bthen\s+|\band\s+)(?:i\s+want\s+to\s+|to\s+)?'
                            r'(?:trade\s+(?:it|with\s+it|that|them)?|use\s+it\s+to\s+trade|start\s+trading|open\s+(?:a\s+)?(?:position|trade))\s*[.!]*$')


def trade_tail(msg):
    """"…, deposit into perps and trade it": the head (the money part) when the trade names no order, else None (C8)."""
    m = _TRADE_TAIL_RE.match(msg or '')
    return m.group('head').strip(' ,') if m else None


def trade_tail_reply():
    return {'text': 'Which order should follow the deposit? Say it in the same sentence — the side, the market and the leverage — e.g. '
                    '"swap 5 SUI to USDC, deposit into perps and long SUI 3x". The deposit is one approval, then the order comes up for its own.'}


def orders_reply(head, orders):
    """Two orders after one deposit: one order at a time — each option is the whole request with one of them (C8)."""
    return {'text': 'One order at a time — the deposit, then ONE order. Which order should follow the deposit? (Ask for the other once it has filled.)',
            'widget': {'type': 'choose', 'options': [{'label': 'Then %s' % o, 'sub': 'deposit first, then this order', 'ask': '%s and %s' % (head, o)}
                                                     for o in orders[:4]]}}


def no_market_reply(head, side, word, markets=('BTC', 'ETH', 'SOL', 'SUI')):
    """No market Kivy can trade in the trade part: name the word that isn't one, or ask which — each option keeps the request (C9)."""
    lead = ('%s isn\'t one of the perps markets I can trade. ' % word.upper()) if word else ''
    return {'text': lead + 'Which market should the %s be on? Pick one — the deposit comes first, then the order for its own approval.' % side,
            'widget': {'type': 'choose', 'options': [{'label': '%s %s' % (side.title(), x), 'sub': 'after the deposit', 'ask': '%s and %s %s' % (head, side, x)}
                                                     for x in markets] + [{'label': 'All markets', 'sub': 'search the perps markets', 'ask': 'market'}]}}


def lev_reply(sym, lev, mx):
    """A leverage over the market's limit, said BEFORE the deposit is signed (C8, #74)."""
    return {'text': '%gx is above the maximum for %s — it allows up to %gx. Say the request again with a leverage up to %gx, and the deposit '
                    'and the order follow as usual.' % (lev, sym, mx, mx)}


def wallet_reply(phrase):
    """Another wallet named as a place the money is or goes (C8)."""
    return {'text': 'Kivy works with the connected wallet only, so it can\'t do the part "%s". Switch your wallet app to that wallet and connect '
                    'it, then ask again — or say it without that part to use the connected wallet.' % phrase}


def plain_deposit_head(head):
    """True when the money part is a deposit from the wallet's own USDC — no swap, sell, withdrawal or lending market — so a
    perps deposit card read from it is the whole money part (C7: "sell 20 sui for usdc, top up my perps…" became a deposit
    of wallet USDC and the swap was lost)."""
    return not re.search(r'(?i)\b(?:swap\w*|sell\w*|convert\w*|exchang\w*|turn|trade|withdr\w*|take|pull|grab|redeem|unlend|from|out\s+of|'
                         r'navi|suilend|ember|matrixdock|prime|high\s*yield|lent|lending)\b', head or '')


def capped_reply(typed, have, ask):
    """A perps deposit larger than the wallet's USDC: say so, never a card quietly capped (C8, #83)."""
    return {'text': 'You hold %s USDC — less than the %s USDC you asked to deposit. Say a smaller amount, e.g. "deposit %s USDC to perps and %s".'
                    % (_g(have), _g(typed), _g(have), ask)}


def deposit_part_reply(sym, side, ask):
    """The money part couldn't be read: say what to type — Kivy CAN do the deposit and then the order (C9)."""
    return {'text': 'I can do the deposit into your perps account first, then the %s %s order as its own approval — but I couldn\'t read the deposit '
                    'part. Say it like "%s" or "deposit 10 USDC to perps and %s".' % (sym, side, _SAY_EG, ask)}


def chain_trade_text(msg):
    """What the chain reader says when a multi-step request names a trade it couldn't hand on (C9: it said Kivy can't open a
    position, though the deposit-then-order flow does exactly that)."""
    if re.search(r'(?i)\bborrow\w*|\bloan\s+against\b', msg or ''):
        return ('Kivy never borrows. I can do the money steps and the deposit into your perps account, then the order as its own '
                'approval — say it without the borrowing, e.g. "%s".' % _SAY_EG)
    if not re.search(r'(?i)\b(?:long|short|ape)\b|\bopen\s+(?:a\s+)?(?:\w+\s+)?(?:position|trade)\b', msg or ''):
        return ('Kivy never uses leverage on lending. Leverage is for perps orders: deposit into your perps account first, then the order '
                'comes up for its own approval — e.g. "%s".' % _SAY_EG)
    return ('I couldn\'t read the deposit part of that. Say it like "%s" — the money steps and the deposit are one approval, then your '
            'order comes up for its own.' % _SAY_EG)


def _g(x):
    try:
        return ('%.6f' % float(x)).rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return str(x)


# ── the chain reader's own answers (C1, C5) ──
_PLACE_FILLER = {'it', 'that', 'them', 'this', 'the', 'my', 'a', 'an', 'wallet', 'best', 'market', 'markets', 'good', 'great', 'place', 'somewhere',
                 'lending', 'yield', 'rate', 'rates', 'safe', 'safer', 'safest', 'same', 'one', 'there', 'work', 'protocol', 'earn', 'interest',
                 'top', 'highest', 'higher', 'most', 'usual', 'savings', 'saving', 'lend', 'lent', 'deposit', 'account', 'half', 'rest', 'all',
                 'equal', 'equally', 'parts', 'each', 'both', 'whatever', 'pays', 'paying', 'low', 'risk', 'low-risk'}


def unknown_place(clause, is_coin):
    """The destination word of a lend clause that names no market Kivy knows ("put it in futures", "deposit it into xyz"), or
    None. is_coin(word) says whether a word is a coin (a coin is never a place: "deposit 5 usdc into …")."""
    m = re.search(r'(?i)\b(?:in|into|onto|on|at|to)\s+(?:the\s+|my\s+|a\s+)?([A-Za-z][A-Za-z0-9-]{2,})\b', clause or '')
    if not m:
        return None
    w = m.group(1)
    if w.lower() in _PLACE_FILLER or is_coin(w) or re.match(r'(?i)^0x', w):
        return None
    return w


def unknown_place_text(word):
    return ('"%s" isn\'t a market Kivy knows, so I won\'t guess where that goes. Say the place — a lending market (e.g. "lend it on NAVI"), '
            '"deposit it into perps" for your Aftermath margin, or "keep it in my wallet".' % word)


def mid_swap_text(coin):
    """A share swapped in the middle of a plan, or a part sent / lent before a later swap: one plan swaps first and then does
    every other step in the new coin, so it would have used the wrong coin or all of it (C5)."""
    return ('In one plan every step after a swap uses the swapped coin, so a part swapped in the middle — or sent or lent before a later swap — '
            'would come out wrong. Say each part with its coin, e.g. "withdraw 30 %s from perps, send 10 %s to alice.sui and swap the rest to SUI", '
            'or split it into two requests.' % (coin, coin))


# ── close -> next step (close_next in server.py, C4) ──
_USE_RE = re.compile(r'(?i)^use\s+(?:(?:the|my|that|this|all\s+the)\s+)?(?:margin|money|usdc|it|proceeds|cash|funds|collateral)?\s*(?:to|for)\s+(?=.*\b(?:long|short)\b)')
_AMT_LEAD = re.compile(r'(?i)^(?:withdraw|take|pull|get|cash)\s+(?:out\s+)?(?P<a>\d+(?:\.\d+)?\s*%|half|a\s+third|a\s+quarter|\$?\d+(?:\.\d+)?(?![\d.]*\s*%)(?:\s*(?:usdc|usd|dollars?))?)'
                       r'(?:\s+(?:of\s+)?(?:(?:the|my)\s+)?(?:usdc|money|margin|funds|cash|proceeds|collateral|it))?(?:\s+out)?'
                       r'(?:\s+(?:of|from)\s+(?:my\s+|the\s+)?(?:perps?|aftermath)(?:\s+account)?)?\s*(?:(?:,|\band\s+then\b|\bthen\b|\band\b)\s*)?')


def use_margin_trade(rest):
    """"use the margin to short btc 3x" -> "short btc 3x" (the order after a close); None when it isn't that."""
    m = _USE_RE.match(rest or '')
    return rest[m.end():] if m else None


def money_words(rest):
    """True when an order after a close names the money ("with the same money", "with it"): its wording is turned into an
    order ask, since the order reader doesn't read those words."""
    return bool(re.search(r'(?i)\b(?:same|money|proceeds|margin|cash|funds|it|that|them)\b', rest or ''))


def close_amount_next(rest):
    """(sentence, next ask) for "withdraw 20 usdc and send it to bob.sui", "withdraw half the margin and lend it on navi" after a
    close: the amount or share typed is what leaves the perps account (it was glued after "withdraw my perps margin and …",
    or all the margin moved). None when the step names no amount."""
    am = _AMT_LEAD.match(rest or '')
    if not am:
        return None
    a, body = am.group('a'), rest[am.end():].strip(' ,')
    body = re.sub(r'(?i)^(?:(?:back\s+)?(?:to|into)\s+(?:my\s+)?wallet)\b\s*(?:(?:,|\band\s+then\b|\bthen\b|\band\b)\s*)?', '', body)   # the wallet is where it lands anyway
    lead = ('withdraw %s %smy perps margin' % (a, '' if a.lower() == 'half' else 'of ')) if re.match(r'(?i)half|a\s|\d+(?:\.\d+)?\s*%', a) else \
        'withdraw %s from my perps account' % (a if re.search(r'(?i)usdc|usd|dollar|\$', a) else a + ' usdc')
    if not body:
        return ' When it has closed, the withdrawal to your wallet comes up for its own approval.', lead
    short = re.sub(r'0x[0-9a-fA-F]{6,}', lambda x: x.group(0)[:6] + '…' + x.group(0)[-4:], body)[:80]
    return ' When it has closed, the next step (%s) comes up for its own approval.' % short, lead + ' and ' + body[:100]


def card_margin(w):
    """The USDC a deposit card puts into perps, when it is exact (the perps deposit card's amount, or a chain's deposit step with
    no swap and a typed start); 0 when it isn't known until the transaction lands (C3)."""
    if (w or {}).get('type') == 'perpdep':
        return round(float(w.get('preset') or 0), 6)
    spec = (w or {}).get('spec') or {}
    pk = next((st for st in (w or {}).get('steps') or [] if st.get('proto') == 'aftermath'), None)
    if pk is None or spec.get('swaps') or (spec.get('source') or {}).get('pick'):
        return 0
    return round(float(pk.get('q') or 0), 6)


# ── texts of the chain reader (C1, C5, C10) ──
def swapped_not_usdc_text(coin, others=False):
    if others:                                       # "swap to sui, put half into navi sui high yield and the rest as margin": two coins at the end
        return ('Perps margin is USDC, but after that swap the money is %s — so the %s part and the perps part can\'t share one plan. Split it '
                'into two requests: the %s part first, then the USDC into perps.' % (coin, coin, coin))
    return ('Perps margin is USDC, so the %s from that swap can\'t go into your perps account. Leave the swap out to deposit the USDC, '
            'or keep the %s in your wallet.' % (coin, coin))


def after_swap_text(op, hand, said, swapped=True):
    """A step that names a coin other than the one in hand ("swap to sui, send 10 usdc to …", "lend 0.1 sui, send 0.2 usdc") (C5)."""
    if not swapped:
        return ('One transaction starts from one coin — here %s — so the %s part can\'t be in it. Do the %s part in its own request.' % (hand, said, said))
    what = {'send': 'send', 'lend': 'lend', 'keep': 'amount kept'}.get(op, 'step')
    return ('Every %s in one plan happens after the swaps, so it would be in %s — not %s. Do the %s part in its own request, or say it in %s.'
            % (what, hand, said, said, hand))


def exact_start_text(est, coin, market, perps_amt):
    """"withdraw ALL usdc from suilend, deposit 50 to perps and lend the rest": the start as a number makes the perps part exact (C10)."""
    return ('Say the start as a number so the perps part is exact — you have %s %s on %s, e.g. "withdraw %s %s from %s, deposit %s to perps and lend '
            'the rest …".' % (est, coin, market, est, coin, market, perps_amt))
