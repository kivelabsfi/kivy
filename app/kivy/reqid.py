"""Request ids (architecture programme change 1, review §10, 2026-11-14): one random id per HTTP request to the server, so one
id finds a request's whole trail in the journals of kivy-site and kivy-lend and in the ledger.

- The Handler starts one at the top of every GET / POST (start) and ends it in its finally (end); it lives in this thread.
- Every 'kivy …' line the request prints ends with " [req <id>]" (install_log_tag, the running service only). The tag goes at
  the END of the line: the watchdog matches line STARTS (kivy/events.py), so no alert changes.
- The builder gets it in the X-Kivy-Request header (kivy/upstream.py lend_cli / lend_post) and ends its own lines with it.
- The ledger keeps it beside the plan (plans.rid) and the check 2 record (checks.rid, from the /api/tx/log request).
- The answer carries it as the X-Kivy-Request header (no-store answers only), for curl and the browser's network tab.
Never shown to users, never taken from the request (a visitor can't put text into the logs this way).

Lines printed by background threads (check 2, the perf log, sweeps) carry no id: they belong to no request.
"""
# How it runs: imported by site/server.py (Handler, start-up block), kivy/upstream.py and kivy/ledger.py inside kivy-site.
import os
import re
import sys
import threading

HEADER = 'X-Kivy-Request'
ID_RE = re.compile(r'^[0-9a-f]{12}$')              # what the builder accepts in the header (12 hex = 48 random bits)
_T = threading.local()


def start():
    """A new id for the request this thread is answering now (the Handler, top of do_GET / do_POST)."""
    _T.rid = os.urandom(6).hex()
    return _T.rid


def end():
    """The request is answered: no id for whatever this thread does next (a keep-alive connection's next request gets its own)."""
    _flush_partial()
    _T.rid = None


def current():
    return getattr(_T, 'rid', None)


def headers():
    """The header that carries the id to the builder, or {} outside a request."""
    rid = current()
    return {HEADER: rid} if rid else {}


class _Tagged:
    """sys.stdout for the running service: a line printed while a request is answered gets " [req <id>]" at its end. Each
    thread keeps its own unfinished line, so print()'s two writes (text, then the newline) never pick up another request's id;
    a whole line goes out in one write."""

    def __init__(self, out):
        self._out = out

    def write(self, s):
        rid = current()
        part = getattr(_T, 'part', '')
        if not rid:
            if part:
                _T.part = ''
                s = part + s
            return self._out.write(s)
        buf = part + s
        if '\n' not in buf:
            _T.part = buf
            return len(s)
        done, _, _T.part = buf.rpartition('\n')
        self._out.write(''.join((ln + ' [req %s]' % rid if ln.strip() else ln) + '\n' for ln in done.split('\n')))
        return len(s)

    def flush(self):
        return self._out.flush()

    def __getattr__(self, name):
        return getattr(self._out, name)


def _flush_partial():
    part = getattr(_T, 'part', '')
    if part and isinstance(sys.stdout, _Tagged):
        _T.part = ''
        sys.stdout._out.write(part)


def install_log_tag():
    """The running service only (server.py's start-up block): tests and tools that import server.py print untouched."""
    if not isinstance(sys.stdout, _Tagged):
        sys.stdout = _Tagged(sys.stdout)
