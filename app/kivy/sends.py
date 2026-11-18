"""Sends read without the AI (send_reply) and cross-account requests (xw_parse, XW) (Section 20).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# send_reply / xw_parse are called by widget_reply and intent_ask; XW is set per request by the Handler (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import difflib
import re
import threading
import unicodedata
from . import amounts as AM
from .config import K, disp
from .intentwords import INTENT_STOP, QUESTION_RE


# ── Sending, read without the AI (2026-10-21: "Send all fund to this wallet : 0x…" was read as the coin FUD) ──
SEND_VERB_RE = re.compile(r'^\s*(?:pls\s+|please\s+|can\s+you\s+|i\s+(?:want|wanna|need)\s+(?:to\s+)?)?(?:send|sned|snd|transfer|trasnfer|tranfer|trnasfer|pay|give|move)\b', re.I)
SEND_SWEEP_RE = re.compile(r'\b(?:all|every|whole|entire)\s+(?:of\s+)?(?:my\s+|the\s+)?(?:funds?|money|assets?|coins?|tokens?|balances?|wallet|crypto|holdings|stuff)\b'
                           r'|\b(?:send|transfer|move|give|pay)\s+(?:over\s+|out\s+)?everything\b|\beverything\s+(?:i\s+(?:have|own|hold)|in\s+(?:my|the)\s+wallet)\b|\bmy\s+whole\s+wallet\b', re.I)
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


# blank or joining marks that are NOT format characters (category Cf), so no_cf's category test missed them: combining grapheme
# joiner, Hangul fillers, Khmer inherent vowels, Mongolian variation selectors, braille blank, variation selectors, tag
# characters (zero-day review 2026-11-13). The page strips the same set in ask().
INVISIBLE_RE = re.compile('[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u2800\u3164\ufe00-\ufe0f\uffa0\U000e0000-\U000e0fff]')


def no_cf(t):
    """Text without invisible characters: format characters (bidi overrides, zero-width: '\u202e10.0\u202c' showed as 0.01,
    a reviewer W2-17) and the invisible marks in INVISIBLE_RE."""
    return INVISIBLE_RE.sub('', ''.join(ch for ch in t if unicodedata.category(ch) != 'Cf'))


REF_NUM_RE = re.compile(r'(?:#\s*|\b(?:invoice|inv|ref|reference|order|ticket|bill|receipt|case|id)\s*(?:no\.?\s*|number\s+|#\s*|:\s*)?)\d+\b(?![.,]\d)', re.I)   # "invoice #12": a reference, not an amount (review W2-12)
ACCT_NUM_RE = re.compile(r'\b(?:my\s+)?(?:acct|account|wallet)\s*(?:no\.?\s*|number\s+|#\s*)?\d{1,2}\b(?![.,]\d)', re.I)   # "acct 1": an account, not an amount (review P-32)


def send_coin_used(text, c):
    """True when a coin word in a send is used AS a coin: an amount or all/half/max/my next to it ("5 deep", "all my deep",
    "deep 5"), or it is what the send verb sends ("send deep to …"). "to my deep wallet", "a treat", "the blue address" are
    words, not coins (review P-30)."""
    for m in re.finditer(r'[A-Za-z][A-Za-z0-9]*', text):
        if ckey(m.group(0)) != c:
            continue
        before, after = text[:m.start()], text[m.end():]
        if re.search(r'(?:\d|\$|%|\b(?:all|max|half|everything|entire|of|some))\s*(?:of\s+)?(?:(?:my|the|your)\s+)?$', before, re.I):
            return True
        if re.match(r'\s*\d', after):
            return True
        ls = re.search(r'([A-Za-z][A-Za-z0-9]*)\s*(?:,|&|\+|\band\b|\bplus\b|\bor\b)\s*(?:(?:all\s+(?:of\s+)?)?(?:my|the)\s+)?$', before, re.I)
        if ls and ckey(ls.group(1)):                 # "all my usdc and sui": the list goes on
            return True
        if re.search(r'^\s*(?:\w+\s+){0,2}?(?:send|sned|snd|transfer|pay|give|move)\s+(?:(?:my|the|some|all\s+(?:of\s+)?my)\s+)?$', before, re.I):
            return True
    return False


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
    two = AM.conflict(msg)                           # two amounts for one send: ask which, never the first (R2, 2026-11-15)
    if two:
        return two
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
        if len(re.findall(r'(?<![\w.])\d+(?:\.\d+)?', base)) > 1 or re.search(r'\bto\s+(?:and|,)', base, re.I):   # a choice would drop the other part (review P-11)
            return {'text': 'Say how much each one gets, in one go — e.g. "send 1 SUI to A and 2 SUI to B", or "send 1 SUI each to A and B".'}
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
    fa = re.search(r'\bfrom\s+(?:my\s+)?(?:acct|account|wallet)\s*(?:no\.?\s*|number\s+|#\s*)?\d{1,2}\b', msg, re.I)
    if fa:                                           # "from acct 2": Kivy can't tell which account that is — never send from this one instead (review P-32)
        return {'text': 'Kivy doesn\'t know which account "%s" is. Switch to that account in your wallet and ask again, or say "from my other account".' % fa.group(0)[5:].strip()}
    an = ACCT_NUM_RE.search(msg) if not to_raw else None
    if an and re.search(r'\b(?:to|into|for)\s+(?:my\s+)?$', msg[:an.start()], re.I):
        others = [x for x in xw_accounts(address) if x['a'].lower() != address.lower()]
        if not others:
            return {'text': 'Kivy can only see the account your wallet is set to (%s…%s). To send to another account, paste its full address '
                            '(0x followed by 64 characters).' % (address[:6], address[-4:])}
        return {'text': 'Which of your accounts is "%s"?' % an.group(0).strip(), 'widget': {'type': 'choose', 'options': [   # numbers mean nothing to Kivy: ask (review P-32)
            {'label': '%s…%s' % (x['a'][:6], x['a'][-4:]) + ((' · ' + x['w']) if x.get('w') else ''), 'ask': msg[:an.start()] + x['a'] + msg[an.end():]} for x in others]}}
    if not to_raw and re.match(r'^\s*(?:\w+\s+){0,3}?(?:pay|give|move)\b', low) and not re.search(r'\d', msg):
        return None                                  # "give me the sui chart": not a send without someone or an amount
    if to and to == address.lower():
        return {'text': 'That\'s your own wallet — sending there would only cost gas. Paste the address you want to send to.'}
    rest = msg.replace(to_raw, ' ') if to_raw else msg
    rest = REF_NUM_RE.sub(' ', ACCT_NUM_RE.sub(' ', rest))   # "acct 1" / "account 2" / "wallet 2" name an account, not an amount (review P-32)
    # which coin(s), how much
    hold = all_balances(address) or {}
    words = [w for w in re.findall(r'[A-Za-z][A-Za-z0-9]*', rest) if w.lower() not in INTENT_STOP | {'send', 'sned', 'snd', 'transfer', 'pay', 'give', 'move',
             'wallet', 'this', 'that', 'address', 'friend', 'fund', 'funds', 'money', 'everything', 'max', 'half', 'worth', 'of', 'dollars', 'usd', 'please', 'pls'}]
    coins = []
    for w in words:
        c = ckey(w)
        if c and c not in coins:
            coins.append(c)
    if len(coins) > 1:                               # "to my deep wallet", "to buy a treat": a coin name used as a word is not a second coin (review P-30)
        used = [c for c in coins if send_coin_used(rest, c)]
        if used:
            coins = used
    tail = (' to %s' % to_raw) if to_raw else ''
    note = ''
    if len(coins) > 1:
        orc = send_or_choice(rest, coins, tail)       # "send 5 usdc or 7 sui to …", "send 5 sui or usdc to …": which one? (QA 2026-11-17, RDR-V2/V12)
        if orc:
            return orc
        head = send_head(rest)                        # "send bob.sui 5 usdc for the 10 sui he sent me": only the coins of the amount list are sent (RDR-V2)
        hc = [c for c in coins if any(ckey(w) == c for w in re.findall(r'[A-Za-z][A-Za-z0-9]*', head))]
        if 0 < len(hc) < len(coins):
            note = 'Only the %s is sent — the %s you mentioned is not part of this send. ' % (' + '.join(disp(c) for c in hc), ' and '.join(disp(c) for c in coins if c not in hc))
            coins, rest = hc, head
    am = re.search(r'\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:usd|dollars?|bucks)\b(?!\s*c)', rest, re.I)
    usd = float(am.group(1) or am.group(2)) if am else None
    if am and len(coins) == 1:                       # "send 5 sui and 5 bucks": two amounts for one coin — ask, never pick one (review P-30)
        q2 = next((x for x in re.finditer(r'(?<![\w.$])(\d+(?:\.\d+)?)\s*(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]*)', rest)
                   if ckey(x.group(2)) == coins[0] and not am.start() <= x.start() < am.end()), None)
        if q2:
            tl = (' to %s' % to_raw) if to_raw else ''
            return {'text': 'I see two amounts — %s %s and $%s. Which one?' % (q2.group(1), disp(coins[0]), num_plain(usd)),
                    'widget': {'type': 'choose', 'options': [{'label': 'Send %s %s' % (q2.group(1), disp(coins[0])), 'ask': 'send %s %s%s' % (q2.group(1), coins[0], tl)},
                                                             {'label': 'Send $%s of %s' % (num_plain(usd), disp(coins[0])), 'ask': 'send $%s of %s%s' % (num_plain(usd), coins[0], tl)}]}}
    # the amount from the ONE amount reader (an ordinal, a count or a reference is never it: "send the 10th payment 5 usdc" sent 10,
    # QA 2026-11-17, RDR-V4); a number it does not count as money is still read when it is the only one ("send 5 to …")
    fa = None if usd is not None else next((a for a in AM.amounts(rest) if a['kind'] in ('units', 'pct') and not a.get('weak')), None)
    n0 = None if usd is not None or fa else AM.first(rest)
    pct = fa['v'] / 100 if fa and fa['kind'] == 'pct' else None
    qty = fa['v'] if fa and fa['kind'] == 'units' else n0
    word = next((m for m in re.finditer(r'\b(all|max|everything|entire|half)\b', rest, re.I) if m.group(1).lower() in amount_words(rest)), None)
    if word and (qty is not None or usd is not None or pct is not None):
        word = None                                  # "send 1 sui to 0x…, max" sent 1,000 USDC: the typed number wins (round 3, 2026-11-03)
    held = [c for c in sorted(hold, key=lambda c: -(float(hold[c]) * (coin_px(c) or 0))) if float(hold.get(c) or 0) * (coin_px(c) or 0) >= 0.01]
    disp_amt = lambda c: dec(word_amount('all', c, hold) or 0)
    # "send half to 0x…", "send half of it to 0x…", "send all of it to …": a share with NO coin named is not "every coin"
    # (it built half of every coin in one plan, 2026-11-13) — ask which coin, like "send it to 0x…" does. "send everything /
    # all my funds" still means every coin.
    if not coins and word and qty is None and usd is None and pct is None and not SEND_SWEEP_RE.search(rest) and held \
            and not re.search(r'\b(?:everything|every\s+coin|each\s+coin|funds?|money|assets?|coins?|tokens?|balances?|crypto|holdings|stuff)\b', rest, re.I) and (word.group(1).lower() == 'half' or re.search(r'\b(?:it|that|this|them|those)\b', rest, re.I)):
        sh = 'half' if word.group(1).lower() == 'half' else 'all'
        if len(held) == 1:                           # one coin in the wallet: that is the coin ("half" was dropped: it offered "send max SUI")
            return send_reply('send %s %s%s' % ('max' if held[0] == 'SUI' and sh == 'all' else sh, held[0], tail), address, history)
        return {'text': 'Which coin should I send %s of%s?' % (sh, tail),
                'widget': {'type': 'choose', 'options': [{'label': 'Send %s %s' % (sh, disp(c)), 'sub': '%s %s' % (dec(word_amount(sh, c, hold) or 0), disp(c)),
                                                          'ask': 'send %s %s%s' % ('max' if c == 'SUI' and sh == 'all' else sh, c, tail)} for c in held]}}
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
        for a in AM.amounts(rest):                   # "send $5 of sui and $3 of usdc to X" sent 5 SUI and 3 USDC: a $ amount is that
            if a['kind'] == 'usd' and a['coin'] in coins:   # many dollars of the coin, at the live price (round 2, 2026-11-16)
                from .readguard import G_STABLE          # a dollar coin is 1:1, like the read guard's "$N of COIN" (never 3.0003 USDC)
                px = 1.0 if a['coin'] in G_STABLE else (coin_px(a['coin']) or 0)
                if not px:
                    return {'text': 'I can\'t price %s right now — say that amount in coins.' % disp(a['coin'])}
                pairs[a['coin']] = '%.6f' % (a['v'] / px)
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
        return {'text': 'You don\'t have any %s in this wallet to send.%s' % (disp(c), lent_hint(address, c))}
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
    out = plan('send', amt, c, recipient=to_raw, holdings=hold, sender=address)
    if note and isinstance(out, dict) and out.get('plan'):
        out = dict(out, text=note + (out.get('text') or ''))
    return out


SEND_REASON_RE = re.compile(r'\s*(?:[(=\u2248~]|\b(?:for|worth|with|because|since|as|after|before|while|leave|leaving|keep|keeping|in\s+exchange|i\s+owe|owe[sd]?|he|she|they'
                            r'|using|at|not|but|except|instead|that\s+i)\b)', re.I)


def send_head(rest):
    """The amount list of a send: the text up to the first word that starts a reason, a price or a "leave" after the first coin
    ("send bob.sui 5 usdc | for the 10 sui he sent me", "send 5 usdc to bob.sui, | leave all my sui", "send 5 usdc | (≈ 7 sui)").
    A second coin after it is mentioned, not sent (QA 2026-11-17, RDR-V2). The whole text when there is no such word."""
    first = next((m for m in re.finditer(r'[A-Za-z][A-Za-z0-9]*', rest) if ckey(m.group(0)) and m.group(0).lower() not in INTENT_STOP), None)
    if not first:
        return rest
    m = SEND_REASON_RE.search(rest, first.end())
    return rest[:m.start()] if m else rest


def send_or_choice(rest, coins, tail):
    """"send 5 usdc or 7 sui to bob.sui" sent both, "send 5 sui or usdc" sent 5 SUI: two coins joined by "or" are asked about
    (QA 2026-11-17, RDR-V2, V12). A choice card, or None."""
    hits = [m for m in re.finditer(r'[A-Za-z][A-Za-z0-9]*', rest) if ckey(m.group(0)) in coins and m.group(0).lower() not in INTENT_STOP]
    for x, y in zip(hits, hits[1:]):
        gap = rest[x.end():y.start()]
        if ckey(x.group(0)) != ckey(y.group(0)) and re.search(r'\bor\b', gap, re.I) and not re.search(r'[,;]|\b(?:and|then|plus)\b', gap, re.I):
            ams = {a['coin']: a for a in AM.amounts(rest) if a['coin'] and a['kind'] in ('units', 'usd')}
            pick = [ams.get(ckey(w.group(0))) for w in (x, y)]
            if not any(pick):
                return {'text': 'Send %s or %s — which one, and how much?' % (disp(ckey(x.group(0))), disp(ckey(y.group(0))))}
            say = lambda a: ('$' + AM._plain(a['v'])) if a['kind'] == 'usd' else AM._plain(a['v'])
            opts = []
            for w, a in zip((x, y), pick):
                a = a or next(p for p in pick if p)
                c = ckey(w.group(0))
                opts.append({'label': 'Send %s %s' % (say(a), disp(c)), 'ask': ('send %s of %s%s' if a['kind'] == 'usd' else 'send %s %s%s') % (say(a), c, tail)})
            return {'text': 'One send — which one?', 'widget': {'type': 'choose', 'options': opts}}
    return None


# ── The user's connected accounts: names in a sentence ("my slush wallet", "account b", 0x0262…) -> which account. Used to
# send to their own other account, and to answer "one account at a time" when a request needs another account to sign
# (plans across several accounts were removed, 2026-11-04). ──
XW = threading.local()           # per request: XW.accounts = [{'a': address, 'w': wallet name, 'n': [nicknames]}]
XW_OTHER_RE = re.compile(r'\bmy\s+other\s+(?:wallet|account)\b', re.I)


# The user's connected accounts for this request (XW.accounts from /api/chat's body), the active one first.
def xw_accounts(address):
    acc = [x for x in (getattr(XW, 'accounts', None) or []) if isinstance(x, dict) and ADDR_RE.match(str(x.get('a') or ''))][:10]
    if address and not any(x['a'].lower() == address.lower() for x in acc):
        acc = [{'a': address, 'w': ''}] + acc
    return acc


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


# Kivy works with one account at a time (2026-11-04; plans across several accounts were removed): the answer when a
# request needs another account to sign — which part to do here, and where to switch for the rest.
def xw_one_account(accts, address, froms):
    name = lambda a: next(((x.get('w') or '')[:40] or a[:6] + '…' + a[-4:] for x in accts if x['a'].lower() == a.lower()), a[:6] + '…' + a[-4:])   # the page's wallet name, capped (S-2)
    order = []
    for a in froms:
        if a.lower() not in [o.lower() for o in order]:
            order.append(a)
    if len(order) > 1 and order[0].lower() == (address or '').lower():
        return {'text': 'That needs approvals from %d of your accounts (%s). Kivy works with one account at a time: ask for the %s part here first, '
                        'then switch to %s and ask for the rest.' % (len(order), ' → '.join(name(a) for a in order), name(order[0]), name(order[1]))}
    return {'text': 'That starts in %s, and Kivy works with one account at a time — the one you have selected now (%s). Switch to %s and ask there.'
                    % (name(order[0]), name(address), name(order[0]))}


def xw_parse(msg, address, history=None):
    """The one-account answer when the request needs another of the user's accounts to sign; None when only the selected
    account is involved (or fewer than 2 are connected) — then the normal readers take it."""
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
        return {'text': 'Kivy works with one account at a time — the one you have selected now (%s). Ask for what to do in this account; '
                        'for another account, switch to it first.' % (accts[me].get('w') or address[:6] + '…' + address[-4:])}
    own = {r[2] for r in refs if isinstance(r[2], int)}
    if not refs or (own <= {me} and not any(r[2] == 'other' for r in refs)):
        return None                                  # only this account: the normal readers
    if not CHAIN_SPLIT.search(msg) and not re.search(r'\b(?:send|move|transfer|forward|withdraw|swap|lend|deposit|use|take|pull|put|park)\b', msg, re.I):
        return None
    if any(r[2] == 'other' for r in refs):
        others = [i for i in range(len(accts)) if i != me]
        if len(others) > 1:
            return {'text': 'Which of your other accounts?', 'widget': {'type': 'choose', 'options': [
                {'label': accts[i]['a'][:6] + '…' + accts[i]['a'][-4:] + ((' · ' + accts[i]['w']) if accts[i].get('w') else ''),
                 'ask': XW_OTHER_RE.sub('my %s wallet' % accts[i]['a'][:6], msg)} for i in others]}}
        refs = [(a, b, others[0] if r == 'other' else r, p) for a, b, r, p in refs]
    # cut into legs
    legs, cur, clauses = [], me, [c for c in CHAIN_SPLIT.split(msg) if c and c.strip()]
    leg = {'acct': cur, 'parts': []}
    for c in clauses:
        rs = [r for r in xw_refs(c, accts, address) if not (isinstance(r[2], str) and r[2].startswith('outsider'))]
        rs = [(a, b, (next(i for i in range(len(accts)) if i != me) if r == 'other' else r), p) for a, b, r, p in rs]
        frm = next((r for r in rs if r[3] in ('from', 'in', 'on', 'using', 'with', 'via') and isinstance(r[2], int)), None)
        to = next((r for r in rs if r[3] in ('to', 'into', 'over to') and isinstance(r[2], int)), None)
        if frm and frm[2] != leg['acct']:
            if leg['parts']:
                legs.append(leg)
            leg = {'acct': frm[2], 'parts': []}
        # a leg only needs to know whether this clause says anything besides who signs (W-4: the rewritten text was
        # never used). "from my vera wallet" / "in my slush" drop out; a "to <account>" counts as a step.
        rest = c
        for a, b, r, p in sorted(rs, reverse=True):
            keep = 'to-account' if to and (a, b) == (to[0], to[1]) else ''
            rest = (rest[:a] if keep else re.sub(r'\b(?:from|in|on|using|with|via)\s+(?:my\s+|the\s+)?$', '', rest[:a])) + keep + rest[b:]
        if rest.strip(' ,\t\n'):
            leg['parts'].append(True)
        if to and to[2] != leg['acct'] and re.search(r'\b(?:send|sned|move|transfer|forward|give|pay|put|bring)\b', c, re.I):
            legs.append(leg)
            leg = {'acct': to[2], 'parts': []}
    if leg['parts'] or not legs:
        legs.append(leg)
    if len(legs) < 2 and legs[0]['acct'] == me:
        return None
    return xw_one_account(accts, address, [accts[lg['acct']]['a'] for lg in legs])


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import num_plain  # noqa: E402
from .situations import amount_words  # noqa: E402
from .chainreads import ADDR_RE, all_balances, ckey, dec, plain_num  # noqa: E402
from .fallback import resolve_recipient  # noqa: E402
from .lending import coin_px, lent_hint, spendable  # noqa: E402
from .chains import CHAIN_SPLIT  # noqa: E402
from .cards import WORD_AMOUNTS, plan, plan_send_many, send_multi_parse, word_amount  # noqa: E402
