"""Which perps account: the wallet's own Aftermath accounts, the ⛔ rule, the choice card and the pick the chat carries (fix
package 2 "Your perps account is yours", QA 2026-11-17 BLD-V1; "no need to remember for other wallet. Just what you found
on that wallet").

An Aftermath account key can be sent to any wallet, and its giver may keep an assistant key that trades the margin away. The
builder (perps.ts "whose perps account") reads every USDC account whose ADMIN key the wallet holds and blocks any that another
wallet can trade. When one usable account has no open orders and no stop / TWAP orders it is used; otherwise the user picks:
  - the chat: a choice card (`card`), whose options ask again with the pick at the end ("… on perps account #1042"). `take`
    reads that pick off the message, or off this chat's own earlier messages, and `scope` keeps it for the request;
  - every perps card the chat returns carries the account in use (`stamp`: widget.perps_account, a chain's spec.perps_account),
    so its build names it; the page's own perps calls send it back (perps_account in the body, &account= on reads), read once
    per HTTP request by `begin`;
  - upstream.lend_cli adds it to every builder perps command (&acct=), lend_post to /perp-pro-orders.
Nothing is stored: no table, no file, no browser storage. The builder checks the named account again at build time.
"""
# How it runs: imported by kivy/perps.py (perp_account raises Choose), kivy/upstream.py (acct on builder calls) and site/server.py
# (begin, scope around code_answer / ai_reply, spec on /api/tx/compose). Leaf module: stdlib only. State: one thread-local per
# request (the Handler runs one request per thread at a time; begin() resets it at the start of each). Tests:
# tests/test_perpaccount_20261117.py.
import functools
import json
import re
import threading
from urllib.parse import parse_qs, urlparse

_REQ = threading.local()          # .pick (the account the request names, '' = none), .used (the account an answer resolved),
                                  # .depth (inside a chat answer), .quiet (a summary: never ask)
MARK_RE = re.compile(r'\s*[,;(]?\s*\b(?:on|with|using|use|from|in|for|via|into|to)\s+(?:my\s+)?(?:aftermath\s+)?perps?\s+account\s*#\s*(\d{1,20})\s*\)?\s*[.!?]*\s*$', re.I)
PICK_ONLY_RE = re.compile(r'^\s*(?:use\s+|switch\s+to\s+|pick\s+)?(?:my\s+)?(?:aftermath\s+)?perps?\s+account\s*#\s*(\d{1,20})\s*[.!]*\s*$', re.I)
NEW_RE = re.compile(r'^\s*(?:please\s+)?open\s+(?:me\s+)?(?:a\s+)?(?:new|fresh|another|second|separate)\s+(?:aftermath\s+)?perps?\s+account\s*[.!]*\s*$', re.I)
LIST_RE = re.compile(r'^\s*(?:show\s+(?:me\s+)?|list\s+|see\s+)?(?:all\s+)?(?:my\s+)?(?:aftermath\s+)?perps?\s+accounts\s*[?.!]*\s*$'
                     r'|^\s*which\s+(?:aftermath\s+)?perps?\s+account\b.*$', re.I)
CARDS = {'perpdep', 'perpwd', 'perpconfirm', 'perpclose', 'perptpsl', 'perppos', 'perpacct'}


class Choose(BaseException):
    """Raised inside a chat answer when the request must name a perps account first; the chat scope turns it into the choice
    card. A BaseException on purpose: the readers' own `except Exception` must never swallow it into a wrong "no account"."""
    def __init__(self, answer):
        super().__init__('pick a perps account')
        self.answer = answer or {}


def current():
    return getattr(_REQ, 'pick', '') or ''


def _clean_id(v):
    s = str(v or '').strip().lstrip('#').rstrip('n')
    return s if re.fullmatch(r'\d{1,20}', s) else ''


def begin(path, raw=None):
    """Start of every HTTP request (server.py _heavy): forget the last request's pick, take this one's — perps_account in a
    perps POST body (or spec.perps_account), &account= on a perps read. Returns the path without its query."""
    _REQ.pick, _REQ.used, _REQ.depth, _REQ.quiet = '', '', 0, False
    p = str(path or '').split('?', 1)[0]
    if p.startswith('/api/perp/') or p in ('/api/tx/perp', '/api/tx/compose'):
        try:
            if raw:
                b = json.loads(raw)
                _REQ.pick = _clean_id(b.get('perps_account') or (b.get('spec') or {}).get('perps_account')) if isinstance(b, dict) else ''
            else:
                _REQ.pick = _clean_id((parse_qs(urlparse(str(path)).query).get('account') or [''])[0])
        except Exception:
            _REQ.pick = ''
    return p


