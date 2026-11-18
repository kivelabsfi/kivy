"""Lending requests the readers missed (to-fix report 2026-11-10 + the weak answer of the 11-13 round). Each function reads ONE
kind of request and returns a reply, a rewrite in the words the readers already know, or None. Nothing here reads the chain:
the caller passes the user's lent rows and the rates (server.py lend_io), so every function is plain data in, plain data out.

- better_market(): "move all of it to a better market" — the target is open, so Kivy asks WHICH market (tap choices, each one
  approval that moves the lent coin: withdraw + lend). It never lends wallet coins on the market the coin already sits in
  (to-fix 2026-11-10: Kivy lent all wallet USDC on Suilend Main, where the lent USDC already was).
- better_followup(): a bare "all of it" right after the user asked about a better rate for one coin: the same question.
- move_conflict(): the AI's plan_lend for a request that MOVES a lent coin: not built, the model is told to ask which market.
- back_wording(): "i would like some of my usdc back from lending please, 0.2" -> "withdraw 0.2 usdc" (11-13 weak answer).
"""
# How it runs: imported by site/server.py — plain_wording calls back_wording; wallet_wording calls better_market;
# _ai_reply's plan_lend gate calls move_conflict. Nothing here runs on its own.
import re

_C = r'[A-Za-z][A-Za-z0-9]{1,9}'
_PRON = r'(?:it|that|them|this|those)'
# "a better market", "a higher-paying one", "somewhere that pays more", "a market with a better rate"
_BETTER = (r'(?:(?:a|another|the|some)\s+)?(?:better|higher|higher[\s-]+paying|better[\s-]+paying|more\s+profitable|more\s+rewarding)\s+'
           r'(?:market|rate|place|pool|yield|one|apy|apr|option|spot)'
           r'|(?:(?:a|another|some)\s+)?(?:market|place|pool)\s+(?:that\s+pays|paying|with)\s+(?:more|a\s+(?:better|higher)\s+(?:rate|yield|apy|apr))')
_SOMEWHERE = r'somewhere\s+(?:better|that\s+pays\s+more|paying\s+more|with\s+a\s+(?:better|higher)\s+(?:rate|yield))'
_MOVE = r'(?:move|shift|switch|transfer|put|migrate|relocate|take)'
_WHAT = (r'(?P<what>(?:all|everything|the\s+lot)(?:\s+of\s+' + _PRON + r')?|' + _PRON + r'(?:\s+all)?|all\s+of\s+' + _PRON +
         r'|(?:(?:all|half)\s+(?:of\s+)?)?(?:my|the)\s+(?:lent\s+)?(?P<coin>' + _C + r')|(?P<coin2>' + _C + r'))')
BETTER_RE = re.compile(r'(?:^|[.!;]\s+|,\s*(?:so\s+|and\s+|then\s+)?|\b(?:so|and|please|pls)\s+)(?:(?:can|could|would)\s+you\s+|please\s+|pls\s+|i\s+want\s+to\s+|i\'?d\s+like\s+to\s+|let\'?s\s+)?'
                       + _MOVE + r'\s+' + _WHAT + r'\s+(?:(?:over\s+)?(?:to|into|onto|on|in)\s+(?:' + _BETTER + r')|' + _SOMEWHERE + r')\s*(?:please|pls|for\s+me)?\s*[.!]*\s*$', re.I)
ALL_OF_IT_RE = re.compile(r'^\s*(?:(?:ok(?:ay)?|yes|yeah|sure)[\s,]+)?(?:move\s+|do\s+)?(?:all|all\s+of\s+(?:it|them)|everything|the\s+whole\s+(?:thing|lot|amount)|it\s+all)'
                          r'(?:\s+(?:please|pls))?\s*[.!]*\s*$', re.I)
_ASKED_BETTER = re.compile(r'\b(?:better|higher|best|highest)\b.{0,30}\b(?:rate|yield|apy|apr|market|place|option)\b|\bpays?\s+more\b', re.I)


