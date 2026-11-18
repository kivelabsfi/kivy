""".sui names: a short cache for the chat, never for a build (fix package 4, 2026-11-15; a reviewer readers scan W2).

One chat message asked the chain for the same name 2-18 times ("send 5 usdc to bob.sui" = 2 lookups; "sned half my funds to
: bob.sui pls" = 18), because several readers each resolve it. Inside a chat answer (chat_scope, around code_answer) a
name's address is now kept NAME_TTL seconds (a name that resolves to nothing: NONE_TTL). Everywhere else — every
/api/tx/* build, the Send card's check, a send's re-check — the name is asked fresh, so a name that just changed owner can
never be built into a transaction to its old address; that fresh answer also replaces the chat's copy. The scope is per
thread: a build on another thread never sees it, and a thread a chat reader starts resolves fresh.
"""
# How it runs: imported by kivy/fallback.py (cached wraps resolve_recipient) and site/server.py (chat_scope wraps
# code_answer). State: _NAMES, at most NAMES_MAX entries. Tests: tests/test_waste_20261115.py.
import functools
import threading
import time

NAME_TTL, NONE_TTL, NAMES_MAX = 60, 10, 2000
_NAMES = {}
_LOCK = threading.Lock()
_SCOPE = threading.local()


def chat_scope(fn):
    """Marks a chat answer: name lookups made inside it (this thread only) may use the cache. Nests (code_answer recurses)."""
    @functools.wraps(fn)
    def inner(*a, **k):
        depth = getattr(_SCOPE, 'depth', 0)
        _SCOPE.depth = depth + 1
        try:
            return fn(*a, **k)
        finally:
            _SCOPE.depth = depth
    return inner


def in_chat():
    return getattr(_SCOPE, 'depth', 0) > 0


def _key(r):
    k = str(r or '').strip().lower()
    return k if k and not k.startswith('0x') and len(k) <= 120 else None   # an address needs no lookup; nothing huge is kept


def cached(fn):
    """Wraps the resolver: fresh outside a chat answer (and the fresh answer is remembered); inside one, a copy under
    NAME_TTL (NONE_TTL for 'no such name') is reused."""
    @functools.wraps(fn)
    def inner(r):
        k = _key(r)
        if k is None:
            return fn(r)
        now = time.time()
        if in_chat():
            hit = _NAMES.get(k)
            if hit and now < hit[0]:
                return hit[1]
        out = fn(r)
        with _LOCK:
            if len(_NAMES) >= NAMES_MAX:            # full: drop what expired, then the oldest half
                for x in [x for x, v in _NAMES.items() if v[0] <= now]:
                    _NAMES.pop(x, None)
                for x in list(_NAMES)[:len(_NAMES) // 2] if len(_NAMES) >= NAMES_MAX else []:
                    _NAMES.pop(x, None)
            _NAMES.pop(k, None)
            _NAMES[k] = (now + (NAME_TTL if out else NONE_TTL), out)
        return out
    return inner
