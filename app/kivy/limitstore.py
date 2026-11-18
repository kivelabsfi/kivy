"""Abuse limits and open sign-in challenges in SQLite: DATA/limits.db (architecture programme change 6, "limits and sign-in nonce
in SQLite", 2026-11-14; a reviewer T2 note). Before, both lived only in process memory: a restart reset every visitor's counts (the
250-a-day AI count per address included) and cancelled every open sign-in, and a second site worker would double every cap.
Now they survive a restart, and two workers on one box could share them. Same limits, windows and answers as before:
kivy/limits.py and accounts.py decide; this module only stores and counts.

Its own file, not a table in ledger.db: limits are written on almost every request and are disposable (deleting the file only
resets counts and cancels open sign-ins), while the ledger holds money records written a few times per transaction. Apart,
limit churn never waits on a plan or check write (SQLite has one writer per file), and the file can be removed at any time.
The nightly backup leaves it out (tools/backup_data.py).

Privacy: visitor internet addresses, browser ids and wallet addresses are stored only as keyed hashes (HMAC-SHA256 with
DATA/.limits_key, made on first use, never backed up); a sign-in message only as its SHA-256. Rows go when their window does
(a minute, an hour, the UTC day, 10 minutes for a challenge): a sweep deletes them every minute.

Only the running service opens it (ON is set in server.py's start-up block): tests and tools that import server.py use the
in-memory code exactly as before (a root-owned -wal once locked the service out of its own database, 2026-11-11).
Fail soft: when the file can't be opened or a statement fails, every function returns None and the caller uses the in-memory
code; the store is tried again after a minute ("kivy limits:" lines). Cost: one transaction per limit check (an indexed count
plus one small insert); WAL, synchronous=NORMAL.
"""
# How it runs: imported by kivy/limits.py and site/accounts.py inside kivy-site; start() (server.py's start-up block) opens the
# file and starts the sweep thread. Nothing here runs on its own.
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

from . import settings

