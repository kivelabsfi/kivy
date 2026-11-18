"""The amount reader: ONE place that says what a typed money amount is (fix package 1, 2026-11-15, a reviewer scan R1/R2/R3/R6).

Before this, each exact reader found "the number" on its own and took the first one it saw, so "send 50, i mean 5 usdc",
"swap 3/4 sui", "swap 1 000 sui" and an address 6 characters too long each built a card for something the user did not
type. Every reader now gets the same reading:

  pre(text) / post(text)   how an amount is WRITTEN, made plain before any reader runs (called by hygiene.tidy_amounts):
                           other scripts' digits -> 0-9, "1 000" -> 1000, "1O" (a letter for a digit) -> an unclear amount,
                           "3/4 sui" -> 0.75 sui (a fraction of ONE coin), "3/4 of my sui" -> 75% of my sui (a share).
  amounts(text)            every money amount in a sentence, with its kind (units / usd / pct) and its coin.
  correction(text)         "50, i mean 5 usdc" -> the corrected amount wins; the reply says "Read as 5 USDC."
  conflict(text)           two different amounts for ONE action and no correction word -> a choice card, never a guess.
  long_address(text)       a 0x address with more than 64 characters -> refused (it was cut to its first 64).
  RCPT / HEX_ADDR / NUM    the one recipient and number pattern the readers share.
  lone_step(...)           the amount typed on the single last step of a multi-step request (R1).
  bare_after_swap(...)     "swap 10 sui to usdc and send 3 to bob.sui": 3 of which coin? (R6)

Round 2 (2026-11-16):
  first(text) / first_pct(text) / first_coin(text)   the first amount of ONE clause, for the multi-step readers (chain_amount):
                           rates, counts, leverage, dates, reference and account numbers are never taken for it.
  said(text)               what the user typed as amounts, for the AI's order gates (typed / usd / pct / pairs): a fact about
                           the world ("my rent is 100 dollars") and a holding next to a real order ("i have 150 usdc. swap 2
                           usdc") are not order amounts.
  clause_conflict(text)    two amounts inside ONE clause of a multi-step request -> the same choice card as conflict().
  unsure(text)             "1.000 sui": 1 or 1000? asked, never guessed.
  two_sided(text)          "swap 5 sui for 10 usdc": spend 5 SUI or get about 10 USDC? asked.
  first_dollars / dollars_ask   "…, send $3 of it to bob.sui" on a step that moves SUI: about 4.19 SUI? asked (it sent 3 SUI).
  all_then_fixed(...)      "withdraw all my usdc from navi and deposit 50 to perps": all out, 50 in, the rest kept.
  pre() also reads "5$", "usd 5", "US$5" as $5; post() reads "lend usdc 5 on navi" as "lend 5 usdc on navi".

Round 3 (fix package 1 of the QA audit 2026-11-17, "Card = what you meant"):
  pre()                    "20 percent / per cent / pc" is 20%; "5,5000" and "5. 5 sui" are an unclear amount (asked).
  first_or_word(text)      keeps a share a share: "50%" (it gave 50, read as 50 coins).
  unread(text)             an amount word that is not one number ("some", "a fifth", "two thirds", "ten") -> asked, never "all".
  frac(text)               "1/10" with any denominator to 99 (the multi-step reader read it as 1).
  repeated(text)           "3 times", "twice", "3 payments of 10": one card is one transaction -> asked.
  modified(text)           "half of 10 usdc", "10 usdc less 2 / plus 2 / minus fees" -> which amount? (the 10 was sent).
  lone_step(...)           a start from several markets counts all of them ("100 from navi and 50 from suilend" = 150).
"""
# How it runs: imported at the top of the reader modules of site/kivy/ (hygiene, readguard, sends, cards, chains, pieces,
# fallback; since round 2 also situations, lending, intent, perpwords, wordings) and by server.py (the AI gates). Pure text work, no state, no network. Imports: stdlib and the leaf module config only; the coin words, action
# verbs and reference-number patterns it shares with the readers are fetched when a function runs (_gcoin, _words), because
# the readers import each other in a cycle and this module must be loadable before any of them.
import re
import unicodedata
from .config import disp

NUM = r'\d+(?:\.\d+)?'                               # a number as the readers see it after tidy_amounts
HEX_ADDR = r'0x[0-9a-fA-F]{64}(?![0-9a-fA-F])'       # a Sui address: exactly 64 characters, never the start of a longer one
SUI_NAME = r'@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b|@[a-z0-9-]{2,}'
RCPT = HEX_ADDR + '|' + SUI_NAME                     # who a send goes to, in every multi-step and multi-send reader
_WORD = r'[A-Za-z][A-Za-z0-9]{1,9}'
_THIN = ' \u00a0\u2007\u2009\u202f'                  # a space, no-break, figure, thin and narrow no-break space
_THOU_RE = re.compile(r'(?<![\w.,/:#$-])(\$?)([1-9]\d{0,2})((?:[%s\'\u2019]\d{3})+)(?!\d|x\b)' % _THIN)   # "$10 100x" is $10 at 100x
_LOOK_RE = re.compile(r'(?<![\w.$@/-])\$?(?=[0-9OoIl.]*\d)(?=[0-9OoIl.]*[OoIl])[0-9OoIl]+(?:\.[0-9OoIl]+)?(?![\w]|\.\w)')
_FRAC = r'(?<![\w./$:-])([1-9]\d?)\s?/\s?([1-9]\d?)(?![\w./:-])'
_FRAC_OF_RE = re.compile(_FRAC + r'(?=\s+of\s+(?:(?:my|the|your|our)\s+)?(' + _WORD + r')\b)', re.I)
_FRAC_COIN_RE = re.compile(_FRAC + r'(?=\s*(' + _WORD + r')\b)', re.I)
_FRAC_LEFT_RE = re.compile(_FRAC)
PLAIN = re.compile(r'(?<![\w.])(%s)(?![\w.%%])' % NUM)       # a bare number, not part of a word, a decimal or a percent
_USD_BACK_RE = re.compile(r'(?<![\w.,$/:#-])(%s)\s?\$(?![\w$]|\s?\d)' % NUM)                 # "5$", "5 $"
_USD_FRONT_RE = re.compile(r'(?i)(?<![\w$.])(?:us\s?\$|usd\s?\$?)\s?(%s)(?![\w]|\.\d|\s*(?:usd|dollars?|bucks|\$|%%|x\b))' % NUM)   # "US$5", "usd 5"
_COIN_FIRST_RE = re.compile(r'(?i)\b(send|transfer|pay|give|swap|convert|exchange|lend|supply|deposit|withdraw|redeem|stake|unstake)'
                            r'(\s+(?:my\s+)?)(' + _WORD + r')\s+(' + NUM + r')(?=\s+(?:to|from|on|in|into|for)\b|\s*[.!]*$)')


def _coin(w):
    """A coin word -> Kivy's coin key; filler words and unknown words -> None."""
    from .readguard import _gcoin
    try:
        return _gcoin(w)
    except Exception:
        return None


def _plain(x):
    """A number as people read it (0.75, 1000 — never 7.5e-01)."""
    from .readguard import _gnum
    return _gnum(float(x))


def _words():
    """(reference-number pattern, account-number pattern, action verbs, money nouns): the readers' own lists, one copy."""
    from .sends import ACCT_NUM_RE, REF_NUM_RE
    from .situations import AMT_VERB, MONEY_NOUNS
    return REF_NUM_RE, ACCT_NUM_RE, AMT_VERB, MONEY_NOUNS