def better_market(msg, history, io):
    """A reply for "move (all of) it / my <coin> to a better market" (or a bare "all of it" right after the user's own
    better-rate question), or None when the message is not one. io: the server's readers —
      coin_of(word) -> coin or None · lent_rows() -> [{'id','name','coin','lent','total'}] (total = its rate %) ·
      rates_of(coin) -> [{'market','total_apr',...}] best first · mk_name(id) -> the market as the readers read it back ·
      label(id) -> its display name · disp(coin) -> the coin's display name."""
    text = 'move all of it to a better market' if better_followup(msg, history) else msg
    m = BETTER_RE.search(text or '')
    if not m:
        return None
    coin_of, disp = io['coin_of'], io.get('disp') or str
    word = m.group('coin') or m.group('coin2')
    coin = coin_of(word) if word else None
    if word and not coin:
        return None                                   # "move my money to a better market": not a coin word — the AI asks
    if not coin:                                      # "it": a coin named earlier in this message, else in the user's own last turn
        coin = _one([coin_of(w) for w in re.findall(_C, text[:m.start()])]) or _said(history, coin_of)
    rows = io['lent_rows']() or []
    if not coin:
        coins = sorted({r['coin'] for r in rows})
        if len(coins) == 1:
            coin = coins[0]                           # the only coin they have lent is the one they mean
        elif not coins:
            return {'text': 'You have nothing lent to move.'}
        else:
            return {'text': 'Which coin should move to a better market?',
                    'widget': {'type': 'choose', 'options': [{'label': disp(c), 'sub': 'lent', 'ask': 'move all my lent %s to a better market' % c} for c in coins[:4]]}}
    mine = [r for r in rows if str(r['coin']).lower() == coin.lower()]
    if not mine:
        return {'text': 'You have no %s lent, so there is nothing to move. To lend %s from your wallet, say "lend %s".' % (disp(coin), disp(coin), coin)}
    low = min(float(r.get('total') or 0) for r in mine)
    better = [x for x in io['rates_of'](coin) or [] if float(x['total_apr']) > low + 0.05]
    lent = sum(float(r['lent']) for r in mine)
    if len(mine) == 1:
        where = 'Your %s %s on %s earns %.2f%%.' % (_num(lent), disp(coin), mine[0]['name'], float(mine[0].get('total') or 0))
    else:
        where = 'Your %s %s is lent on %d markets (%s).' % (_num(lent), disp(coin), len(mine), ', '.join(
            '%s on %s at %.2f%%' % (_num(r['lent']), r['name'], float(r.get('total') or 0)) for r in mine))
    if not better:
        return {'text': where + ' No market Kivy lends on pays more right now.'}
    opts = []
    for x in better[:4]:
        src = [r for r in mine if r['id'] != x['market']]
        if not src:
            continue
        if len(src) == 1:
            ask = 'move all %s from %s to %s' % (coin, io['mk_name'](src[0]['id']), io['mk_name'](x['market']))
        else:
            ask = 'withdraw all %s from %s and lend it on %s' % (coin, ' and '.join(io['mk_name'](r['id']) for r in src), io['mk_name'](x['market']))
        opts.append({'label': 'To %s' % io['label'](x['market']), 'sub': '%.2f%% a year · one approval' % float(x['total_apr']), 'ask': ask})
    if not opts:
        return None
    return {'text': where + ' Which market should %s move to? These pay more, and the move is one approval.' % ('it' if len(mine) == 1 else 'it all'),
            'widget': {'type': 'choose', 'options': opts}}


def move_conflict(msg, coin, market, lent_rows):
    """The AI's plan_lend for a request that MOVES a lent coin ("move it to …", "switch my usdc to …") — a lend from the wallet
    is the wrong card for it (to-fix 2026-11-10: wallet USDC lent on Suilend Main, where the lent USDC already sat). Returns the
    note for the model (ask which market, with move options) or None. market: the key the model asked for, or None."""
    if not re.search(r'\b(?:move|shift|switch|migrate|relocate|transfer)\b', msg or '', re.I) or re.search(r'\bwallet\b', msg or '', re.I):
        return None
    mine = [r for r in lent_rows() or [] if str(r['coin']).lower() == str(coin or '').lower()]
    if not mine:
        return None
    src = ', '.join(r['name'] for r in mine)
    return ('The user asked to MOVE their %s, which is lent on %s. A move is withdraw + lend in one approval, and plan_lend would lend '
            'WALLET coins instead%s — it was NOT prepared. %s' % (
                coin, src, ' on a market where it already sits' if market and any(r['id'] == market for r in mine) else '',
                'Call ask_choice: one option per market they could move to, each message exactly "move all %s from <its market> to <market>" '
                '(they tap one; Kivy builds the move). Never pick the market yourself.' % coin))


def _one(coins):
    cs = [c for c in dict.fromkeys(coins) if c]
    return cs[0] if len(cs) == 1 else None


def _said(history, coin_of):
    """The one coin of the user's own latest earlier turn that names a coin (never Kivy's text or a bracketed memo)."""
    for h in reversed(history or []):
        tx = str(h.get('text') or h.get('content') or '') if isinstance(h, dict) and h.get('role') == 'user' else ''
        if not tx or tx.lstrip().startswith('['):
            continue
        cs = [c for c in dict.fromkeys(coin_of(w) for w in re.findall(_C, tx)) if c]
        if cs:
            return cs[0] if len(cs) == 1 else None
    return None