def take(msg, history=None):
    """(message without the pick, the pick): "… on perps account #1042" at the end of this message; else the newest such pick in
    this chat's own earlier messages (user turns only), up to an "open a new perps account" (after that the user picks again)."""
    m = PICK_ONLY_RE.match(msg or '')
    if m:
        return 'perps', m.group(1)
    m = MARK_RE.search(msg or '')
    if m:
        return (msg[:m.start()].rstrip(' ,;') or 'perps'), m.group(1)
    for h in reversed(history or []):
        if not (isinstance(h, dict) and h.get('role') == 'user'):
            continue
        t = str(h.get('text') or h.get('content') or '')
        if NEW_RE.match(t):
            return msg, ''
        m = PICK_ONLY_RE.match(t) or MARK_RE.search(t)
        if m:
            return msg, m.group(1)
    return msg, ''


def _clean_history(history):
    out = []
    for h in history or []:
        if isinstance(h, dict) and h.get('role') == 'user' and isinstance(h.get('text'), str):
            m = MARK_RE.search(h['text'])
            if m:
                h = dict(h, text=h['text'][:m.start()].rstrip(' ,;') or 'perps')
        out.append(h)
    return out


def need(answer):
    """A perps account answer (builder /perp-account, /perp-preview, /perp-positions): remember the account it resolved; inside
    a chat answer, one that says the user must pick raises Choose (the scope shows the choice card)."""
    if isinstance(answer, dict):
        a = _clean_id(answer.get('id') or answer.get('account') if answer.get('has', True) else '')
        if a and not answer.get('choose'):
            _REQ.used = a
        if answer.get('choose') and getattr(_REQ, 'depth', 0) > 0 and not getattr(_REQ, 'quiet', False):
            raise Choose(answer)
    return answer


class quiet:
    """`with quiet():` — a summary (net worth, the wallet's positions list) reads the account without ever asking to pick."""
    def __enter__(self):
        self.was = getattr(_REQ, 'quiet', False)
        _REQ.quiet = True

    def __exit__(self, *a):
        _REQ.quiet = self.was


def _num(x, d=2):
    try:
        v = float(x or 0)
    except Exception:
        v = 0.0
    s = ('%.' + str(d) + 'f') % v
    return s.rstrip('0').rstrip('.') if '.' in s else s


def short(a):
    a = str(a or '')
    return a[:6] + '…' + a[-4:] if len(a) > 12 else a


def blocked_line(a):
    who = next((x for x in a.get('others') or [] if x), '')
    return '⛔ Account #%s — another wallet%s can trade it, so Kivy won\'t use it' % (a.get('id'), ' (%s)' % short(who) if who else '')


def blocked_text(answer):
    """The ⛔ lines for every blocked account, with what the user can do; '' when none."""
    bl = [a for a in (answer or {}).get('accounts') or [] if a.get('blocked')]
    if not bl:
        return ''
    return '\n'.join(blocked_line(a) + '.' for a in bl) + '\nRemove the other key in Aftermath\'s app, or open a new account.'


def number(answer):
    """' #1042' for the account an answer uses, '' when none."""
    a = _clean_id((answer or {}).get('id') or (answer or {}).get('account'))
    return ' #%s' % a if a else ''


def others(answer):
    """When the wallet holds more perps accounts than the one in use: say so, and how to see them; '' otherwise."""
    n = len([a for a in (answer or {}).get('accounts') or [] if _clean_id(a.get('id')) != _clean_id((answer or {}).get('id') or (answer or {}).get('account'))])
    if not n or not number(answer):
        return ''
    return ' This wallet holds %d other perps account%s — "my perps accounts" lists them.' % (n, '' if n == 1 else 's')


def summary(a):
    """One account in a line: margin · positions · open orders · stop / TWAP orders."""
    ps = a.get('positions') or []
    parts = ['%s USDC margin' % _num(a.get('margin'))]
    if ps:
        parts.append('%d position%s (%s)' % (len(ps), '' if len(ps) == 1 else 's', ', '.join('%s %s' % (p.get('symbol'), p.get('side')) for p in ps[:3])))
    else:
        parts.append('no positions')
    if a.get('orders'):
        parts.append('%d open order%s' % (a['orders'], '' if a['orders'] == 1 else 's'))
    elif a.get('orders') is None and 'orders' in a:
        parts.append('open orders unknown')
    if a.get('tickets'):
        parts.append('%d stop / TWAP order%s' % (a['tickets'], '' if a['tickets'] == 1 else 's'))
    elif a.get('tickets') is None:
        parts.append('stop / TWAP orders unknown')
    return ' · '.join(parts)