def pre(text):
    """Before tidy_amounts reads the words one by one: digits of other scripts become 0-9, a thousands gap is closed
    ("1 000" = 1000, also with a no-break / thin space or an apostrophe: "1'000"), and a number written with a letter for a digit ("1O", "l0") becomes
    "an unclear amount" (the readers then ask instead of reading the 1)."""
    t = ''.join(str(unicodedata.digit(ch)) if ch > '\x7f' and unicodedata.category(ch) == 'Nd' else ch for ch in str(text or ''))
    t = _THOU_RE.sub(lambda m: m.group(1) + m.group(2) + re.sub(r'\D', '', m.group(3)), t)
    # round 2: "5$", "5 $", "usd 5", "US$5" are $5 ("send 5$ of sui" sent 5 SUI; "swap usd 5 of sui to usdc" swapped 5 USDC).
    # Not after "at" / "@": "long btc $10 at 350$" keeps its own reading in the trade reader.
    t = _USD_BACK_RE.sub(lambda m: m.group(0) if re.search(r'(?i)(?:\bat|@)\s*$', m.string[:m.start()]) else '$' + m.group(1), t)
    t = re.sub(r'(?i)(?<![\w.,$])\$?\d+(?:\.\d+)?[km]\d+(?![\w.])', ' an unclear amount ', t)    # "1k5" was read as 1 (1500? 1005?): asked
    t = re.sub(r'(?i)(?<![\w.,$])(%s)\s?(?:pct|percent|per\s?cent|pc)\b' % NUM, r'\1%', t)            # "5pct of my usdc" was read as 5 USDC; "20 percent of
    # my sui" as 20 SUI (QA 2026-11-17, RDR-V5)
    t = re.sub(r'(?<![\w.,$])\$?\d+(?:,\d+)+(?![\w.,]|,\d)', lambda m: m.group(0) if re.fullmatch(r'\$?(?:[1-9]\d{0,2}(?:,\d{3})+|\d+,\d{1,2}|0,\d{3})', m.group(0))
               else ' an unclear amount ', t)                                                   # "5,5000 deep" was read as 5 (RDR-V12)
    t = re.sub(r'(?<![\w.,$])\d+\.\s+\d+(?:\.\d+)?(?=\s+(%s)\b)' % _WORD, lambda m: ' an unclear amount ' if _coin(m.group(1)) else m.group(0), t)   # "swap 5. 5 sui" (RDR-V12)
    if not re.search(r'(?i)\b(?:long|short|lev\w*|perps?|margin)\b', t):                        # (in a trade "x3" is leverage)
        t = re.sub(r'(?<![\w.,$])([1-9]\d*(?:\.\d+)?)[x\u00d7*](%s)(?![\w.]|::)' % NUM, r'\1 x \2', t)   # "2x5 usdc" was read as 2: "2 x 5" is asked about
        t = re.sub(r'(?i)(?<=[a-z])\s+[x\u00d7]\s?([23])(?![\w.])', r' \1 times', t)         # "send 5 usdc x2" sent 5 once: asked like "twice"
    t = _USD_FRONT_RE.sub(lambda m: m.group(0) if re.search(r'\d\s*$', m.string[:m.start()]) else '$' + m.group(1), t)
    return _LOOK_RE.sub(lambda m: m.group(0) if _coin(m.group(0)) else ' an unclear amount ', t)


def post(text):
    """After tidy_amounts: "3/4 sui" is 0.75 SUI (a fraction of ONE coin, like "half a sui" = 0.5 SUI) and "3/4 of my sui" is
    75% of the balance. A fraction that stands on no coin ("send 1/2 to …") is left for the multi-step reader, which reads it
    as a share of the coin in hand; conflict() asks about it in a one-step request."""
    def share(m):
        n, d = int(m.group(1)), int(m.group(2))
        return _plain(round(100.0 * n / d, 4)) + '%' if n <= d and _coin(m.group(3)) else m.group(0)

    def units(m):
        glued = not m.string[m.end():m.end() + 1].isspace()          # "3/4sui": keep the number and the coin apart
        return _plain(round(int(m.group(1)) / int(m.group(2)), 9)) + (' ' if glued else '') if _coin(m.group(3)) else m.group(0)
    def coin_first(m):                                # round 2: "lend usdc 5 on navi" = "lend 5 usdc on navi" (the amount was dropped)
        return '%s%s%s %s' % (m.group(1), m.group(2), m.group(4), m.group(3)) if _coin(m.group(3)) else m.group(0)
    return _COIN_FIRST_RE.sub(coin_first, _FRAC_COIN_RE.sub(units, _FRAC_OF_RE.sub(share, str(text or ''))))


def long_address(text):
    """The refusal for a 0x address longer than 64 characters (two ids glued, an object id with extra characters): every
    reader refuses it the way the single send does, none cuts it to its first 64 (R3). None when there is none."""
    m = re.search(r'0x[0-9a-fA-F]{65,}(?!\s*::)', str(text or ''))
    if not m:
        return None
    return ("That address isn't complete: a Sui address is 0x followed by 64 characters, and this one has %d. "
            'Copy it again from the wallet that will receive it.' % (len(m.group(0)) - 2))


# Numbers that are not money amounts: blanked out (same length, so positions still fit the original text).
_NOT_AMOUNT = [
    r'0x[0-9a-fA-F]+(?:::\w+)*',                                             # addresses, coin types
    r'(?<![\w.])@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b|(?<![\w@])@[a-z0-9-]{2,}',   # names
    r'\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\b\.?)?|\b\d{1,2}\s*[ap]\.?m\b\.?',   # dates, times
    r'\d+(?:\.\d+)?x\b',                                                     # leverage: 3x
    r'(?<![\w.])\d+\s?/\s?\d+(?![\w.])',                                       # a ratio: "50/50 into usdc and wal" (a fraction on a coin is read by post())
    r'(?:\b(?:limit|price|stop(?:[\s-]*loss)?|sl|tp|take[\s-]*profit|trigger|entry)\b(?:\s+(?:at|of|price|to|is|=|:))*\s*|\bat\s+(?:a\s+price\s+of\s+)?|@\s*'
    r'|\bwhen\s+\w+\s+(?:hits|reaches)\s+|\b(?:above|below|under|over|hits|reaches)\s+)\$?\s*\d+(?:\.\d+)?(?!\s*x\b)',   # prices
    r'\b(?:apy|apr|yield|rates?|interest|returns?|fees?|slippage)\b(?:\s+(?:is|was|of|at|around|about|=|:|~))*\s*\d+(?:\.\d+)?\s*(?:%|percent)'
    r'|\b\d+(?:\.\d+)?\s*(?:%|percent)\s*(?:apy|apr|yield|interest|fees?|slippage)\b',   # rates
    r'(?<![\w.$])\d+\s+(?:times|steps?|transactions?|txs?|txns?|days?|hours?|hrs?|minutes?|mins?|weeks?|months?|years?|markets?|wallets?|accounts?'
    r'|people|friends|recipients|addresses|ways|parts?|pieces)\b',           # counts
]
_NOT_AMOUNT_RE = [re.compile(x, re.I) for x in _NOT_AMOUNT]


_PRICE_RE = _NOT_AMOUNT_RE[5]


def _mask(text, prices=True, accts=True):
    t = str(text or '')
    ref, acct = _words()[:2]
    for rx in [ref, acct] + _NOT_AMOUNT_RE:           # reference and account numbers first, then the list above
        if (prices or rx is not _PRICE_RE) and (accts or rx is not acct):
            t = rx.sub(lambda m: ' ' * len(m.group(0)), t)
    return t


