"""Input hygiene: amount and wording tidying, filler, polite orders, the seed-phrase guard (Section 6).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# tidy_amounts / tidy_wording / strip_filler / looks_secret are called by code_answer and the Handler (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import json
import re
from pathlib import Path
from . import amounts as AM
from . import perpwords as PERPWORDS


try:                                                 # the 2048 seed-phrase words (public list): a pasted seed phrase is never sent on (2026-10-25)
    BIP39 = set(json.load(open(Path(__file__).resolve().parent.parent / 'bip39_english.json')))
except Exception:
    BIP39 = set()
    print('kivy: bip39_english.json missing — the seed-phrase guard only catches private keys', flush=True)
SEED_WARN = ("That looks like a seed phrase or private key, so I didn't read, keep or pass it on. Never share it with anyone — Kivy included: "
             "whoever has it owns your wallet, and real support never asks for it. If you've pasted it anywhere else, move your coins to a new wallet now.")
ADVICE_RE = re.compile(r"\b(?:is|are)\s+\$?[a-z]{2,10}\s+(?:a\s+)?(?:good|bad|smart|safe)\s+(?:investment|buy|bet)\b|\bshould\s+i\s+(?:buy|sell|invest(?:\s+in)?|ape(?:\s+into)?|hold|dump)\b(?!.*\b(?:lend|navi|suilend|ember|market)\b)"
                       r"|\bworth\s+(?:buying|investing)\b|\bwill\s+\$?[a-z]{2,10}\s+(?:go\s+up|go\s+down|pump|dump|moon|crash|rise|fall)\b|\bprice\s+predictions?\b|\bgood\s+time\s+to\s+(?:buy|sell)\b", re.I)
SEED_ASK_RE = re.compile(r'\b(?:seed|recovery|secret|backup)\s+(?:phrase|words?)\b|\bprivate\s+keys?\b|\bmnemonic\b|\b(?:12|24|twelve|twenty[\s-]?four)\s+words\b|\bexport\s+(?:my\s+)?key\b', re.I)


FILLER_RE = re.compile(r'^\s*(?:(?:ok(?:ay)?|alright|all\s+right|yes|yeah|yep|sure|hey|hi|hello|so|well|um+|kivy)\b[\s,!.:-]*)+', re.I)
ACTION_START = re.compile(r'^(?:please\s+|pls\s+|just\s+)?(?:swap|send|transfer|lend|deposit|withdraw|claim|sweep|close|open|long|short|move|convert|buy|sell|stake|put)\b', re.I)


NUM_WORDS = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10'}
APOS_RE = re.compile('[’‘ʼʻ′´`＇]')
NT_RE = re.compile(r"(?i)(?<![\w.@-])(do|does|did|is|are|was|were|have|has|had|should|would|could|must|need)nt(?![\w.-])")


def norm_words(t):
    """One spelling of an apostrophe and of a contraction for every reader and the read guard: a phone's ’ ‘ ʼ (and ` ´) are ',
    "dont", "wont", "cant", "shouldnt"… get theirs back. "don’t swap 5 sui" was not a "don't" and built the swap (QA 2026-11-17, RDR-V6)."""
    t = NT_RE.sub(lambda m: m.group(1) + "n't", APOS_RE.sub("'", str(t or '')))
    return re.sub(r"(?i)(?<![\w.@-])(won|can|don)t(?![\w.-])", lambda m: m.group(1) + "'t", t)


_NEG_BEFORE_RE = re.compile(r"(?i)\b(?:don't|do\s+not|never|not|won't|will\s+not|can't|cannot|shouldn't|wouldn't|didn't|no\s+need\s+to)\s+(?:(?:please|just|ever|really|even)\s+)*$"
                            r"|\b(?:should|shall|do|did|would|must)\s+(?:i|we)\s+(?:(?:really|just|ever)\s+)*$|\bif\s+(?:i|you|we)\s+(?:\w+\s+)?$")
_QUOTED_BEFORE_RE = re.compile(r"(?i)\b(?:asked|asks|said|says|told\s+me|wrote|writes|texted|messaged|dm'?d)\b\s*:?\s*[\"“‘']?\s*$|[:\"“‘]\s*$")


def _rewrite_ok(before):
    """A polite-order rewrite below replaces a fragment with a plain order. Not when the fragment is negated, asked about or quoted
    ("please do not bring half … back", "should i bring all of it back", "my friend asked: could you put …"): the read guard then
    judges the user's own words (QA 2026-11-17, RDR-V1)."""
    return not (_NEG_BEFORE_RE.search(before) or _QUOTED_BEFORE_RE.search(before))


