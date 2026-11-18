"""Chains: one sentence with several steps (withdraw/swap/lend/send/perps/keep), one card (Section 17).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# chain_parse is called by widget_reply and intent; typo_chain by code_answer (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import difflib
import re
from . import perpacct as PERPACCT
from . import amounts as AM
from . import perpwords as PERPWORDS
from .config import K, cap_usd, disp
from .intentwords import COIN_NAMES, INTENT_FORMS, INTENT_STOP, QUESTION_RE, VERB_OF

widget_reply = None        # server.py's (Section 7): set by server.py once it is defined (a module never imports server.py)


# ── Section 17: chains — a sentence with several steps (withdraw / swap / lend / send / perps / keep) read into one spec;
#    chain_parse is called by widget_reply and typo_chain, the card posts the spec to /api/tx/compose -> builder.ts POST /compose ──
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
CHAIN_PERPS = re.compile(PERPWORDS.CHAIN_PERPS_PAT, re.I)   # the same list as PERP_WORD, + margin/collateral ("futures" was missing: lent on Ember, scan 2026-11-14 C1)
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


def fam_pick(rows, fam):
    """A bare protocol as a LENDING TARGET ("…lend it on navi"): its main market ('navi' = NAVI, 'suilend' = Suilend Main Market)
    when that lists the coin, else its best market that does — the same market the one-step lend card opens (review P-13,
    2026-10-29: chains took the best one, Prime / Ember, so one word meant two markets). rows = lend_rows_for(coin), best first."""
    rows = [r for r in rows if r['market'].split('-')[0] == fam]
    return next((r for r in rows if r['market'] == fam), rows[0] if rows else None)


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
        return 'rest', AM.first(t)                   # "the remaining 10": what they expect is left
    pct = AM.first_pct(t)                            # the amount reader's reading (round 2, 2026-11-16): "at 5% apy" is a rate, not a share
    if pct is not None:
        return 'frac', pct
    if re.search(r'\bhalf\b', t):
        return 'frac', 0.5
    fr = AM.frac(t)                                  # "1/2", "2/3" (review multi-step scenarios 2026-10-25); "1/10" was read as 1 (QA 2026-11-17, RDR-V20)
    if fr is not None:
        return 'frac', fr
    sw = AM.share_word(t)                            # "a third", "two thirds", "three quarters", "a fifth" (2026-11-17: read, not ask)
    if sw is not None:
        return 'frac', sw
    n = AM.first_dollars(t)                          # a count, a date, a reference or an account number is passed over; a $ amount
    if n is not None:                                # stays known as dollars (_chain_parse asks when the step moves another coin)
        return 'units', n
    if re.search(r'\b(all|everything|whole|max)\b', t):
        return 'all', None
    return None, None


def unread_ask(msg):
    """A step of a multi-step request whose amount is a word the readers can't turn into one number ("some", "a bit", "a fifth",
    "three quarters", "2 thirds", "ten") or is repeated ("3 times", "twice", "3 payments of 10"): the question to ask, or None.
    A step with no amount word at all ("send it to bob.sui") still takes the rest; the first step keeps its amount picker for
    "some" / "a bit" (QA 2026-11-17, RDR-V3, V20)."""
    cl = [c for c in CHAIN_SPLIT.split(msg) if c and c.strip()]
    def verb(c):                                      # the clause's first action word, as _chain_parse reads it
        hits = [(m.start(), v) for v, pat in CHAIN_VERB for m in [re.search(r'\b(?:%s)\b' % pat, c, re.I)] if m]
        return min(hits)[1] if hits else None
    vi = [i for i, c in enumerate(cl) if verb(c)]
    if len(vi) < 2:
        return None
    for i in vi:
        if verb(cl[i]) == 'keep':
            continue                                  # "…, leave three quarters in my wallet": what stays is the rest
        body = re.sub(_CRCPT, ' ', cl[i], flags=re.I)
        rp = AM.repeated(body)
        if rp:
            return {'text': 'One card is one transaction, so a step can\'t run "%s". Say the total for that step in one go — e.g. "send 30 USDC to bob.sui".' % rp}
        w = AM.unread(body, vague=i != vi[0])
        if w and AM.share_word(w) is None and (w[0].isdigit() or chain_amount(body)[0] is None):
            return {'text': 'How much is "%s"? Say that step\'s amount as a number or a share — e.g. "20 USDC", "20%%" or "half".' % w}
    return None


# "use 0.4 usdc from my wallet: 0.1 to 0x…, 0.1 to 0x…, the rest on ember" — items of a list start with an amount and
# say no verb: a recipient makes it a send, a market a lend (2026-10-25)
LIST_ITEM_RE = re.compile(r'^\s*(?:(?:the\s+)?(?:rest|remainder|other\s+half)|half|a\s+(?:third|quarter)|\$?\d+(?:\.\d+)?\s*%?'
                          r'(?:\s+(?:of\s+)?(?:my\s+)?[A-Za-z][A-Za-z0-9]{1,9})?)\s+(?:to|on|in|into|at)\b', re.I)
_CRCPT = AM.RCPT                                   # the one recipient pattern: exactly 64 characters, never the first 64 of more (R3)


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
        return {'text': 'You have no %s lent on %s through Kivy.%s' % (disp(x), LEND_MKTS.get(mk, mk.replace('fam:', '').title()
                                                                                       .replace('Navi', 'NAVI')), lent_hint(address, x, lead=': '))} if mk else None
    amt = AM.first_or_word(rest) or 'all'            # the amount reader's reading (round 2): never a count, a date, an account number
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


REAL_WORD_TYPOS = {'spilt': 'split', 'spit': 'split', 'slit': 'split', 'swp': 'swap',
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
                rows = [x for x in [fam_pick(rows, mk_i[4:])] if x]   # the protocol's main market first (review P-13)
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
    if AM.long_address(msg) and re.search(r'(?i)\b(?:send|sned|snd|transfer|trasnfer|tranfer|trnasfer|pay|give|move)\b', msg):
        return {'text': AM.long_address(msg)}      # a send to an address over 64 characters: refused, never cut to its first 64 (R3)
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
    body = pieces_rewrite(body)                      # "empty my USDC deposit on X" = a withdrawal, as the reader reads it (2026-11-08)
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
    sui_alias = {'navi-prime': 'navi-sui-prime', 'navi-highyield': 'navi-sui-yield'}   # "NAVI Prime" for SUI is NAVI SUI Prime
    same = lambda want, have: any(h and (h == want or h == sui_alias.get(want) or (want.startswith('fam:') and h.split('-')[0] == want[4:])) for h in have)
    parts = [q for p0 in CHAIN_SPLIT.split(body) for q in re.split(r'\band\b(?!\s+(?:ember|navi|suilend|matrixdock|prime|high)\b)', p0 or '', flags=re.I)]
    for part in parts:                               # "put half into X and the rest as margin": each half checked on its own
        part = (part or '').strip()
        mk = chain_market(part)[0] if part else None
        if not mk or mk in ('best', 'there') or CHAIN_PERPS.search(part) or re.search(r'\b(?:move|shift|rebalance|switch)\b', part, re.I):
            continue                                 # a move names both its markets; the perps account has its own check above
        name = LEND_MKTS.get(mk) or mk.replace('fam:', '').title().replace('Navi', 'NAVI')
        lendv = None if re.search(r'\bempty\b', part, re.I) else re.search(r'\b(?:lend\w*|deposit\w*|put|park\w*|supply|stick)\b', part, re.I)   # "empty my USDC deposit on X": a noun   # a lend verb decides first ("put the withdrawn funds into X")
        if not lendv and re.search(r'\b(?:withdr\w*|take|pull|remove|get|cash|from|out\s+of|empty)\b', part, re.I):
            if not same(mk, src_m) and name not in missing:
                missing.append('the withdrawal from %s' % name)
        elif lendv or re.search(r'\b(?:into|on|in)\b', part, re.I):
            if not same(mk, sink_m) and name not in missing:
                missing.append('the deposit on %s' % name)
    if not missing:
        return r
    return {'text': "I can't fit all of that into one transaction — this part isn't in the plan: %s. One transaction takes one starting coin, "
                    "at most 2 swaps, and each split part can be lent, sent or kept. Split it into two requests, or say it differently." % ', '.join(missing[:5])}


# 'the best staked SUI': swaps in the best-paying staked SUI (staked_ranked) and reads the sentence again.
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


# The chain reader itself: normalise the sentence, try fan-outs, split into clauses, read source -> swaps -> sinks,
# then chain_coverage makes sure nothing the user named was dropped. Returns a chain_card, a question, or None.
def _chain_parse(msg, address):
    msg = re.sub(r'(?<=[A-Za-z0-9%)])\.\s+(?=[A-Za-z])', ', ', msg)   # "Withdraw … . Swap to SUI. Lend …": sentences are steps (review 2026-10-25)
    msg = re.sub(r'\s\+\s*(?=\$?\d)', ' plus ', msg)                # "+ 0.4 usdc from my wallet" = a top-up
    # "take 0.1 usdc off suilend ember", "get all my usdc out of navi prime", "unlend …": a withdrawal from that market.
    # They were read as wallet money and the coverage check then refused the card (Claude's 3-4 step scenarios, 2026-11-07).
    _mk = r'(?:the\s+)?(?:suilend|navi|ember|matrixdock|prime|high\s*yield)\b'
    _nb = r'(?:[^,;.]|\.(?=\d)){0,40}?'               # within the clause; a decimal point is not a sentence end
    msg = re.sub(r'(?i)\b(?:take|get|grab|pull)\b(?=%s\b(?:off|out\s+of|from)\s+%s)' % (_nb, _mk), 'withdraw', msg)
    msg = re.sub(r'(?i)(\bwithdraw\b%s)\b(?:off|out\s+of)\s+(?=%s)' % (_nb, _mk), r'\1from ', msg)
    # arrows as steps: "… -> sui -> navi -> done" = swap to SUI, lend on NAVI (they read as a list of source markets)
    _ar = r'\s*(?:->|→|=>)\s*' if not re.search(r'[\'"“‘][^\'"”’]*(?:->|→|=>)', msg) else r'(?!x)x'   # quoted arrows are text, not steps
    msg = re.sub(r'(?i)%s(?:done|finish(?:ed)?)\b' % _ar, '', msg)
    msg = re.sub(r'(?i)%s(?=(?:lend|send|swap|keep|deposit|withdraw|put|park|transfer)\b)' % _ar, ', ', msg)
    msg = re.sub(r'(?i)%s(?=(?:the\s+)?(?:navi|suilend|ember|matrixdock|prime|high\s*yield)\b)' % _ar, ', lend on ', msg)
    msg = re.sub(r'(?i)%s(?=[a-z]{2,8}\b)' % _ar, ', swap to ', msg)
    msg = re.sub(r'(?i)\bunlend\b', 'withdraw', msg)
    # "withdraw all my lent usdc and put it in perps": every market that lends it, one approval — it asked which one (2026-11-13)
    msg = re.sub(r'(?i)\b(withdraw|take\s+out|pull|redeem|move|get)\s+all\s+(?:of\s+)?(?:my\s+|the\s+)?(?:lent|lended|deposited|supplied)\s+([a-z][a-z0-9]{1,9})\b(?!\s+(?:from|on|in)\b)',
                 lambda m: '%s all my %s from everywhere' % (m.group(1), m.group(2)) if _pw_coin(m.group(2)) else m.group(0), msg)
    msg = split_rewrite(pieces_rewrite(msg))
    ua = unread_ask(msg)                             # "…and send some / a fifth / ten to bob.sui" sent all of it (QA 2026-11-17, RDR-V3, V20)
    if ua:
        return ua
    fo = fanout_parse(msg, address)                  # one coin into several coins, each lent or kept (2026-10-24)
    if fo is not None:
        return fo
    pc = pieces_parse(msg, address)                  # part of a coin swapped, the rest kept or used (2026-11-08)
    if pc is not None:
        return pc
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
    # "swap it to usdc, then to usdsui": a bare "to X" right after a swap is the second swap (2026-11-04: it was refused as
    # "isn't in the plan", and "swap 0.5 sui to usdc then to usdsui" built SUI -> USDC only, dropping the USDsui hop)
    for i, c in enumerate(clauses):
        mt = verbs[i] is None and re.match(r'^\s*(?:on)?(?:in)?to\s+(?:some\s+)?([A-Za-z]+)\s*[?.!]*\s*$', c, re.I)
        if mt and [v for v in verbs[:i] if v][-1:] == ['swap'] and (ckey(mt.group(1)) or COIN_NAMES.get(mt.group(1).lower())):
            verbs[i] = 'swap'
        elif verbs[i] is None and [v for v in verbs[:i] if v][-1:] == ['swap'] and re.match(r'^\s*to\s+(?:%s)\s*[?.!]*\s*$' % _CRCPT, c, re.I):
            verbs[i] = 'send'                         # "swap 0.5 sui to usdc then to bob.sui": the send was dropped
    trade_at = [i for i, c in enumerate(clauses) if re.search(r'\b(?:long|short|ape)\b|\bopen\s+(?:a\s+)?(?:\w+\s+)?position\b', c, re.I)]
    perpish = re.search(r'\b(?:perps?|leverage|margin|\d+(?:\.\d+)?\s*x)\b', msg, re.I)
    side_only = lambda c: perpish and re.match(r'^\s*(?:(?:market|limit)\s+)?(?:buy|sell)(?:\s+order)?\s*[?.!]*\s*$', c, re.I)   # "…, market buy" = the order side
    joined = re.search(r'\b(?:withdraw|swap|convert|send|move|take)\b.+\b(?:and|then|&)\s+(?:then\s+)?(?:go\s+|open\s+(?:a\s+)?)?(?:long|short|ape)\b', msg, re.I)
    if joined or trade_at and any(v in ('withdraw', 'swap', 'move', 'send') for i, v in enumerate(verbs) if v and i not in trade_at and not side_only(clauses[i])):   # a money step AND a trade step
        return {'text': PERPWORDS.chain_trade_text(msg)}   # what to type: the deposit, then the order for its own approval (C9)
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
    accts = xw_accounts(address)
    ow = PERPWORDS.other_wallet(msg, [n for x in accts for n in list(x.get('n') or []) + (x.get('w') or '').lower().split()[:1]])
    if ow and len(accts) < 2:                         # "withdraw from navi on my ledger wallet, deposit to perps on my slush wallet" (C8);
        return PERPWORDS.wallet_reply(ow)             # with 2+ accounts the cross-account reader (xw_parse) already had it
    if CHAIN_BOT.search(msg):                         # "…then start a trading bot with all the SUI" (2026-10-21)
        return {'text': 'Kivy doesn\'t run bots, schedules or repeating trades — every step needs your own approval. '
                        'I can do the money steps once, here, if you drop that part.'}
    if CHAIN_REFUSE.search(msg):
        return {'text': PERPWORDS.chain_trade_text(msg)}   # what to type: the deposit, then the order for its own approval (C9)
    if address is None:
        return {'text': K()['no_wallet']}
    if not ADDR_RE.match(address or ''):
        return None
    lent = lend_positions_rows(address)
    hold = dict(all_balances(address) or {})
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
            mw = re.search(r'(\d+(?:\.\d+)?)\s+([A-Za-z]+)', AM.plain_text(c))   # never a count or a date (round 2)
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
            mw = re.search(r'(\d+(?:\.\d+)?)\s+([A-Za-z]+)', AM.plain_text(c))   # never a count or a date (round 2)
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
                    oc = [x for x in chain_coins(re.sub(r'0x[0-9a-fA-F]+', ' ', c2))[0] if x != coins[0]]
                    if oc:                           # "lend 1 sui and 2 usdc on navi": never add up two coins ("not enough for 3"; 2026-11-14)
                        return {'text': PERPWORDS.after_swap_text(v2, disp(coins[0]), disp(oc[0]), swapped=False)}
                    n2 = AM.first(c2)
                    if n2 is None and re.search(r'\b(?:the\s+rest|rest\s+of|remaining|what(?:\'s|\s+is)\s+left|everything\s+else)\b', c2, re.I):
                        rest_all = True                  # "send 1 usdc to x and lend the rest of my usdc": the rest of the wallet (2026-10-24)
                        continue
                    if n2 is None:
                        return {'text': 'How much in total should I take from your wallet?'}
                    total += n2
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
            src = {'op': 'wallet', 'coin': wc, 'amount': total, 'est': total, 'px': coin_px(wc) or 0, 'summed': not rest_all}   # summed: every part typed (C3/C10)
            hand = wc
        if v in ('withdraw', 'move') or (v == 'use' and mk and mk not in ('best', 'there') and re.search(r'\b(from|out\s+of)\b', c, re.I)):
            if src and v in ('withdraw', 'move') and not re.search(r'\b(?:from|out\s+of|off)\b', c, re.I) \
                    and (not coins or coins == [hand]) and kind in (None, 'all', 'rest'):
                # "swap 2 sui to usdc then MOVE IT to my perps account", "…then move the money to navi", "withdraw my perps margin and
                # WITHDRAW the usdc and lend…": the coin in hand goes on — a next step, not a second start (perps scan 2026-11-13)
                dm = re.search(r'\b(?:to|into|onto|in)\s+(.+?)\s*[.!]*$', c, re.I)
                if v == 'move' and dm and CHAIN_PERPS.search(dm.group(1)):
                    sinks.append({'op': 'perps', 'amount': ('rest', None)})
                    continue
                if v == 'move' and dm and chain_market(dm.group(1), hand)[0] not in (None, 'there'):
                    sinks.append({'op': 'lend', 'mk': chain_market(dm.group(1), hand)[0], 'amount': ('rest', None)})
                    continue
                if (v == 'move' and dm and re.search(r'\bwallet\b', dm.group(1), re.I)) or (v == 'withdraw' and re.search(r'\bwallet\b', c, re.I)):
                    sinks.append({'op': 'keep', 'amount': ('rest', None)})
                    continue
                if v == 'withdraw' and src['op'] in ('perps_out', 'withdraw') and not mk and not CHAIN_PERPS.search(re.sub(r'(?i)\bthe\s+margin\b|\bmy\s+margin\b', ' ', c)):
                    continue                             # "…and withdraw it": already out
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
                    return {'text': PERPACCT.blocked_text(acct) or 'You don\'t have an Aftermath perps account, so there is no margin to withdraw.'}
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
                    pz_src = re.sub(r'(?i)\b(?:plus|including|incl\.?|and|with|\+)\b[^,;]*?\bwallet\b', ' ', re.sub(r'0x\w+', ' ', pz))   # a wallet top-up's number is not a market's
                    if kind != 'units' and re.search(r'(?<![\w.])\d*\.?\d+(?!\s*%)', pz_src):   # a number AND "the rest / all / half":
                        return {'text': 'Say one amount for each market ("0.1 from NAVI and 0.2 from Ember"), one amount for every market '   # never re-read it
                                        '("0.2 USDC from NAVI and Ember" = 0.2 from each), "all" or a share ("half").'}               # (review 11-08 C-1)
                    if kind == 'units':
                        # a number per market ("0.1 from navi and 0.2 from ember") = those amounts; ONE number for several
                        # markets ("0.2 usdc from navi and ember") = that much from EACH (2026-11-08)
                        per = []
                        for sg in re.split(r'\s*(?:,|&|\band\b|\bplus\b)\s*', pz):
                            k2 = chain_market(sg)[0] if sg.strip() else None
                            if k2 and k2 not in ('best', 'there'):
                                nm = re.search(r'(?<![\w.])(\d*\.?\d+)(?!\s*%)', re.sub(r'0x\w+', ' ', sg))
                                per.append((k2, float(nm.group(1)) if nm else None))
                        nums = [a for _, a in per if a is not None]
                        worded = any(re.search(r'(?i)\b(?:rest|all|half|everything|remaining|remainder|third|quarter|max)\b|%', sg)
                                     for sg in re.split(r'\s*(?:,|&|\band\b|\bplus\b)\s*', pz) if chain_market(sg)[0])
                        if worded or every or len(per) != len(frms) or not nums or per[0][1] is None or len(nums) not in (1, len(per)):   # "0.1 from navi and the rest from ember" (review 11-08 C-1)
                            return {'text': 'Say one amount for each market ("0.1 from NAVI and 0.2 from Ember"), one amount for every market '
                                            '("0.2 USDC from NAVI and Ember" = 0.2 from each), "all" or a share ("half").'}
                        amts = nums * len(per) if len(nums) == 1 else nums
                        picked, exact = [], {k for k, _ in per if not k.startswith('fam:')}
                        for (k2, a2) in zip([k for k, _ in per], amts):
                            rs = [r for r in lent if r['coin'].lower() == coin.lower() and hit(r, k2) and (k2 == r['id'] or r['id'] not in exact)]
                            nm2 = LEND_MKTS.get(k2) or k2.replace('fam:', '').title().replace('Navi', 'NAVI')
                            if not rs:
                                return {'text': 'You have no %s lent on %s.' % (disp(coin), nm2)}
                            if len(rs) > 1:                    # "0.1 from navi and …": which NAVI market — one tap each (2026-11-08)
                                wm = re.search(r'(?i)\b%s\b(?!\s+(?:main|prime|high|sui|ember))' % re.escape(k2[4:] if k2.startswith('fam:') else k2), msg)
                                if not wm:
                                    return {'text': 'Your %s is on %d %s markets (%s) — which one?' % (disp(coin), len(rs), nm2, ', '.join(r['name'] for r in rs))}
                                return {'text': 'Which %s market? Your %s is on %d of them.' % (nm2, disp(coin), len(rs)),
                                        'widget': {'type': 'choose', 'options': [
                                            {'label': r['name'], 'sub': '%s %s lent' % (dec(r['lent']), disp(coin)),
                                             'ask': msg[:wm.start()] + MKT_EXACT.get(r['id'], r['name']) + msg[wm.end():]} for r in rs[:4]]}}
                            if a2 > rs[0]['lent'] + 1e-9:
                                return {'text': 'You have %s %s on %s — less than %s.' % (dec(rs[0]['lent']), disp(coin), rs[0]['name'], dec(a2))}
                            picked.append((rs[0], round(a2, 9)))
                        if len({r['id'] for r, _ in picked}) != len(picked):
                            return {'text': 'That names the same market twice — say each market once.'}
                        r0, a0 = picked[0]
                        src = {'op': 'withdraw', 'market': r0['id'], 'coin': r0['coin'], 'amount': a0, 'est': sum(a for _, a in picked),
                               'px': r0.get('px') or coin_px(r0['coin']) or 0, 'first_est': a0,
                               'more': [{'market': r['id'], 'coin': r['coin'], 'amount': a, 'est': a} for r, a in picked[1:]]}
                        hand, last_mkt = r0['coin'], None
                        if v == 'move' and move_to_coin and move_to_coin != hand:
                            swaps.append({'to': move_to_coin}); hand = move_to_coin
                        continue
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
                return {'text': 'You have no %s lent%s through Kivy.%s' % (disp(coin) if coin else 'coins', (' on %s' % LEND_MKTS.get(frm, frm.replace('fam:', '').title())) if frm and frm != 'best' else '',
                                                                       lent_hint(address, coin, lead=': ') if coin and frm and frm != 'best' else '')}
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
            mb = re.search(r'\b(?:buy|get)\s+(?:me\s+)?(?:some\s+)?([A-Za-z]+)\b', c, re.I)          # "to buy USDC", "to get (me) vSUI", "buy X with Y"
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
            part = (kind == 'frac' and amt < 1 - 1e-9) or (kind == 'units' and not (not swaps and isinstance(src.get('amount'), (int, float))
                                                                                    and abs(float(src['amount']) - amt) < 1e-9))
            if part or any(k['amount'][0] in ('units', 'frac') for k in sinks):   # "withdraw all my margin, swap half to sui …" swapped ALL of it; "send 10 to
                return {'text': PERPWORDS.mid_swap_text(disp(hand))}   # alice.sui, swap the rest to sui" sent 10 SUI — every step after a swap is in the new coin (C5)
            if tgt != hand:
                if len(swaps) >= 2:
                    return {'text': 'At most two swaps in one transaction — please split it.'}
                swaps.append({'to': tgt}); hand = tgt
            continue
        if (v == 'lend' or (v == 'use' and src)) and CHAIN_PERPS.search(c):   # "deposit into aftermath as margin", "use it as margin"
            sinks.append({'op': 'perps', 'amount': (kind, amt) if kind in ('frac', 'units') else ('rest', None)})
            continue
        if v == 'lend':
            up = None if mk else PERPWORDS.unknown_place(c, lambda w: bool(ckey(w) or lend_token(w) or COIN_NAMES.get(w.lower())))
            if up:                                    # "put it in futures" was lent on the best market (scan 2026-11-14 C1): ask where
                return {'text': PERPWORDS.unknown_place_text(up)}
            sinks.append({'op': 'lend', 'mk': mk or 'best', 'amount': (kind, amt), 'said': coins})
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
                sinks.append({'op': 'send', 'to': to, 'name': who, 'amount': (sk, sa),
                              'said': [x for x in chain_coins(re.sub(_CRCPT, ' ', seg or c, flags=re.I))[0]]})   # the coin typed for this send (C5)
            continue
        if v == 'keep':
            sinks.append({'op': 'keep', 'amount': (kind if kind else 'rest', amt), 'said': coins})
            continue
    if src is None:
        return None
    for k in sinks:                                   # "…, send $3 of it to bob.sui" sent 3 SUI: dollars on a step that moves a coin that is
        if isinstance(k['amount'][1], AM.Dollars) and k['op'] != 'perps' and AM.dollars_ask(msg, k['amount'][1], hand, coin_px(hand)):
            return AM.dollars_ask(msg, k['amount'][1], hand, coin_px(hand))   # not a dollar coin -> asked, in coins (round 2, 2026-11-16)
    if isinstance(src.get('amount'), AM.Dollars) and AM.dollars_ask(msg, src['amount'], src['coin'], coin_px(src['coin'])):
        return AM.dollars_ask(msg, src['amount'], src['coin'], coin_px(src['coin']))
    if len(sinks) > 4:
        return {'text': 'A plan can end in at most 4 steps — split it into two.'}
    if (src['op'] == 'perps_out' or src.get('more')) and not sinks:
        sinks.append({'op': 'keep', 'amount': ('rest', None)})       # "withdraw from aftermath and swap to sui" / "…from suilend and navi": it comes home
    if not sinks and len(swaps) <= 1:
        return None                                  # withdraw-then-swap / a plain swap: their own cards (with a slider) do it
    if not sinks:
        sinks.append({'op': 'keep', 'amount': ('rest', None)})
    # amounts: "all"/none on the last step = the rest; a lone step takes everything — unless an amount was typed on it: "withdraw
    # 100 usdc from navi and send 50 to bob.sui" sent all 100 (review scan R1, 2026-11-15). AM.lone_step says what the typed amount means.
    if len(sinks) == 1:
        a0 = sinks[0]['amount']
        how, val = AM.lone_step(src, sinks[0], swaps, disp(hand))
        if how == 'refuse':
            return {'text': val}
        if how == 'start':                            # no start amount typed: start with exactly what the step needs
            src.update(amount=round(val, 9), est=val, pick=False)
            sinks[0]['amount'] = ('rest', None)
        elif how == 'keep':                           # the typed amount (or share) stands; the rest stays in the wallet
            sinks.append({'op': 'keep', 'amount': ('rest', None)})
        elif how == 'swap':                           # "…then lend 3 usdc": 3, the rest stays in the wallet (review P-23)
            try:
                est = float(src.get('est') or 0) * float(src.get('px') or 0) / float(coin_px(hand) or 0)
            except Exception:
                est = 0
            if est and float(a0[1]) > est * 0.98:
                return {'text': 'That swap gives about %s %s — not enough to %s %s. Say "%s it" for all of it, or a smaller amount.' % (dec(est), disp(hand), sinks[0]['op'], dec(a0[1]), sinks[0]['op'])}
            sinks.append({'op': 'keep', 'amount': ('rest', None)})
        else:
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
            if swaps:                                 # "withdraw 20 usdc, swap to sui and put it in perps": the swap typed makes SUI, margin is USDC (scan #112)
                return {'text': PERPWORDS.swapped_not_usdc_text(disp(hand), len(sinks) > 1 or any(chain_market(x)[0] not in (None, 'best', 'there')
                                                                                                  for x in clauses[1:] if CHAIN_PERPS.search(x)))}
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
            elif others and not (all(k['amount'][0] == 'frac' for k in others)
                                 and abs(pk['amount'][1] + sum(k['amount'][1] for k in others) - 1) < 1e-6):   # "half into perps, half lent": all of it
                return {'text': 'Say where the rest goes, e.g. "half into perps and lend the rest on NAVI".'}
        elif pk['amount'][0] == 'units' and others:
            # "take 0.2 usdc, put 0.1 into perps and lend the rest": from a known start the other part is what perps leaves
            rest = [k for k in others if k['amount'][0] == 'rest']
            said = float(src['amount']) if src and src.get('op') in ('wallet', 'withdraw') and not swaps and not src.get('more') \
                and isinstance(src.get('amount'), (int, float)) and not re.search(r'\b(?:plus|including|together\s+with|along\s+with|as\s+well\s+as)\b', msg, re.I) else None   # a typed withdrawal too (C10)
            if said and not src.get('summed') and not any(abs(float(x) - said) < 1e-12 for x in re.findall(r'(?<![\w.])\d*\.?\d+', msg)):
                said = None                                    # only a start the user typed (or the sum of the parts typed), never a balance filled in for them
            if not rest and said and others and all(k['amount'][0] == 'units' for k in others) \
                    and abs(said - pk['amount'][1] - sum(k['amount'][1] for k in others)) < 1e-9:
                pass                                           # "send 5 usdc to alice.sui, deposit 20 usdc to perps": every part typed, perps takes exactly its 20 (C10)
            elif len(rest) == 1 and said and all(k['amount'][0] in ('units', 'rest') for k in others):
                left = said - pk['amount'][1] - sum(k['amount'][1] for k in others if k['amount'][0] == 'units')
                if left <= 1e-12:
                    return {'text': 'Those amounts add up to more than the %s %s you start with.' % (dec(said), disp(src['coin']))}
                rest[0]['amount'] = ('units', round(left, 9))
            elif AM.all_then_fixed(src, pk, others, src.get('est'), swaps) is not None:
                kept = AM.all_then_fixed(src, pk, others, src.get('est'), swaps)   # all out, the typed amount to perps, the rest stays in the wallet
                if kept > 0:
                    rest[0]['amount'] = ('units', kept)
                else:
                    sinks = others = []                   # the typed amount is the whole position: everything goes to perps
            elif src and src.get('op') == 'withdraw' and src.get('amount') == 'all' and not src.get('more') and len(rest) == 1:
                return {'text': PERPWORDS.exact_start_text(dec(src['est']), disp(src['coin']), LEND_MKTS.get(src['market'], src['market']), dec(pk['amount'][1]))}   # (C10)
            else:
                return {'text': 'With other steps, give the perps deposit as a share ("half") or make it the last step ("the rest into perps").'}
        sinks = others + [pk]                                  # Aftermath appends its deposit, so it goes last and takes the rest
        if pk['amount'][0] == 'units' and not others and src and src.get('op') == 'wallet' and not swaps and abs(float(src['amount']) - pk['amount'][1]) > 1e-9:
            return {'text': 'Start with exactly the amount for perps, e.g. "take %s USDC from my wallet and put it into perps".' % dec(pk['amount'][1])}
        pk['amount'] = ('rest', None)
        try:
            pa = perp_account(address)
            if not pa.get('has'):
                return {'text': PERPACCT.blocked_text(pa) or 'You don\'t have an Aftermath perps account yet. Say "open perps account" first, then ask again.'}
        except Exception:
            pass
    for k in sinks:                                   # "swap to sui, send 10 usdc to alice.sui" sent 10 SUI: every step after the swaps (and the swap
        bad_c = [x for x in k.get('said') or [] if x != hand]   # to USDC that perps margin needs) is in the coin in hand at the end (C5)
        if bad_c:
            return {'text': PERPWORDS.after_swap_text(k['op'], disp(hand), disp(bad_c[0]), bool(swaps))}
    if sum(1 for k in sinks if k['amount'][0] == 'rest') > 1:
        return {'text': 'Only one step can take "the rest" — give the others an amount.'}
    # "take 0.2 sui from my wallet, lend 0.1 on navi, keep the rest": the kept part never leaves the wallet — start with only what is
    # used and drop the keep step (2026-11-08). Withdrawals, swaps, top-ups and picker cards are left as they are.
    kk = [k for k in sinks if k['op'] == 'keep']
    if src and src.get('op') == 'wallet' and not swaps and not src.get('pick') and not src.get('plus') and len(kk) == 1 \
            and len(sinks) > 1 and not any(k['op'] == 'perps' for k in sinks) and isinstance(src.get('amount'), (int, float)) \
            and not (kk[0]['amount'][0] == 'rest' and kk[0]['amount'][1]):   # "keep the remaining 0.5": its arithmetic is checked below
        tot = float(src['amount'])
        sz = lambda k, fx: tot * k['amount'][1] if k['amount'][0] == 'frac' else k['amount'][1] if k['amount'][0] == 'units' else max(tot - fx, 0)
        fx0 = sum(sz(k, 0) for k in sinks if k['amount'][0] != 'rest')
        others = [k for k in sinks if k is not kk[0]]
        sizes = [sz(k, fx0) for k in others]
        if fx0 <= tot * 1.0001 and sum(sizes) > 0 and all(z > 0 for z in sizes):   # amounts over the start: left for the check below to refuse
            for k, z in zip(others, sizes):
                k['amount'] = ('units', round(z, 9))
            others[-1]['amount'] = ('rest', None)
            src['amount'] = src['est'] = round(sum(sizes), 9)
            sinks = others
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
        nm = AM.first(pm.group(2))
        plus = nm if nm is not None else have
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
                pk = fam_pick(rows, m[4:])           # the protocol's main market first, like the one-step card (review P-13)
                m = pk['market'] if pk else None
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
                        return (fam_pick(lend_rows_for(hand), mk2[4:]) or {}).get('market')
                    return mk2
                used = {_res(x) for x in sinks if x is not k and x.get('op') == 'lend'}
                opts = []
                if spans:
                    sp = spans[-1]                            # the last mention: where the money goes, not where it comes from
                    for r in [r for r in lend_rows_for(hand) if r['market'] not in used][:3]:
                        nm = MKT_EXACT.get(r['market'], LEND_MKTS.get(r['market'], r['name']))
                        opts.append({'label': 'Use %s instead' % LEND_MKTS.get(r['market'], r['name']), 'sub': '%.2f%% a year' % float(r.get('total_apr') or 0),
                                     'ask': msg[:sp.start()] + nm + msg[sp.end():]})
                return {'text': ('%s can\'t be lent on %s. It can be lent on: %s.' % (disp(hand), wname, ', '.join(where_ok))) if where_ok
                        else '%s can\'t be lent on any market Kivy supports — it can only be kept in your wallet.' % disp(hand),
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


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import BORROW_RE, perp_account  # noqa: E402
from .situations import (  # noqa: E402
    DUST_RE, DUST_TO_RE, REAL_COINS, STAKED_ASK, STAKED_NAMED, _mk_of, dust_coins, staked_line,
    staked_ranked)
from .wordings import _pw_coin  # noqa: E402
from .intent import ENGLISH, NEVER_FIX  # noqa: E402
from .chainreads import ADDR_RE, COINS, NET, all_balances, ckey, coin_icon, dec  # noqa: E402
from .fallback import resolve_recipient  # noqa: E402
from .lending import (  # noqa: E402
    LEND_MKTS, coin_px, lend_positions_rows, lend_rates_all, lend_rows_for, lend_token, lent_hint, spend_check,
    spendable)
from .pieces import pieces_parse, pieces_rewrite  # noqa: E402
from .txreads import rev_fee_pct, rev_lend_pct, slip_for, swap_quote  # noqa: E402
from .sends import SEND_BRIDGE_RE, XW, xw_accounts  # noqa: E402