def first(text):
    """The first plain number of ONE clause that can be its amount (round 2: the multi-step readers' "first number"). Rates
    ("5% apy"), counts ("3 days"), leverage, dates, times, names, reference and account numbers are passed over when another
    number stands in the clause ("for 3 days lend 5 on navi" is 5, it was 3). They only decide BETWEEN numbers: the one
    number of a clause is still read as before ("swap 5 wallet SUI", "from my wallet 20 usdc"), so no amount is ever lost to
    "all of it". Price words stay readable ("send over 5 usdc" is 5); a clause that names two amounts is asked about before
    (clause_conflict). A float, or None."""
    m = PLAIN.search(_mask(text, prices=False)) or PLAIN.search(re.sub(r'0x[0-9a-fA-F]+', ' ', str(text or '')))
    return float(m.group(1)) if m else None


def plain_text(text):
    """A clause with the numbers that are never amounts (dates, times, leverage, rates, names, addresses, reference numbers)
    blanked out — same length, so a reader's own pattern keeps its positions ("on 2026-11-20 I've got 80 USDC in my wallet")."""
    t = str(text or '')
    for rx in [_words()[0]] + [x for x in _NOT_AMOUNT_RE if x is not _PRICE_RE and x is not _NOT_AMOUNT_RE[-1]]:   # counts stay: "5 wallet SUI"
        t = rx.sub(lambda m: ' ' * len(m.group(0)), t)
    return t


def first_or_word(text, words=('all', 'everything', 'max', 'half')):
    """The first amount of a one-step request as the card readers take it: a number (as typed) or one of the amount words,
    whichever comes first. A count, a date, a time, leverage, a name, a reference or an account number is passed over when a
    real amount stands in the sentence ("on 2026-11-20 put 5 usdc into perps" is 5, it was 2026); alone it is read as before.
    A string, or None."""
    rx = re.compile(r'(?i)(?<![\w.])(%s|%s)(?![\w.])(\s*%%)?' % (NUM, '|'.join(words)))
    m = rx.search(_mask(text, prices=False)) or re.search(r'(?i)\b(%s|%s)\b(\s*%%)?' % (NUM, '|'.join(words)), str(text or ''))
    return (m.group(1) + ('%' if m.group(2) else '')) if m else None   # "50% of my sui" stays "50%" — a share, never 50 coins (RDR-V5)


def usd_first(text):
    """The first $ amount of a clause as typed ("5", "0.5") — never a price ("at $0.8", "limit $0.8"). None when there is none."""
    return next((_plain(a['v']) for a in amounts(text) if a['kind'] == 'usd'), None)


_LIMIT_PX_RE = re.compile(r'(?i)(?:\blimit\s+(?:order\s+)?(?:price\s+)?(?:of\s+|at\s+)?|\bat\s+(?:a\s+price\s+of\s+)?|@\s*)\$?\s*(%s)(?!\s*(?:x\b|%%)|[\w.])' % NUM)


def limit_price(text):
    """The limit price typed with an order ("at $0.8", "limit 0.8", "@ 0.8") as typed, or None — so it is never taken for the size."""
    m = _LIMIT_PX_RE.search(str(text or ''))
    return m.group(1) if m else None


class Dollars(float):
    """A step amount that was typed in dollars ("send $3 of it"): a plain number for every reader, and still known to be dollars."""


def first_dollars(text):
    """first(), marked as Dollars when that first number is a $ amount ("send $3 to bob.sui", "send 3 dollars of it")."""
    n = first(text)
    usd = next((a['v'] for a in amounts(text) if a['kind'] == 'usd'), None)
    return Dollars(n) if n is not None and usd is not None and abs(usd - n) < 1e-12 else n


def several(text):
    """True for a request with more than one action or more than one recipient (the multi-step and multi-send readers)."""
    t = str(text or '')
    return len(re.findall(r'\b%s\b' % _ONE_VERB, t, re.I)) > 1 or len(set(x.lower() for x in re.findall(RCPT, t, re.I))) > 1


def dollars_ask(msg, usd, coin, px):
    """Round 2: a $ amount on a step of a multi-step request that moves a coin which is not a dollar coin ("take 10 sui from my
    wallet, send $3 of it to bob.sui" sent 3 SUI). None when the coin is a dollar coin ($3 of USDC is 3 USDC); else a choice
    card with the amount in coins at the live price (the user confirms the reading), or a plain ask when it can't be priced
    or placed."""
    from .readguard import G_STABLE
    if coin in G_STABLE:
        return None
    n = _plain(usd)
    hits = list(re.finditer(r'\$\s*%s(?![\w.])(?:\s+(?:worth\s+)?of\s+(?:it|that|this|them))?|(?<![\w.])%s\s*(?:usd|dollars?|bucks)\b(?:\s+(?:worth\s+)?of\s+(?:it|that|this|them))?'
                            % (re.escape(n), re.escape(n)), str(msg or ''), re.I))
    if not px or len(hits) != 1:
        return {'text': 'I can\'t tell how much %s "$%s" is in that step. Say it in coins, e.g. "1.5 %s".' % (disp(coin), n, disp(coin))}
    units = _plain(round(float(usd) / float(px), 6))
    ask = msg[:hits[0].start()] + '%s %s' % (units, coin) + msg[hits[0].end():]
    return {'text': '"$%s" in that step is about %s %s at the live price ($%s). Use that?' % (n, units, disp(coin), _plain(round(float(px), 6))),
            'widget': {'type': 'choose', 'options': [{'label': '%s %s' % (units, disp(coin)), 'sub': '≈ $%s' % n, 'ask': ' '.join(ask.split())}]}}


def first_pct(text):
    """The first share of ONE clause ("50%", "50 %") as 0.5 — never a rate ("at 5% apy"). None when there is none."""
    m = re.search(r'(%s)\s*%%' % NUM, _mask(text, prices=False))
    return float(m.group(1)) / 100 if m else None


def first_coin(text):
    """The first amount of ONE clause that sits on a coin word ("I've got 80 USDC from my wallet"): (80.0, 'USDC' as typed,
    the position right after the coin word), or None."""
    a = next((a for a in amounts(text) if a['kind'] == 'units' and a['cw']), None)
    return (a['v'], a['cw'], a['e']) if a else None


