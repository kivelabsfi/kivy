"""Everyday wordings: plain_wording() / wallet_wording() say a request the readers' way (Section 6).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# plain_wording runs at the end of tidy_wording; wallet_wording first in widget_reply_raw (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import re
from . import amounts as AM
from . import followup as FOLLOWUP
from . import lendmoves as LENDMOVES
from . import perpwords as PERPWORDS
from .config import K, disp
from .intentwords import INTENT_STOP, QUESTION_RE
from .patterns import MKT_SPACE_RE, TICKER_WORD_RE

market_overview = None     # server.py's (Section 21): set by server.py once it is defined (a module never imports server.py)
widget_reply = None        # server.py's (Section 7): set by server.py once it is defined (a module never imports server.py)


# ── Everyday wordings (better-answer hunt, a reviewer 2026-11-13: tests/better_answers_fable_1113.json) ──
# 106 requests got a worse reply than Kivy already gives for the same intent in another wording: a verb the readers did not
# know ("put my usdc on navi" -> the generic "What would you like to do with USDC?" menu), one coin read the wrong way round
# ("swap my sui" bought SUI), a balance question the AI then added up wrongly. Two passes, both BEFORE the readers:
#   plain_wording(t)                 words only, run at the end of tidy_wording: the same request in the wording a reader knows
#   wallet_wording(msg, address, h)  needs the wallet: a reply, {'_rewrite', '_say'?} or None; run first by widget_reply_raw
# Neither invents an amount, a coin or a market: a rewrite only renames the verb, and anything unclear still gets a question.
_PW_C = r'[A-Za-z][A-Za-z0-9]{1,9}'
_PW_AMT = r'(?:\$?\d+(?:\.\d+)?\s*%?|all|half|max|everything|a\s+third|a\s+quarter)'
_PW_WHAT = r'((?:%s\s+(?:of\s+)?)?(?:(?:my|the)\s+)?(%s))' % (_PW_AMT, _PW_C)      # "0.5 sui", "half my usdc", "my usdsui", "usdc"
_PW_END = r'\s*[.!]*\s*$'
_PW_PEOPLE = r'(?:friends?|someone|somebody|mate|buddy|brother|sister|mom|mum|dad|wife|husband|family|parents?|people|a\s+person)'


def _pw_coin(w):
    """A coin word (never a filler or a verb): ckey / lend_token."""
    return None if not w or w.lower() in G_STOP | {'out', 'back', 'up', 'any', 'more', 'some', 'is', 'are', 'do', 'does', 'much', 'many', 'everything', 'one', 'now', 'here', 'there'} \
        else (ckey(w) or lend_token(w))


def plain_wording(t):
    """The same request in a wording the readers already know (2026-11-13). Words only: no wallet, no amounts added."""
    t0 = t
    t = re.sub(r"^\s*whats\b", "what's", t, flags=re.I)                       # "whats my balance" went to the AI; "what's …" is read by code
    t = re.sub(r'^\s*wd\b(?=\s+\S)', 'withdraw', t, flags=re.I)               # "wd usdc"
    t = re.sub(r'^\s*swp\b(?=\s+\S)', 'swap', t, flags=re.I)                  # "swp sui to usdc"
    t = re.sub(r'^\s*un-?lend\b(?=\s+\S)', 'withdraw', t, flags=re.I)         # "unlend usdc" was "corrected" to "lend usdc" — the opposite job
    t = re.sub(r'^\s*bal\s*[?.!]*\s*$', 'balance', t, flags=re.I)
    t = re.sub(r'^\s*stake\s*[?.!]*\s*$', 'stake sui', t, flags=re.I)
    if re.search(r'0x[0-9a-fA-F]{6,}|(?<![\w.])@?[a-z0-9-]+\.sui\b|(?<![\w@])@[a-z0-9-]{2,}\b', t, re.I):
        t = re.sub(r'^\s*(?:tip|gift|donate)\s+(?=\S)', 'send ', t, flags=re.I)   # "tip 0.1 sui to 0x…": a send, like give / pay
    # the Send card, said loosely: "i want to send", "send some", "send coins to my friend" (the AI asked three questions in words)
    if re.match(r"^\s*(?:i(?:\s+(?:want|wanna|need|would\s+like)|'d\s+like|\s+d\s+like)\s+to\s+|let\s+me\s+|can\s+i\s+|how\s+do\s+i\s+)?(?:send|transfer)"
                r"(?:\s+(?:some|something|money|funds?|coins?|tokens?|crypto|some\s+(?:money|coins?|crypto|funds?)))?"
                r"(?:\s+to\s+(?:(?:my|a|some)\s+)?%s)?\s*[?.!]*\s*$" % _PW_PEOPLE, t, re.I) and not re.match(r'^\s*(?:send|transfer)\s*[?.!]*\s*$', t, re.I):
        return 'send'
    # lend verbs: "put my usdc on navi", "put usdc in navi", "stick half my sui into suilend" (with an amount it already worked)
    m = re.match(r'^\s*(?:put|stick|stash)\s+%s\s+(?:on|in|into|onto|to)\s+(?:the\s+)?(?=(?:%s)\b)' % (_PW_WHAT, G_LEND_MKT), t, re.I)
    if m and _pw_coin(m.group(2)):
        t = 'lend %s on %s' % (m.group(1), t[m.end():])
    t = LENDMOVES.back_wording(t, _pw_coin, _mk_of) or t   # "i would like some of my usdc back from lending please, 0.2" = withdraw 0.2 usdc (2026-11-14)
    # withdraw verbs: "pull my usdc", "pull usdc from navi", "remove my usdc from suilend", "take out 0.1 usdc"
    m = re.match(r'^\s*(?:pull\s+out|take\s+out|get\s+back|pull|remove|unstash)\s+%s(?:\s+(?:back\s+)?(?:from|out\s+of|off)\s+(?:the\s+)?(%s))?%s' % (_PW_WHAT, G_LEND_MKT, _PW_END), t, re.I)
    if m and _pw_coin(m.group(2)):
        t = 'withdraw %s%s' % (m.group(1), (' from ' + m.group(3)) if m.group(3) else '')
    # a market and no coin: "get out of navi", "pull out of navi", "exit suilend" (the market was dropped: every position, or another market's)
    m = re.match(r'^\s*(?:(?:get|pull|cash|take)(?:\s+(?:me|it\s+all|everything|all|my\s+(?:money|funds|coins)))?\s+out\s+of|exit|leave|quit)\s+(?:the\s+)?(%s)%s' % (G_LEND_MKT, _PW_END), t, re.I)
    if m:
        t = 'withdraw from %s' % m.group(1)
    # "close my suilend position" / "close my lending position": lending, not perps ("You have no open perps positions.")
    m = re.match(r'^\s*(?:close|exit|end)\s+(?:(?:all\s+)?my\s+|the\s+)?(%s)\s+(?:positions?|deposits?|loans?|lend\w*|savings)%s' % (G_LEND_MKT, _PW_END), t, re.I)
    if m:
        t = 'withdraw from %s' % m.group(1)
    if re.match(r'^\s*(?:close|exit|end)\s+(?:(?:all\s+)?my\s+|the\s+)?(?:lending|lent|lend|savings|earn)\s+(?:positions?|deposits?)%s' % _PW_END, t, re.I):
        t = 'my lending positions'
    t = re.sub(r'^\s*exit\s+(?=(?:my|the)\s+(?:\w+\s+){0,2}(?:position|long|short|trade)\b)', 'close ', t, flags=re.I)   # "exit my btc position"
    # two coins and a direction: "trade sui for usdc", "sui to usdc", "usdc -> sui" (asked "Which way?")
    m = re.match(r'^\s*(?:(?:trade|exchange|change|switch)\s+(?:my\s+)?)?(%s)\s*(?:\bto\b|\bfor\b|\binto\b|->|→|=>|>)\s*(%s)%s' % (_PW_C, _PW_C, _PW_END), t, re.I)
    if m and _pw_coin(m.group(1)) and _pw_coin(m.group(2)) and _pw_coin(m.group(1)) != _pw_coin(m.group(2)):
        t = 'swap %s to %s' % (m.group(1), m.group(2))
    # "i need usdc", "i need more sui for gas": get that coin = the swap card into it
    m = re.match(r"^\s*i\s+(?:need|want)\s+(?:(?:some|more|a\s+bit\s+of|a\s+little|to\s+(?:get|buy))\s+)*(%s)(?:\s+for\s+(?:gas|fees?))?%s" % (_PW_C, _PW_END), t, re.I)
    if m and _pw_coin(m.group(1)):
        t = 'buy %s' % m.group(1)
    # "invest 1 usdc", "save my usdc", "grow my sui": lending
    m = re.match(r'^\s*(?:invest|save|grow)\s+%s%s' % (_PW_WHAT, _PW_END), t, re.I)
    if m and _pw_coin(m.group(2)):
        t = 'lend %s' % m.group(1)
    # "buy btc perp" / "sell eth perps": a perps order, not "spot or perps?"
    m = re.match(r'^\s*(buy|sell)\s+(%s)\s+(?:perps?|perpetuals?)%s' % (_PW_C, _PW_END), t, re.I)
    if m and chart_symbol(m.group(2)):
        t = '%s %s' % ('long' if m.group(1).lower() == 'buy' else 'short', m.group(2))
    # "show portfolio", "open perps", "list perps markets": pages Kivy has (they were read as a perps MARKET name — "list" as LIT)
    if re.match(r'^\s*(?:open|show|view|see|list|check|display)\s+(?:me\s+)?(?:my\s+|the\s+)?portfolio%s' % _PW_END, t, re.I):
        t = 'my portfolio'
    elif re.match(r'^\s*(?:(?:open|show|view|see|check|display)\s+(?:me\s+)?(?:my\s+|the\s+)?wallet|my\s+wallet)\s*[?.!]*\s*$', t, re.I):
        t = 'wallet balance'
    elif re.match(r'^\s*(?:open|show|view|see|list|display)\s+(?:me\s+)?(?:the\s+|all\s+(?:the\s+)?)?(?:perps?|perpetuals?|trading)\s+markets?%s' % _PW_END, t, re.I):
        t = 'perps markets'
    elif re.match(r'^\s*(?:open|show|view|see|display|go\s+to)\s+(?:me\s+)?(?:my\s+|the\s+)?(?:perps?|perpetuals?)%s' % _PW_END, t, re.I):
        t = 'perps'
    # "lend usdc wherever is best"
    if re.match(r'^\s*(?:lend|deposit|supply|put|park)\b', t, re.I):
        t = re.sub(r"\s+wherever\s+(?:is\s+|it'?s\s+|it\s+is\s+|pays\s+|has\s+|gives\s+)?(?:the\s+)?(?:best|most|highest)(?:\s+(?:rate|yield|apy|apr))?\s*[.!]*\s*$", ' at the best rate', t, flags=re.I)
    # "close all and withdraw everything": the exit list (it opened the lending positions only)
    if re.match(r'^\s*close\s+(?:all|everything|it\s+all)\s+(?:and|then|,)\s*(?:withdraw|cash\s+out|take\s+out|pull\s+out)\s+(?:everything|all|it\s+all)(?:\s+(?:from\s+)?everywhere)?%s' % _PW_END, t, re.I):
        t = 'get me out of everything'
    # one coin's holding, asked plainly: "how much usdc do i have", "do i have any usdc", "where is my usdc", "my usdc"
    m = re.match(r"^\s*(?:how\s+(?:much|many)\s+(%s)(?:\s+(?:do\s+i\s+(?:have|hold|own)|have\s+i(?:\s+got)?|i\s+(?:have|hold|own|got)))?(?:\s+(?:in\s+total|now|right\s+now|left))?"
                 r"|do\s+i\s+(?:have|hold|own)\s+(?:any\s+|some\s+)?(%s)|(?:have\s+i\s+)?got\s+any\s+(%s)|where(?:'s|\s+is|\s+are|\s+did)\s+(?:all\s+)?my\s+(%s)(?:\s+go)?"
                 r"|(?:show\s+(?:me\s+)?)?my\s+(%s)(?:\s+(?:balance|holdings?|total))?|(%s)\s+(?:balance|holdings?))\s*[?.!]*\s*$" % ((_PW_C,) * 6), t, re.I)
    if m:
        c = next(g for g in m.groups() if g)
        if _pw_coin(c):
            t = 'what is my %s worth' % c
    return t if t != t0 else t0


MAXLEV_Q_RE = re.compile(r"^\s*(?:what(?:'s|\s+is)\s+(?:the\s+)?|how\s+much\s+is\s+(?:the\s+)?)?(?:max(?:imum)?|highest|most|top)\s+leverage\s+(?:on|for|of|with|i\s+can\s+(?:use|get)\s+on)\s+(?:the\s+)?([A-Za-z0-9&]{2,12})(?:\s+(?:perps?|market))?\s*[?.!]*\s*$"
                         r"|^\s*how\s+much\s+leverage\s+(?:can\s+i\s+(?:use|get|have)|is\s+(?:there|allowed)|do\s+(?:i|you)\s+(?:get|have|allow))\s+(?:on|for|with)\s+(?:the\s+)?([A-Za-z0-9&]{2,12})\s*[?.!]*\s*$"
                         r"|^\s*(?:what(?:'s|\s+is)\s+)?([A-Za-z0-9&]{2,12})(?:'s)?\s+(?:max(?:imum)?|highest)\s+leverage\s*[?.!]*\s*$", re.I)
EARN_ON_RE = re.compile(r"^\s*(?:how\s+much|what)\s+(?:(?:interest|yield|money)\s+)?(?:would|will|can|could|do|does)\s+.{0,40}?\b(?:earn|make|yield|pay\s+me)\b", re.I)


def _last_said(history, find):
    """What the user's own latest earlier turn named (never Kivy's text, never a bracketed page memo): find(text) -> value or None.
    For "it / that": only the user's words can say what "it" is (the same source said_recipient() trusts)."""
    for h in reversed(history or []):
        if isinstance(h, dict) and h.get('role') == 'user':
            tx = str(h.get('text') or h.get('content') or '')
            if tx.lstrip().startswith('['):
                continue
            v = find(tx)
            if v:
                return v
    return None


