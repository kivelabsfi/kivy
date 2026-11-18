"""Kivy's ledger (architecture programme change 8, 2026-11-14): one SQLite file, DATA/ledger.db, for what is today spread over
intents.db, checks.jsonl and diary.md — plans, landed transactions with their label and verdict, check records — plus a `jobs`
table: work that must survive a restart (check 2 first; the Walrus save, the diary line and alerts later).

Phase 3 is DUAL-WRITE: the old files stay the source of truth and keep being written; the ledger is written beside them and
compare() counts the differences once a day ("kivy ledger: compare …" — zero for a week, then reads switch over). The one thing
it changes now: a check 2 that was running when the service restarted is started again at start-up (before, the 10-minute
scan caught up later, and a restart lost the queue).

Only the running service writes it (ON is set in server.py's start-up block): tests and tools that import server.py never open
the live database (a root-owned -wal file once locked the service out, 2026-11-11). Every call fails soft: the ledger never
breaks a request or a check.

Request ids (kivy/reqid.py, 2026-11-14): a plan keeps the id of the build request that saved it (plans.rid), a check 2 job and
its record the id of the /api/tx/log request that started it (jobs.rid, checks.rid) — so a digest or plan id leads to the
journal lines of both requests, and a request id to its plan.

Visitor IPs (2026-11-15, a reviewer scan S5): a plan carries the internet address that built it ('ip': only that visitor may
report on it, and the unreported-transaction scan matches it). forget_ips() blanks it — and any other per-visitor network field
in NET_KEYS — in plans older than 30 days, rows stored before this rule included: a minute after start-up and every 6 hours.
Everything else in the plan is kept. The 24-hour intents.db copy is gone long before; backups keep a copy up to 14 days more.

Plans themselves (2026-11-18, QA fix package 5): never signed — deleted after 5 days; signed — after 30 (prune_plans, same
schedule as the IPs).
"""
# How it runs: imported by site/server.py inside kivy-site; start() opens the database; the functions below are called next to
# the old writes (remember_intent, verify_later, the checks.jsonl and diary writes).
import json
import re
import sqlite3
import threading
import time

from . import reqid
from . import settings

ON = False                                           # set True by server.py's start-up block (the service only)
_LOCK = threading.Lock()
_DB = {'c': None, 'failed': False}
SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (iid TEXT PRIMARY KEY, at REAL, kind TEXT, address TEXT, policy TEXT, plan TEXT, rid TEXT);
CREATE TABLE IF NOT EXISTS txs (digest TEXT PRIMARY KEY, iid TEXT, address TEXT, label TEXT, verdict TEXT, at REAL);
CREATE TABLE IF NOT EXISTS checks (id INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT, iid TEXT, at TEXT, ok INTEGER, policy TEXT, rec TEXT,
                                   rid TEXT);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, payload TEXT, state TEXT DEFAULT 'open',
                                 tries INTEGER DEFAULT 0, created REAL, done REAL, rid TEXT, UNIQUE(kind, key));