def amounts(text):
    """Every money amount in a sentence, in order: [{'v': 5.0, 'kind': 'units' | 'usd' | 'pct', 'coin': 'USDC' or None,
    's': start, 'e': end, 'cw': the coin word when it follows the number}]. A number counts when it sits on a coin or a
    money noun ("10 sui", "10 of my usdc", "usdc 10"), right after an action verb ("send 5 to …"), next to another amount
    ("5-10 usdc", "5 10 usdc"), or is a $ amount. Addresses, names, dates, times, leverage ("3x"), prices ("at 0.9"),
    rates, reference and account numbers, and counts ("3 times") are not amounts."""
    t, raw = _mask(text), str(text or '')
    AMT_VERB, MONEY_NOUNS = _words()[2:]
    out = []
    for m in re.finditer(r'(?<![\w.,$])(\$\s*)?(%s)(?![\w]|\.\d)(\s*(?:%%|percent\b))?' % NUM, t):
        before, after = t[:m.start()], t[m.end():]
        usd_w = re.match(r'\s*(?:(?:usd|dollars?|bucks)\b(?!\s*c)|\$(?![\w$]|\s?\d))', after, re.I)
        usd_b = None if m.group(3) or usd_w else re.search(r'(?i)(?<![\w$.])(?:us\s?\$|usd)\s*$', before)   # "usd 5", "US$ 5"
        if usd_b and re.search(r'\d\s*$', before[:usd_b.start()]):
            usd_b = None                              # "10 usd 5 sui": the usd belongs to the 10
        a = {'v': float(m.group(2)), 's': m.start(), 'e': m.end(), 'ne': m.end(2), 'coin': None, 'cw': None}
        if m.group(1) or usd_w or usd_b:
            a['kind'] = 'usd'
            if usd_w:
                a['e'] += usd_w.end()
            if usd_b:
                a['s'] = usd_b.start()
            of = re.match(r'\s+(?:worth\s+)?(?:of\s+)?(?:(?:my|the|your)\s+)?(' + _WORD + r')\b', t[a['e']:])
            a['coin'] = _coin(of.group(1)) if of else None
            out.append(a)
            continue
        nx = re.match(r'(\s+(?:of\s+)?(?:(?:my|the|your|our|this|that)\s+)?)(' + _WORD + r')\b', after)
        noun = bool(nx and nx.group(2).lower() in MONEY_NOUNS | {'it', 'them'})
        verb = bool(re.search(r'\b%s\s+(?:me\s+|it\s+|them\s+|only\s+|just\s+|about\s+|around\s+)?$' % AMT_VERB, before, re.I))
        if m.group(3):                                # 'ctx': the share sits on a coin, money or a verb ("100 % sure" does not);
            a['kind'] = 'pct'                         # 'pc': its coin ("50% of my usdc") — for the AI gates (said())
            pv = re.search(r'\b(' + _WORD + r')\s+$', before)
            a['pc'] = (_coin(nx.group(2)) if nx else None) or (_coin(pv.group(1)) if pv and not noun else None)
            a['ctx'] = bool(a['pc'] or noun or verb)
            out.append(a)
            continue
        a['kind'] = 'units'
        if nx and _coin(nx.group(2)):
            a['coin'], a['cw'] = _coin(nx.group(2)), nx.group(2)
            a['e'] += nx.end()
        pv = re.search(r'\b(' + _WORD + r')\s+$', before)
        if m.group(2) == '2' and pv and _coin(pv.group(1)) and re.match(r'\s+(?:0x|@|[\w-]+\.sui\b|' + _WORD + r'\b)', raw[m.end():]):
            continue                                  # "usdc 2 bob.sui", "usdc 2 sui": chat shorthand for "to", not an amount
        if not a['coin'] and pv and not noun:
            a['coin'] = _coin(pv.group(1))                # "usdc 10"
        near = bool(out) and bool(re.fullmatch(r'\s*(?:[-\u2013+x\u00d7*~]|,?\s*or(?:\s+(?:maybe|rather|even|perhaps))?\b)?\s*', t[out[-1]['e']:m.start()], re.I))
        if near and out[-1]['kind'] == 'usd':         # "$5-10 of sui", "$5 or 10": dollars like the amount beside it (round 2)
            a['kind'], a['cw'], a['e'] = 'usd', None, a['ne']
        # a bare number right in front of an amount on a coin ("… and 3 4 sui to carol.sui"): one of two amounts ('weak': the AI
        # gates do not take it for a typed amount)
        ahead = re.match(r'\s*(?:[-\u2013~]\s*)?\$?%s(?![\w]|\.\d)\s+(?:of\s+)?(?:my\s+)?(%s)\b' % (NUM, _WORD), after)
        if ahead and _coin(ahead.group(1)) and not (a['coin'] or noun or near or verb):
            a['weak'] = True
            out.append(a)
            continue
        if a['coin'] or noun or near or verb:         # near: "5-10 usdc", "5 10 usdc", "5 usdc or 10" (round 2)
            out.append(a)
    return out


def _show(a, coin=None):
    """An amount as the reply says it: "$5", "5%", "5 USDC", "5"."""
    n = _plain(a['v'])
    c = coin or a.get('coin')
    return '$' + n if a['kind'] == 'usd' else n + '%' if a['kind'] == 'pct' else n + (' ' + disp(c) if c else '')


def _typed(a, coin_word=None):
    """The same amount as words a reader reads back."""
    n = _plain(a['v'])
    return '$' + n if a['kind'] == 'usd' else n + '%' if a['kind'] == 'pct' else n + (' ' + coin_word if coin_word else '')


# Words that take an amount back. "no" alone counts only between two numbers ("50 no 5") or after a comma or dash.
_MARK = (r"i\s+mean(?:t)?|sorry|oops|whoops|no\s+wait|wait\s+no|wait|actually|make\s+(?:that|it)|correction|or\s+rather|rather|typo"
         r"|scratch\s+that|my\s+bad|nope")
_FILL = r"(?:(?:i\s+mean(?:t)?|make\s+(?:that|it)|it'?s|it\s+is|just|only|should\s+be|change\s+(?:that|it)\s+to)\b[\s,:]*)*"
_AMT = r'\$?\s*%s' % NUM
_CORR_A_RE = re.compile(
    r'(?<![\w.,$])(?P<a>' + _AMT + r')(?P<ap>\s*%)?(?P<ac>\s+(?:of\s+)?(?:my\s+)?' + _WORD + r')??\s*(?:[,;\u2014\u2013-]|\.{2,}|\u2026)?\s*\(?\s*'
    r'(?:' + _MARK + r'|no)\b[\s,:]*' + _FILL + r'(?P<b>' + _AMT + r')(?![\w]|\.\d)(?P<bp>\s*%)?(?P<bc>\s+(?:of\s+)?(?:my\s+)?' + _WORD + r')?', re.I)
_CORR_B_RE = re.compile(
    r'^(?P<head>.*?\d.*?)(?P<cut>\s*)(?:(?P<p>[,;\u2014\u2013-]|\.{2,}|\u2026|\()\s*)?(?:(?:' + _MARK + r')|(?P<weak>no))\b[\s,:]*' + _FILL +
    r'(?P<b>' + _AMT + r')(?![\w]|\.\d)(?P<bp>\s*%)?(?:\s+(?P<bc>' + _WORD + r'))?\s*\)?\s*[.!]*\s*$', re.I | re.S)


CHANGED = ('', '')                                   # correction(): the amount AND its coin were changed — a changed order, not read here
# A bare "no" with no comma or dash before it ("send 5 usdc to bob.sui no 3") takes the amount back only when the new amount
# ends the sentence (the pattern) and the word before it is not one that "no 3" belongs to ("room no 3", "with no 3 …").
_NO_NOUN_RE = re.compile(r'(?i)\b(?:room|house|flat|apt|apartment|unit|number|lot|box|gate|door|floor|level|table|seat|block|version|step|option|plan'
                         r'|part|page|line|item|or|with|and|has|have|is|are|was|be|says?|said|if|but|there|yes|of|for)\s*$')


