"""Ways of saying a multi-step plan + the pieces reader (one coin cut into pieces) (Section 17).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# pieces_rewrite / pieces_parse are called by the chain reader; guess_ask by widget_reply (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import re
from . import perpacct as PERPACCT
from . import amounts as AM
from .config import cap_usd, disp
from .intentwords import COIN_NAMES, INTENT_STOP, VERB_OF


# ── Ways of saying a multi-step plan + the "pieces" reader (Claude's and a reviewer 3-4 step scenarios, 2026-11-07/08) ──
# pieces_rewrite turns common phrasings into the forms the chain reader already knows; pieces_parse reads "swap PART of a
# coin, use the swapped part, keep or use the rest" (one coin cut into pieces, each swapped or not, then lent / sent / kept).
_PSTOP = INTENT_STOP | {'go', 'pay', 'one', 'two', 'free', 'usd', 'dollars', 'dollar', 'rest', 'lot', 'other', 'both', 'left', 'stays',
                        'stay', 'keep', 'leave', 'work', 'way', 'ways', 'wallet', 'kivy', 'yo', 'lent', 'lend', 'send', 'swap', 'main',
                        'market', 'prime', 'high', 'yield', 'ember', 'navi', 'suilend', 'aftermath', 'perps', 'perp', 'margin', 'result'}
_PSHARE = r'half|a\s+third|one\s+third|a\s+quarter|one\s+quarter|two\s+thirds|\d+(?:\.\d+)?\s*%'


def _pcoin(w):
    """A coin word ('sui', 'USDsui', 'bitcoin') -> Kivy's coin key; English words that happen to be tickers -> None."""
    lw = (w or '').lower()
    if not lw or lw in _PSTOP or lw in VERB_OF:
        return None
    return COIN_NAMES.get(lw) or ckey(w) or lend_token(w)