ON = False                                           # set True by server.py's start-up block (the service only)
RETRY = 60                                           # seconds on the in-memory limits after a failure, then try the file again
_LOCK = threading.RLock()
_S = {'c': None, 'key': None, 'down': 0.0}
SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (k TEXT NOT NULL, at REAL NOT NULL, until REAL NOT NULL);
CREATE INDEX IF NOT EXISTS hits_k ON hits(k, at);
CREATE TABLE IF NOT EXISTS seen (k TEXT NOT NULL, item TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (k, item)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS challenges (nonce TEXT PRIMARY KEY, addr TEXT NOT NULL, msg TEXT NOT NULL, exp REAL NOT NULL,
                                       ip TEXT, bind TEXT) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS challenges_exp ON challenges(exp);
CREATE INDEX IF NOT EXISTS challenges_ip ON challenges(ip);
CREATE INDEX IF NOT EXISTS challenges_addr ON challenges(addr, ip);
"""


def _key():
    p = settings.DATA / '.limits_key'
    if not p.exists():
        tmp = p.with_name('.limits_key.%d.tmp' % os.getpid())
        tmp.write_bytes(secrets.token_bytes(32)); os.chmod(tmp, 0o600)
        try:
            os.link(tmp, p)                          # first writer wins: a second worker never replaces the key the hashes use
        except FileExistsError:
            pass
        tmp.unlink(missing_ok=True)
    k = p.read_bytes()
    if len(k) < 32:
        raise ValueError('short key')
    return k


def _db():
    if not ON or time.time() < _S['down']:
        return None
    if _S['c'] is None:
        try:
            settings.DATA.mkdir(parents=True, exist_ok=True)
            key = _key()
            c = sqlite3.connect(str(settings.DATA / 'limits.db'), timeout=0.5, check_same_thread=False, isolation_level=None)
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=NORMAL')
            c.executescript(SCHEMA)
            _S.update(c=c, key=key)
        except Exception as exc:
            _fail(exc, 'not opened')
            return None
    return _S['c']


def _fail(exc, what):
    """Close, use the in-memory limits for RETRY seconds, say so once (the next failure is a minute away at the earliest)."""
    c, _S['c'] = _S['c'], None
    try:
        if c is not None:
            c.close()
    except Exception:
        pass
    _S['down'] = time.time() + RETRY
    print('kivy limits: store %s (%s) — in-memory limits for %d s' % (what, type(exc).__name__, RETRY), flush=True)


def h(s):
    """The keyed hash a visitor address / browser id / wallet is stored as (128 bits, hex)."""
    return hmac.new(_S['key'], str(s).encode('utf-8', 'replace'), hashlib.sha256).hexdigest()[:32]


def _tx(fn):
    """Run fn(c) in one write transaction (BEGIN IMMEDIATE: the count and the insert can't interleave with another worker's).
    None when the store is off or the statement failed — the caller then uses its in-memory code."""
    with _LOCK:
        c = _db()
        if c is None:
            return None
        try:
            c.execute('BEGIN IMMEDIATE')
            try:
                out = fn(c)
            except Exception:
                c.execute('ROLLBACK')
                raise
            c.execute('COMMIT')
            return out
        except Exception as exc:
            _fail(exc, 'write failed')
            return None


def _n(c, k, since):
    return c.execute('SELECT count(*) FROM hits WHERE k = ? AND at > ?', (k, since)).fetchone()[0]


def count_add(kind, who, limit, window, now):
    """Sliding window: True and one hit recorded when `who` made fewer than `limit` `kind` hits in the last `window` seconds."""
    def run(c):
        k = kind + '|' + h(who)
        if _n(c, k, now - window) >= limit:
            return False
        c.execute('INSERT INTO hits VALUES (?, ?, ?)', (k, now, now + window))
        return True
    return _tx(run)


def ai_ip_add(ip, per_min, per_day, now, day0):
    """An AI answer for this address: fewer than per_min in the last minute AND fewer than per_day since UTC midnight day0."""
    def run(c):
        return _ai_ip(c, ip, per_min, per_day, now, day0)
    return _tx(run)


def _ai_ip(c, ip, per_min, per_day, now, day0):
    k = 'ai|' + h(ip)
    n_day, n_min = c.execute('SELECT coalesce(sum(at >= ?), 0), coalesce(sum(at > ?), 0) FROM hits WHERE k = ? AND at >= ?',
                             (day0, now - 60, k, min(day0, now - 60))).fetchone()
    if n_min >= per_min or n_day >= per_day:
        return False
    c.execute('INSERT INTO hits VALUES (?, ?, ?)', (k, now, max(day0 + 86400, now + 60)))
    return True


def ai_gate(vid, ip, per_min, per_visitor, ip_min, ip_day, now, day0):
    """The AI answer gate in one transaction: everyone together (per_min a minute), this browser id (per_visitor a minute), this
    address (ip_min a minute, ip_day a UTC day). 'ok' (all recorded), 'all' / 'visitor' / 'ip' (the limit that refused; nothing
    recorded), or None (store off)."""
    def run(c):
        if _n(c, 'g|ai', now - 60) >= per_min:
            return 'all'
        kv = 'v|' + h(vid) if vid else None
        if kv and _n(c, kv, now - 60) >= per_visitor:
            return 'visitor'
        if ip and not _ai_ip(c, ip, ip_min, ip_day, now, day0):
            return 'ip'
        c.execute('INSERT INTO hits VALUES (?, ?, ?)', ('g|ai', now, now + 60))
        if kv:
            c.execute('INSERT INTO hits VALUES (?, ?, ?)', (kv, now, now + 60))
        return 'ok'
    return _tx(run)


def seen_add(who, item, limit, window, now):
    """Different items (wallets) one address looked up in the last `window` seconds: False when `item` is new and `limit` are
    already there. A known item's time is refreshed at most once a minute (one write per panel load, not one per read), so it
    may leave the window up to 60 s early."""
    def run(c):
        k, it = h(who), h(item)
        rows = dict(c.execute('SELECT item, at FROM seen WHERE k = ? AND at >= ?', (k, now - window)).fetchall())
        if it not in rows and len(rows) >= limit:
            return False
        if now - rows.get(it, 0) >= 60:
            c.execute('INSERT INTO seen VALUES (?, ?, ?) ON CONFLICT(k, item) DO UPDATE SET at = excluded.at', (k, it, now))
        return True
    return _tx(run)


def challenge_put(nonce, address, msg, exp, ip, bind, now, keep_mine=4, per_ip=20, total=5000):
    """Store one sign-in challenge with today's caps (accounts.challenge): expired ones dropped; this address FROM THIS ADDRESS
    keeps only its newest keep_mine before the new one; one visitor address holds at most per_ip ('busy'); when total are open,
    the address holding the most loses its oldest. 'ok', 'busy', or None (store off)."""
    def run(c):
        a, i = h(address), (h(ip) if ip else None)
        c.execute('DELETE FROM challenges WHERE exp < ?', (now,))
        old = [n for (n,) in c.execute('SELECT nonce FROM challenges WHERE addr = ? AND ip IS ? ORDER BY exp DESC LIMIT -1 OFFSET ?',
                                       (a, i, keep_mine))]
        c.executemany('DELETE FROM challenges WHERE nonce = ?', [(n,) for n in old])
        if i and c.execute('SELECT count(*) FROM challenges WHERE ip = ?', (i,)).fetchone()[0] >= per_ip:
            return 'busy'
        if c.execute('SELECT count(*) FROM challenges').fetchone()[0] >= total:
            top = c.execute('SELECT ip FROM challenges GROUP BY ip ORDER BY count(*) DESC LIMIT 1').fetchone()[0]
            c.execute('DELETE FROM challenges WHERE nonce = (SELECT nonce FROM challenges WHERE ip IS ? ORDER BY exp LIMIT 1)', (top,))
        c.execute('INSERT INTO challenges VALUES (?, ?, ?, ?, ?, ?)',
                  (nonce, a, hashlib.sha256(msg.encode('utf-8', 'replace')).hexdigest(), exp, i, bind))
        return 'ok'
    return _tx(run)


def challenge_take(nonce, address, message):
    """Use a challenge up: deleted in the same statement that reads it (single use, also across workers), whatever comes next.
    (address and message match, expires at, browser binding hash), or None (no such open challenge, or store off)."""
    def run(c):
        rows = c.execute('DELETE FROM challenges WHERE nonce = ? RETURNING addr, msg, exp, bind', (nonce,)).fetchall()   # all: the statement ends
        if not rows:
            return None
        r = rows[0]
        same = hmac.compare_digest(r[0], h(address)) and isinstance(message, str) and \
            hmac.compare_digest(r[1], hashlib.sha256(message.encode('utf-8', 'replace')).hexdigest())
        return same, r[2], r[3]
    return _tx(run)


def sweep(now=None):
    """Delete rows whose window has passed (every minute from start()'s thread)."""
    now = now or time.time()

    def run(c):
        c.execute('DELETE FROM hits WHERE until < ?', (now,))
        c.execute('DELETE FROM seen WHERE at < ?', (now - 3600,))
        c.execute('DELETE FROM challenges WHERE exp < ?', (now,))
        return True
    return _tx(run)


def start(log=print):
    """Open the file (service start-up) and start the sweep; False (in-memory limits) when it can't be opened."""
    with _LOCK:
        ok = _db() is not None
    log('kivy limits: %s' % ('shared store on (limits.db)' if ok else 'store off — in-memory limits'), flush=True)

    def loop():
        while True:
            time.sleep(60)
            try:
                sweep()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name='kivy-limits').start()
    return ok