def correction(text):
    """An amount the user took back: "send 50, i mean 5 usdc to X", "lend 50 no 5 usdc", "long btc $500 no $5 3x",
    "send 50 usdc to bob.sui, sorry 5". Returns (the sentence with only the corrected amount, "Read as 5 USDC."), None when
    there is no correction, or CHANGED when the correction also names another coin ("send 50 usdc, i mean 5 sui": both were
    sent). Only an amount is corrected here: a changed coin, place or recipient stays with the read guard, which refuses to guess."""
    t0 = str(text or '')
    t = _mask(t0)
    m = _CORR_A_RE.search(t)
    if m and t0[m.start():m.end()] == m.group(0):     # the two amounts stand side by side (nothing blanked out between them)
        word = lambda g: re.sub(r'^\s+(?:of\s+)?(?:my\s+)?', '', g or '', flags=re.I)
        ac, bc = word(m.group('ac')), word(m.group('bc'))
        if ac and not _coin(ac):
            return None
        end = m.end()
        if bc and not _coin(bc):                      # "… no 5 to bob.sui": the word after the number is not its coin
            bc, end = '', m.start('bc')
        if ac and bc and _coin(ac) != _coin(bc):
            return CHANGED if re.search(r'(?:%s)\b' % _MARK, m.group(0), re.I) else None
        usd = '$' in m.group('a') or '$' in m.group('b')
        if bool(m.group('ap')) != bool(m.group('bp')) or (usd and (ac or bc)):
            return None
        n = _plain(re.sub(r'[$\s]', '', m.group('b')))
        cw = bc or ac
        amt = ('$' + n) if usd else n + ('%' if m.group('bp') else '') + ((' ' + cw) if cw else '')
        tail = re.sub(r'^\s*\)', '', t0[end:]) if '(' in m.group(0) else t0[end:]
        new = t0[:m.start()] + amt + ('' if not tail or tail[0] in ' ,.;!?)' else ' ') + tail
        said = ('$' + n) if usd else n + ('%' if m.group('bp') else '') + ((' ' + disp(_coin(cw))) if cw else '')
        return ' '.join(new.split()), 'Read as %s.' % said
    m = _CORR_B_RE.match(t)
    if m and not (m.group('weak') and not m.group('p') and _NO_NOUN_RE.search(t[:m.end('cut')])):
        head = t0[:m.end('cut')].rstrip()
        ams = amounts(head)
        if m.group('weak') and len(ams) > 1 and len(re.findall(r'\b%s\b' % _ONE_VERB, head, re.I)) > 1:
            return CHANGED                            # "withdraw 100 … and send 50 to bob.sui no 30": which amount? never guessed
        bc = m.group('bc') or ''
        usd = '$' in m.group('b')
        if len(ams) == 1 and bc and _coin(bc) and ams[0]['coin'] and _coin(bc) != ams[0]['coin']:
            return CHANGED
        if len(ams) != 1 or (bc and not _coin(bc)):
            return None
        a = ams[0]
        if (a['kind'] == 'pct') != bool(m.group('bp')) or (usd and a['kind'] != 'usd') or (a['kind'] == 'usd' and bc):
            return CHANGED if m.group('weak') and not m.group('p') else None   # "lend 5 usdc on navi no 6%": coins or a share? never guessed
        b = dict(a, v=float(re.sub(r'[$\s]', '', m.group('b'))))
        new = head[:a['s']] + _typed(b, a['cw'] or (bc if not a['coin'] else None)) + head[a['e']:]
        return ' '.join(new.split()), 'Read as %s.' % _show(b, a['coin'] or _coin(bc))
    return None


_ONE_VERB = r'(?:swap|convert|trade|exchange|send|sned|snd|trasnfer|tranfer|trnasfer|transfer|pay|give|lend|supply|deposit|earn|put|withdraw|redeem|move|shift|buy|sell|long|short|stake|unstake)'
_MANY_RE = re.compile(r'\b(?:then|rest|remaining|remainder|each|every|split|between|keep|leave|except|plus|minus|both|respectively|per|twice|have|hold'
                      r'|got|own|worth|another|more|after|before|first|next|also)\b', re.I)
_TARGET_RE = re.compile(r'\b(?:to|into|onto|on|in|at|from|for|with|of|out)\b', re.I)


_BETWEEN_RE = re.compile(r'(?i)\bbetween\s+(?=\$?\s*%s\s*(?:%%\s*|[a-z][a-z0-9]{1,9}\s+)?and\s+\$?\s*\d)' % NUM)


_TIGHT_RE = re.compile(r'(?i)\s*(?:[-\u2013~]|or(?:\s+(?:maybe|rather|even|perhaps))?\b)?\s*')


def _pair(seg):
    """Two amounts of one kind that stand for ONE action in seg (a whole one-step request, or one clause of a multi-step one):
    (the amounts, the choices, the coin, the coin word) or None. Amounts that each have their own place ("5 usdc on navi and
    3 usdc on suilend") are not a pair; "5 to 10 usdc" (a range), "5+5" and "5 x 3" are."""
    t, ams = _mask(seg), amounts(seg)
    for kind in ('usd', 'units', 'pct'):
        g = [a for a in ams if a['kind'] == kind]
        vals = list(dict.fromkeys(a['v'] for a in g))
        coins = {a['coin'] for a in g if a['coin']}
        gaps = [t[g[i]['e']:g[i + 1]['s']] for i in range(len(g) - 1)]
        gaps = [('to' if re.search(RCPT, seg[g[i]['e']:g[i + 1]['s']], re.I) else '') + x for i, x in enumerate(gaps)]   # "send A 5 usdc and B 3 usdc": B is
        # the second amount's place (QA 2026-11-17, RDR-V11: both options sent to A)
        calc = len(g) == 2 and re.fullmatch(r'\s*[+x\u00d7*]\s*', gaps[0])        # "5+5", "5 x 5": one value typed twice, still two readings
        if (len(vals) < 2 and not calc) or len(coins) > 1 or len(g) > 3:
            continue
        rng = len(g) == 2 and not g[0]['cw'] and re.fullmatch(r'\s*to\s*', gaps[0], re.I)   # "5 to 10 usdc": a range (round 2)
        if any(_TARGET_RE.search(x) for x in gaps) and not rng:
            # "lend 5 usdc on navi and 3 usdc on suilend": each amount has its own place — unless two of them stand side by
            # side with nothing between ("lend 5 6 usdc on navi and 5 usdc on suilend" lent 5 and 5; round 2)
            i = next((i for i, x in enumerate(gaps) if _TIGHT_RE.fullmatch(x) and g[i]['v'] != g[i + 1]['v']
                      and len({c for c in (g[i]['coin'], g[i + 1]['coin']) if c}) < 2), None)
            if i is None:
                continue
            g = g[i:i + 2]
            return g, [dict(g[0], v=a['v']) for a in g], g[0]['coin'] or g[1]['coin'], g[0]['cw'] or g[1]['cw']
        picks = [dict(g[0], v=v) for v in vals]
        if len(g) == 2 and re.fullmatch(r'\s*\+\s*', gaps[0]):
            picks.append(dict(g[0], v=round(g[0]['v'] + g[1]['v'], 9)))      # "5+3": maybe the sum
        if len(g) == 2 and re.fullmatch(r'\s*[x\u00d7*]\s*', gaps[0]):
            picks.append(dict(g[0], v=round(g[0]['v'] * g[1]['v'], 9)))      # "5 x 3": maybe the product
        return g, picks, next(iter(coins), None), next((a['cw'] for a in g if a['cw']), None)
    return None


def _pair_card(whole, at, seg, two):
    """The choice card for _pair(): every option is the WHOLE request with the one amount (seg starts at `at` in whole)."""
    g, picks, coin, cw = two
    mk = lambda a: ' '.join((whole[:at + g[0]['s']] + _typed(a, cw) + whole[at + g[-1]['e']:]).split())
    return {'text': 'I see two amounts — %s and %s. Which one?' % (_show(picks[0], coin), _show(picks[1], coin)),
            'widget': {'type': 'choose', 'options': [{'label': _show(a, coin), 'ask': mk(a)} for a in picks]}}


_ORDER_SPLIT_RE = re.compile(r'(?i)\s*(?:,|;|&|\band\b|\bthen\b)\s*(?=(?:then\s+)?(?:go\s+|open\s+(?:an?\s+)?)?(?:long|short)\b)')