def card(answer, msg):
    """The choice card: each usable account (id, margin, positions, open orders and stop / TWAP orders) + "Open a new account".
    Each option asks again: the same request with the pick at the end. Blocked accounts are named in the text, never offered."""
    accts = [a for a in (answer or {}).get('accounts') or [] if not a.get('blocked')]
    base = re.sub(r'[\s.!?]+$', '', MARK_RE.sub('', msg or '') or 'perps') or 'perps'
    opts = [{'label': 'Account #%s' % a.get('id'), 'sub': summary(a), 'value': '$%s' % _num(a.get('margin')),
             'icon': '/icons/aftermath.png', 'coin': 'Aftermath', 'ask': '%s on perps account #%s' % (base, a.get('id'))} for a in accts]
    opts.append({'label': 'Open a new account', 'sub': 'a new Aftermath perps account, only your wallet holds its key',
                 'icon': '/icons/aftermath.png', 'coin': 'Aftermath', 'ask': 'open a new perps account'})
    pe = (answer or {}).get('pick_error')
    if len(accts) > 1:
        head = 'Which perps account should Kivy use? This wallet holds the keys of %d Aftermath perps accounts, and Kivy never picks one for you.' % len(accts)
    elif accts and accts[0].get('orders') == 0 and accts[0].get('tickets') == 0:
        head = 'This wallet holds the key of one Aftermath perps account: #%s.' % accts[0].get('id')
    elif accts:
        a = accts[0]
        head = ('Account #%s already has %s. If you placed %s, use it; if not, open a new account — anyone can send a perps account to a wallet.'
                % (a.get('id'), ' and '.join(x for x in ('open orders' if a.get('orders') else '', 'stop / TWAP orders' if a.get('tickets') else '') if x)
                   or 'orders Kivy could not check', 'them' if (a.get('orders') or a.get('tickets')) else 'any'))
    else:
        head = 'This wallet has no perps account Kivy can use.'
    bt = blocked_text(answer)
    return {'text': '\n'.join(x for x in ((pe or ''), head, bt) if x), 'widget': {'type': 'choose', 'options': opts}}


def stamp(out):
    """Every perps card names the account in use (the page shows "Account #1042" and its build sends it back)."""
    if not isinstance(out, dict) or not isinstance(out.get('widget'), dict):
        return out
    w = out['widget']
    a = getattr(_REQ, 'used', '') or _clean_id(w.get('account'))
    if not a:
        return out
    t = w.get('type')
    if t in CARDS or (t == 'chart' and w.get('trade')):
        w['perps_account'] = a
    elif t == 'chain' and isinstance(w.get('spec'), dict):
        sp = w['spec']
        if (sp.get('source') or {}).get('op') == 'perps_out' or any(isinstance(k, dict) and k.get('op') == 'perps' for k in sp.get('sinks') or []):
            w['perps_account'] = a
            sp['perps_account'] = a
    return out


def spec(sp):
    """/api/tx/compose: the clean spec the server sends the builder, with the account the card named (perps steps only)."""
    if current() and ((sp.get('source') or {}).get('op') == 'perps_out' or any(k.get('op') == 'perps' for k in sp.get('sinks') or [])):
        sp = dict(sp, perps_account=current())
    return sp


def chat_scope(kind):
    """Wraps code_answer ('code': the message and this chat's history give the pick), ai_reply ('ai') and apply_route_check
    ('route'; its second argument is the code answer, passed through) — those two keep the pick code_answer found. Inside,
    Choose becomes the choice card; every perps card that comes out is stamped with the account."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(msg, history, *a, **k):
            depth = getattr(_REQ, 'depth', 0)
            if depth == 0:
                _REQ.used, _REQ.quiet = '', False
                if kind == 'code':
                    msg, _REQ.pick = take(msg, history)
                    history = _clean_history(history)
            _REQ.depth = depth + 1
            try:
                r = fn(msg, history, *a, **k)
            except Choose as c:
                if depth:
                    raise
                ch = card(c.answer, msg)
                return (ch, msg, history, False) if kind == 'code' else ch
            finally:
                _REQ.depth = depth
            if depth == 0:
                if kind == 'code' and isinstance(r, tuple) and r:
                    r = (stamp(r[0]),) + tuple(r[1:])
                elif kind in ('ai', 'route'):
                    r = stamp(r)
            return r
        return inner
    return wrap


class wallet_only:
    """`with wallet_only():` — the wallet judged on its own: no pick, never ask ("open a new perps account")."""
    def __enter__(self):
        self.was = (current(), getattr(_REQ, 'quiet', False))
        _REQ.pick, _REQ.quiet = '', True

    def __exit__(self, *a):
        _REQ.pick, _REQ.quiet = self.was