def tidy_amounts(msg):
    """Amounts as people type them: ".1" = 0.1 (it was read as 1 — ten times more), "0,1" = 0.1 (a comma before 1-2 digits is a
    decimal point; "1,000" stays a thousand), "0.1sui" = "0.1 sui", "1k" = 1000, "five sui" = "5 sui". Addresses are never touched."""
    def tok(t):
        if t.lower().startswith('0x'):
            return ('0x' + t[2:]) if t.startswith('0X') else t   # "SEND … TO 0X7777…" (caps lock) was not read as an address (2026-10-31)
        t = re.sub(r'(?<![\d.,])\.(\d)', r'0.\1', t)
        t = re.sub(r'^(\$?)([1-9]\d{0,2})((?:,\d{3})+)(?=(?:\.\d+)?[A-Za-z%]*[.,!?]?$)', lambda m: m.group(1) + m.group(2) + m.group(3).replace(',', ''), t)   # "1,000" / "12,345" = thousands (review P-8)
        t = re.sub(r'^(\$?)0,(\d{3})(?=[A-Za-z%]*[.,!?]?$)', r'\g<1>0.\2', t)   # "0,100" = 0.1
        t = re.sub(r'^(\$?)([1-9]\d{0,2})((?:_\d{3})+)(?=[A-Za-z%]*[.,!?]?$)', lambda m: m.group(1) + m.group(2) + m.group(3).replace('_', ''), t)   # "1_000"
        t = re.sub(r'(?i)^(\$?)(\d+(?:\.\d+)?)e([+-]?\d{1,2})(?=[.,!?]?$)', lambda m: m.group(1) + _gnum(float(m.group(2)) * 10 ** int(m.group(3))), t)   # "1e3", "1e-3" (was read as 3, 2026-10-31)
        t = re.sub(r'(?<=\d),(\d{1,2})(?!\d)', r'.\1', t)
        t = re.sub(r'(?i)^(\$?)(\d+(?:\.\d+)?)k$', lambda m: m.group(1) + ('%g' % (float(m.group(2)) * 1000)), t)
        m = re.match(r'^(\$?\d+(?:\.\d+)?)([A-Za-z][A-Za-z0-9]{1,9})$', t)
        if m and (ckey(m.group(2)) or lend_token(m.group(2))):
            t = m.group(1) + ' ' + m.group(2)
        return t
    msg = AM.pre(msg)                                # other scripts' digits, "1 000", "1O": one reading for every reader (package 1, 2026-11-15)
    msg = re.sub(r'(?i)\bhalf\s+a\s+dollar\b', '$0.5', str(msg or ''))
    msg = re.sub(r'(?i)\b(?:the\s+)?(?:my\s+)?(?:whole|entire|full)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})(?:\s+balance)?\b',   # "my whole sui" = "all my sui":
                 lambda m: 'all my ' + m.group(1) if (ckey(m.group(1)) or lend_token(m.group(1))) else m.group(0), msg)   # the readers knew 'all' only (CW-4, 2026-11-03)
    msg = re.sub(r'(?i)\ba\s+buck\s+fifty\b', '$1.5', msg)   # "a buck fifty of the USDsui" was read as the coin BUCK (a reviewer 1103)
    msg = re.sub(r'(?i)(?<![\w.$])(\d+(?:\.\d+)?)\s+bucks\b', r'$\1', msg)
    msg = re.sub(r'(?i)\b(?:a|one)\s+buck\b(?=\s+(?:of|worth)\b)', '$1', msg)
    msg = re.sub(r'(?i)\b(?:a|one)\s+dollar\b(?=\s+(?:of|worth)\b)', '$1', msg)
    msg = re.sub(r'(?i)(?<![\w.$])(\d+)\s*cents?\b', lambda m: '$' + _gnum(int(m.group(1)) / 100.0), msg)   # "100 cents of sui" (2026-11-01)
    msg = re.sub(r'(?<![\d.])(?:(\d+)\s?)?([½¼¾⅛])', lambda m: _gnum((float(m.group(1)) if m.group(1) else 0) + {'½': 0.5, '¼': 0.25, '¾': 0.75, '⅛': 0.125}[m.group(2)]),
                 str(msg or ''))                     # "swap ½ sui", "1½ sui" (2026-11-01)
    out = ' '.join(tok(t) for t in str(msg or '').split(' '))
    out = re.sub(r'(?i)(?<![\w.])(\d+(?:\.\d+)?)\s*(k|thousand|m|mn|million|hundred)\s+(?=([A-Za-z]{2,10})\b)',   # "1 k deep", "2m sui" (review P-8/P-27); "2 hundred deep" (QA 2026-11-17, RDR-V12)
                 lambda m: (_gnum(float(m.group(1)) * {'k': 1000, 'thousand': 1000, 'hundred': 100}.get(m.group(2).lower(), 1000000)) + ' ')
                 if (ckey(m.group(3)) or lend_token(m.group(3))) else m.group(0), out)
    out = re.sub(r'(?i)\bhalf\s+an?\s+(?=([A-Za-z]{2,10})\b)', lambda m: '0.5 ' if (ckey(m.group(1)) or lend_token(m.group(1))) else m.group(0), out)   # "half a sui" (2026-11-01)
    nw = dict(NUM_WORDS, eleven='11', twelve='12', thirteen='13', fourteen='14', fifteen='15', sixteen='16', seventeen='17', eighteen='18',
              nineteen='19', twenty='20', thirty='30', forty='40', fifty='50')
    fr = {'tenth': 0.1, 'fifth': 0.2, 'quarter': 0.25, 'third': None, 'half': 0.5}     # "a tenth of one SUI" was read as "a ETH" (2026-10-31)
    out = re.sub(r'(?i)\b(?:(?:a|one)\s+)?(tenth|fifth|quarter|half)\s+(?:of\s+)?(?:(?:a|one)\s+single|a\s+whole|one|a|1)\s+(?=([A-Za-z]{2,10})\b)',
                 lambda m: _gnum(fr[m.group(1).lower()]) + ' ' if (ckey(m.group(2)) or lend_token(m.group(2))) else m.group(0), out)
    out = re.sub(r'(?i)\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+and\s+a\s+(half|quarter)\s+(?=([A-Za-z]{2,10})\b)',
                 lambda m: _gnum(float(nw.get(m.group(1).lower(), m.group(1))) + fr[m.group(2).lower()]) + ' ' if (ckey(m.group(3)) or lend_token(m.group(3))) else m.group(0), out)
    tens = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90}
    out = re.sub(r'(?i)\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[-\s]+(one|two|three|four|five|six|seven|eight|nine)\s+(?=([A-Za-z]{2,10})\b)',
                 lambda m: '%d ' % (tens[m.group(1).lower()] + int(nw[m.group(2).lower()])) if (ckey(m.group(3)) or lend_token(m.group(3))) else m.group(0), out)
    nw.update({k: str(v) for k, v in tens.items()})
    big = re.compile(r'(?i)(?:hundred|thousand|million|billion|dozen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred\s+and)[\s-]*$|-$')
    return AM.post(re.sub(r'(?i)\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\s+(?=([A-Za-z]{2,10})\b)',
                  lambda m: m.group(0) if big.search(m.string[:m.start()]) else   # the tail of "two hundred fifty" / "twenty-five": left for Kivy to ask (round 3)
                  nw[m.group(1).lower()] + ' ' if (ckey(m.group(2)) or lend_token(m.group(2))) else m.group(0), out))   # "3/4 sui" = 0.75 SUI


