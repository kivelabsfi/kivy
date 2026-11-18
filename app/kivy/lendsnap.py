"""Check 2's 'before' reading of a lend-type build, started beside the build (fix package 4, 2026-11-15; a reviewer server scan W1).

Check 2 compares a wallet's lent positions after a lend with a reading taken BEFORE the user signs (check2.remember_intent ->
lend_positions_rows, waited for up to 5 s). The four lend-type build routes (/api/tx/lend deposit, /api/tx/swaplend,
/api/tx/compose with a lend step, /api/tx/withdrawswap with lend_to) cleared the wallet's position cache right after the
build, so that reading always started cold, after the build: +1-3 s on every such build. Now begin() clears the cache and
starts the reading in the background BEFORE the route asks the builder; remember_intent's reading then finds it in the
cache, or waits for the same read (lend_positions_all's per-wallet lock), so it is still a reading made during this request,
before the transaction is handed out: the same 'before', one read, no wait after the build. A build longer than the cache's
10 s 'fresh' window reads again, as before. Routes that don't lend keep the old clear-after-build (after()).
"""
# How it runs: imported by site/server.py; begin() before the builder call, after() where the cache used to be cleared.
# Reads through kivy/lending.py (builder.ts 'positions-all'). Tests: tests/test_waste_20261115.py.
import threading

from .lending import _LEND, lend_positions_rows


def _clear(addr):
    for a in {addr, str(addr).lower()}:              # the route's spelling and remember_intent's (lower case)
        _LEND['pos'].pop(a, None)
        _LEND.get('posall', {}).pop(a, None)


def _read(addr):
    try:
        lend_positions_rows(addr)
    except Exception:
        pass                                          # remember_intent reads again (and notes it) if this one failed


def begin(addr, lends):
    """Before the builder call. lends: check 2 will want this wallet's 'before' reading (the build puts coins into a
    market). Returns whether the reading was started here, for after()."""
    if not lends:
        return False
    _clear(addr)
    threading.Thread(target=_read, args=(str(addr).lower(),), daemon=True, name='kivy-lendsnap').start()   # the key remember_intent reads
    return True


def after(addr, started):
    """Where the route cleared the cache after the build: still cleared unless begin() started this request's reading."""
    if not started:
        _clear(addr)
