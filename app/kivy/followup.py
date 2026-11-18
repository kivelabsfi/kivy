"""A follow-up that leans on the chat (to-fix 2026-11-14): "lend it on suilend", "put that on navi", "lend it all" — "it" is
the one coin of the USER's own last turn (never Kivy's replies, never a bracketed page memo — the source said_recipient()
trusts), so the lend card opens with its slider. The AI had asked "how much?" with choices instead of showing the card.
Pure text work: the caller passes a coin finder, a market finder and a "does the wallet hold it" check; no chain reads.
"""
# How it runs: imported by site/server.py — wallet_wording calls lend_it; the rewrite is read by the usual readers.
import re

_C = r'[A-Za-z][A-Za-z0-9]{1,9}'
_TAIL = r'(?:[\s,]*(?:please|pls|thanks|too|as\s+well|instead|this\s+time))*\s*[.!]*\s*$'
_PERPS = re.compile(r'\b(?:perps?|long|short|position|margin|leverage|close|aftermath|futures|\d+\s*x)\b', re.I)


def _user_turns(history):
    return [str(h.get('text') or h.get('content') or '') for h in history or []
            if isinstance(h, dict) and h.get('role') == 'user' and str(h.get('text') or h.get('content') or '').strip()
            and not str(h.get('text') or h.get('content') or '').lstrip().startswith('[')]


LEND_IT_RE = re.compile(r'^\s*(?:please\s+)?(?:(?:can|could)\s+you\s+)?(?:lend|deposit|supply|put|park)\s+(?P<what>it\s+all|all\s+of\s+(?:it|them|that)|it|that|them|those|this)'
                        r'(?:\s+(?P<where>(?:on|in|at|into|onto|to|with)\s+(?:the\s+)?[a-z][a-z0-9 ]{1,40}?))?' + _TAIL, re.I)


def lend_it(msg, history, coin_of, market_of, held):
    """"lend it on suilend" -> "lend USDC on suilend" when the user's own last turn named exactly one coin they hold; None
    otherwise (no coin, two coins, a perps turn — "close my sui long" then "lend it" means the USDC margin, not SUI).
    held(coin) -> True when the wallet holds some of it."""
    m = LEND_IT_RE.match(msg or '')
    if not m:
        return None
    where = m.group('where') or ''
    if where and not (market_of(where) or re.search(r'\b(?:best|highest)\b', where, re.I)):
        return None                                   # "put it in perps", "put it somewhere": other readers / the AI
    turns = _user_turns(history)
    if not turns or _PERPS.search(turns[-1]):
        return None
    coins = [c for c in dict.fromkeys(coin_of(w) for w in re.findall(_C, turns[-1]) if not market_of(w)) if c]
    if len(coins) != 1 or not held(coins[0]):
        return None
    allw = re.match(r'(?i)it\s+all|all\s+of', m.group('what'))
    return 'lend %s%s%s' % ('all my ' if allw else '', coins[0], (' ' + where) if where else '')