def _one_coin(text):
    """The one coin a sentence names (None when it names none or several)."""
    cs = []
    for w in re.findall(_PW_C, str(text or '')):
        c = _pw_coin(w)
        if c and c not in cs and not _mk_of(w):
            cs.append(c)
    return cs[0] if len(cs) == 1 else None


DEP_TRADE_RE = re.compile(r'^(?P<head>.+?)\s*(?:,\s*(?:and\s+)?(?:then\s+)?|\band\s+then\s+|\bthen\s+|\band\s+|\bafter\s+that\s+)'
                          r'(?P<trade>(?:(?:i\s+want\s+to\s+|to\s+)?(?:open|go|take|place|enter)\s+)?(?:(?:a|an|my)\s+)?(?:[\w.&$]+\s+){0,3}?(?:long|short)\b.*)$', re.I)


def deposit_then_trade(msg, address, history=None):
    """"withdraw 10 usdc from navi, deposit into perps and long sui 3x", "deposit 10 usdc to perps and open a 2x btc long" (2026-11-13: yes): the money step is its own transaction — the chain card, or the perps deposit card — and the trade is the
    NEXT STEP, sent by the page once the deposit has landed (the same way a close carries its next step). It was refused ("Kivy
    can deposit margin … but not open a position") or the trade ate the sentence and the deposit was lost.
    Perps scan 2026-11-14: a stop-loss / take-profit typed with the trade is said, never dropped (C2); the order's margin is what
    reached perps (C3); a swap or withdrawal is never lost to the order reader (C7); a second order, another wallet, borrowing,
    a deposit over the balance and a leverage over the market's limit are said before anything is signed (C8); no market or an
    unknown one asks (C9)."""
    m = DEP_TRADE_RE.match(msg or '')
    if not m:
        th = PERPWORDS.trade_tail(msg)               # "…, deposit into perps and trade it": which order? (C8)
        if th and CHAIN_PERPS.search(th) and re.search(r'\b(?:deposit\w*|put|add|fund|top\s*up|move|send|into|to)\b', th, re.I) \
                and not QUESTION_RE.search(msg) and not CHAIN_BOT.search(msg):
            return PERPWORDS.trade_tail_reply()
        return None
    head, trade = m.group('head').strip(' ,'), m.group('trade').strip(' ,.!')
    if not CHAIN_PERPS.search(head) or not re.search(r'\b(?:deposit\w*|put|add|fund|top\s*up|move|send|into|to)\b', head, re.I) \
            or re.search(r'\b(?:long|short|close|borrow\w*)\b', head, re.I) or CHAIN_BOT.search(msg) or QUESTION_RE.search(msg):
        return None
    if BORROW_RE.search(msg) or re.search(r'\b(?:long|short)[\s-]+(?:term|run|haul|while|time)\b', trade, re.I):
        return None                                  # "…and borrow more usdc to add": the borrow answer says so (C8); "hold it long term" is no order
    accts = xw_accounts(address)
    ow = PERPWORDS.other_wallet(msg, [n for x in accts for n in list(x.get('n') or []) + (x.get('w') or '').lower().split()[:1]])
    if ow:                                           # "…from my other wallet": the connected wallet only (C8); with 2+ accounts connected
        return None if len(accts) > 1 else PERPWORDS.wallet_reply(ow)   # the cross-account reader (xw_parse) answers it
    order, extras = PERPWORDS.trade_extras(trade)    # "…long sui 3x and set a stop loss at 0.8": the order, then what to say about the rest (C2)
    if re.search(r'\b(?:close|cancel|reduce|flip)\b', order, re.I):
        return None                                  # "…long sui 3x and close my eth long": never half of it — the chain reader says what to type
    orders = PERPWORDS.split_orders(order)
    if orders:                                       # "long and short btc", "long sui 3x and short btc 2x": one order at a time (C8)
        return PERPWORDS.orders_reply(head, orders)
    side = re.search(r'\b(long|short)\b', order, re.I).group(1).lower()
    t2 = MKT_SPACE_RE.sub(lambda x: (x.group(1) + x.group(2)) if chart_symbol(x.group(1) + x.group(2)) else x.group(0), order)
    stop = TRADE_STOP | {'same', 'money', 'all', 'it', 'that', 'then', 'using', 'use', 'lev', 'times', 'on', 'x'}
    syms = list(dict.fromkeys(chart_symbol(w) for w in TICKER_WORD_RE.findall(t2) if w.lower() not in stop and chart_symbol(w)))
    if len(syms) > 1:                                # "…and long sol, pump is coming": which market? never the first word (QA 2026-11-17, RDR-V22)
        return {'text': 'Which market should the order be on — %s? Say the request again with one market.' % ' or '.join(syms[:4])}
    sym = syms[0] if syms else None
    if not sym:                                      # no market, or one Aftermath doesn't list: ask, keeping the request (C9)
        if not address:
            return {'text': K()['no_wallet']}
        word = next((w for w in TICKER_WORD_RE.findall(t2) if w.lower() not in stop | {'something', 'anything', 'whatever', 'one', 'this', 'coin', 'token',
                                                                                      'trade', 'leveraged', 'leverage', 'maximum', 'highest', 'full'}), None)
        return PERPWORDS.no_market_reply(head, side, word)
    if not address:
        return {'text': K()['no_wallet']}
    try:
        mx = float(perp_market(sym).get('max_lev') or 0)
    except Exception:
        mx = 0
    ask = PERPWORDS.order_ask(t2, sym, mx)
    if mx and (PERPWORDS.lev_of(ask) or 0) > mx + 1e-9:   # 500x on a 15x market: said before the deposit is signed (C8, #74)
        return PERPWORDS.lev_reply(sym, PERPWORDS.lev_of(ask), mx)
    size = AM.usd_first(t2)                          # never the limit price (round 2)
    # the deposit's own USDC amount is the margin of the order ("deposit 10 usdc to perps and long sui 3x" = 10 USDC in at 3x)
    hm = re.search(r'(?<![\w.$])(\d+(?:\.\d+)?)\s*(?:usdc|usd|dollars?)\b', head, re.I) or re.search(r'\$\s*(\d+(?:\.\d+)?)', head)
    usdc_head = bool(hm) and not re.search(r'\b(?:swap|convert|sell|exchange)\b', head, re.I)
    try:
        r = widget_reply(head, address, history)
    except Exception:
        r = None
    w = (r or {}).get('widget') or {}
    ok = (w.get('type') == 'chain' and any((k or {}).get('op') == 'perps' for k in (w.get('spec') or {}).get('sinks') or [])) \
        or (w.get('type') == 'perpdep' and PERPWORDS.plain_deposit_head(head))   # a swap or withdrawal is never lost to the order reader (C7)
    if not ok:
        if r and not w.get('type') in ('lend', 'swaplend', 'swap', 'withdraw', 'perpdep'):   # a question or a plain "can't" about the money step: say it, with the trade named
            return dict(r, text=(r.get('text') or '') + ' Then say "%s" for the order.' % ask)
        return PERPWORDS.deposit_part_reply(sym, side, ask)
    if w.get('type') == 'perpdep' and usdc_head and float(hm.group(1)) > float(w.get('max') or 0) + 1e-9:
        return PERPWORDS.capped_reply(hm.group(1), w.get('max') or 0, ask)   # 999 USDC asked, 150 held: say it, no capped card (C8, #83)
    mg = PERPWORDS.card_margin(w) if not size and usdc_head else 0
    if mg > 0:                                       # the margin is what reaches perps: the card's own deposit step, not the first number typed (C3)
        ask += ' with %s usdc margin' % num_plain(mg)
    w['next'] = ask
    try:
        mark = float((perp_market(sym) or {}).get('price') or 0) if extras else None
    except Exception:
        mark = None
    words = PERPWORDS.tpsl_words(extras, side, mark)
    bad = PERPWORDS.tpsl_problem(*PERPWORDS.tpsl_numbers(extras, side, mark), side, mark) if words and mark else ''
    if bad:                                          # wrong side of the price: said now, never carried to the order
        say = PERPWORDS.tpsl_off_text(bad, sym, side)
    elif words:                                      # the order that follows the deposit places it in its own approval (2026-11-15)
        w['next'] = ask + ', ' + words
        say = ' Your %s comes with that order, in the same approval.' % words.replace('stop loss', 'stop-loss').replace('take profit', 'take-profit')
    else:
        say, _ = PERPWORDS.extras_say(extras, sym, side, mark)
    r['text'] = (r.get('text') or '').rstrip() + ' When it has landed, your %s %s order comes up for its own approval.%s' % (sym, side, say)
    return r