def pieces_rewrite(msg):
    """'wallet 0.21 USDC: send …', '0.2 sui from wallet: …', 'put 0.2 usdc to work: …', 'split it three ways: navi, suilend,
    ember', 'the remainder', 'everything left', 'usdc 2 sui n lend …', 'keep nothing': the same request in the words the chain
    reader knows. Text that matches none of these comes back unchanged."""
    s = re.sub(r'\s*\(([^()]{0,60})\)', lambda m: m.group(0) if re.search(   # "(it's lent not staked)": an aside — but "(and lend the rest
        r'(?i)0x|\d|\.sui\b|\b(?:lend|send|swap|deposit|withdraw|keep|put|park|transfer|convert|perps?|navi|suilend|ember|prime)\b', m.group(1)) else '', msg)   # on suilend)" is a step (review 11-08 C-2)
    s = re.sub(r'(?i)\b([a-z][a-z0-9]{1,9})\s+2\s+(?=[a-z]{2,10}\b|0x)',   # "usdc 2 sui", "rest 2 0x…": chat shorthand for "to"
               lambda m: m.group(1) + ' to ' if (_pcoin(m.group(1)) or m.group(1).lower() in ('rest', 'it', 'them')) else m.group(0), s)
    s = re.sub(r'(?i)(?<=\s)n(?=\s+(?:lend|send|swap|keep|deposit|put|then)\b)', 'and', s)
    s = re.sub(r'(?i)\b(swap|convert|change|turn|flip)\s+(?:it\s+|them\s+)?2\s+([A-Za-z][A-Za-z0-9]{1,9})\b(?!\s+(?:to|into|for|2)\b)',
               lambda m: '%s it to %s' % (m.group(1), m.group(2)) if _pcoin(m.group(2)) else m.group(0), s)   # "swap 2 sui" = to SUI
    s = re.sub(r'(?i)\bunstake\b(?=\s+[^,;]{0,20}?\b(?:usdc|usdsui|usdt|usdy|ausd)\b[^,;]{0,25}?\bfrom\b)', 'withdraw', s)   # a stablecoin is lent, not staked
    s = re.sub(r'(?i)\s*[,;]?\s*(?:\b(?:and|then)\s+)*\b(?:keep|leave)\s+nothing(?:\s+(?:back|in\s+(?:my\s+)?wallet))?\b', '', s)
    s = re.sub(r'(?i)\b(?:the\s+)?(?:remaining|leftover)\s+(?=\d)', '', s)                   # "the remaining 0.12 on ember": that amount
    s = re.sub(r'(?i)(?<!\bmy\s)\b(?:the\s+)?(?:remaining|leftover)\s+([A-Za-z][A-Za-z0-9]{1,9})\b(?![^,;]{0,15}\b(?:in|from)\s+(?:my\s+)?wallet)',
               lambda m: 'the rest of the %s' % m.group(1) if _pcoin(m.group(1)) else m.group(0), s)
    s = re.sub(r'(?i)(?<!\bmy\s)\b(?:the\s+)?(?:remainder|remaining|leftover)\b(?![^,;]{0,20}\b(?:in|from)\s+(?:my\s+)?wallet)', 'the rest', s)   # "my remaining usdc in wallet" is a top-up
    s = re.sub(r"(?i)\b(?:everything|whatever(?:'s|\s+is)?|all\s+(?:that(?:'s|\s+is)\s+)?|what(?:'s|\s+is))\s+left(?:\s+over)?\b", 'the rest', s)
    sw = re.search(r'(?i)\bsplit\s+(?:it|them|that|this|everything)?\s*(two|three|four|2|3|4)\s+ways?\s*(?::|between|across|over|on|among)?\s*(.+)$', s)
    if sw:                                           # "split it three ways: navi, suilend, ember"
        n = {'two': 2, 'three': 3, 'four': 4}.get(sw.group(1).lower()) or int(sw.group(1))
        parts = [p.strip() for p in re.split(r'\s*(?:,|&|\band\b|\bplus\b)\s*', sw.group(2)) if p.strip()]
        if len(parts) == n and all(chain_market(p)[0] not in (None, 'best', 'there') for p in parts):
            share = {2: 'half', 3: 'a third', 4: 'a quarter'}[n]
            s = s[:sw.start()] + ', '.join('lend %s on %s' % (share, p) for p in parts[:-1]) + ' and lend the rest on %s' % parts[-1]
    s = re.sub(r'(?i)(\b(?:in)?to\s+[A-Za-z][A-Za-z0-9]{1,9})\s+(?=(?:half|a\s+third|a\s+quarter|the\s+rest|\d{1,3}\s*%)\s+(?:in)?to\b)', r'\1 and ', s)   # "half into sui half into usdsui"
    amt = r'\$?(\d*\.?\d+)\s*([A-Za-z][A-Za-z0-9]{1,9})'
    head = lambda m: ('take %s %s from my wallet, ' % (m.group(1), m.group(2))) if _pcoin(m.group(2)) else m.group(0)
    s = re.sub(r'(?i)^\s*(?:first\s+)?(?:select|pick)\b', 'take', s)
    s = re.sub(r'(?i)^\s*(?:from\s+)?(?:my\s+)?wallet\s*:?\s*' + amt + r'\b\s*[:,]?\s*', head, s)       # "wallet 0.21 USDC:", "from my wallet: 0.2 usdsui,"
    s = re.sub(r'(?i)^\s*(?:put|use|take)?\s*' + amt + r'(?:\s+from\s+(?:my\s+)?wallet)?(?:\s+to\s+work)?\s*:\s*', head, s)   # "0.2 sui from wallet:", "put 0.2 usdc to work:"
    s = re.sub(r'(?i)\band\s+do\s*:\s*', ', ', s)                                                 # "take 0.25 usdc and do: …"
    _m = r'(?:the\s+)?(?:(?:navi|suilend)(?:\s+(?:main|prime|high\s*yield|ember|sui\s+(?:prime|high\s*yield)))?|ember|matrixdock|prime|high\s*yield)(?:\s+market)?'
    s = re.sub(r'(?i)\b(?:i\s+(?:want|need|would\s+like)\s+(?:to\s+(?:get|have|move)\s+)?|get\s+|move\s+)(\$?\d*\.?\d+)\s*([A-Za-z][A-Za-z0-9]{1,9})\s+(?:out\s+of|from)\s+(%s)\s+'
               r'(?:as|into|in)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+(?:sitting|lent|parked|earning|deposited)\s+(?:on|in|at)\s+(%s)\b' % (_m, _m),
               lambda m: 'withdraw %s %s from %s, swap it to %s, lend it on %s' % m.groups() if _pcoin(m.group(2)) and _pcoin(m.group(4)) else m.group(0), s)   # a goal: "… out of X as SUI sitting on Y"
    s = re.sub(r'(?i)\bi\s+(?:have|got|hold)\s+(?:some\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:lent|deposited|sitting|parked)\s+(?:on|in|at)\s+(%s)\s*[,;]\s*(?:take|withdraw|pull|get|use)\s+(\$?\d*\.?\d+|half|all)\s+of\s+it\b' % _m,
               lambda m: 'withdraw %s %s from %s' % (m.group(3), m.group(1), m.group(2)) if _pcoin(m.group(1)) else m.group(0), s)   # "i have usdc lent on ember, take 0.2 of it"
    tw = re.match(r'(?i)^\s*(lend|deposit|put|park|send)\b(.+?)\s*,?\s+from\s+(?:my\s+)?wallet\s*[.!]?\s*$', s)
    if tw and not re.search(r'(?i)\b(?:withdr\w*|swap|convert|half|rest|third|quarter)\b|%', tw.group(2)):   # "lend 0.1 usdc on navi, … from my wallet"
        body = re.sub(r'0x[0-9a-fA-F]{6,}', ' ', tw.group(2))
        nums = re.findall(r'(?<![\w.$])(\d*\.?\d+)(?:\s*([A-Za-z][A-Za-z0-9]{1,9}))?', body)
        cs = {_pcoin(c) for _, c in nums if c and _pcoin(c)}
        if len(nums) >= 2 and len(cs) == 1:
            s = 'take %s %s from my wallet, %s%s' % (_gnum(round(sum(float(n) for n, _ in nums), 9)), cs.pop(), tw.group(1), tw.group(2))
    s = re.sub(r'(?i)\b(withdraw\s+\$?\d*\.?\d+\s*([A-Za-z][A-Za-z0-9]{1,9})\s+from\s+[^,;]+?)\s*(?:,|;|\band\b)\s*(?:then\s+)?(?:also\s+)?withdraw\s+(?=\$?\d*\.?\d+\s*\2\s+from\b)',
               r'\1 and ', s)                                                                       # a second withdrawal of the same coin
    s = re.sub(r'(?i)(^|[,;]\s*)(\d*\.?\d+(?:\s*%)?|half|a\s+third|a\s+quarter)\s+(lend|send|deposit|park|swap|keep)\b', r'\1\3 \2', s)   # "0.1 lend on navi", "half lend on navi" (scan 2026-11-14 C6): the verb first
    sh = r'half|a\s+third|a\s+quarter|the\s+rest|\d{1,3}(?:\.\d+)?\s*%|\d*\.?\d+'
    s = re.sub(r'(?i)(^|[,;]\s*|\.\s+)(%s)\s+lent\s+(on|in|at)\b' % sh, r'\1lend \2 \3', s)                 # "Half lent on NAVI Prime"
    s = re.sub(r'(?i)(^|[,;]\s*|\.\s+)(%s)\s+(?:in)?to\s+(?:the\s+|my\s+)?(?:aftermath\s+)?(?:perps?|margin)\b' % sh, r'\1deposit \2 into perps', s)   # "Half into Aftermath perps"
    s = re.sub(r'(?i)([,;]\s*)(?:all|everything|it\s+all)\s+(?:in)?to\s+([A-Za-z][A-Za-z0-9]{1,9})\b(?=\s*(?:[,;]|$|\bthen\b|\band\b))',
               lambda m: '%sswap it to %s' % (m.group(1), m.group(2)) if _pcoin(m.group(2)) else m.group(0), s)   # "…, all to sui, send …"
    s = re.sub(r'(?i)\b(lend|deposit|put|park)\s+(it|them|that|the\s+lot|everything)\s+(on|in|into|at)\s+([^,;]{2,30}?)\s+as\s+([A-Za-z][A-Za-z0-9]{1,9})\b',
               lambda m: 'swap %s to %s, %s it %s %s' % (m.group(2), m.group(5), m.group(1), m.group(3), m.group(4)) if _pcoin(m.group(5)) else m.group(0), s)   # "lend it on navi prime as usdsui"
    s = re.sub(r'(?i)\bempty\s+(?:out\s+)?(?:just\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:deposit|position|lend\w*|supply)?\s*(?:on|in|at|from)\s+',
               lambda m: 'withdraw all my %s from ' % m.group(1) if _pcoin(m.group(1)) else m.group(0), s)   # "empty my USDC deposit on Suilend Ember"
    return s