CREATE INDEX IF NOT EXISTS checks_digest ON checks(digest);
CREATE INDEX IF NOT EXISTS plans_at ON plans(at);
CREATE INDEX IF NOT EXISTS jobs_open ON jobs(kind, state);
"""


def _db():
    if not ON or _DB['failed']:
        return None
    if _DB['c'] is None:
        try:
            c = sqlite3.connect(str(settings.DATA / 'ledger.db'), timeout=5, check_same_thread=False, isolation_level=None)
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(SCHEMA)
            for t in ('plans', 'checks', 'jobs'):     # a ledger made before request ids (2026-11-14): add the column
                if 'rid' not in [r[1] for r in c.execute('PRAGMA table_info(%s)' % t)]:
                    c.execute('ALTER TABLE %s ADD COLUMN rid TEXT' % t)
            _DB['c'] = c
        except Exception as exc:
            _DB['failed'] = True
            print('kivy ledger: off (%s)' % type(exc).__name__, flush=True)
            return None
    return _DB['c']


def _run(sql, args=()):
    c = _db()
    if c is None:
        return None
    try:
        with _LOCK:
            return c.execute(sql, args).fetchall()
    except Exception as exc:
        print('kivy ledger: write failed (%s)' % type(exc).__name__, flush=True)
        return None


def start(log=print):
    """Open the database (service start-up); returns the open check 2 jobs to run again."""
    if _db() is None:
        return []
    rows = _run("SELECT payload FROM jobs WHERE kind = 'check2' AND state = 'open' AND created > ?", (time.time() - 86400,)) or []
    if rows:
        log('kivy ledger: %d check(s) open at the last stop — running them again' % len(rows), flush=True)
    out = []
    for (p,) in rows:
        try:
            out.append(json.loads(p))
        except ValueError:
            pass
    threading.Thread(target=_daily, daemon=True, name='kivy-ledger').start()
    return out


def plan_put(iid, it):
    if not iid or not isinstance(it, dict):
        return
    g = it.get('guard') or {}
    _run('INSERT OR REPLACE INTO plans (iid, at, kind, address, policy, plan, rid) VALUES (?, ?, ?, ?, ?, ?, ?)',
         (iid, float(it.get('at') or time.time()), it.get('kind'), it.get('address'), g.get('policy') if isinstance(g, dict) else None,
          json.dumps(it, default=str)[:200000], reqid.current()))


def job_add(kind, key, payload):
    _run("INSERT INTO jobs (kind, key, payload, created, rid) VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind, key) DO UPDATE SET "
         "state = 'open', tries = tries + 1, payload = excluded.payload, rid = coalesce(excluded.rid, jobs.rid)",   # a re-run at start-up keeps it
         (kind, key, json.dumps(payload), time.time(), reqid.current()))


def job_done(kind, key):
    _run("UPDATE jobs SET state = 'done', done = ? WHERE kind = ? AND key = ?", (time.time(), kind, key))


def check_put(rec, iid=None):
    if not isinstance(rec, dict):
        return
    rid = reqid.current()                            # check 2 runs in its own thread: the id comes from its job (verify_later's key)
    if not rid:
        rid = ((_run("SELECT rid FROM jobs WHERE kind = 'check2' AND key = ?", ('%s|%s' % (rec.get('digest'), iid or ''),)) or [(None,)])[0][0])
    _run('INSERT INTO checks (digest, iid, at, ok, policy, rec, rid) VALUES (?, ?, ?, ?, ?, ?, ?)',
         (rec.get('digest'), iid, rec.get('at'), None if rec.get('ok') is None else int(bool(rec.get('ok'))), rec.get('policy'),
          json.dumps(rec, default=str)[:100000], rid))


def tx_put(digest, iid=None, address=None, label=None, verdict=None):
    """A landed transaction: its label (the diary line's words) and check 2's mark; only the fields given change."""
    if not digest:
        return
    _run('INSERT INTO txs (digest, at) VALUES (?, ?) ON CONFLICT(digest) DO NOTHING', (digest, time.time()))
    for col, v in (('iid', iid), ('address', address), ('label', label), ('verdict', verdict)):
        if v:
            _run('UPDATE txs SET %s = ? WHERE digest = ?' % col, (v, digest))


def compare(checks_file, log=print):
    """Dual-write check: the last day's check records in checks.jsonl against the ledger's. Zero differences for a week is the
    gate for switching reads to the ledger (tracker, phase 3)."""
    since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 86400))
    old = set()
    try:
        with open(checks_file) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if str(r.get('at') or '') >= since and r.get('digest'):
                    old.add(r['digest'])
    except OSError:
        return None
    new = {d for (d,) in (_run('SELECT digest FROM checks WHERE at >= ?', (since,)) or []) if d}
    diff = len(old ^ new)
    log('kivy ledger: compare last 24 h — checks.jsonl %d, ledger %d, differences %d' % (len(old), len(new), diff), flush=True)
    return diff


IP_KEEP_DAYS = 30                                    # 2026-11-15: visitor IPs kept 30 days for abuse protection, then deleted
NET_KEYS = ('ip',)                                   # per-visitor network data in a stored plan (only the IP today)
_NET_RE = re.compile(r'"(%s)": "(?:[^"\\]|\\.)*"' % '|'.join(NET_KEYS))


def _blank(o):
    """Set every NET_KEYS field anywhere in a plan to None; True when something changed."""
    hit = False
    if isinstance(o, dict):
        for k in list(o):
            if k in NET_KEYS and o[k] is not None:
                o[k] = None
                hit = True
            else:
                hit = _blank(o[k]) or hit
    elif isinstance(o, list):
        for x in o:
            hit = _blank(x) or hit
    return hit


def forget_ips(now=None, batch=200):
    """Blank the visitor IP (NET_KEYS) in every plan older than IP_KEEP_DAYS, rows already stored included; nothing else in the
    plan changes. A plan cut at 200 kB (not valid JSON) is cleaned as text. Batches, so ledger writes never wait long. Returns
    the number of plans changed (None when the ledger is off)."""
    if _db() is None:
        return None
    cut = (now or time.time()) - IP_KEEP_DAYS * 86400
    lo = _DB.get('ip_done', -1e18) - 86400               # this process already cleaned everything before its last cut (+ a day)
    like = ' OR '.join('plan LIKE ?' for _ in NET_KEYS)
    pats = ['%%"%s": "%%' % k for k in NET_KEYS]
    changed, last = 0, -1
    while True:
        rows = _run('SELECT rowid, iid, plan FROM plans WHERE at >= ? AND at < ? AND rowid > ? AND (%s) ORDER BY rowid LIMIT ?' % like,
                    (lo, cut, last, *pats, batch))
        if rows is None:
            return changed                                # a failed read: try again next time (ip_done not moved)
        if not rows:
            _DB['ip_done'] = cut
            return changed
        for rowid, iid, text in rows:
            last = rowid
            try:
                o = json.loads(text)
                new = json.dumps(o, default=str) if _blank(o) else text
            except (ValueError, TypeError):
                new = _NET_RE.sub(lambda m: '"%s": null' % m.group(1), text or '')
            if new != text:
                _run('UPDATE plans SET plan = ? WHERE iid = ?', (new, iid))
                changed += 1


# Plan keep times (2026-11-18, QA SRV-V2 fix package 5): a plan nobody signed is kept 5 days (enough to debug "why didn't they
# sign?"), every other plan 30 days. Check 2 needs a plan for about a day; kivystats keeps its own table, the chain keeps the rest.
# Signed = a landed transaction or a check record names the plan.
PLAN_KEEP_UNSIGNED_DAYS, PLAN_KEEP_DAYS = 5, 30


def prune_plans(now=None):
    """Delete plans past their keep time; returns the number deleted (None when the ledger is off or the delete failed)."""
    if _db() is None:
        return None
    now = now or time.time()
    a = _run('DELETE FROM plans WHERE at < ? AND iid NOT IN (SELECT iid FROM txs WHERE iid IS NOT NULL) '
             'AND iid NOT IN (SELECT iid FROM checks WHERE iid IS NOT NULL) RETURNING iid', (now - PLAN_KEEP_UNSIGNED_DAYS * 86400,))
    b = _run('DELETE FROM plans WHERE at < ? RETURNING iid', (now - PLAN_KEEP_DAYS * 86400,))
    if a is None or b is None:
        return None
    return len(a) + len(b)


def _prune_plans_logged():
    try:
        n = prune_plans()
        if n:
            print('kivy ledger: %d plan(s) past their keep time deleted (unsigned %d days, signed %d days)'
                  % (n, PLAN_KEEP_UNSIGNED_DAYS, PLAN_KEEP_DAYS), flush=True)
    except Exception as exc:
        print('kivy ledger: plan pruning failed (%s)' % type(exc).__name__, flush=True)


def _forget_ips_logged():
    try:
        n = forget_ips()
        if n:
            print('kivy ledger: visitor IPs blanked in %d plan(s) older than %d days' % (n, IP_KEEP_DAYS), flush=True)
    except Exception as exc:
        print('kivy ledger: IP pruning failed (%s)' % type(exc).__name__, flush=True)


def _daily():
    from . import settings as _s
    time.sleep(60)                                   # IPs: also soon after a start — promotes restart the service more often than 6 h
    _forget_ips_logged()
    _prune_plans_logged()
    while True:
        time.sleep(3600 * 6)
        try:
            compare(_s.DATA / 'checks.jsonl')
            _run("DELETE FROM jobs WHERE state = 'done' AND done < ?", (time.time() - 7 * 86400,))
        except Exception:
            pass
        _forget_ips_logged()
        _prune_plans_logged()
