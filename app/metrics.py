"""Kivy's own numbers (2026-10-22: for fundraising, and to learn where Kivy fails).

Anonymous by design: a visitor is a random id from their browser; a wallet address is stored only as a salted hash,
so users can be counted and followed over time (retention) without keeping who they are. Transactions count only
when the chain confirms them and they match what Kivy built (PTB = one on-chain transaction; actions = the steps in it).
SQLite in /opt/kivy/data (the service's only writable folder)."""
# How it runs: imported by site/server.py inside the kivy-site service (:8772); every function runs in the request
# that calls it. Writers in server.py: POST /api/m (visit, wallet_seen, event, mark_team for team browsers),
# /api/feedback (feedback), /api/pipeline (pipe: plan, simulation, check 1, the wallet's answer), verify_later()'s
# background check of each signed transaction (ptb, pipe: chain result + check 2), the chat handler (event 'msg',
# failure for questions it could not route), failed builds (failure 'build_error'), sweep_caches() (snapshot_today).
# Readers: /internal/metrics (summary), /internal/public (public_summary), /internal/export (export_rows) — local-only,
# token-protected routes that the separate kivystats dashboard (kivystats.kivelabs.xyz) calls; tools/stats_weekly.py
# also calls /internal/metrics. tools/feedback_weekly.py and stats_weekly.py read metrics.db read-only;
# tools/metrics_backfill.py (ptb) and tools/pipeline_backfill.py (pipe) import this module for one-off backfills.
# Output files: metrics.db (tables in SCHEMA) and feedback.md (a readable feedback log) in the data folder.
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
DB = DATA / 'metrics.db'
_lock = threading.RLock()   # re-entrant: export_rows('daily') calls summary() while holding it — a plain Lock froze every stats request (2026-10-23)
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits   (day TEXT, vid TEXT, n INTEGER DEFAULT 1, PRIMARY KEY (day, vid));
CREATE TABLE IF NOT EXISTS visitors (vid TEXT PRIMARY KEY, first_day TEXT, last_day TEXT);
CREATE TABLE IF NOT EXISTS wallets  (wh TEXT PRIMARY KEY, first_day TEXT, last_day TEXT, first_tx_day TEXT);
CREATE TABLE IF NOT EXISTS wallet_days (day TEXT, wh TEXT, PRIMARY KEY (day, wh));
CREATE TABLE IF NOT EXISTS events   (id INTEGER PRIMARY KEY, ts REAL, day TEXT, vid TEXT, name TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS ptbs     (digest TEXT PRIMARY KEY, ts REAL, day TEXT, wh TEXT, status TEXT, kind TEXT,
                                     n_actions INTEGER, usd REAL, verified INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS actions  (digest TEXT, idx INTEGER, service TEXT, coin TEXT, amount REAL, usd REAL, PRIMARY KEY (digest, idx));
CREATE TABLE IF NOT EXISTS failures (id INTEGER PRIMARY KEY, ts REAL, day TEXT, kind TEXT, text TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, ts REAL, day TEXT, vid TEXT, rating INTEGER, note TEXT, ask TEXT, answer TEXT);
CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS team_v   (vid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS team_w   (wh TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS pipeline (iid TEXT PRIMARY KEY, ts REAL, day TEXT, wh TEXT, kind TEXT, plan TEXT, sim TEXT, check1 TEXT,
                                     sign TEXT, digest TEXT, chain TEXT, check2 TEXT);
CREATE INDEX IF NOT EXISTS ev_day ON events(day, name);
CREATE INDEX IF NOT EXISTS ptb_day ON ptbs(day);
CREATE INDEX IF NOT EXISTS fail_day ON failures(day, kind);
"""

EVENTS = {'visit', 'connect', 'card', 'approve', 'reject', 'signed', 'tx_failed', 'terminal', 'market', 'wallet_panel', 'feature', 'msg'}
SERVICES = ('swap', 'send', 'lend', 'withdraw', 'claim', 'perps_open', 'perps_close', 'perps_deposit', 'perps_withdraw',
            'perps_margin', 'perps_tpsl', 'perps_cancel', 'perps_account')


# This thread's SQLite connection (WAL mode, so readers don't block the site); creates the tables and adds later columns.
def _db():
    c = getattr(_local, 'c', None)
    if c is None:
        DATA.mkdir(exist_ok=True)
        c = sqlite3.connect(str(DB), timeout=10, check_same_thread=False)
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)
        for col in ('failures ADD COLUMN vid TEXT', 'ptbs ADD COLUMN gas_sui REAL DEFAULT 0', 'wallets ADD COLUMN first_vid TEXT', 'ptbs ADD COLUMN fee_sui REAL DEFAULT 0'):
            try:                                         # columns added after the first version (2026-10-22)
                c.execute('ALTER TABLE ' + col)
            except sqlite3.OperationalError:
                pass
        _local.c = c
    return c


# The private salt behind wh(), created once in the data folder (never leaves the box, not in backups).
def _salt():
    f = DATA / 'metrics.salt'
    if not f.exists():
        DATA.mkdir(exist_ok=True)
        f.write_text(secrets.token_hex(32)); os.chmod(f, 0o600)
    return f.read_text().strip().encode()


_SALT = None


def wh(address):
    """A wallet address -> its salted hash (the address itself is never stored)."""
    global _SALT
    if _SALT is None:
        _SALT = _salt()
    return hmac.new(_SALT, str(address or '').lower().encode(), hashlib.sha256).hexdigest()[:24]


def day(ts=None):
    return time.strftime('%Y-%m-%d', time.gmtime(ts or time.time()))


VID_RE = re.compile(r'^[0-9a-f]{16,32}$')


def redact(text, n=300):
    """Free text for the failure log: addresses, .sui names and long numbers blanked; capped."""
    t = re.sub(r'0x[0-9a-fA-F]{6,}', '0x…', str(text or ''))
    t = re.sub(r'\b[\w-]+\.sui\b', 'name.sui', t)
    t = re.sub(r'@\w{2,}', '@name', t)
    t = re.sub(r'\b[a-z]{3,}(\s+[a-z]{3,}){11,}\b', '[words removed]', t, flags=re.I)   # a pasted seed phrase never lands here
    return t[:n]


# A page visit by an anonymous browser id (POST /api/m): one visits row per day, first/last day per visitor.
def visit(vid):
    if not VID_RE.match(str(vid or '')):
        return
    d = day()
    with _lock:
        c = _db()
        c.execute('INSERT INTO visits(day, vid) VALUES(?,?) ON CONFLICT(day, vid) DO UPDATE SET n = n + 1', (d, vid))
        c.execute('INSERT INTO visitors(vid, first_day, last_day) VALUES(?,?,?) ON CONFLICT(vid) DO UPDATE SET last_day = ?', (vid, d, d, d))
        c.commit()


# A wallet connected (POST /api/m): stored only as wh(address), with the visitor id that connected it first.
def wallet_seen(address, d=None, vid=None):
    if not re.match(r'^0x[0-9a-f]{64}$', str(address or '').lower()):
        return
    h, d = wh(address), d or day()
    v = vid if VID_RE.match(str(vid or '')) else None
    with _lock:
        c = _db()
        c.execute('INSERT INTO wallets(wh, first_day, last_day, first_vid) VALUES(?,?,?,?) ON CONFLICT(wh) DO UPDATE SET '
                  'first_day = MIN(first_day, excluded.first_day), last_day = MAX(last_day, excluded.last_day), '
                  'first_vid = COALESCE(first_vid, excluded.first_vid)', (h, d, d, v))
        c.execute('INSERT OR IGNORE INTO wallet_days(day, wh) VALUES(?,?)', (d, h))
        c.commit()


# One usage event (names in EVENTS), capped per browser and per day.
def event(vid, name, detail=''):
    if name not in EVENTS or not VID_RE.match(str(vid or '')):
        return
    with _lock:
        c = _db()
        if c.execute('SELECT COUNT(*) FROM events WHERE day = ? AND vid = ?', (day(), vid)).fetchone()[0] > 2000 \
                or c.execute('SELECT COUNT(*) FROM events WHERE day = ?', (day(),)).fetchone()[0] > 300000:
            return                                       # one browser (or a flood of new ids) can't fill the table
        c.execute('INSERT INTO events(ts, day, vid, name, detail) VALUES(?,?,?,?,?)', (time.time(), day(), vid, name, redact(detail, 80)))
        c.commit()


# Where Kivy struggled (a question it could not route, a refused request, a failed build), redacted and capped per day.
def failure(kind, text, detail='', vid=''):
    with _lock:
        c = _db()
        if c.execute('SELECT COUNT(*) FROM failures WHERE day = ?', (day(),)).fetchone()[0] > 20000:
            return
        c.execute('INSERT INTO failures(ts, day, kind, text, detail, vid) VALUES(?,?,?,?,?,?)',
                  (time.time(), day(), kind[:30], redact(text), redact(detail, 200), vid if VID_RE.match(str(vid or '')) else ''))
        c.commit()


# POST /api/feedback: a thumbs up/down under an answer (rating 1/-1) or a Settings message (0); rebuilds feedback.md.
def feedback(vid, rating, note, ask, answer):
    if not VID_RE.match(str(vid or '')) or rating not in (1, -1, 0):   # 0 = a message from Settings › Feedback (2026-10-23)
        return False
    if rating == 0 and not str(note or '').strip():
        return False
    with _lock:
        c = _db()
        if c.execute('SELECT COUNT(*) FROM feedback WHERE day = ? AND vid = ?', (day(), vid)).fetchone()[0] > 200 \
                or c.execute('SELECT COUNT(*) FROM feedback WHERE day = ?', (day(),)).fetchone()[0] > 5000:   # all visitors: a flood can't fill the disk
            return False
        c.execute('INSERT INTO feedback(ts, day, vid, rating, note, ask, answer) VALUES(?,?,?,?,?,?,?)',
                  (time.time(), day(), vid, rating, redact(note, 500), redact(ask), redact(answer, 400)))
        c.commit()
    if time.time() - _FBMD.get('t', 0) > 20:          # the log is rebuilt at most every 20 s (a flood can't burn the CPU); the weekly report reads the table
        _FBMD['t'] = time.time()
        try:
            feedback_md()
        except Exception:
            pass
    return True


_FBMD = {}


FEEDBACK_MD = DATA / 'feedback.md'


def feedback_md():
    """Every 👍 / 👎 and Settings message as a readable log, newest on top, a heading per day — like Kivy's diary (2026-10-23).
    Rebuilt from the table each time, so it always matches it. Addresses are already hidden (redact). a reviewer summarises it weekly."""
    with _lock:
        rows = _db().execute('SELECT ts, day, rating, note, ask, answer FROM feedback ORDER BY ts DESC').fetchall()
    out, cur = ['# Kivy feedback — 👍 / 👎 under answers and Settings › Feedback (newest on top, times UTC)', ''], None
    one = lambda x, n: ' '.join(str(x or '').split())[:n]
    for ts, d, rating, note, ask, answer in rows:
        if d != cur:
            out += (['', '## ' + d] if cur else ['## ' + d]); cur = d
        hm = time.strftime('%H:%M', time.gmtime(ts))
        if rating == 0:
            out.append('- **%s UTC** — ✉ %s %s' % (hm, one(ask, 20), '"%s"' % one(note, 500)))
        else:
            out.append('- **%s UTC** — %s%s · asked: "%s" · Kivy: "%s"' % (hm, '👍' if rating > 0 else '👎', (' ' + one(note, 300)) if note else '',
                                                                          one(ask, 160), one(answer, 220)))
    tmp = FEEDBACK_MD.with_suffix('.tmp')
    tmp.write_text('\n'.join(out) + '\n')
    tmp.replace(FEEDBACK_MD)


def ptb(digest, address, status, kind, actions, verified, source='live', ts=None, gas=0.0, fee=0.0):
    """One confirmed on-chain transaction made through Kivy, with its steps. A digest is recorded once."""
    ts = ts or time.time()
    d = day(ts)
    acts = [a for a in (actions or []) if a.get('service') in SERVICES]
    # the money in this PTB, counted once: its largest step (a $1 swap that is then lent is $1 moved, not $2);
    # each service's own volume stays in the actions table
    usd = round(max([float(a.get('usd') or 0) for a in acts] or [0]), 6)
    with _lock:
        c = _db()
        old = c.execute('SELECT verified FROM ptbs WHERE digest = ?', (digest,)).fetchone()
        if old and (old[0] or not verified):
            return False
        if old:                                      # recorded first without its plan (someone else logged it): the verified record replaces it
            c.execute('DELETE FROM ptbs WHERE digest = ?', (digest,))
            c.execute('DELETE FROM actions WHERE digest = ?', (digest,))
        h = wh(address) if address else ''
        c.execute('INSERT INTO ptbs(digest, ts, day, wh, status, kind, n_actions, usd, verified, source, gas_sui, fee_sui) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (digest, ts, d, h, status, kind, len(acts), usd, 1 if verified else 0, source, float(gas or 0), float(fee or 0)))
        for i, a in enumerate(acts):
            c.execute('INSERT OR REPLACE INTO actions(digest, idx, service, coin, amount, usd) VALUES(?,?,?,?,?,?)',
                      (digest, i, a['service'], str(a.get('coin') or '')[:16], float(a.get('amount') or 0), float(a.get('usd') or 0)))
        if h and status == 'SUCCESS' and verified:     # a wallet counts as transacting only on a tx Kivy built for it (review 2026-10-24)
            c.execute('INSERT INTO wallets(wh, first_day, last_day, first_tx_day) VALUES(?,?,?,?) ON CONFLICT(wh) DO UPDATE SET '
                      'first_day = MIN(first_day, excluded.first_day), last_day = MAX(last_day, excluded.last_day), '
                      'first_tx_day = MIN(COALESCE(first_tx_day, excluded.first_tx_day), excluded.first_tx_day)', (h, d, d, d))
            c.execute('INSERT OR IGNORE INTO wallet_days(day, wh) VALUES(?,?)', (d, h))
        c.commit()
    return True


PIPE_FIELDS = ('kind', 'plan', 'sim', 'check1', 'sign', 'digest', 'chain', 'check2')


def pipe(iid, address=None, create=False, ts=None, **fields):
    """One transaction followed through every check (2026-10-23): Kivy's plan -> simulation of the exact bytes the
    wallet signs -> check 1 -> the user's answer -> the real chain result -> check 2. Keyed by Kivy's plan id.
    create=False only fills a row that already exists (rows start when a real user taps Approve)."""
    if not re.match(r'^[0-9a-f]{16}$', str(iid or '')):
        return
    with _lock:
        c = _db()
        if create:
            t = ts or time.time()                    # ts: a backfilled transaction keeps its own time
            c.execute('INSERT OR IGNORE INTO pipeline(iid, ts, day, wh) VALUES(?,?,?,?)', (iid, t, day(t), wh(address) if address else ''))
        for k, v in fields.items():
            if k in PIPE_FIELDS and v is not None:
                c.execute('UPDATE pipeline SET %s = ? WHERE iid = ?' % k, (v if isinstance(v, str) else json.dumps(v), iid))
        c.commit()


def mark_team(vid=None, addresses=()):
    """Our own browsers and wallets, so the numbers can be shown without the team's testing."""
    with _lock:
        c = _db()
        if vid and VID_RE.match(vid):
            c.execute('INSERT OR IGNORE INTO team_v(vid) VALUES(?)', (vid,))
        for a in addresses:
            c.execute('INSERT OR IGNORE INTO team_w(wh) VALUES(?)', (wh(a),))
        c.commit()


# Helpers of summary(): retention (D1/D7/D30) and weekly cohorts, for wallets and for visitors.
def _retention(c, table_first, table_days, key, F=''):
    """Dn = of the users first seen at least n days ago, the share active again exactly n days later (and 'n or later')."""
    today = day()
    out = {}
    for n in (1, 7, 30):
        cut = day(time.time() - n * 86400)
        base = c.execute('SELECT COUNT(*) FROM %s f WHERE first_day <= ?%s' % (table_first, F), (cut,)).fetchone()[0]
        on = c.execute('SELECT COUNT(DISTINCT f.%s) FROM %s f JOIN %s d ON d.%s = f.%s WHERE f.first_day <= ?%s '
                       "AND d.day = date(f.first_day, '+%d day')" % (key, table_first, table_days, key, key, F, n), (cut,)).fetchone()[0]
        later = c.execute('SELECT COUNT(DISTINCT f.%s) FROM %s f JOIN %s d ON d.%s = f.%s WHERE f.first_day <= ?%s '
                          "AND d.day >= date(f.first_day, '+%d day')" % (key, table_first, table_days, key, key, F, n), (cut,)).fetchone()[0]
        out['d%d' % n] = {'base': base, 'exact': on, 'later': later,
                          'pct': round(on * 100 / base, 1) if base else None, 'pct_later': round(later * 100 / base, 1) if base else None}
    return out


def _cohorts(c, table_first, table_days, key, weeks=8, F=''):
    """Weekly cohorts by first week: share active in week 0..k after."""
    rows = c.execute("SELECT strftime('%%Y-%%W', first_day) w, COUNT(*) FROM %s f WHERE 1=1%s GROUP BY w ORDER BY w DESC LIMIT ?" % (table_first, F), (weeks,)).fetchall()
    out = []
    for w, size in rows[::-1]:
        act = c.execute("SELECT strftime('%%Y-%%W', d.day) aw, COUNT(DISTINCT d.%s) FROM %s d JOIN %s f ON f.%s = d.%s "
                        "WHERE strftime('%%Y-%%W', f.first_day) = ?%s GROUP BY aw ORDER BY aw" % (key, table_days, table_first, key, key, F), (w,)).fetchall()
        out.append({'week': w, 'size': size, 'active': [[aw, n] for aw, n in act]})
    return out


def summary(days=30, exclude_team=False):
    """Everything the dashboard shows. exclude_team: leave out our own browsers and wallets (team_v / team_w)."""
    with _lock:
        c = _db()
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        since = day(time.time() - days * 86400)
        V = ' AND vid NOT IN (SELECT vid FROM team_v)' if exclude_team else ''        # visitor-keyed tables
        W = ' AND wh NOT IN (SELECT wh FROM team_w)' if exclude_team else ''          # wallet-keyed tables
        FV = ' AND f.vid NOT IN (SELECT vid FROM team_v)' if exclude_team else ''
        FW = ' AND f.wh NOT IN (SELECT wh FROM team_w)' if exclude_team else ''
        ok = "status = 'SUCCESS' AND (verified = 1 OR source = 'backfill')" + W   # never a digest someone merely posted
        pok = "p.status = 'SUCCESS' AND (p.verified = 1 OR p.source = 'backfill')" + (' AND p.wh NOT IN (SELECT wh FROM team_w)' if exclude_team else '')
        s = {
            'generated': time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime()), 'exclude_team': exclude_team,
            'totals': {
                'unique_visitors': q('SELECT COUNT(*) FROM visitors WHERE 1=1' + V),
                'visits': q('SELECT COALESCE(SUM(n), 0) FROM visits WHERE 1=1' + V),
                'unique_wallets_connected': q('SELECT COUNT(*) FROM wallets WHERE 1=1' + W),
                'unique_wallets_transacting': q('SELECT COUNT(*) FROM wallets WHERE first_tx_day IS NOT NULL' + W),
                'wallet_connects': q("SELECT COUNT(*) FROM events WHERE name = 'connect'" + V),
                'ptbs': q('SELECT COUNT(*) FROM ptbs WHERE ' + ok),
                'ptbs_failed': q("SELECT COUNT(*) FROM ptbs WHERE status != 'SUCCESS'" + W),
                'ptbs_unverified': q("SELECT COUNT(*) FROM ptbs WHERE status = 'SUCCESS' AND verified = 0 AND source != 'backfill'" + W),
                'actions': q('SELECT COUNT(*) FROM actions a JOIN ptbs p USING(digest) WHERE ' + pok),
                'volume_usd': round(q('SELECT COALESCE(SUM(usd), 0) FROM ptbs WHERE ' + ok), 2),
                'ptbs_backfilled': q("SELECT COUNT(*) FROM ptbs WHERE source = 'backfill' AND " + ok),
            },
            'by_service': [dict(zip(('service', 'actions', 'usd'), r)) for r in c.execute(
                'SELECT a.service, COUNT(*), ROUND(SUM(a.usd), 2) FROM actions a JOIN ptbs p USING(digest) WHERE ' + pok +
                ' GROUP BY a.service ORDER BY SUM(a.usd) DESC, COUNT(*) DESC').fetchall()],
            'daily': [dict(zip(('day', 'visitors', 'wallets', 'ptbs', 'actions', 'usd'), r)) for r in c.execute(
                "WITH RECURSIVE d(x) AS (SELECT ? UNION ALL SELECT date(x, '+1 day') FROM d WHERE x < date('now')) "
                'SELECT x, (SELECT COUNT(*) FROM visits WHERE day = x' + V + '), (SELECT COUNT(*) FROM wallet_days WHERE day = x' + W + '), '
                '(SELECT COUNT(*) FROM ptbs WHERE day = x AND ' + ok + '), '
                '(SELECT COUNT(*) FROM actions a JOIN ptbs p USING(digest) WHERE p.day = x AND ' + pok + '), '
                '(SELECT ROUND(COALESCE(SUM(usd), 0), 2) FROM ptbs WHERE day = x AND ' + ok + ') FROM d', (since,)).fetchall()],
            'retention': {'wallets': _retention(c, 'wallets', 'wallet_days', 'wh', FW), 'visitors': _retention(c, 'visitors', 'visits', 'vid', FV)},
            'cohorts': {'wallets': _cohorts(c, 'wallets', 'wallet_days', 'wh', F=FW), 'visitors': _cohorts(c, 'visitors', 'visits', 'vid', F=FV)},
            'usage': [dict(zip(('event', 'detail', 'n'), r)) for r in c.execute(
                'SELECT name, detail, COUNT(*) FROM events WHERE day >= ?' + V + ' GROUP BY name, detail ORDER BY COUNT(*) DESC LIMIT 40', (since,)).fetchall()],
            'failures': [dict(zip(('kind', 'text', 'n', 'last'), r)) for r in c.execute(
                'SELECT kind, text, COUNT(*), MAX(day) FROM failures WHERE day >= ?' + V.replace('vid', 'COALESCE(vid, \'\')') + ' GROUP BY kind, lower(text) ORDER BY COUNT(*) DESC LIMIT 60', (since,)).fetchall()],
            'failure_kinds': [dict(zip(('kind', 'n'), r)) for r in c.execute(
                'SELECT kind, COUNT(*) FROM failures WHERE day >= ?' + V.replace('vid', 'COALESCE(vid, \'\')') + ' GROUP BY kind ORDER BY COUNT(*) DESC', (since,)).fetchall()],
            'pipeline': [dict(zip(('iid', 'ts', 'kind', 'plan', 'sim', 'check1', 'sign', 'digest', 'chain', 'check2', 'wh'), r)) for r in c.execute(
                'SELECT iid, ts, kind, plan, sim, check1, sign, digest, chain, check2, wh FROM pipeline WHERE 1=1' + W + ' ORDER BY ts DESC LIMIT 80').fetchall()],
            'feedback': {'up': q('SELECT COUNT(*) FROM feedback WHERE rating = 1' + V), 'down': q('SELECT COUNT(*) FROM feedback WHERE rating = -1' + V),
                         'recent': [dict(zip(('day', 'rating', 'note', 'ask', 'answer'), r)) for r in c.execute(
                             'SELECT day, rating, note, ask, answer FROM feedback WHERE 1=1' + V + ' ORDER BY id DESC LIMIT 40').fetchall()]},
        }
        # per-day series behind every number card's graph (2026-10-23) — dashboard only, the public page doesn't get these
        dm = {r['day']: {} for r in s['daily']}
        for key, sql in (
                ('visits', 'SELECT day, SUM(n) FROM visits WHERE day >= ?' + V),
                ('new_visitors', 'SELECT first_day, COUNT(*) FROM visitors WHERE first_day >= ?' + V),
                ('new_wallets', 'SELECT first_day, COUNT(*) FROM wallets WHERE first_day >= ?' + W),
                ('new_txw', 'SELECT first_tx_day, COUNT(*) FROM wallets WHERE first_tx_day >= ?' + W),
                ('lag', 'SELECT first_tx_day, ROUND(AVG(julianday(first_tx_day) - julianday(first_day)), 1) FROM wallets WHERE first_tx_day >= ?' + W),
                ('txw', 'SELECT day, COUNT(DISTINCT wh) FROM ptbs WHERE day >= ? AND ' + ok),
                ('failed', "SELECT day, COUNT(*) FROM ptbs WHERE day >= ? AND status != 'SUCCESS'" + W),
                ('msgs', "SELECT day, COUNT(*) FROM events WHERE day >= ? AND name = 'msg'" + V),
                ('ai', "SELECT day, COUNT(*) FROM events WHERE day >= ? AND name = 'msg' AND detail = 'ai'" + V),
                ('conn_v', "SELECT day, COUNT(DISTINCT vid) FROM events WHERE day >= ? AND name = 'connect'" + V),
                ('approves', "SELECT day, COUNT(*) FROM events WHERE day >= ? AND name = 'approve'" + V),
                ('signed', "SELECT day, COUNT(*) FROM events WHERE day >= ? AND name = 'signed'" + V),
                ('fees', 'SELECT day, ROUND(SUM(fee_sui), 9) FROM ptbs WHERE day >= ? AND ' + ok),
                ('fee_ptbs', 'SELECT day, COUNT(*) FROM ptbs WHERE day >= ? AND fee_sui > 0 AND ' + ok),
                ('gas', 'SELECT day, ROUND(SUM(gas_sui), 9) FROM ptbs WHERE day >= ? AND ' + ok)):
            for d, n in c.execute(sql + ' GROUP BY 1', (since,)).fetchall():
                if d in dm:
                    dm[d][key] = n
        s['daily_more'] = [dict({'day': d}, **v) for d, v in dm.items()]
        msgs = q("SELECT COUNT(*) FROM events WHERE name = 'msg'" + V)
        ai = q("SELECT COUNT(*) FROM events WHERE name = 'msg' AND detail = 'ai'" + V)
        vis = s['totals']['visits'] or 0
        conn_v = q("SELECT COUNT(DISTINCT vid) FROM events WHERE name = 'connect'" + V)
        lag = [r[0] for r in c.execute("SELECT julianday(first_tx_day) - julianday(first_day) FROM wallets WHERE first_tx_day IS NOT NULL" + W + ' ORDER BY 1').fetchall()]
        appr_n = q("SELECT COUNT(*) FROM events WHERE name = 'approve'" + V)
        signed_n = q("SELECT COUNT(*) FROM events WHERE name = 'signed'" + V)
        allp = q('SELECT COUNT(*) FROM ptbs WHERE (verified = 1 OR source = \'backfill\')' + W)
        okp = s['totals']['ptbs']
        pct = lambda a, b: round(a * 100.0 / b, 1) if b else None
        s['engagement'] = {
            'messages': msgs, 'messages_per_visit': round(msgs / vis, 2) if vis else None, 'ai_share_pct': pct(ai, msgs),
            'visitors_who_connected_pct': pct(conn_v, s['totals']['unique_visitors']),
            'connected_who_transacted_pct': pct(s['totals']['unique_wallets_transacting'], s['totals']['unique_wallets_connected']),
            'days_to_first_tx_median': (lag[len(lag) // 2] if lag else None),
            'approve_to_signed_pct': pct(signed_n, appr_n), 'chain_success_pct': pct(okp, allp),
            'ptbs_per_transacting_wallet': round(okp / s['totals']['unique_wallets_transacting'], 2) if s['totals']['unique_wallets_transacting'] else None,
        }
        s['revenue'] = {'gas_sui_paid': round(q('SELECT COALESCE(SUM(gas_sui), 0) FROM ptbs WHERE ' + ok), 6),
                        'kivy_fees_sui': round(q('SELECT COALESCE(SUM(fee_sui), 0) FROM ptbs WHERE ' + ok), 6),
                        'fee_paying_ptbs': q('SELECT COUNT(*) FROM ptbs WHERE fee_sui > 0 AND ' + ok),
                        'volume_usd': s['totals']['volume_usd'], 'by_service': s['by_service']}
        s['snapshots'] = [dict(zip(('day', 'data'), (r[0], json.loads(r[1])))) for r in c.execute('SELECT day, data FROM snapshots ORDER BY day DESC LIMIT 60').fetchall()]
        s['funnel'] = {'visitors': s['totals']['unique_visitors'], 'connected': s['totals']['unique_wallets_connected'],
                       'transacted': s['totals']['unique_wallets_transacting'],
                       'approve_clicks': q("SELECT COUNT(*) FROM events WHERE name = 'approve'" + V),
                       'rejects': q("SELECT COUNT(*) FROM events WHERE name = 'reject'" + V),
                       'signed': q("SELECT COUNT(*) FROM events WHERE name = 'signed'" + V)}
    return s


def snapshot_today():
    """Once a day: freeze the headline numbers (both with and without the team) so the deck can show history as it was."""
    d = day()
    with _lock:
        if _db().execute('SELECT 1 FROM snapshots WHERE day = ?', (d,)).fetchone():
            return False
    a, b = summary(30, False), summary(30, True)
    data = {'all': a['totals'], 'users': b['totals'], 'engagement_users': b['engagement']}
    with _lock:
        c = _db()
        c.execute('INSERT OR IGNORE INTO snapshots(day, data) VALUES(?,?)', (d, json.dumps(data)))
        c.commit()
    return True


def export_rows(what, exclude_team=False):
    """CSV rows for due diligence: 'ptbs' (every confirmed transaction id — check each on SuiVision), 'actions', 'daily', 'snapshots'."""
    W = ' AND p.wh NOT IN (SELECT wh FROM team_w)' if exclude_team else ''
    with _lock:
        c = _db()
        if what == 'ptbs':
            head = ['day', 'utc_time', 'digest', 'explorer', 'status', 'kind', 'actions', 'money_moved_usd', 'gas_sui', 'source', 'team']
            rows = c.execute("SELECT p.day, strftime('%H:%M:%S', p.ts, 'unixepoch'), p.digest, 'https://suivision.xyz/txblock/' || p.digest, p.status, p.kind, "
                             "p.n_actions, p.usd, p.gas_sui, p.source, CASE WHEN p.wh IN (SELECT wh FROM team_w) THEN 'yes' ELSE 'no' END "
                             "FROM ptbs p WHERE (p.verified = 1 OR p.source = 'backfill')" + W + ' ORDER BY p.ts').fetchall()
        elif what == 'actions':
            head = ['day', 'digest', 'step', 'service', 'coin', 'amount', 'usd']
            rows = c.execute("SELECT p.day, a.digest, a.idx + 1, a.service, a.coin, a.amount, a.usd FROM actions a JOIN ptbs p USING(digest) "
                             "WHERE p.status = 'SUCCESS' AND (p.verified = 1 OR p.source = 'backfill')" + W + ' ORDER BY p.ts, a.idx').fetchall()
        elif what == 'snapshots':
            head = ['day', 'numbers_json']
            rows = c.execute('SELECT day, data FROM snapshots ORDER BY day').fetchall()
        else:
            head = ['day', 'visitors', 'active_wallets', 'ptbs', 'actions', 'money_moved_usd']
            rows = [tuple(r.values()) for r in summary(365, exclude_team)['daily']]
            return head, rows
    return head, rows


def public_summary():
    """What the public page shows: totals and a daily chart — no ids, no addresses (everyone counted, like the dashboard)."""
    s = summary(90, False)
    t = s['totals']
    return {'generated': s['generated'],
            'totals': {k: t[k] for k in ('unique_visitors', 'unique_wallets_connected', 'unique_wallets_transacting', 'ptbs', 'actions', 'volume_usd')},
            'by_service': s['by_service'], 'daily': s['daily'], 'retention_wallets': s['retention']['wallets']}
