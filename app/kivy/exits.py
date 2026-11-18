"""The exit lane (architecture programme change 10, 2026-11-14). A build that takes money OUT of a position — close or cancel a
perps order, withdraw perps margin, add margin or a stop to a position, withdraw a lent coin, withdraw-and-swap, or a chain that
starts with a withdrawal — gets build slots of its own and is refused last. In a burst (a crash with many people closing at once)
an exit never waits behind new entries. SOUL: never route an exit through something you don't control; the builder keeps the
same lane (builder.ts EXIT_EXTRA). The degrade order: entries are refused first, exits only when their own slots are full too.

Shares per address (QA 2026-11-17 ATK-S1, fix package 5): 4 addresses × 6 requests labelled "withdraw" held all 20 slots and a real
user's withdraw got "busy". Now one address holds at most BUILDS_AT_ONCE builds (a fourth waits for one of its own to finish) and
at most EXIT_LANE_PER_IP of the exit lane's own slots, so the lane stays open for others.
"""
# How it runs: imported by site/server.py; Handler._heavy asks is_exit() for every transaction build and takes a slot here
# when the shared build slots are full.
import threading
import time

EXIT_SLOTS = 4                                   # on top of the 16 shared build slots; exits use the shared ones first
_EXIT_ALL = threading.BoundedSemaphore(EXIT_SLOTS)
BUILDS_AT_ONCE = 3                               # builds one address may have running at once, both pools together
EXIT_LANE_PER_IP = 1                             # exit-lane slots one address may hold
_COND = threading.Condition()
_BY_IP, _LANE_IP = {}, {}                        # address -> builds running / exit-lane slots held
PERP_EXITS = {'close', 'cancel', 'withdraw', 'margin', 'tpsl'}


def is_exit(path, req):
    """True when this build request takes money out of (or protects) a position."""
    req = req if isinstance(req, dict) else {}
    act = str(req.get('action') or '').lower()
    if path == '/api/tx/perp':
        return act in PERP_EXITS
    if path == '/api/tx/lend':
        return act == 'withdraw'
    if path == '/api/tx/withdrawswap':
        return True
    if path == '/api/tx/compose':
        src = ((req.get('spec') or {}) if isinstance(req.get('spec'), dict) else {}).get('source') or {}
        return isinstance(src, dict) and src.get('op') in ('withdraw', 'perps_out')
    return False


def acquire(build_pool, exit_wanted, timeout=8, who=None):
    """A build slot: entries wait for a shared one; exits take a shared one if free, else one of their own. Returns the pool the
    slot came from (give it back with release(pool, who)), or None when none came free in `timeout` seconds. `who` (the visitor's
    real address; None for local calls and tests) is held to BUILDS_AT_ONCE and EXIT_LANE_PER_IP."""
    end = time.time() + timeout
    if who:
        with _COND:
            while _BY_IP.get(who, 0) >= BUILDS_AT_ONCE:
                left = end - time.time()
                if left <= 0:
                    return None
                _COND.wait(left)
            _BY_IP[who] = _BY_IP.get(who, 0) + 1
    pool = _take(build_pool, exit_wanted, end, who)
    if pool is None and who:
        _drop(_BY_IP, who)
    return pool


def _take(build_pool, exit_wanted, end, who):
    left = max(0.0, end - time.time())
    if not exit_wanted:
        return build_pool if build_pool.acquire(timeout=left) else None
    if build_pool.acquire(blocking=False):
        return build_pool
    if who:
        with _COND:
            mine = _LANE_IP.get(who, 0) < EXIT_LANE_PER_IP
            if mine:
                _LANE_IP[who] = _LANE_IP.get(who, 0) + 1
        if not mine:                                 # this address already holds its exit-lane slot: wait for a shared one
            return build_pool if build_pool.acquire(timeout=left) else None
        if _EXIT_ALL.acquire(timeout=left):
            return _EXIT_ALL
        _drop(_LANE_IP, who)
        return None
    return _EXIT_ALL if _EXIT_ALL.acquire(timeout=left) else None


def _drop(d, who):
    with _COND:
        n = d.get(who, 1) - 1
        if n <= 0:
            d.pop(who, None)
        else:
            d[who] = n
        _COND.notify_all()


def release(pool, who=None):
    """Give back a slot acquire() returned."""
    pool.release()
    if who:
        if pool is _EXIT_ALL:
            _drop(_LANE_IP, who)
        _drop(_BY_IP, who)
