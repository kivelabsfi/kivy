"""No amount said -> the card's slider, never the AI's "how much?" choice (Kivy rule since 2026-11-13; to-fix 2026-11-14: a lend
request with no amount got ask_choice options "lend 1 USDC on Suilend" / "lend 2 USDC …" / "lend all …" instead of the card).

amount_choice(options, coin_of) reads the options the model passed to ask_choice: when they are amount picks for ONE lend, swap
or withdraw of ONE coin, it returns that job (verb, coin, the words after the coin), so the caller can open the job's card with
an empty amount instead. Options that are different jobs (a real "did you mean…?") come back as None and stay a choice.
"""
# How it runs: imported by site/server.py — _ai_reply's ask_choice tool calls amount_choice. Pure text work.
import re

_JOB = {'lend': 'lend', 'deposit': 'lend', 'supply': 'lend', 'swap': 'swap', 'convert': 'swap', 'exchange': 'swap', 'sell': 'swap',
        'withdraw': 'withdraw', 'redeem': 'withdraw'}
_OPT_RE = re.compile(r'^\s*(?P<v>lend|deposit|supply|swap|convert|exchange|sell|withdraw|redeem)\s+'
                     r'(?P<a>\$?\d+(?:\.\d+)?\s*%?|all|half|max|everything|a\s+quarter|a\s+third|\d+\s*percent)\s+(?:of\s+)?(?:my\s+|the\s+)?'
                     r'(?P<c>[A-Za-z][A-Za-z0-9]{1,9})\b(?P<rest>.*)$', re.I)
_MONEY = re.compile(r'\b(?:lend|deposit|supply|swap|convert|exchange|sell|buy|withdraw|redeem|send|transfer|pay|move|long|short)\b', re.I)


def amount_choice(options, coin_of):
    """{'job': 'lend'|'swap'|'withdraw', 'coin': coin, 'rest': ' on suilend'} when the options only pick an amount for one job
    and one coin (2+ such options, different amounts; any other option is not a money job), else None."""
    picks, jobs, coins, amts, rest = 0, set(), set(), set(), None
    for o in options or []:
        if not isinstance(o, dict):
            continue
        m = _OPT_RE.match(str(o.get('message') or ''))
        if not m:
            if _MONEY.search(str(o.get('message') or '')):
                return None                           # another money job among the options: a real choice
            continue
        c = coin_of(m.group('c'))
        if not c:
            return None
        picks += 1
        jobs.add(_JOB[m.group('v').lower()])
        coins.add(c)
        amts.add(re.sub(r'\s+', '', m.group('a').lower()))
        r = ' '.join(m.group('rest').split())
        rest = r if rest is None else (rest if rest == r else '')
    if picks < 2 or len(jobs) != 1 or len(coins) != 1 or len(amts) < 2:
        return None
    return {'job': jobs.pop(), 'coin': coins.pop(), 'rest': rest or ''}


def target_of(rest, coin_of):
    """The coin a swap option goes to ("to SUI" -> 'SUI'), or None."""
    m = re.search(r'\b(?:to|into|for)\s+([A-Za-z][A-Za-z0-9]{1,9})\b', rest or '', re.I)
    return coin_of(m.group(1)) if m else None