def clause_conflict(text):
    """Round 2: two amounts inside ONE clause of a multi-step request ("withdraw 100 usdc from navi and send 20 30 usdc to
    bob.sui" sent 20; "swap 5 sui to usdc and lend 2 3 usdc on navi" took 7 SUI). The same choice card as conflict(), each
    option the whole request with one amount. Also a list of sends ("…, 2 sui to alice.sui and 3 4 sui to carol.sui"). None for
    a one-step request to one place (conflict() has it) or when every clause is fine."""
    t0 = str(text or '')
    if len(re.findall(r'\b%s\b' % _ONE_VERB, t0, re.I)) < 2 and len(set(x.lower() for x in re.findall(RCPT, t0, re.I))) < 2:
        return None
    from .chains import CHAIN_SPLIT                   # the multi-step reader's own clause breaks, and an order after a money step
    ends = sorted((m.start(), m.end()) for rx in (CHAIN_SPLIT, _ORDER_SPLIT_RE) for m in rx.finditer(t0))
    cuts, pos = [], 0
    for a, b in ends:
        if a >= pos:
            cuts.append((pos, a))
            pos = b
    cuts.append((pos, len(t0)))
    for s, e in cuts:
        seg = t0[s:e]
        if _MANY_RE.search(seg) or len(re.findall(r'\b%s\b' % _ONE_VERB, seg, re.I)) > 1:
            continue                                  # "5 usdc plus 3 from my wallet", "split 100 usdc 30 70 …": their own readers
        two = _pair(seg)
        if two:
            return _pair_card(t0, s, seg, two)
    return None


_EURO_RE = re.compile(r'(?<![\w.,/:#-])(\$?)([1-9]\d{0,2})\.(\d\d0)(?![\w]|[.,]\d)')


def unsure(text):
    """Round 2: "swap 1.000 sui" — one, or one thousand written the European way? A number with exactly three decimals that
    end in 0 ("1.000", "2.500", "12.340") is asked about, never guessed (it was read as 1). None when no amount is written
    that way, or the user says the dot is a decimal point."""
    t0 = str(text or '')
    if re.search(r'(?i)\bdecimal', t0):
        return None
    ams = [a for a in amounts(t0) if a['kind'] in ('units', 'usd')]
    for m in _EURO_RE.finditer(_mask(t0)):
        a = next((a for a in ams if a['s'] <= m.start() < a['e']), None)
        if not a:
            continue
        if re.match(r'[ \'\u00a0\u202f]\d{3}(?!\d)', t0[m.end():]):
            return {'text': 'I can\'t read "%s" as an amount. Type it as one number, e.g. "1000000".' % t0[m.start():m.end() + 4]}
        small, big = _plain(float(m.group(2) + '.' + m.group(3))), m.group(2) + m.group(3)
        coin = disp(a['coin']) if a['coin'] and a['kind'] == 'units' else ''
        lab = lambda n: (m.group(1) + n + (' ' + coin if coin else '')).strip()
        mk = lambda n: t0[:m.start()] + m.group(1) + n + t0[m.end():]
        return {'text': 'Is "%s" %s or %s? I won\'t guess between a decimal point and a thousands dot.' % (m.group(0), lab(small), lab(big)),
                'widget': {'type': 'choose', 'options': [{'label': lab(small), 'ask': mk(small)}, {'label': lab(big), 'ask': mk(big)}]}}
    return None


_HELD_RE = re.compile(r"(?i)\b(?:have|has|had|hold|holding|own|got)\s+(?:(?:about|around|only|just|like|roughly)\s+)?\$?\s*$")
_FACT_RE = re.compile(r"(?i)\b((?:[a-z']+\s+){0,3})(?:is|are|was|were|costs?|cost\s+me)\s+(?:(?:about|around|only|just|roughly)\s+)?\$?\s*$")
_ORDER_NOUN_RE = re.compile(r'(?i)\b(?:amounts?|size|margin|collateral|order|total|sum|quantity|qty|stake|deposit|budget|it|that|this|number|figure|answer)\b')


_BOTH_RE = re.compile(r'(?i)^\s*(swap|convert|exchange|trade|sell)\s+(%s)\s+(?:of\s+)?(?:my\s+)?(%s)\s+(to|into|for)\s+(%s)\s+(%s)\s*[.!]*\s*$'
                      % (NUM, _WORD, NUM, _WORD))


def two_sided(text):
    """Round 2: "swap 5 sui for 10 usdc" — an amount on BOTH sides of one swap (it swapped 5 SUI and dropped the 10). A swap
    takes one amount: spend 5 SUI, or get about 10 USDC? A choice card, or None."""
    m = _BOTH_RE.match(str(text or ''))
    if not m or not _coin(m.group(3)) or not _coin(m.group(6)) or _coin(m.group(3)) == _coin(m.group(6)):
        return None
    v, a, ca, to, b, cb = m.groups()
    return {'text': 'A swap takes one amount — spend %s %s, or get about %s %s?' % (a, disp(_coin(ca)), b, disp(_coin(cb))),
            'widget': {'type': 'choose', 'options': [{'label': 'Spend %s %s' % (a, disp(_coin(ca))), 'ask': '%s %s %s to %s' % (v, a, ca, cb)},
                                                     {'label': 'Get about %s %s' % (b, disp(_coin(cb))), 'ask': '%s %s to %s %s' % (v, ca, b, cb)}]}}


def said(text):
    """What the user typed as ORDER amounts, for the AI's gates (review scan F8): {'typed': [numbers of coins], 'usd': [$ amounts],
    'pct': [shares], 'pairs': [(value, is a share, its coin or None)]}. One reading with amounts(), and two kinds of numbers are
    left out: a fact about the world ("my rent is 100 dollars" opened a $100 order) — always; a holding ("i have 150 usdc") —
    when a real order amount stands beside it ("i have 150 usdc. swap 2 usdc to sui" let 150 pass)."""
    t0 = str(text or '')
    rows = []
    for a in amounts(t0):
        if (a['kind'] == 'pct' and not a.get('ctx')) or a.get('weak') or re.match(r'\.\s*\S', t0[a['ne']:]):
            continue                                  # "100 % sure"; "send 10. sui": a number cut off at its dot is not a typed amount
        before = t0[:a['s']]
        f = _FACT_RE.search(before)
        why = 'held' if _HELD_RE.search(before) else 'fact' if f and not _ORDER_NOUN_RE.search(f.group(1)) else None
        rows.append((a, why))
    real = any(why is None for _, why in rows)
    keep = [a for a, why in rows if why is None or (why == 'held' and not real)]
    return {'typed': [a['v'] for a in keep if a['kind'] == 'units'], 'usd': [a['v'] for a in keep if a['kind'] == 'usd'],
            'pct': [a['v'] for a in keep if a['kind'] == 'pct'],
            'pairs': [(a['v'], a['kind'] == 'pct', a['coin'] or a.get('pc')) for a in keep if a['kind'] != 'usd']}


def all_then_fixed(src, perps, others, est, swaps=None):
    """Round 2: "withdraw all my usdc from navi and deposit 50 to perps" — everything comes out, exactly the typed amount goes
    to perps and the rest stays in the wallet. Returns the amount the KEEP step takes (the perps deposit goes last and takes
    what is left), 0 when the typed amount is the whole position, or None when this is not that request: another step wants
    the rest ("…and lend the rest"), the start is not one whole withdrawal, or the amount is more than is held (the caller
    keeps its refusal, which says the amount held)."""
    if swaps or not (src and src.get('op') == 'withdraw' and src.get('amount') == 'all' and not src.get('more') and not src.get('plus')):
        return None                                   # after a swap the typed amount is in another coin than the start (the caller refuses)
    if perps['amount'][0] != 'units' or len(others) != 1 or others[0]['op'] != 'keep' or others[0]['amount'][0] != 'rest' or others[0]['amount'][1]:
        return None
    want, have = float(perps['amount'][1]), float(est or 0)
    if want <= 0 or want > have + 1e-9:
        return None
    return max(int((have - want) * 1e6) / 1e6, 0.0)       # cut to 6 decimals, never rounded up: perps gets at least what was typed