def wallet_wording(msg, address, history=None):
    """Requests that need the wallet (or the chat) to be read right (2026-11-13). A reply, {'_rewrite', '_say'?} or None."""
    low = msg.lower().strip()
    if QUESTION_RE.search(msg) and not MAXLEV_Q_RE.match(msg) and not EARN_ON_RE.match(msg):
        return None
    # "max leverage on btc": the market's own limit, from Aftermath (the AI offered orders and never said the number)
    m = MAXLEV_Q_RE.match(msg)
    if m:
        sym = chart_symbol(next(g for g in m.groups() if g))
        if sym:
            try:
                mx = float(perp_market(sym).get('max_lev') or 0)
            except Exception:
                mx = 0
            if mx:
                r = {'text': '%s perps go up to %gx on Aftermath. Kivy never picks the leverage for you: say it with the order, e.g. "long %s $10 3x".' % (sym, mx, sym)}
                try:
                    mv = market_overview(sym, address)
                    if mv:
                        mv['venue'] = 'perp' if mv.get('perp') else 'spot'
                        r['widget'] = mv
                except Exception:
                    pass
                return r
    # "how much would i earn on 4 usdc in a year": the sum, at today's best rate (the AI showed the table and no number)
    m = EARN_ON_RE.match(msg)
    if m and not re.search(r'\b(?:have\s+i|did\s+i|am\s+i|long|short|perps?|leverage|swap|sell|trade)\b', low):
        nm = re.search(r'\$?(\d+(?:\.\d+)?)\s+(?:of\s+)?(%s)' % _PW_C, msg)
        coin = _pw_coin(nm.group(2)) if nm else None
        if coin and float(nm.group(1)) > 0:
            rows = lend_rows_for(coin)
            if rows:
                amt, best = float(nm.group(1)), rows[0]
                apr, px = float(best['total_apr']), float(coin_px(coin) or 0)
                name = LEND_MKTS.get(best['market'], best.get('name') or best['market'])
                yr = amt * apr / 100
                usd = (' (about $%s)' % dec(yr * px, 4)) if px and coin not in G_STABLE else ''
                return {'text': '%s %s lent on %s — the best %s rate right now, %.2f%% a year — would earn about %s %s in a year%s: about %s a month, %s a day. '
                                'Rates move with demand, so this is today\'s rate, not a promise.'
                                % (num_plain(amt), disp(coin), name, disp(coin), apr, dec(yr, 6), disp(coin), usd, dec(yr / 12, 6), dec(yr / 365, 6)),
                        'widget': {'type': 'choose', 'options': [
                            {'label': 'Lend %s %s on %s' % (num_plain(amt), disp(coin), name), 'sub': '%.2f%% a year' % apr, 'ask': 'lend %s %s on %s' % (num_plain(amt), coin, mk_name(best['market']))},
                            {'label': 'See all %s markets' % disp(coin), 'sub': 'rates and collateral', 'ask': 'lending markets for %s' % disp(coin)}]}}
    # bare "long" / "short": which market? (the AI opened the deposit card)
    m = re.match(r'^\s*(?:go\s+|open\s+(?:a\s+)?)?(long|short)(?:\s+(?:position|trade|something))?\s*[?.!]*\s*$', msg, re.I)
    if m:
        return {'text': 'Which market do you want to %s? Tap one for its chart, or say it with a size — e.g. "%s BTC $10".' % (m.group(1).lower(), m.group(1).lower()),
                'widget': {'type': 'perpmarkets', 'rows': perp_market_list()}}
    # "long it" / "short that": the market the user's own last turn named, sized by the user (the AI offered 25x nobody asked for)
    m = re.match(r'^\s*(?:go\s+|open\s+(?:a\s+)?)?(long|short)\s+(?:it|that|this|that\s+one|this\s+one)(?:\s+(?:position|trade))?\s*[?.!]*\s*$', msg, re.I)
    if m:
        sym = _last_said(history, lambda tx: next((chart_symbol(w) for w in TICKER_WORD_RE.findall(tx) if w.lower() not in TRADE_STOP | INTENT_STOP | {'chart', 'show', 'how', 'what', 'is', 'whats'} and chart_symbol(w)), None))
        if sym:
            return {'_rewrite': '%s %s' % (m.group(1).lower(), sym)}
        return None
    # "lend my sui and my usdc": two coins are two lends — say so with both, one tap each (the AI promised two cards and showed one)
    m = re.match(r'^\s*(?:lend|deposit|supply)\s+(?:(?:all\s+)?my\s+)?(%s)\s*(?:and|&|\+|,)\s*(?:(?:all\s+)?my\s+)?(%s)(\s+(?:on|in|at|into)\s+.+?)?%s' % (_PW_C, _PW_C, _PW_END), msg, re.I)
    if m and _pw_coin(m.group(1)) and _pw_coin(m.group(2)) and _pw_coin(m.group(1)) != _pw_coin(m.group(2)) and not _mk_of(m.group(1) + ' ' + m.group(2)):
        a, b, tail = _pw_coin(m.group(1)), _pw_coin(m.group(2)), (m.group(3) or '')
        return {'text': 'One lend takes one coin, so these are two — each with its own card and approval. Which first?',
                'widget': {'type': 'choose', 'options': [{'label': 'Lend %s' % disp(c), 'sub': 'its own card', 'ask': 'lend %s%s' % (c, tail)} for c in (a, b)]}}
    dt = deposit_then_trade(msg, address, history)
    if dt:
        return dt
    if not address:
        return None
    bm = LENDMOVES.better_market(msg, history, lend_io(address))   # "move all of it to a better market": which market? (to-fix 2026-11-10)
    if bm:
        return bm
    li = FOLLOWUP.lend_it(msg, history, _pw_coin, _mk_of, lambda c: spendable(c, all_balances(address) or {}) * float(coin_px(c) or 0) >= 0.01)
    if li:                                            # "lend it on suilend": the coin of the user's last turn, the card's slider (to-fix 2026-11-14)
        return {'_rewrite': li}
    # "withdraw 0.2 usdc" with no market: one market -> its card at that amount; several -> ask which (the AI picked one silently)
    m = re.match(r'^\s*(?:withdraw|redeem|take\s+out|take\s+back|pull\s+out|cash\s+out|pull)\s+\$?(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?usdc(?:\s+(?:back|out|please))?%s' % _PW_END, msg, re.I)
    if m:
        amt = float(m.group(1))
        mine = [r for r in lend_positions_rows(address) if r['coin'] == 'USDC']
        if not mine:
            return {'text': 'You have no USDC lent through Kivy.%s' % lent_hint(address, 'USDC', lead=' ')}
        if len(mine) > 1:
            fit = [r for r in mine if float(r['lent']) + 1e-9 >= amt]
            return {'text': 'You have USDC in %d markets. Which one should the %s USDC come from?%s' % (len(mine), num_plain(amt),
                            '' if fit else ' None of them holds that much on its own — pick one and withdraw what it has.'),
                    'widget': {'type': 'choose', 'options': [
                        {'label': 'From %s' % r['name'], 'sub': '%s USDC lent' % dec(r['lent'], 6),
                         'ask': 'withdraw %s usdc from %s' % (num_plain(amt) if float(r['lent']) + 1e-9 >= amt else 'all', mk_name(r['id']))} for r in mine[:6]]}}
        if amt > float(mine[0]['lent']) + 1e-9:
            return {'text': 'You have %s USDC lent on %s — less than %s.' % (dec(mine[0]['lent'], 6), mine[0]['name'], num_plain(amt))}
        card = withdraw_card(mine[0])
        card['widget']['preset'] = round(amt, 6)
        return card
    # "move my usdc to navi" (no "from"): from the wallet when it holds the coin (a lend), else from the one market that lends it (a move)
    m = re.match(r'^\s*(?:move|shift)\s+%s\s+(?:over\s+)?(?:to|into|onto)\s+(?:the\s+)?(%s)%s' % (_PW_WHAT, G_LEND_MKT, _PW_END), msg, re.I)
    if m and _pw_coin(m.group(2)) and not re.search(r'\bfrom\b', msg, re.I):
        coin, what, to = _pw_coin(m.group(2)), m.group(1), m.group(3)
        hold = all_balances(address) or {}
        if spendable(coin, hold) * float(coin_px(coin) or 0) >= 0.01:
            return {'_rewrite': 'lend %s on %s' % (what, to)}
        tk = _mk_of(to)
        src = [r for r in lend_positions_rows(address) if r['coin'].lower() == coin.lower() and not (tk and _rows_on([r], tk))]
        if len(src) == 1:
            amt = re.match(r'^\s*(%s)' % _PW_AMT, what, re.I)
            return {'_rewrite': 'move %s %s from %s to %s' % (amt.group(1) if amt else 'all my', coin, mk_name(src[0]['id']), to),
                    '_say': 'Your %s is lent on %s, so this moves it from there.' % (disp(coin), src[0]['name'])}
        if len(src) > 1:
            return {'text': 'Your %s is lent in %d markets. Which one should move to %s?' % (disp(coin), len(src), to),
                    'widget': {'type': 'choose', 'options': [{'label': 'From %s' % r['name'], 'sub': '%s %s lent' % (dec(r['lent'], 6), disp(coin)),
                                                              'ask': 'move all my %s from %s to %s' % (coin, mk_name(r['id']), to)} for r in src[:6]]}}
        return {'_rewrite': 'lend %s on %s' % (what, to)}
    # "put it somewhere safe", "lend my usdc somewhere safe": the lower-risk market (broad collateral), said why — the card and the words agree
    m = re.match(r"^\s*(?:put|lend|park|deposit|stash|keep)\s+((?:%s\s+(?:of\s+)?)?(?:(?:my|the)\s+)?(%s|it|that|this|them))\s+(?:(?:some|any)\s*(?:where|place)|in\s+a\s+place|on\s+a\s+market|in\s+something)\s+"
                 r"(?:that(?:'s|\s+is)\s+|which\s+is\s+)?(?:safe|safer|safest|low[-\s]risk|secure)%s" % (_PW_AMT, _PW_C, _PW_END), msg, re.I)
    if m:
        pron = m.group(2).lower() in ('it', 'that', 'this', 'them')
        coin = _last_said(history, _one_coin) if pron else _pw_coin(m.group(2))
        mk = safest_market(coin) if coin else None
        if coin and mk:
            what = re.sub(r'\b(?:it|that|this|them)\b', coin, m.group(1), flags=re.I) if pron else m.group(1)
            return {'_rewrite': 'lend %s on %s' % (what, mk_name(mk)),
                    '_say': 'No market is risk-free; %s is the lower-risk choice for %s — its borrowers post many large coins as collateral, so the risk is spread.' % (LEND_MKTS.get(mk, mk), disp(coin))}
    # "buy sui with it": the coin of the user's own last turn pays (the swap card: either coin can still be changed on it)
    m = re.match(r'^\s*(?:buy|get)\s+(?:some\s+)?(%s)\s+(?:with|using|from)\s+(?:it|that|this|them)%s' % (_PW_C, _PW_END), msg, re.I)
    if m and _pw_coin(m.group(1)):
        src = _last_said(history, _one_coin)
        if src and src != _pw_coin(m.group(1)):
            return {'_rewrite': 'swap %s to %s' % (src, m.group(1))}
    # "buy usdc and lend it": no paying coin named — the swap-and-lend card, starting from a coin the wallet holds (it has a coin picker)
    m = re.match(r'^\s*(?:buy|get)\s+(?:some\s+)?(%s)\s*(?:,|and|then|and\s+then)\s*(?:lend|deposit|supply)\s+(?:it|that|them)(\s+(?:on|in|at|into)\s+.+?)?%s' % (_PW_C, _PW_END), msg, re.I)
    if m and _pw_coin(m.group(1)):
        to = _pw_coin(m.group(1))
        hold = all_balances(address) or {}
        src = next((c for c in ['USDC', 'SUI'] + [c for c in hold if c not in ('USDC', 'SUI')]
                    if c != to and spendable(c, hold) * float(coin_px(c) or 0) >= 0.01), None)
        if src:
            return {'_rewrite': 'swap %s to %s and lend it%s' % (src, to, m.group(2) or '')}
    # "unstake my ssui" (no market): where it is — lent in one market -> withdraw + swap to SUI; in the wallet -> swap to SUI
    m = re.match(r'^\s*un-?stake\s+((?:%s\s+(?:of\s+)?)?(?:(?:my|the)\s+)?(%s))%s' % (_PW_AMT, _PW_C, _PW_END), msg, re.I)
    if m and _pw_coin(m.group(2)):
        coin = _pw_coin(m.group(2))
        lst = [c for c in STAKED_SUI if ckey(c) or lend_token(c)]
        rows = lend_positions_rows(address)
        hold = all_balances(address) or {}
        if coin == 'SUI':                               # plain SUI is not staked: say what Kivy's staking is, and what they hold of it
            mine = [(c, 'in your wallet', None) for c in lst if float(hold.get(c) or 0) * float(coin_px(c) or 0) >= 0.01] + \
                   [(r['coin'], 'lent on ' + r['name'], r) for r in rows if r['coin'] in lst]
            text = ('Kivy stakes through liquid staking only (sSUI, vSUI, haSUI) — SUI staked with a validator in your wallet app is unstaked there, not here.')
            if not mine:
                return {'text': text + ' You hold no staked SUI through Kivy right now.'}
            return {'text': text + ' Your staked SUI:', 'widget': {'type': 'choose', 'options': [
                {'label': 'Unstake %s' % disp(c), 'sub': where, 'ask': ('unstake all my %s from %s' % (c, mk_name(r['id']))) if r else 'swap all my %s to SUI' % c} for c, where, r in mine[:6]]}}
        if coin in lst:
            mine = [r for r in rows if r['coin'].lower() == coin.lower()]
            amt = re.match(r'^\s*(%s)' % _PW_AMT, m.group(1), re.I)
            a = amt.group(1) if amt else 'all my'
            if len(mine) == 1:
                return {'_rewrite': 'withdraw %s %s from %s, swap it to SUI, keep it in my wallet' % ('all' if not amt or amt.group(1).lower() in ('all', 'max', 'everything') else amt.group(1), coin, mk_name(mine[0]['id']))}
            if len(mine) > 1:
                return {'text': 'Your %s is lent in %d markets. Which one should I unstake from?' % (disp(coin), len(mine)),
                        'widget': {'type': 'choose', 'options': [{'label': 'From %s' % r['name'], 'sub': '%s %s lent' % (dec(r['lent'], 6), disp(coin)),
                                                                  'ask': 'unstake %s %s from %s' % (a, coin, mk_name(r['id']))} for r in mine[:6]]}}
            if float(hold.get(coin) or 0) * float(coin_px(coin) or 0) >= 0.01:
                return {'_rewrite': ('swap %s %s to SUI' % (amt.group(1), coin)) if amt else 'swap %s to SUI' % coin}
            return {'text': 'You hold no %s — nothing to unstake.' % disp(coin)}
    return None


def lend_io(address):
    """The readers kivy/lendmoves.py needs (it never reads the chain itself): the wallet's lent rows and the live rates."""
    return {'coin_of': _pw_coin, 'lent_rows': lambda: lend_positions_rows(address), 'rates_of': lend_rows_for, 'mk_name': mk_name,
            'label': lambda k: LEND_MKTS.get(k, k), 'disp': disp}


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import BORROW_RE, TRADE_STOP, chart_symbol, num_plain, perp_market, perp_market_list  # noqa: E402
from .situations import STAKED_SUI, _mk_of, _rows_on, mk_name, safest_market  # noqa: E402
from .readguard import G_LEND_MKT, G_STABLE, G_STOP  # noqa: E402
from .chainreads import all_balances, ckey, dec  # noqa: E402
from .lending import (  # noqa: E402
    LEND_MKTS, coin_px, lend_positions_rows, lend_rows_for, lend_token, lent_hint, spendable, withdraw_card)
from .chains import CHAIN_BOT, CHAIN_PERPS  # noqa: E402
from .sends import xw_accounts  # noqa: E402