def better_followup(msg, history):
    """A bare "all of it" right after the user's own better-rate / move question about one coin: that question again, as a
    move of all of it (to-fix 2026-11-10: "all of it" left the target open and Kivy lent wallet USDC where it already sat).
    True when it is one; "it" is then the one coin of that question (better_market reads it from the user's turn)."""
    if not ALL_OF_IT_RE.match(msg or ''):
        return None
    turns = [h for h in history or [] if isinstance(h, dict) and str(h.get('text') or h.get('content') or '').strip()]
    if len(turns) < 2 or turns[-1].get('role') not in ('model', 'assistant', 'kivy'):
        return None
    prev = next((str(h.get('text') or h.get('content') or '') for h in reversed(turns) if h.get('role') == 'user'
                 and not str(h.get('text') or h.get('content') or '').lstrip().startswith('[')), '')
    said = str(turns[-1].get('text') or turns[-1].get('content') or '')
    return bool(prev and _ASKED_BETTER.search(prev) and re.search(r'\b(?:move|switch|market|rate|pays?)\b', said, re.I))


_REQ = (r"(?:i(?:'d|\s+would)\s+(?:like|love|want)|i\s+(?:want|need|wanna)|can\s+i\s+(?:get|have|take)|could\s+i\s+(?:get|have|take)|"
        r"(?:please\s+)?(?:give|get|send|bring)\s+me|let\s+me\s+(?:have|get|take)|i\s+(?:want|need)\s+to\s+(?:get|take))")
_BACK_RE = re.compile(r'^\s*(?:(?:hi|hey|hello|ok(?:ay)?)[\s,!]+)?(?:please\s+)?' + _REQ + r'\s+(?:to\s+(?:get|take)\s+)?(?:back\s+)?'
                      r'(?P<n0>\$?\d+(?:\.\d+)?\s+(?:of\s+)?)?'
                      r'(?P<what>(?:some|a\s+bit|a\s+little|part|a\s+part|a\s+portion)\s+of\s+(?:my|the)|my|some(?:\s+of\s+(?:my|the))?|the)?\s*'
                      r'(?P<n1>\$?\d+(?:\.\d+)?\s+)?(?P<coin>' + _C + r')\s+back'
                      r'(?:\s+(?:from|out\s+of|off)\s+(?:my\s+|the\s+)?(?P<src>lending|lent|savings|deposits?|(?P<mk>[a-z]+(?:\s+(?:main|prime|ember|market|high\s*yield))?)))?'
                      r'(?:\s+(?:please|pls|thanks?|thank\s+you))?\s*(?:[,:;-]|—)?\s*(?:(?:about|around|just|only|like)\s+)?(?P<n2>\$?\d+(?:\.\d+)?)?'
                      r'(?:\s*(?P<coin2>' + _C + r'))?(?:\s+(?:please|pls|thanks?|thank\s+you))?\s*[.!]*\s*$', re.I)


def back_wording(t, coin_of, market_of):
    """"i would like some of my usdc back from lending please, 0.2" -> "withdraw 0.2 usdc"; "give me back 0.2 usdc from lending"
    -> "withdraw 0.2 usdc". Words only: the amount is the one number the user typed (none -> the withdraw card's slider)."""
    s = re.sub(r'(?i)^(\s*(?:please\s+)?(?:give|get|bring|send)\s+me)\s+back\s+(\$?\d+(?:\.\d+)?\s+)?(%s)\b' % _C, r'\1 \2\3 back', t or '')
    m = _BACK_RE.match(s)
    if not m:
        return None
    coin = coin_of(m.group('coin'))
    if not coin or (m.group('coin2') and coin_of(m.group('coin2')) != coin):
        return None
    if len([g for g in ('n0', 'n1', 'n2') if m.group(g)]) > 1:
        return None                                   # two numbers: which one? the AI asks
    n = re.sub(r'\s+of$', '', (m.group('n0') or m.group('n1') or m.group('n2') or '').strip())
    mk = m.group('mk')
    if mk and not market_of(mk):
        return None                                   # "back from my friend": not a market
    if not mk and not m.group('src'):
        return None                                   # "i want my usdc back", "i want 2 usdc back": no lending word — left as it was
    some = not n and re.match(r'(?i)(?:some|a\s+bit|a\s+little|part|a\s+part|a\s+portion)\b', m.group('what') or '')
    return 'withdraw %s%s%s' % ((n + ' ') if n else 'some ' if some else '', m.group('coin'), (' from ' + mk) if mk else '')


def _num(v):
    """0.4 -> '0.4', 2.0 -> '2', 0.300404 -> '0.300404' (up to 6 decimals, no trailing zeros)."""
    s = ('%.6f' % float(v)).rstrip('0').rstrip('.')
    return s or '0'