def conflict(text):
    """Two different amounts for ONE action with no correction word between them ("send 5-10 usdc", "send 5 10 usdc",
    "swap 5 sui 10 sui to usdc", "long btc $10 and $500 3x", two leverages): a choice card that asks which one — never the
    first. Also a fraction that stands on no coin ("send 1/2 to …") in a one-step request. None when the sentence is fine,
    or has several steps or recipients (their own readers pair each amount with its part)."""
    t0 = _BETWEEN_RE.sub('', str(text or ''))           # "between 5 and 10 usdc": a range of amounts, not a split between places
    if len(re.findall(r'\b%s\b' % _ONE_VERB, t0, re.I)) != 1 or _MANY_RE.search(t0) or len(set(x.lower() for x in re.findall(RCPT, t0, re.I))) > 1:
        return None
    fr = _FRAC_LEFT_RE.search(re.sub(r'0x[0-9a-fA-F]+', ' ', t0))
    if fr and int(fr.group(1)) < int(fr.group(2)):
        dec_, pct_ = _plain(round(int(fr.group(1)) / int(fr.group(2)), 9)), _plain(round(100.0 * int(fr.group(1)) / int(fr.group(2)), 4))
        return {'text': 'I can\'t tell what "%s" is a fraction of. Say it as an amount of the coin (e.g. "%s SUI") or as a share of what you '
                        'have (e.g. "%s%% of my SUI").' % (fr.group(0).strip(), dec_, pct_)}
    two = _pair(t0)
    if two:
        return _pair_card(t0, 0, t0, two)
    if re.search(r'\b(?:long|short)\b', t0, re.I):
        lv = list(re.finditer(r'(?<![\w.])(%s)\s*x\b' % NUM, t0, re.I))
        vals = list(dict.fromkeys(float(x.group(1)) for x in lv))
        if len(vals) > 1:
            mk = lambda v: ' '.join((t0[:lv[0].start()] + _plain(v) + 'x' + t0[lv[-1].end():]).split())
            return {'text': 'I see two leverages — %sx and %sx. Which one?' % (_plain(vals[0]), _plain(vals[1])),
                    'widget': {'type': 'choose', 'options': [{'label': _plain(v) + 'x', 'ask': mk(v)} for v in vals]}}
    return None


def lone_step(src, sink, swaps, hand_name):
    """R1: the ONE last step of a multi-step request and the amount typed on it. "withdraw 100 usdc from navi and send 50 to
    bob.sui" sent all 100: the step took "the rest" whatever was typed. Returns ('rest', None) when the step takes everything
    (no amount typed, or the same amount as the start), ('keep', None) when the typed amount stands and the rest stays in the
    wallet, ('start', n) when the start becomes n (no start amount typed, or a larger amount taken from the wallet),
    ('swap', None) after a swap (the caller sizes it), or ('refuse', text) when it is more than the start."""
    kind, amt = sink['amount']
    if sink['op'] == 'keep' or kind not in ('units', 'frac') or not amt:
        return 'rest', None
    if kind == 'frac':
        return ('keep', None) if amt < 1 - 1e-9 else ('rest', None)
    if swaps:
        return 'swap', None                            # after a swap the amount is in the new coin: the caller sizes it (review P-23)
    plain = src.get('op') in ('wallet', 'withdraw') and not src.get('more') and not src.get('plus')
    start = float(src['amount']) if isinstance(src.get('amount'), (int, float)) else None
    extra = [x.get('amount') if isinstance(x.get('amount'), (int, float)) else x.get('est') for x in src.get('more') or []]
    if start is not None and extra and all(isinstance(e, (int, float)) for e in extra):
        start += sum(float(e) for e in extra)        # "withdraw 100 … from navi and 50 from suilend": the step starts with 150 (RDR-V20)
    limit = float(src.get('max') or 0) if src.get('pick') else (start if start is not None else float(src.get('est') or 0))
    if limit and float(amt) > limit * (1 + 1e-9) + 1e-12:
        return 'refuse', ('You asked to %s %s %s, but that step starts with %s %s. Say one amount — e.g. "%s it" for all of it, or a smaller amount.'
                          % (sink['op'] if sink['op'] != 'perps' else 'deposit', _plain(amt), hand_name, _plain(limit), hand_name,
                             sink['op'] if sink['op'] != 'perps' else 'deposit'))
    if plain and (src.get('pick') or (src.get('op') == 'wallet' and start is not None and float(amt) < start - 1e-9)):
        return 'start', float(amt)                     # "withdraw my usdc from navi and send 50 to bob.sui": withdraw the 50; "take 20 usdc from
                                                       # my wallet and deposit 5 to perps": take the 5 (a kept wallet part never leaves the wallet)
    if start is not None and abs(start - float(amt)) < 1e-9:
        return 'rest', None
    return 'keep', None


def bare_after_swap(msg, pieces, src_coin, coin_of=None):
    """R6: "swap 10 sui to usdc and send 3 to bob.sui" took 13 SUI and sent 3 SUI. A part after a swap whose amount names no
    coin is asked about: 3 of the coin just bought or 3 of the start coin? pieces = the pieces reader's parts in the order
    typed, each with its clause in 'cl'. A choice card, or None."""
    got = next((p['coin'] for p in pieces if p.get('coin') and p['coin'] != src_coin), None)
    bare = [p for i, p in enumerate(pieces) if i and got and p['amt'] and p['amt'][0] == 'units' and not p.get('acoin')
            and p.get('coin') in (None, src_coin) and any(q.get('coin') == got for q in pieces[:i])
            and not any((coin_of or _coin)(w) for w in re.findall(_WORD, _mask(p.get('cl') or '')))]   # "send the leftover usdc 0.1 to …" names its coin
    if not bare:
        return None
    n0 = _plain(bare[0]['amt'][1])
    text = '%s of which coin — the %s from the swap, or %s?' % (n0, disp(got), disp(src_coin))
    cuts = []
    for p in bare:
        c = p.get('cl') or ''
        n = re.search(r'(?<![\w.$])(%s)(?![\w.])' % NUM, _mask(c))
        if not n or not c or msg.count(c) != 1:
            return {'text': text + ' Say it with the coin, e.g. "%s %s".' % (n0, disp(got))}
        cuts.append((c, n.end()))

    def opt(coin):
        out = msg
        for c, at in cuts:
            out = out.replace(c, c[:at] + ' ' + coin + c[at:])
        return out
    return {'text': text, 'widget': {'type': 'choose', 'options': [{'label': '%s %s' % (n0, disp(x)), 'ask': opt(x)} for x in (got, src_coin)]}}


# QA 2026-11-17 (RDR-V3, V12, V20): amount words the readers can't turn into one number. Kivy asks; a card never takes "all" for them.
_VAGUE = (r"some|a\s+(?:little\s+)?bit|(?:only\s+)?a\s+little|part\s+of\s+(?:it|that|them)|a\s+(?:small\s+|big\s+|large\s+|tiny\s+)?(?:part|portion|share|chunk|piece)"
          r"|a\s+few|a\s+couple|most\s+of|double|a\s+lot|lots|plenty|a\s+dozen|dozens")
_NUM_WORDS = (r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
              r"|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand")
_SHARES = (r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|\d+)\s+(?:thirds|quarters|fifths?|sixths?|sevenths?|eighths?|ninths?|tenths?|hundredths?)"
           r"|(?:two|three|four|five|six|seven|eight|nine|\d+)\s+(?:thirds?|quarters?)")