def tidy_wording(msg):
    """Everyday wrappers around a request, removed before any reader sees it (2026-10-27): a trailing "please"/"thanks"
    (it made 'ember please' read as Suilend Main), "can you …" before an action, "get/take/pull X (back) out of M" = withdraw,
    and a trailing "and withdraw it" (read as a second starting coin)."""
    t = re.sub(r'(?<=\d)[\u00a0\u202f\u2009\u2007](?=\d{3}(?!\d))', '', norm_words(msg))   # "1 000" with a no-break space was read as 1 (round 3)
    t = re.sub(r'\d[\d\u00b7\u066b\u066c\u2044\u2027\u30fb\u0300-\u036f\u0483-\u0489\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]*',
               lambda m: m.group(0) if m.group(0).isdigit() else ' an unclear amount ', t)   # "1·5", "1٫5", "1⁄0", "1́2": Kivy asks (review W3-5)
    t = ' '.join(re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\uFE0F]', ' ', t).split())   # emoji are not words (2026-11-01)
    t = re.sub(r'^(?:right|right\s+then|ok\s+so|so\s+yeah)\s*,\s*', '', t, flags=re.I)
    t = re.sub(r'https?://\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:xyz|com|io|net|org|app|finance|fi|co|me|link|site|top|club|live|info)\b(?:/\S*)?', 'a link', t, flags=re.I)   # a URL's words are not an order (2026-11-01)
    t = re.sub(r'\bSU[l1]\b', 'SUI', t)
    t = PERPWORDS.split_places(t, lambda x: chain_market(x)[0] not in (None, 'best', 'there'))   # "split 40 usdc between perps and navi": places, not coins (C10)
    t = re.sub(r'^(?:split|divide)\s+(\d[\d.]*\s+[A-Za-z][A-Za-z0-9]{1,9})\s*[:,]?\s*(?=half\s+(?:to|into)\b)', r'swap \1: ', t, flags=re.I)
    t = re.sub(r'^(?:split|divide)\s+(\d[\d.]*\s+[A-Za-z][A-Za-z0-9]{1,9})\s+(?:evenly\s+|equally\s+)?(?:between|into)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+(?:and|&)\s+([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$',
               r'swap \1: half to \2, half to \3', t, flags=re.I)   # "split 0.6 sui between usdc and vsui" (the AI built 0.6 -> USDC only, 2026-11-01)                # "SUl" with a lower-case L (review 1031)
    if re.search(r'\d[\d,.]*\s*(?:baht|thb)\b|฿\s*\d', t, re.I) and len(re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', t)) >= 2:
        t = re.sub(r'(?:฿\s*)?\d[\d,.]*\s*(?:baht|thb)\b|฿\s*\d[\d,.]*', 'some baht', t, flags=re.I)   # "it cost me 500 baht; swap 0.16 SUI" (a reviewer 1031)
    fm = re.search(r"\b(?:the\s+only\s+thing\s+i\s+want(?:\s+today|\s+now)?\s+is\s+to|all\s+i\s+want(?:\s+today|\s+now)?\s+is\s+to|(?:so\s+)?for\s+now,?\s+(?:nothing\s+else,?\s+)?just(?:\s+this)?\s*:|just\s+this\s*:)\s+(.+?)\s*[.!]*$", t, re.I)
    if fm and re.match(r'(?:swap|exchange|convert|send|lend|withdraw|deposit)\b', fm.group(1), re.I):   # a ramble that ends in ONE order
        t = re.sub(r'^exchange\b', 'swap', fm.group(1), flags=re.I)
    # The polite-order rewrites keep what follows the order (a second step, "tomorrow", "if the rate…", a market's second word):
    # only a harmless tail is dropped, so the read guard and the chain reader still see the rest (QA 2026-11-17, RDR-V1).
    pm = re.search(r"\b(?:would\s+you\s+mind|could\s+you|can\s+you|please)\s+arrang(?:e|ing)\s+for\s+(\d[\d.]*\s+[A-Za-z][A-Za-z0-9]{1,9})\s+to\s+(?:earn(?:\s+(?:interest|yield))?|be\s+lent)\s+(?:on|in|at)\s+(?:the\s+)?(%s|[A-Za-z]+)(?:\s+market)?(.*)$" % G_LEND_MKT, t, re.I)
    if pm and _rewrite_ok(t[:pm.start()]):            # "would you mind arranging for 0.29 SUI to earn interest on NAVI, if that's something you can help with?"
        tail = '' if re.match(r"(?i)\s*(?:,\s*(?:if\s+(?:that'?s|it'?s|that\s+is|it\s+is|this\s+is)\s+(?:something\s+you\s+can\s+(?:help\s+(?:me\s+)?with|do)|possible|ok(?:ay)?|alright|not\s+too\s+much\s+trouble)"
                                r"|please|thanks?(?:\s+you)?|for\s+me))?\s*[?.!]*\s*$", pm.group(3)) else pm.group(3)
        t = 'lend %s on %s%s' % (pm.group(1), pm.group(2), tail)
    gm = re.search(r"\bi'?d\s+(?:appreciate|like|love)\s+(?:getting|to\s+get|to\s+have)\s+((?:\d[\d.]*|all|half)\s+(?:of\s+)?(?:my\s+)?[A-Za-z][A-Za-z0-9]{1,9})\s+back\s+from\s+(?:the\s+)?(.+?)(?:\s+(?:in)?to\s+my\s+wallet)?\s*[.!]*$", t, re.I)
    if gm and _rewrite_ok(t[:gm.start()]):            # "I'd appreciate getting 0.42 sSUI back from the Main Market into my wallet"
        t = 'withdraw %s from %s' % (gm.group(1), gm.group(2))
    bm = re.search(r"\bbring\s+(half|all|\d[\d.]*)\s+(?:of\s+)?(?:the\s+|my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:in|on|from)\s+(%s)\s+back\b" % G_LEND_MKT, t, re.I)
    if bm and _rewrite_ok(t[:bm.start()]):            # "…please just bring half of the USDsui in Ember back to my wallet. The other one should stay."
        rest = re.sub(r'(?i)^\s*(?:(?:in)?to\s+(?:my\s+|the\s+)?wallet\b)?', '', t[bm.end():])
        more = re.sub(r'^[\s.!]+', '', rest)
        harmless = not more or (re.match(r'^\s*[.!]', rest) and not re.search(r'\d|\?', more) and not G_VERB_RE.search(more)
                                and not LATER_RE.search(more) and not re.search(r'\b(?:if|when|unless|once)\b', more, re.I))
        t = 'withdraw %s %s from %s' % bm.group(1, 2, 3) + ('' if harmless else ' ' + rest.strip())
    am = re.search(r"\b(?:bring|get|take)\s+(?:all\s+(?:of\s+)?(?:that|it)|it\s+all|everything)\s+back\s+from\s+(%s)\b" % G_LEND_MKT, t, re.I) \
        or re.search(r"\b(?:need|want)\s+(?:the|my)\s+(%s)\s+(?:money|funds|coins?|savings)\s+back\b" % G_LEND_MKT, t, re.I)
    if am and _rewrite_ok(t[:am.start()]):            # "can you bring all of that back from Ember?", "I need the Ember money back…"
        cn = next((w for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', t) if w.lower() not in G_STOP and w.lower() != am.group(1).lower() and (ckey(w) or lend_token(w))), None)
        t = 'withdraw all %sfrom %s' % (cn + ' ' if cn else '', am.group(1))
    um2 = re.search(r"\b(?:could|can|would)\s+you\s+put\s+(\d[\d.]*\s+[A-Za-z][A-Za-z0-9]{1,9})\s+(?:into|in|on)\s+(?:the\s+)?(?:ordinary\s+|regular\s+|normal\s+|main\s+|plain\s+)?"
                    r"((?:navi|suilend|ember)(?:\s+(?:main|prime|ember|matrix\w*|market|high\s*yield|sui\s+prime))*)\b(.*)$", t, re.I)
    if um2 and _rewrite_ok(t[:um2.start()]):          # "…could you put 0.31 SUI into the ordinary NAVI lending market for me? Nothing fancy."
        tail = '' if re.match(r'(?i)\s*(?:(?:lending\s+)?market)?(?:\s+for\s+me)?\s*[?.!]*(?:\s+(?:nothing\s+(?:fancy|else)|thanks?(?:\s+you)?|cheers)\s*[.!]*)?\s*$', um2.group(3)) else um2.group(3)
        t = 'lend %s on %s%s' % (um2.group(1), re.sub(r'(?i)\s+market$', '', um2.group(2)), tail)
    if re.search(r"\bsavings?[\s-]*account\b|\bmake\s+my\s+money\s+work\b|\bput\s+(?:my\s+)?\w+\s+to\s+work\b", t, re.I) and not re.search(r'\d|\bowe\b|\bloan\b|\brisk|\bmean\b|\b(?:fee|charge|cost|cut|price)s?\b|\bif\s+i\b', t, re.I):
        sc = next((w for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', t) if w.lower() not in G_STOP and (ckey(w) or lend_token(w))), None)
        if sc:                                        # "a savings-account sort of thing for the SUI I already have?": the SUI markets
            t = 'lending markets for %s' % sc
    if re.search(r"\b(?:look\s+at|see|show)\b.{0,40}\b(?:money|coins?|funds)\s+(?:i'?ve|i\s+have)\s+(?:already\s+)?(?:put\s+away|deposited|lent|saved)\b|\bdeposits\s+i\s+(?:have\s+|'ve\s+)?(?:already\s+)?made\b", t, re.I) and not re.search(r'\d', t):
        t = 'what have i lent'                        # "…look at the money I've already put away", "show the deposits I have already made"
    t = re.sub(r'^(?:eh\s+bro|eh|bro|yo|oi|oy)\b\s*,?\s*', '', t, flags=re.I)
    t = re.sub(r'\s+(?:lah|la|leh|lor|liao)\s*([?.!]*)\s*$', r'\1', t, flags=re.I)   # Singlish particles (a reviewer 1101)
    t = re.sub(r'^(?:cn|can|could|pls|plz)\s+u\s+', 'can you ', t, flags=re.I)
    t = re.sub(r'\bwd\b(?=\s+(?:\d|all|half|my)\b)', 'withdraw', t, flags=re.I)
    t = re.sub(r'\bfrm\b', 'from', t, flags=re.I)
    t = re.sub(r'\s+(?:pls|plz)\b', '', t, flags=re.I)
    t = re.sub(r'^put\s+((?:\d[\d.,]*|all|half)\s+(?:of\s+)?(?:my\s+)?[A-Za-z][A-Za-z0-9]{1,9})\s+(?:on|in|into)\s+(?=(?:%s)\b)' % G_LEND_MKT, r'lend \1 on ', t, flags=re.I)
    um = re.match(r'^unstake\s+(.+?)\s+from\s+(%s)\s*[.!]*$' % G_LEND_MKT, t, re.I)
    if um:                                            # "unstake all my ssui from suilend": withdraw it and swap to SUI
        t = 'withdraw %s from %s, swap it to SUI, keep it in my wallet' % (re.sub(r'^my\s+|^all\s+my\s+', lambda x: 'all ' if 'all' in x.group(0).lower() else '', um.group(1), flags=re.I), um.group(2))
    t = re.sub(r'[\s,;]*(?:(?:a\s+|just\s+a\s+)?(?:tiny|small|little|quick)\s+(?:practice|test)\s+run|cheers|ta|tia|thanks\s+a\s+lot)\s*[.!]*\s*$', '', t, flags=re.I)
    t = re.sub(r'[\s,.!]*\b(?:please|pls|plz|thanks|thank you|thx|for me|kindly)\s*[.!]*\s*$', '', t, flags=re.I)
    am = re.match(r'^(\d[\d.]*)\s*([A-Za-z][A-Za-z0-9]{1,9})\s*(?:->|→|=>)\s*([A-Za-z][A-Za-z0-9]{1,9})(?:\s*(?:->|→|=>)\s*(.+?))?\s*[.!]*$', t)
    if am:                                            # "0.17 sui -> usdsui -> ember savings": swap, then lend (2026-11-01)
        t = 'swap %s %s to %s' % am.group(1, 2, 3) + (' and lend it on %s' % re.sub(r'\s+(?:savings|vault|market)$', '', am.group(4), flags=re.I) if am.group(4) else '')
    t = re.sub(r'^(?:retrieve|cash\s+back|bring\s+back)\s+', 'withdraw ', t, flags=re.I)
    t = re.sub(r'^(?:flip|yeet|ape|aping|ape\s+in)\s+(?=\d)', 'swap ', t, flags=re.I)        # slang for a swap (2026-11-01)
    t = re.sub(r"^(?:i\s+(?:want|wanna|would\s+like)\s+to\s+)?do\s+what\s+[A-Za-z]+\s+did\s*[:,\-—]\s*", '', t, flags=re.I)
    t = re.sub(r'^(?:get|take|bring|pull)\s+((?:\d[\d.,]*|all|half)\s+[A-Za-z]\w*)\s+back\s+from\s+', r'withdraw \1 from ', t, flags=re.I)
    t = re.sub(r',?\s*\bunchanged\b\s*,?', ' ', t, flags=re.I)
    t = re.sub(r'[\s,;]*(?:and\s+)?\b(?:leave|keep)\s+(?:the\s+|that\s+)?(?:[A-Za-z][A-Za-z0-9]{1,9}\s+)?(?:it\s+)?(?:loose\s+in\s+my\s+wallet|unlent)\b', ', keep it in my wallet', t, flags=re.I)
    t = re.sub(r'[\s,;]*(?:and\s+)?\b(?:leave|keep)\s+(?:the\s+|that\s+)?(?:[A-Za-z]+\s+){0,2}here\b', ', keep it in my wallet', t, flags=re.I)
    t = re.sub(r'[\s,;]*with\s+the\s+(?:result|output)\s+stay(?:ing|s)\s+in\s+(?:my|this|the)\s+wallet\b', ', keep it in my wallet', t, flags=re.I)
    t = verbless_order(' '.join(t.split()))
    t = ' '.join(t.split())
    t = re.sub(r'^(?:(?:can|could|would|will)\s+you\s+)(?:please\s+)?(?=(?:get|take|pull(?!\s+up)|withdraw|swap|convert|send|lend|deposit|move)\b)', '', t, flags=re.I)
    t = re.sub(r'^withdraw\s+it\s*:\s*', 'withdraw ', t, flags=re.I)
    if not re.search(r'\b(?:clos(?:e|ing)|positions?|longs?|shorts?|perps?|margin|aftermath)\b', t, re.I):   # "close my sui long then withdraw it": the withdraw is the next step (2026-11-13)
        t = re.sub(r'[\s,]*(?:and\s+)?withdraw\s+(?:it|them)\s*[.!]*$', '', t, flags=re.I)
    t = re.sub(r'^(?:get|take|pull)\s+((?:\d[\d.,]*|all|half)\s+\w+)\s+(?:back\s+)?out\s+of\s+', r'withdraw \1 from ', t, flags=re.I)
    try:
        t = plain_wording(t)                          # everyday verbs and wordings the readers did not know (hunt 2026-11-13)
    except Exception as exc:
        print('kivy plain_wording:', str(exc)[:160], flush=True)
    try:                                              # "chuck it in perps", "fund my perps account", "-> perps", "futures" = perps (C1)
        t = PERPWORDS.perps_wording(t)
        t = PERPWORDS.wallet_plus_market(t)           # "20 from my wallet plus 10 from navi into perps" (C10) — perps scan 2026-11-14
    except Exception as exc:
        print('kivy perps_wording:', str(exc)[:160], flush=True)
    return t or msg


def verbless_order(t):
    """An order typed without its verb (a reviewer 1031b, 2026-11-01), given its verb so the readers build it:
    "0.32 SUI into vSUI" = swap, "wallet SUI 0.09, turn it…" = use 0.09 SUI from my wallet, "2.5 USDsui back from Ember" = withdraw,
    "start with 0.28 SUI, buy USDC" = swap, "all of what comes out goes into NAVI Main" = lend it on, "; then SUI;" = swap it to SUI."""
    u = tidy_amounts(t)                              # "0,18sui", "a tenth of one SUI" become numbers first
    isc = lambda w: bool(w) and w.lower() not in G_STOP and bool(ckey(w) or lend_token(w))
    v = u
    m = re.match(r'^(?:my\s+)?wallet\s+([A-Za-z][A-Za-z0-9]{1,9})\s+(\d[\d.]*)\s*,?\s*', v, re.I)
    if m and isc(m.group(1)):
        v = 'use %s %s from my wallet, ' % (m.group(2), m.group(1)) + v[m.end():]
    m = re.match(r'^start\s+with\s+(\d[\d.]*)\s+([A-Za-z][A-Za-z0-9]{1,9})\s*,\s*buy\s+([A-Za-z][A-Za-z0-9]{1,9})\b', v, re.I)
    if m and isc(m.group(2)) and isc(m.group(3)):
        v = 'swap %s %s to %s' % m.group(1, 2, 3) + v[m.end():]
    m = re.match(r'^(\d[\d.]*)\s+(?:wallet\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:from\s+(?:the|my|this)\s+wallet\s+)?(?:goes\s+)?(?:into|to|for)\s+([A-Za-z][A-Za-z0-9]{1,9})\b(?:\s+only\b)?', v, re.I)
    if m and isc(m.group(2)) and isc(m.group(3)):
        v = 'swap %s %s to %s' % m.group(1, 2, 3) + v[m.end():]
    m = re.match(r'^(\d[\d.]*\s+[A-Za-z][A-Za-z0-9]{1,9})\s+back\s+from\s+', v, re.I)
    if m and isc(m.group(1).split()[1]):
        v = 'withdraw %s from ' % m.group(1) + v[m.end():]
    v = re.sub(r'\bbuy\s+([A-Za-z][A-Za-z0-9]{1,9})\s+with\s+(?:that|the|those)\s+[A-Za-z][A-Za-z0-9]{1,9}\b', lambda x: 'swap it to %s' % x.group(1) if isc(x.group(1)) else x.group(0), v, flags=re.I)
    m = re.match(r'^(%s)\s+first\s+(?:take\s+back|take\s+out|withdraw|pull\s+out)\s+(\d[\d.]*\s+[A-Za-z][A-Za-z0-9]{1,9})\b' % G_LEND_MKT, v, re.I)
    if m and isc(m.group(2).split()[1]):              # "ember first take back 0.83 usdsui …"
        v = 'withdraw %s from %s' % (m.group(2), m.group(1)) + v[m.end():]
    if re.match(r'^(?:withdraw|swap|use|get|take|pull|bring|convert|exchange|buy)\b', v, re.I):  # "… usdsui change that into sui": a second step
        v = re.sub(r'(?<![,;])\s+(?:and\s+)?(?:then\s+)?(?:change|turn|convert|make)\s+(?:that|it|them)\s+(?:in)?to\s+', ', swap it to ', v, flags=re.I)
        v = re.sub(r'(, swap it to [A-Za-z][A-Za-z0-9]{1,9})\s+(?:for|in|into|to)\s+(?:my|this|the)\s+wallet\s*[.!]*$', r'\1, keep it in my wallet', v, flags=re.I)
        v = re.sub(r'(?<![,;])\s+(?:and\s+)?(?:then\s+)?make\s+(?:that|it|them)\s+(?=[A-Za-z][A-Za-z0-9]{1,9}\b)', ', swap it to ', v, flags=re.I)
    v = re.sub(r'^(withdraw\s+[^,;]*?)\s+(?:back\s+)?(?:in)?to\s+my\s+wallet\s*[.!]*$', r'\1', v, flags=re.I)   # the wallet is where a withdrawal lands anyway
    v = re.sub(r'[\s,;]*(?:and\s+)?(?:then\s+)?all\s+(?:of\s+)?(?:what\s+comes\s+out|the\s+(?:output|result|proceeds|resulting\s+[A-Za-z][A-Za-z0-9]{1,9}))\s+goes?\s+(?:in|into|on|to)\s+', ', then lend it on ', v, flags=re.I)
    v = re.sub(r'([;,])\s*then\s+(?:to\s+|into\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s*(?=[;,])', lambda x: '%s swap it to %s' % (x.group(1), x.group(2)) if isc(x.group(2)) else x.group(0), v, flags=re.I)
    v = re.sub(r'^(withdraw\b[^;]*?, swap it to [A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$', r'\1, keep it in my wallet', v, flags=re.I)
    return v if v != u else t


POLITE_ORDER_RE = re.compile(r'^\s*(?:could|can|would|will)\s+you\s+(?:please\s+|kindly\s+)?((?:swap|send|transfer|lend|deposit|withdraw|convert)\b[^?]*?)'
                             r'(?:\s+for\s+me)?(?:,?\s+please)?\s*\?\s*$', re.I)


def strip_filler(msg):
    """"ok send 1 usdc to …", "hey kivy swap …": the request without the greeting — only when an action follows, so a bare "yes" stays."""
    m = FILLER_RE.match(msg or '')
    rest = msg[m.end():] if m else msg
    pm = POLITE_ORDER_RE.match(rest)                 # "could you swap 0.15 SUI to USDsui for me?" with an amount: the order itself (2026-10-31)
    if pm and re.search(r'\d', pm.group(1)):
        return pm.group(1)
    gm = re.match(r'^\s*would\s+you\s+mind\s+(swapp|send|lend|withdraw|deposit|convert|transferr)ing\b([^?]*?)(?:\s+for\s+me)?(?:,?\s+please)?\s*\?\s*$', rest, re.I)
    if gm and re.search(r'\d', gm.group(2)):              # "would you mind swapping 0.3 sui into usdsui?"
        return {'swapp': 'swap', 'transferr': 'transfer'}.get(gm.group(1).lower(), gm.group(1).lower()) + gm.group(2)
    rest2 = re.sub(r'^\s*(?:if\s+you\s+(?:don\'?t|do\s+not)\s+mind|if\s+possible|whenever\s+you\s+(?:can|get\s+a\s+chance))\s*,?\s*', '', rest, flags=re.I)
    if rest2 != rest and ACTION_START.match(rest2):        # "if possible, swap 0.5 sui to usdc": the order
        return rest2
    return rest if m and ACTION_START.match(rest) else msg


RAW_ERR_RE = re.compile(r'MoveAbort|abort code|simulation failed|dry.?run|Traceback|TypeError|ReferenceError|ECONN|ETIMEDOUT|fetch failed|'
                        r'status code \d|HTTP Error|<html|\{"|\bat \S+\.(?:ts|js):\d|0x[0-9a-f]{20,}::|'
                        r'urlopen error|timed out|Expecting value', re.I)   # SDK / upstream wording, not Kivy's (+ Python's own: a reviewer K-19)


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


def chat_looks_secret(chat, run_min=12):
    """The saved-chat guard (/api/chats/save): True when any text in the chat, read as the user typed it, holds a seed phrase or
    a private key. Each string is checked raw — the chat's JSON text turned a line break into the two characters '\\n', so a
    phrase pasted one word per line read as 'nability …' and was saved (review scan 2026-11-15, V5). The JSON text is still read
    too, so everything the old check caught (a phrase spread over a list of short strings) is still caught."""
    if looks_secret(json.dumps(chat or {}), run_min):
        return True
    stack, n = [chat], 0
    while stack and n < 100000:                      # every string at any depth, without recursion (the body is at most 160 kB)
        x = stack.pop()
        n += 1
        if isinstance(x, str):
            if looks_secret(x, run_min):
                return True
        elif isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            stack.extend(x)
    return False


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .readguard import G_LEND_MKT, G_STOP, G_VERB_RE, _gnum  # noqa: E402
from .situations import LATER_RE  # noqa: E402
from .wordings import plain_wording  # noqa: E402
from .chainreads import ckey  # noqa: E402
from .lending import lend_token  # noqa: E402
from .chains import chain_market  # noqa: E402
