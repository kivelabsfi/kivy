"""Kivy's one door to the builder (kivy-lend): every GET (lend_cli) and POST (lend_post) to builder.ts, the waits, the 'busy' line the watchdog alerts on, and the short per-wallet read cache cleared when a wallet signs.
Each call carries the request id (X-Kivy-Request, kivy/reqid.py) when it is made while a request is answered; the builder ends
its log lines with it.

Moved verbatim out of site/server.py (architecture programme change 3, 2026-11-14); server.py imports these names back,
so callers and tests that use them through server.py are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
import json
import re
import time
import urllib.request
from urllib.parse import urlencode
from . import perpacct
from . import release
from . import reqid
from .settings import BUILDER


_DIRTY = {}             # address -> when Kivy last saw it sign a transaction (its positions/balances changed)


# per-wallet reads one chat or panel asked for 2-3 times (W-03). Rewards: 5 minutes (scaling fix 2, 2026-11-13) — the
# read costs ~15 of the ~20 chain calls of one wallet-panel load and rewards grow slowly. Never served after that wallet signs a
# transaction Kivy logged (mark_dirty: a claim, a lend, a withdraw), and the claim card never uses it: it builds from the chain.
_CLI_TTL = {'perp-positions': 15, 'rewards': 300}
_CLI_CACHE = {}


# How long the server waits for the builder (review scaling study 2026-11-10, 2026-11-11 "go"; was 300 s POST / 150 s GET):
# Cloudflare drops the visitor at 125 s, so a longer wait holds a slot for nobody. Builds get more: the builder may retry inside
# (stale price 2 x 20 s, Aftermath -> Cetus). BUILDER_BUILDS = the GET commands that build (= builder.ts BUILD_PATHS, tested).
BUILD_WAIT, READ_WAIT = 100, 60
BUILDER_BUILDS = {'build', 'withdrawswap', 'swaplend', 'compose', 'dust', 'sendmany', 'perp', 'perp-order', 'perp-limit', 'perp-stop',
                  'perp-scale', 'perp-twap', 'perp-cancel', 'perp-margin', 'perp-close', 'perp-tpsl', 'perp-tpsl-multi'}


def builder_busy(out, what):
    """True (and one "kivy busy:" line — the watchdog alerts on these) when the builder answered "busy": its slots were full."""
    if isinstance(out, dict) and out.get('ok') is False and 'builder is busy' in str(out.get('error') or ''):
        print('kivy busy: builder refused %s, all its slots taken' % what, flush=True)
        return True
    return False


# Every GET to builder.ts goes through here: GET /<cmd>?a=..&a=.. (rates, positions-all, build, perp-*, tx-facts …).
def lend_cli(*args, timeout=None):
    """Ask Kivy's lending builder (kivy-lend.service, localhost only). It never holds a key. A wallet's perps positions and
    rewards are reused for 15 s / 5 min — never after that wallet signs a transaction (mark_dirty). A perps command carries the
    perps account this request names (kivy/perpacct.py: &acct=, BLD-V1)."""
    import copy
    cmd, rest = args[0], [str(x) for x in args[1:]]
    timeout = timeout or (BUILD_WAIT if cmd in BUILDER_BUILDS else READ_WAIT)
    acct = perpacct.current() if cmd.startswith('perp') and cmd != 'perp-params' else ''
    key = (cmd, tuple(rest), acct) if cmd in _CLI_TTL and len(rest) == 1 else None
    if key:
        hit = _CLI_CACHE.get(key)
        if hit and time.time() - hit[0] < _CLI_TTL[cmd] and hit[0] > _DIRTY.get(rest[0].lower(), 0):
            return copy.deepcopy(hit[1])
    if cmd in BUILDER_BUILDS and not release.build_allowed():   # the builder runs another release: never build across versions
        return {'ok': False, 'error': release.UPDATING}
    url = '%s/%s?%s' % (BUILDER, cmd, urlencode([('a', x) for x in rest] + ([('acct', acct)] if acct else [])))
    rh = reqid.headers()                               # the request id, when called while a request is answered
    with urllib.request.urlopen(urllib.request.Request(url, headers=rh) if rh else url, timeout=timeout) as r:
        out = json.load(r)
    if builder_busy(out, cmd):                         # a busy READ is no data — never "nothing lent": fail like an unreachable builder
        if cmd not in BUILDER_BUILDS:
            raise RuntimeError('the transaction builder is busy')
    if key and isinstance(out, dict) and out.get('ok') is not False:
        if len(_CLI_CACHE) > 5000:
            _CLI_CACHE.clear()
        _CLI_CACHE[key] = (time.time(), copy.deepcopy(out))
    return out


# ── Section 18: builder POSTs + withdraw cards + plan() — builder.ts POST endpoints (/compose, /dust, /sendmany, /swapcheck,
#    /swapalt, /kind-calls), the withdraw slider card, and plan(): the confirm card for swap / send / USDC lend / withdraw ──
def lend_post(path, body, timeout=BUILD_WAIT):
    """POST to Kivy's builder (localhost only). A swap route that a DEX on it rejects in the test run (MoveAbort in a
    'router' module — the 0x7de5… adapter flakes now and then) gets ONE retry with a fresh route (2026-10-25)."""
    if path not in ('/kind-calls', '/perp-pro-orders') and not release.build_allowed():   # those two only read
        return {'ok': False, 'error': release.UPDATING}
    if path == '/perp-pro-orders' and perpacct.current():   # the perps account the page named (BLD-V1)
        body = dict(body, perps_account=perpacct.current())
    def once():
        req = urllib.request.Request(BUILDER + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', **reqid.headers()})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            o = json.load(r)
        builder_busy(o, path)                           # a busy build stays a reply: callers show its "try again" text
        return o
    out = once()
    if isinstance(out, dict) and out.get('ok') is False and re.search(r'MoveAbort.*router::|router::assert', str(out.get('error') or ''))             and '(tried ' not in str(out.get('error')):   # the builder already tried every router (2026-10-26): no second round
        print('kivy route retry: %s %s' % (path, str(out.get('error'))[:120]), flush=True)
        out = once()
    return out
