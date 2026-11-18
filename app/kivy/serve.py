"""Serving the site so a restart drops nothing (architecture programme change 4, 2026-11-14).

- Socket: when systemd hands over its socket (kivy-site.socket, which stays open across restarts), the server uses it, so a
  visitor who arrives while the service restarts waits a moment in the queue instead of being refused. Without systemd
  (tests, hand runs, the staging twin) the server binds the port itself, as before.
- Stop: on SIGTERM the server stops taking new connections, lets the requests already running finish (up to `grace`
  seconds, inside systemd's TimeoutStopSec), then returns. Before, a restart cut every build and check 2 in flight
  (43 site restarts in 7 days, review 2026-11-13).
"""
# How it runs: site/server.py's start-up calls run(Handler, PORT) inside kivy-site; nothing here runs on its own.
import os
import signal
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer

ACTIVE = [0]                       # requests being answered right now (not idle keep-alive connections)
_LOCK = threading.Lock()
STOPPING = threading.Event()


def _counted(handler):
    class Counted(handler):
        def _run(self, fn):
            with _LOCK:
                ACTIVE[0] += 1
            try:
                return fn()
            finally:
                with _LOCK:
                    ACTIVE[0] -= 1
                if STOPPING.is_set():
                    self.close_connection = True        # no further requests on this connection once stopping

        def do_GET(self):
            return self._run(super().do_GET)

        def do_POST(self):
            return self._run(super().do_POST)
    Counted.__name__ = handler.__name__
    return Counted


def systemd_socket():
    """The listening socket systemd passed (LISTEN_FDS, fd 3), or None."""
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or int(os.environ.get('LISTEN_FDS') or 0) < 1:
        return None
    return socket.socket(fileno=3)


def run(handler, port, log=print, grace=25):
    class Server(ThreadingHTTPServer):
        request_queue_size = 256                         # waiting connections (Python's default 5 reset ~5% of a burst — load test 2026-10-24)
        daemon_threads = True

        def handle_error(self, request, client_address):
            if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                return                                   # the visitor left before the reply was sent (review K-20)
            super().handle_error(request, client_address)

    sock = systemd_socket()
    if sock is not None:
        httpd = Server(('127.0.0.1', port), _counted(handler), bind_and_activate=False)
        httpd.socket = sock
        how = 'systemd socket'
    else:
        httpd = Server(('127.0.0.1', port), _counted(handler))
        how = 'own socket'

    def stop(signum, frame):
        if not STOPPING.is_set():
            STOPPING.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()   # shutdown() waits for serve_forever: never in its thread
    signal.signal(signal.SIGTERM, stop)
    log('kivy serving on 127.0.0.1:%d (%s)' % (port, how), flush=True)
    httpd.serve_forever()
    end = time.time() + grace
    while ACTIVE[0] and time.time() < end:
        time.sleep(0.1)
    log('kivy stop: %s' % ('all requests finished' if not ACTIVE[0] else '%d request(s) still running after %ds' % (ACTIVE[0], grace)), flush=True)