_UNREAD_RE = re.compile(r"(?i)(?<![\w.])(%s|%s|%s)(?![\w.])" % (_SHARES, _VAGUE, _NUM_WORDS))
_PICKER = re.compile(r"(?i)(?:some|a\s+(?:little\s+)?bit|(?:only\s+)?a\s+little|part\s+of\s+(?:it|that|them)|a\s+(?:small\s+|big\s+|large\s+|tiny\s+)?(?:part|portion|share|chunk|piece))$")
_REPEAT_RE = re.compile(r"(?i)\b(?:twice|thrice|(?:two|three|four|five|\d+)\s+times|\d+\s+(?:payments|instal{1,2}ments|transfers)\s+of|in\s+\d+\s+(?:payments|instal{1,2}ments|transfers))\b")


def unread(text, vague=True):
    """An amount word that is not one number ("some", "a bit", "a fifth", "three quarters", "two thirds", "ten", "a dozen"):
    the word as typed, or None. Never the coin bought by a swap ("…then to some usdc"). vague=False leaves "some / a bit /
    a little / part of it" alone (a step whose card opens with its own amount picker)."""
    t = str(text or '')
    for m in _UNREAD_RE.finditer(t):
        w = m.group(1)
        if not vague and _PICKER.match(w):
            continue
        if re.search(r'(?i)\b(?:to|into|for)\s+$', t[:m.start()]):
            continue                                  # "…then to some usdc": the coin bought, not an amount
        if re.match(r'(?i)[\s-]*(?:(?:best|top|different|other|separate|good)\s+)?(?:markets?|protocols?|places|vaults?|pools?|steps?|ways|parts|pieces|coins|times|days?|weeks?'
                    r'|months?|hours?|minutes?|mins?|people|friends|wallets?|accounts?|recipients|addresses|transactions?|check\w*)\b', t[m.end():]):
            continue                                  # "lend it on the two best markets", "double-check": a count or a word, not an amount
        return ' '.join(w.split())
    return None


_SHARE_NUM = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9}
_SHARE_DEN = {'half': 2, 'halves': 2, 'third': 3, 'thirds': 3, 'quarter': 4, 'quarters': 4, 'fifth': 5, 'fifths': 5, 'sixth': 6, 'sixths': 6,
              'seventh': 7, 'sevenths': 7, 'eighth': 8, 'eighths': 8, 'ninth': 9, 'ninths': 9, 'tenth': 10, 'tenths': 10}
_SHARE_WORD_RE = re.compile(r"(?i)(?<![\w.])(a|an|one|two|three|four|five|six|seven|eight|nine|\d)\s+(%s)(?![\w.])" % '|'.join(sorted(_SHARE_DEN, key=len, reverse=True)))


def share_word(text):
    """A share written in words ("a third", "two thirds", "three quarters", "a fifth", "2 thirds") as 0 < f < 1, or None — the
    multi-step reader takes it as that share of the step's coin (2026-11-17: 'read them', not ask)."""
    m = _SHARE_WORD_RE.search(str(text or ''))
    if not m:
        return None
    n = _SHARE_NUM.get(m.group(1).lower()) or int(m.group(1))
    d = _SHARE_DEN[m.group(2).lower()]
    return n / d if 0 < n < d else None


def frac(text):
    """A fraction written with digits that stands on no coin ("send 1/10 to bob.sui", "lend 2/5 on navi") as a share 0 < f < 1,
    any denominator to 99 (the multi-step reader knew one-digit ones only: "1/10" was read as 1). None when there is none."""
    m = _FRAC_LEFT_RE.search(re.sub(r'0x[0-9a-fA-F]+', ' ', str(text or '')))
    return int(m.group(1)) / int(m.group(2)) if m and int(m.group(1)) < int(m.group(2)) else None


def repeated(text):
    """"send 10 to bob.sui 3 times", "send bob.sui 10 twice", "3 payments of 10": one card is one transaction — the words as
    typed, or None."""
    m = _REPEAT_RE.search(str(text or ''))
    return ' '.join(m.group(0).split()) if m else None


_SHARE_OF_RE = re.compile(r'(?i)\b(half|a\s+third|a\s+quarter|a\s+fifth|a\s+tenth)\s+of\s+(\$?\s*%s)(\s+(?:my\s+)?%s)?\b' % (NUM, _WORD))
_LESS_RE = re.compile(r'(?i)(?<![\w.,$])(\$?%s)(\s+%s)?\s+(?:(less|minus|plus)\s+(\$?%s)(?=\s*(?:to\b|$|[,.;!?]))'
                      r'|(less|minus|plus|after|before|net\s+of|incl(?:uding|\.)?|excl(?:uding|\.)?)\s+(?:the\s+)?(?:network\s+)?(fees?|gas|tax(?:es)?)\b)' % (NUM, _WORD, NUM))


def modified(text):
    """Words that change a typed amount ("send half of 10 usdc", "send 10 usdc less 2", "…plus 2", "…minus fees") were dropped and
    the typed number sent (QA 2026-11-17, RDR-V12). Which amount? A choice card, a plain ask, or None."""
    t0 = str(text or '')
    m = _SHARE_OF_RE.search(t0)
    if m and len(re.findall(r'\b%s\b' % _ONE_VERB, t0, re.I)) > 1:
        m = None                                      # multi-step: "swap a third of 0.3 usdc …, lend the other 0.2" — the step reader takes the share (2026-11-17)
    if m and (not m.group(3) or _coin(m.group(3).split()[-1])):
        n = float(re.sub(r'[$\s]', '', m.group(2)))
        f = {'half': 0.5, 'a third': 1 / 3, 'a quarter': 0.25, 'a fifth': 0.2, 'a tenth': 0.1}[' '.join(m.group(1).lower().split())]
        usd = '$' in m.group(2)
        cw = m.group(3) or ''
        lab = lambda v: ('$' if usd else '') + _plain(round(v, 6)) + cw
        mk = lambda v: ' '.join((t0[:m.start()] + lab(v) + t0[m.end():]).split())
        return {'text': '%s of %s — which amount?' % (m.group(1).capitalize(), lab(n).strip()),
                'widget': {'type': 'choose', 'options': [{'label': lab(n * f).strip(), 'ask': mk(n * f)}, {'label': lab(n).strip(), 'ask': mk(n)}]}}
    m = _LESS_RE.search(t0)
    if m and (not m.group(2) or _coin(m.group(2).split()[-1])):
        cw = m.group(2) or ''
        if m.group(6):                                # "send 10 usdc minus fees": Kivy's own fees are on the card, never taken off the amount
            return {'text': 'Kivy can\'t take %s off an amount. Say the exact amount to use, e.g. "%s" — the card shows every fee.'
                            % (m.group(6).lower(), ' '.join((t0[:m.start()] + m.group(1) + cw + t0[m.end():]).split()))}
        op, rhs = m.group(3).lower(), m.group(4)
        a, b = float(m.group(1).lstrip('$')), float(rhs.lstrip('$'))
        v = a - b if op in ('less', 'minus') else a + b
        if v <= 0:
            return {'text': 'That leaves nothing to use. Say the exact amount.'}
        dl = '$' if m.group(1).startswith('$') else ''
        mk = lambda x: ' '.join((t0[:m.start()] + dl + _plain(round(x, 9)) + cw + t0[m.end():]).split())
        return {'text': '%s — which amount?' % ' '.join(m.group(0).split()),
                'widget': {'type': 'choose', 'options': [{'label': (dl + _plain(round(x, 9)) + cw).strip(), 'ask': mk(x)} for x in (v, a)]}}
    return None