def _pclause(c):
    """What one clause of a pieces request says: an amount (a share, a number, 'the rest'), the coin it ends in, where it goes."""
    t = re.sub(r'0x[0-9a-fA-F]{6,}|(?<![\w.])@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b|(?<![\w.])@[a-z0-9-]{2,}', ' ', c, flags=re.I)   # "alice.sui" is not SUI (scan 2026-11-14 C5)
    t = re.sub(r'(?i)\b([A-Za-z][A-Za-z0-9]{1,9})\s+half\b', lambda m: m.group(1) if _pcoin(m.group(1)) else m.group(0), t)   # "keep the USDC half"
    sh = lambda w: 0.5 if w.startswith('half') else 2 / 3 if 'two' in w else 1 / 3 if 'third' in w else 0.25 if 'quarter' in w \
        else float(re.match(r'[\d.]+', w).group(0)) / 100
    d = {'amt': None, 'acoin': None, 'total': None, 'tgt': None, 'bare': None, 'dest': None}
    num = re.search(r'(?<![\w.$])(\d*\.?\d+)(?![\d.]*\s*%)(?:\s*([A-Za-z][A-Za-z0-9]{1,9}))?', t)
    of = re.search(r'(?i)(?<![\w.])(%s)\s+of\s+(?:the\s+|my\s+)?(\d*\.?\d+)\s*([A-Za-z][A-Za-z0-9]{1,9})\b' % _PSHARE, t)
    shm = re.search(r'(?i)(?<![\w.])(%s)(?![\w.])' % _PSHARE, t)
    if of and _pcoin(of.group(3)):                   # "a third of 0.3 usdc"
        d['amt'], d['total'] = ('frac', sh(of.group(1).lower())), (float(of.group(2)), _pcoin(of.group(3)))
    elif re.search(r'(?i)\b(?:the\s+)?(?:rest|other\s+half)\b', t) and not num:
        d['amt'] = ('rest', None)
    elif shm:
        d['amt'] = ('frac', sh(shm.group(1).lower()))
    elif num:
        d['amt'], d['acoin'] = ('units', float(num.group(1))), _pcoin(num.group(2)) if num.group(2) else None
    if d['amt'] and d['amt'][0] == 'frac' and not 0 < d['amt'][1] <= 1:
        d['amt'] = ('bad', None)
    for m in re.finditer(r'(?i)\b(?:(?:in)?to|into|for|as)\s+(?:some\s+|the\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b', t):
        if _pcoin(m.group(1)):
            d['tgt'] = _pcoin(m.group(1))
            break
    bm = re.search(r'(?i)(?:\b(?:%s)|(?<![\w.])\d*\.?\d+|\bother\s+half)\s+(?:of\s+it\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b' % _PSHARE, t)
    if bm and _pcoin(bm.group(1)):
        d['bare'] = _pcoin(bm.group(1))              # "half sui on navi": a share and the coin it becomes
    d['refs'] = [_pcoin(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', t) if _pcoin(w)]
    d['all'] = bool(re.search(r'(?i)\b(?:everything|both|all\s+of\s+it|all\s+of\s+them|the\s+lot)\b', t))
    addr = re.search(AM.HEX_ADDR, c)
    wd = re.search(r'(?i)\b(?:withdr\w*|take|pull|grab|get|from|out\s+of)\b', t)
    lv = re.search(r'(?i)\b(?:lend\w*|lent|deposit\w*|put|park\w*|supply|earn\w*)\b', t)
    mk = chain_market(t)[0]
    if addr:
        d['dest'] = ('send', addr.group(0).lower())
    elif CHAIN_PERPS.search(t) and (lv or not wd):  # "withdraw all my margin": the perps account is where it starts, not where it goes (C5)
        d['dest'] = ('perps', None)
    elif mk and mk != 'there' and (lv or (not wd and re.search(r'(?i)\b(?:on|in|into|at)\b', t))):
        d['dest'] = ('lend', mk)
    elif lv and not wd:
        d['dest'] = ('lend', 'best')
    elif re.search(r'(?i)\b(?:keep|leave|stays?|hold)\b', t):
        d['dest'] = ('keep', None)
    elif re.search(r'(?i)\b(?:send|transfer|give|pay)\b|\.sui\b|@\w', c):
        nm = re.search(_CRCPT, c, re.I)
        d['dest'] = ('name', nm.group(0) if nm else None)   # a send to a name: resolved by pieces_parse (it went to the plain reader, which sent the wrong coin, C5)
    return d


def pieces_parse(msg, address):
    """One coin cut into pieces, each swapped (or not) and then lent, sent or kept, in ONE transaction: "take 0.28 USDC, swap
    half to SUI, lend that SUI on NAVI and keep the USDC half", "withdraw 0.18 USDC from Ember, swap a third to SUI, send the
    SUI to 0x…, lend the rest of the USDC on NAVI", "swap 0.2 usdc to sui and lend it on navi; send 0.1 usdc to 0x…" (a
    start of 0.3). A chain card, a refusal that names the problem, or None when the message is not one (the plain chain
    reader answers then). Built by the split path of builder.ts /compose; every piece names its coin."""
    if getattr(XW, 'fan_depth', 0) or getattr(XW, 'piece_depth', 0) or not ADDR_RE.match(address or ''):
        return None
    if re.search(r'(?i)\b(?:long|short|leverage|borrow|bridge|bot|every\s+(?:day|week|hour))\b', msg):
        return None
    cl = [x.strip() for x in re.split(r'(?i)\s*(?:[,;:]|\band\s+then\b|\bthen\b|\bafter\s+that\b|\band\b(?=\s+(?:lend|send|keep|leave|swap|convert|change|turn|trade|'
                                      r'deposit|put|park|transfer|give|the\s+(?:rest|other)|half|rest|\d)))\s*', msg) if x and x.strip()]
    cl = [re.sub(r'(?i)^(?:and|then|also|first|finally|ok|so)\s+', '', x).strip() for x in cl]
    cl = [x for x in cl if x and not re.match(r'(?i)^(?:please|pls|thanks?|thank\s+you|done)\W*$', x)]
    if not 2 <= len(cl) <= 7:
        return None
    info = [_pclause(x) for x in cl]
    h = info[0]
    srcish = re.search(r'(?i)\b(?:withdr\w*|take|use|pull|grab|get)\b|\bwallet\b', cl[0])
    if srcish and not h['tgt'] and not (h['dest'] and h['dest'][0] != 'keep'):
        src_coin, rows, head = None, list(zip(cl[1:], info[1:])), cl[0]
        if re.search(r'(?i)\bwallet\b', cl[0]) and not re.search(r'(?i)\b(?:withdr\w*|from\s+(?:the\s+)?(?:navi|suilend|ember|matrixdock|perps|aftermath))\b', cl[0]):
            src_coin = h['acoin']                    # a wallet start names its coin; a withdrawal's coin comes from the reader below
    elif h['amt'] and (h['acoin'] or h['total']) and (h['tgt'] or h['dest'] or (h['bare'] and h['bare'] != (h['acoin'] or h['total'][1]))):
        src_coin, rows, head = h['acoin'] or h['total'][1], list(zip(cl, info)), None   # "swap 0.2 usdc to sui, …": the wallet, every piece added up
    else:
        return None
    pieces, totals = [], []
    for c, b in rows:
        amt = b['amt']
        if amt and amt[0] == 'bad':
            return None
        tgt = b['tgt'] or b['bare']
        if amt is not None or b['tgt']:
            if amt is None:
                return None                          # "swap it to X" with no share: a straight chain, not pieces
            pieces.append({'amt': amt, 'coin': tgt, 'dest': b['dest'], 'ut': False, 'acoin': b['acoin'], 'cl': c})
            if b['total']:
                totals.append(b['total'])
        elif b['dest']:
            ref = b['refs'][0] if b['refs'] else None
            if b['all'] and not ref:                 # "lend everything on navi": every open piece AND what no piece took
                open_ = [p for p in pieces if not p['dest']]
                if not open_:
                    return None
                for p in open_:
                    p['dest'] = b['dest']
                if not any(p['amt'][0] == 'rest' for p in pieces):
                    pieces.append({'amt': ('rest', None), 'coin': None, 'dest': b['dest'], 'ut': False, 'acoin': None, 'restref': True, 'allrest': True})
                continue
            if ref and ref != src_coin and any(p['coin'] == ref for p in pieces):
                p = next((p for p in reversed(pieces) if p['coin'] == ref and not p['dest']), None)
                if not p:
                    return None
                p['dest'] = b['dest']
            elif ref or re.search(r'(?i)\b(?:rest|other\s+half)\b', c):
                pieces.append({'amt': ('rest', None), 'coin': ref, 'dest': b['dest'], 'ut': False, 'acoin': None, 'restref': True})
            else:                                    # "lend it on navi": the piece just named
                p = next((p for p in reversed(pieces) if not p['dest']), None)
                if not p:
                    return None
                p['dest'] = b['dest']
        else:
            return None                              # a clause this reader doesn't understand: leave it to the chain reader
    if not pieces or any(p['dest'] and p['dest'][0] == 'name' and not p['dest'][1] for p in pieces) or sum(1 for p in pieces if p['dest'] and p['dest'][0] == 'perps') > 1:
        return None                                  # a send with no recipient: the plain reader asks; one perps deposit at most
    for p in pieces:                                 # "send 10 usdc to alice.sui, swap the rest to sui": the name resolved HERE — the plain reader
        if p['dest'] and p['dest'][0] == 'name':     # swapped everything first and sent 10 SUI (perps scan 2026-11-14 C5)
            to = resolve_recipient(p['dest'][1])
            if not to:
                return {'text': 'I couldn\'t find %s. Give a full 0x… address or a .sui name that exists.' % p['dest'][1]}
            if to == address.lower():
                return {'text': '%s…%s is your own wallet — sending there moves nothing. Remove it and ask again.' % (address[:6], address[-4:])}
            p['dest'] = ('send', to, p['dest'][1])   # the name stays on the card's step, the full address below it
    if head is None and pieces[0]['amt'][0] == 'units' and pieces[0]['coin'] and pieces[0]['coin'] != src_coin and len(pieces) > 1 \
            and all(p['amt'][0] in ('frac', 'rest') and p['coin'] in (None, pieces[0]['coin']) for p in pieces[1:]):
        return None                                  # "swap 10 sui to usdc, half into perps, half on navi": ALL of it swapped, then the USDC split — the plain reader's (C6)
    # the start: read by the chain reader itself (withdrawals, typos, limits), with the pieces put back after it
    def start(text):
        XW.piece_depth = 1
        try:
            r = chain_parse(text + ', then keep it in my wallet', address)
        finally:
            XW.piece_depth = 0
        w = (r or {}).get('widget') if isinstance(r, dict) else None
        if not isinstance(w, dict) or w.get('type') != 'chain' or (w.get('spec') or {}).get('swaps'):
            return None, r
        return w, r
    if head is not None:
        w0, r0 = start(head)
        if w0 is None:
            return None
        src = dict(w0['spec']['source'])
        src_coin = 'USDC' if src.get('op') == 'perps_out' else src.get('coin')
        est = float(w0['steps'][-1].get('q') or 0)
    else:
        src = None
    for p in pieces:
        if p.get('restref') and p['coin'] not in (None, src_coin):
            return None                              # "keep the SUI" when no piece became SUI
        if p['amt'][0] == 'units' and p['acoin'] and p['acoin'] != src_coin:
            if p['coin'] not in (None, p['acoin']) or head is None:
                return None                          # "send 0.1 SUI" beside USDC steps is the wallet's own SUI: not this reader's
            p['ut'], p['coin'] = True, p['acoin']    # "0.1 sui on navi" after "take 0.25 usdc": checked against the start below
        p['coin'] = p['coin'] if p['coin'] and p['coin'] != src_coin else src_coin
    if not any(p['coin'] != src_coin for p in pieces) or len(pieces) > 4:
        return None                                  # nothing swapped: the plain chain reader (it knows every same-coin split)
    pp = next((p for p in pieces if p['dest'] and p['dest'][0] == 'perps'), None)
    if pp:                                           # "swap 60% to USDC, deposit that USDC into perps, keep the rest" (2026-11-08)
        if pp['coin'] != 'USDC':
            return {'text': 'Perps margin is USDC — swap that part to USDC first, e.g. "swap 60% to USDC and deposit it into perps".'}
        try:
            pa = perp_account(address)
            if not pa.get('has'):
                return {'text': PERPACCT.blocked_text(pa) or 'You don\'t have an Aftermath perps account yet. Say "open perps account" first, then ask again.'}
        except Exception:
            pass
    if sum(1 for p in pieces if p['amt'][0] == 'rest') > 1:
        return None
    units = [p for p in pieces if p['amt'][0] == 'units']
    fracs = [p for p in pieces if p['amt'][0] == 'frac']
    if head is None:                                 # "swap 0.2 usdc to sui …; send 0.1 usdc …": the start is every piece added up
        if totals:
            if any(t[1] != src_coin for t in totals) or len({t[0] for t in totals}) > 1:
                return None
            total = totals[0][0]
        elif units and not fracs and not any(p['amt'][0] == 'rest' for p in pieces):
            ask = AM.bare_after_swap(msg, pieces, src_coin, _pcoin)   # "swap 10 sui to usdc and send 3 to bob.sui" took 13 SUI and sent 3 SUI: 3 of
            if ask:                                           # which coin? asked, never added on top in the start coin (review scan R6, 2026-11-15)
                return ask
            total = sum(p['amt'][1] for p in units)
        else:
            return None
        w0, r0 = start('take %s %s from my wallet' % (_gnum(total), src_coin))
        if w0 is None:
            return r0 if isinstance(r0, dict) and r0.get('text') and not r0.get('widget') else None   # "your wallet has 0.1 USDC, not 0.3"
        src = dict(w0['spec']['source'])
        est = float(w0['steps'][-1].get('q') or 0)
    if any(p['ut'] for p in pieces):                 # pieces named in their new coin: only when they add up to the start exactly
        said = float(src.get('amount')) if isinstance(src.get('amount'), (int, float)) else None
        if fracs or not said or abs(sum(p['amt'][1] for p in units) - said) > 1e-9:
            return {'text': 'Say each part in %s, the coin you start with — e.g. "take %s %s, swap 0.1 of it to SUI and lend it on NAVI, …".'
                            % (disp(src_coin), _gnum(said or est), disp(src_coin))}
    fs0 = sum(p['amt'][1] for p in fracs)
    us0 = sum(p['amt'][1] for p in units)
    if any(p.get('allrest') for p in pieces) and ((fracs and fs0 >= 1 - 1e-9) or (units and us0 >= est * 0.999)):
        pieces = [p for p in pieces if not p.get('allrest')]   # "everything" when the parts already took it all: no empty rest
    said = float(src.get('amount')) if isinstance(src.get('amount'), (int, float)) else None
    if units and fracs and said and not any(p['ut'] for p in pieces):   # "a third of 0.3 usdc …, the other 0.2 usdc …"
        for p in units:
            p['amt'] = ('frac', p['amt'][1] / said)
        fracs, units = fracs + units, []
    fsum, usum = sum(p['amt'][1] for p in fracs), sum(p['amt'][1] for p in units)
    if fsum > 1 + 1e-9 or (units and fracs):
        return {'text': 'Those parts add up to more than everything.'} if fsum > 1 + 1e-9 else None
    if usum > est * 1.001 + 1e-12:
        return {'text': 'Those amounts add up to %s %s, but you\'d have about %s. Try smaller amounts.' % (dec(usum), disp(src_coin), dec(est))}
    if len(pieces) == 1 and not (fracs and fsum < 1 - 1e-9) and not (units and usum < est * 0.999):
        return None                                  # all of it swapped and used: a straight chain, the plain reader's
    if not any(p['amt'][0] == 'rest' for p in pieces):
        if (fracs and fsum < 1 - 1e-9) or (units and usum < est * 0.999):
            pieces.append({'amt': ('rest', None), 'coin': src_coin, 'dest': ('keep', None), 'ut': False})   # what no piece takes stays yours
        else:
            pieces[-1]['amt'] = ('rest', None)       # the last part takes exactly what is left (no rounding dust)
        if len(pieces) > 4:
            return None
    # a kept piece of a WALLET coin never leaves the wallet: drop it and start with only what is used (2026-11-08: "take 1 USDC,
    # swap half, lend it, keep the USDC" is just "swap 0.5 USDC and lend it" — the keep step was noise). A withdrawal's keep stays: it moves money home.
    kp = [p for p in pieces if p['coin'] == src_coin and (p['dest'] or ('keep', None))[0] == 'keep']
    if src.get('op') == 'wallet' and not src.get('plus') and len(kp) == 1 and len(pieces) > 1 and not w0.get('pick'):
        part = lambda p, fx: est * p['amt'][1] if p['amt'][0] == 'frac' else p['amt'][1] if p['amt'][0] == 'units' else max(est - fx, 0)
        fx0 = sum(part(p, 0) for p in pieces if p['amt'][0] != 'rest')
        used = [p for p in pieces if p is not kp[0]]
        sizes = [part(p, fx0) for p in used]
        new_total = round(sum(sizes), 9)
        if new_total > 0 and fx0 <= est * 1.0001:
            w1, r1 = start('take %s %s from my wallet' % (_gnum(new_total), src_coin))
            if w1 is not None:
                w0, src, est = w1, dict(w1['spec']['source']), float(w1['steps'][-1].get('q') or 0)
                for p, z in zip(used, sizes):
                    p['amt'] = ('units', round(z, 9))
                used[-1]['amt'] = ('rest', None)          # the last part takes exactly what is left
                pieces = used
                if not any(p['coin'] != src_coin for p in pieces):
                    return None                            # only the wallet coin left: the plain reader has it
    # each piece: its sink in the spec and its steps on the card
    steps, sinks = list(w0['steps'][:-1]), []
    fixed = sum(est * p['amt'][1] if p['amt'][0] == 'frac' else p['amt'][1] for p in pieces if p['amt'][0] != 'rest')
    for p in pieces:
        to, op = p['coin'], (p['dest'] or ('keep', None))[0]
        part = est * p['amt'][1] if p['amt'][0] == 'frac' else p['amt'][1] if p['amt'][0] == 'units' else max(est - fixed, 0)
        k = {'op': op, 'amount': {'rest': True} if p['amt'][0] == 'rest' else {'frac': round(p['amt'][1], 6)} if p['amt'][0] == 'frac' else {'units': p['amt'][1]}}
        if op == 'send':
            k.update({'to': p['dest'][1], 'coin': to})
        else:
            k['to'] = to                             # perps: 'USDC'
        if op == 'lend':
            mk = p['dest'][1]
            lr = lend_rows_for(to)
            if mk.startswith('fam:'):
                lr = [x for x in [fam_pick(lr, mk[4:])] if x]
            elif mk not in ('best', 'there'):
                if to == 'SUI' and mk in ('navi-highyield', 'navi-prime'):
                    mk = {'navi-highyield': 'navi-sui-yield', 'navi-prime': 'navi-sui-prime'}[mk]
                lr = [x for x in lr if x['market'] == mk]
            if not lr:
                where = ', '.join(LEND_MKTS.get(x['market'], x['name']) for x in lend_rows_for(to)) or 'nowhere Kivy supports'
                wname = LEND_MKTS.get(mk) or (mk[4:].title().replace('Navi', 'NAVI') if mk.startswith('fam:') else 'that market')
                return {'text': '%s can\'t be lent on %s. Where it can be lent: %s.' % (disp(to), wname, where)}
            k['market'], k['apr'] = lr[0]['market'], round(float(lr[0]['total_apr']), 2)
        if to != src_coin:
            try:
                out = float(swap_quote(src_coin, to, part)['out']) if part > 0 else 0
            except Exception:
                p1, p2 = coin_px(src_coin) or 0, coin_px(to) or 0
                out = part * p1 / p2 if p2 else 0
            sp = round(slip_for(src_coin, to) * 100, 3)
            steps.append({'coin': to, 'title': 'Swap %s %s to %s' % (dec(part), disp(src_coin), disp(to)), 'q': part,
                          'tt': 'Swap {q} %s to %s' % (disp(src_coin), disp(to)), 'sub': '≈ %s %s (max %g%% slippage)' % (dec(out), disp(to), sp)})
        else:
            out = part
        if op == 'lend':
            nm = LEND_MKTS.get(k['market'], k['market'])
            steps.append({'coin': to, 'proto': k['market'], 'title': 'Lend ≈ %s %s on %s' % (dec(out), disp(to), nm), 'q': out,
                          'tt': 'Lend ≈ {q} %s on %s' % (disp(to), nm), 'sub': '%.2f%% a year' % k['apr'] + (' · Kivy fee %g%%' % rev_lend_pct() if rev_lend_pct() else '')})
        elif op == 'perps':
            steps.append({'coin': 'USDC', 'proto': 'aftermath', 'title': 'Deposit ≈ %s USDC into your perps account' % dec(out), 'q': out,
                          'tt': 'Deposit ≈ {q} USDC into your perps account', 'sub': 'Aftermath perps margin — no position is opened'})
        elif op == 'send':
            a = k['to']
            if cap_usd() and out * (coin_px(to) or 0) > cap_usd() * 1.0001:
                return {'text': "That send is about $%.2f, over my $%.0f limit per send. Try a smaller amount." % (out * (coin_px(to) or 0), cap_usd())}
            nm = p['dest'][2] if len(p['dest']) > 2 else a[:6] + '…' + a[-4:]
            steps.append({'coin': to, 'title': 'Send ≈ %s %s to %s' % (dec(out), disp(to), nm), 'sub': 'Full address: ' + a, 'full': True,
                          'q': out, 'tt': 'Send ≈ {q} %s to %s' % (disp(to), nm)})
        else:
            steps.append({'coin': to, 'title': 'Keep ≈ %s %s in your wallet' % (dec(out), disp(to)), 'q': out, 'tt': 'Keep ≈ {q} %s in your wallet' % disp(to)})
        sinks.append({x: k[x] for x in ('op', 'to', 'coin', 'amount', 'market') if x in k})
    for st in steps:
        st['icon'] = coin_icon(st.get('coin'))
    spec = {'source': src, 'swaps': [], 'sinks': sinks}
    return {'text': 'Here is the plan: %d steps in one transaction — you approve once, and if any step fails nothing happens.' % len(steps),
            'widget': dict({'type': 'chain', 'steps': steps, 'spec': spec, 'fee_pct': rev_fee_pct()},
                           **({'pick': w0['pick']} if w0.get('pick') and not units else {}))}


# "the same one as last time", "somewhere else", "into my account", "either half or a third": Kivy would have to guess, so it
# asks — with the request itself as one-tap options where it can (a reviewer 3-4 step scenarios, 2026-11-07; the owner OK 2026-11-08)
class _Span:
    """One group of a match, used like a whole match (start / end / group(0))."""
    def __init__(self, m, g):
        self.m, self.g = m, g

    def start(self):
        return self.m.start(self.g)

    def end(self):
        return self.m.end(self.g)

    def group(self, _=0):
        return self.m.group(self.g)


def guess_ask(msg, address):
    m = re.search(r'(?i)\b(?:on|in|into|at|to)\s+(?:the\s+)?(?:same\s+(?:one|place|market|protocol|spot)\s+(?:as\s+)?(?:last\s+time|before|usual|i\s+used)|usual\s+(?:one|place|market|spot)|my\s+usual)\b', msg)
    if not m:                                        # "…and half somewhere else": only where a lend part names no place
        e = re.search(r'(?i)\b(?:lend|deposit|put|park|half|rest|the\s+other)\b[^,;.?]{0,40}?\b((?:(?:on|in|to|into|at)\s+)?(?:(?:somewhere|some\s+place|someplace)\s+else|elsewhere|another\s+(?:market|place|protocol)))\b', msg)
        m = e and _Span(e, 1)
    if m:
        coin = next((_pcoin(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', msg[:m.start()])[::-1] if _pcoin(w)), None) or 'USDC'
        named = chain_market(msg[:m.start()])[0]
        fam = named[4:] if (named or '').startswith('fam:') else None
        rows = [r for r in lend_rows_for(coin) if r['market'] != named and (not fam or r['market'].split('-')[0] != fam)][:3]
        word = m.group(0)
        lead = re.match(r'(?i)(on|in|into|at|to)\s', word)
        opts = [{'label': LEND_MKTS.get(r['market'], r['name']), 'sub': '%.2f%% a year' % float(r.get('total_apr') or 0),
                 'ask': msg[:m.start()] + ('%s %s' % (lead.group(1), MKT_EXACT.get(r['market'], LEND_MKTS.get(r['market'], r['name']))) if lead
                                           else 'on %s' % MKT_EXACT.get(r['market'], LEND_MKTS.get(r['market'], r['name']))) + msg[m.end():]} for r in rows]
        return {'text': ('I don\'t keep a record of past choices — which market?' if re.search(r'(?i)same|usual', word) else 'Which market should the other part go to?'),
                'widget': {'type': 'choose', 'options': opts} if opts else None}
    m = re.search(r'(?i)\b(?:in|into|to)\s+my\s+account\b(?!\s*(?:0x|@|\w+\.sui))', msg)
    if m and not CHAIN_PERPS.search(msg) and not re.search(r'(?i)\b(?:navi|suilend|ember|matrixdock|lend\w*)\b|0x[0-9a-f]{6,}|\.sui\b', msg):   # (review 11-08 C-3)
        return {'text': 'Which account — lend it, or your Aftermath perps margin?', 'widget': {'type': 'choose', 'options': [
            {'label': 'Lend it', 'sub': 'the best rate for the coin', 'ask': msg[:m.start()] + 'and lend it on the best market' + msg[m.end():]},
            {'label': 'Perps margin', 'sub': 'Aftermath — no position is opened', 'ask': msg[:m.start()] + 'into my perps account' + msg[m.end():]}]}}
    m = re.search(r'(?i)\beither\s+(%s|\d*\.?\d+)\s+or\s+(%s|\d*\.?\d+)\b' % (_PSHARE, _PSHARE), msg)
    if m:
        return {'text': 'Which one — %s or %s?' % (m.group(1), m.group(2)), 'widget': {'type': 'choose', 'options': [
            {'label': x.capitalize(), 'ask': msg[:m.start()] + x + msg[m.end():]} for x in (m.group(1), m.group(2))]}}
    return None


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import perp_account  # noqa: E402
from .readguard import _gnum  # noqa: E402
from .chainreads import ADDR_RE, ckey, coin_icon, dec  # noqa: E402
from .fallback import resolve_recipient  # noqa: E402
from .lending import LEND_MKTS, coin_px, lend_rows_for, lend_token  # noqa: E402
from .chains import CHAIN_PERPS, MKT_EXACT, _CRCPT, chain_market, chain_parse, fam_pick  # noqa: E402
from .txreads import rev_fee_pct, rev_lend_pct, slip_for, swap_quote  # noqa: E402
from .sends import XW  # noqa: E402
