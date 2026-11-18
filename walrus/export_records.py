#!/usr/bin/env python3
# export_records.py — step 1 of the Walrus saver: turns Kivy's security records into plain text for Walrus Memory.
# Run by sync_records.sh (kivy-walrus.timer, as user kivy); prints a JSON list of {key, digest, ns, text} on stdout, which upload.mjs reads.
# Reads (read-only): Kivy's metrics database (pipeline table), the after-signing checks log, the Terms agreements database,
# kivy_pkgs.json (the allowlist) and ledger.jsonl (what upload.mjs already saved). Writes only its own progress file when
# given one (--state; senders.py may add to its senders.json cache).
# Links: metrics.py (metrics.wh = Kivy's salted wallet hash), senders.py (wallet hash for old records, from Sui GraphQL).
"""Kivy security records -> plain-text records for Walrus Memory (2026-10-24).

One record per transaction Kivy built (metrics.db pipeline: plan, check 1, sign, chain, check 2), one per after-signing
check with no pipeline row (data/checks.jsonl, older ones), and one for the rule set (kivy_pkgs.json allowlist).
Each wallet has its own space on Walrus (namespace wallet-<random id>, kept only in spaces.jsonl since 2026-11-18; older
records are in wallet-<salted hash>); the rule set is in kivy-rules. No raw address leaves the box; old records without a wallet get it from the chain (senders.py). Records already saved
(ledger.jsonl) are skipped, so this is safe to run again. A transaction waits for its check 2 (at most 2 hours).
Runs on its own: kivy-walrus.timer every minute, as user kivy (sync_records.sh; nothing is sent when nothing is new), so a new transaction is on Walrus ~1 minute after check 2.
  python3 export_records.py            # print the new records as JSON (upload.mjs reads this)
  python3 export_records.py --state export_state.db   # the same records, reading only what is new since the last run

Incremental (fix package 4, 2026-11-15; a reviewer page-ops scan S2): every minute it re-read the whole pipeline table, all of
checks.jsonl twice and all of ledger.jsonl, so its cost grew with every transaction ever made. With --state it keeps where it
stopped in a small SQLite file (export_state.db): the ledger's and the checks log's byte offsets, the first pipeline row and
Terms agreement not yet saved, the saved keys, and each digest's first and last check line. Each run reads only the new lines
and rows (plus the few still waiting) and prints exactly what the full read prints, in the same order. Without a usable
state file (not given, not writable in the service sandbox, or any error) it does the full read, as before. The state is
rebuilt from scratch once a day and whenever a source file was replaced or shortened. Tests: tests/test_waste_20261115.py.
"""
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, '/opt/kivy')
import metrics  # noqa: E402
import senders  # noqa: E402

HERE = Path(__file__).resolve().parent
LEDGER = HERE / 'ledger.jsonl'
CUSTOMERS = Path('/opt/kivy/data/customers.db')
METRICS_DB = Path('/opt/kivy/data/metrics.db')
CHECKS = Path('/opt/kivy/data/checks.jsonl')
PKGS = Path('/opt/kivy/kivy_pkgs.json')
TX_WAIT = 2 * 3600                                     # a transaction waits this long for its check 2


# helpers: j = JSON column or None · when = unix time -> UTC text · verdict = one check's result as a sentence
def j(v):
    try:
        return json.loads(v) if v else None
    except Exception:
        return None


def when(ts):
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime('%Y-%m-%d')   # the day only (W-4 A+)


# W-4 A+ (2026-11-18): a Walrus record can never be deleted, so it carries nothing that finds the wallet on chain:
# no transaction id, the day only, amounts rounded to 2 significant figures. The link record <-> transaction stays on this
# box (ledger.jsonl: key, digest, blob id; the stats and checks files), so deleting it there leaves the Walrus copy untraceable.
def day(at):
    return str(at or '')[:10] or 'unknown day'


def rnd(t):
    """Every number with a decimal point, or of 3+ digits, rounded to 2 significant figures (0.123456 -> 0.12, 52.76 -> 53,
    -100000 -> -100000); dates and times (2026-11-18, 09:45) and words with digits (v1.2, 10x, p95) stay as they are."""
    def one(m):
        x = float(m.group(0))
        if x == 0:
            return '0'
        r = float('%.2g' % abs(x)) * (1 if x > 0 else -1)
        return ('%d' % r) if abs(r) >= 10 else ('%.10f' % r).rstrip('0').rstrip('.')
    num = r'-?(?:\d+\.\d+|\d{3,})'
    return re.sub(r'(?<![\w.:])(?<!\d-)' + num + r'(?![\w.:]|-\d)', one, str(t))


def ref(key):
    """A record's short reference: a salted hash of its local key (finds the record from our ledger; useless without the salt)."""
    return metrics.wh('walrus-record:' + key)[:12]


def verdict(c):
    # check reasons can quote a full address (e.g. a check-1 block naming who would receive a coin): scrub them too
    return rnd(scrub(_verdict(c)))


def _verdict(c):
    if not c:
        return 'not recorded'
    notes = ' (%s)' % '; '.join(c['notes']) if c.get('notes') else ''
    if c.get('ok') is None:
        return 'not run' + notes
    if c.get('ok'):
        return 'ok' + notes
    return 'FAILED: ' + '; '.join(c.get('problems') or [c.get('why') or 'no reason recorded']) + notes


DROPPED = Path(__file__).resolve().parent / 'run.log'    # records left out for carrying a raw address (review W2-3); run.log is the one log the sandboxed service may write


def who(w):
    """'you' and 'Kivy' stay; any other wallet (shown shortened in the logs) becomes 'another wallet'."""
    return w if w in ('you', 'Kivy') else 'another wallet'


def scrub(t):
    """Recipients in plan steps (0x addresses, shortened or full, and .sui names) become 'another wallet'."""
    return re.sub(r'0x[0-9a-fA-F]+(?:\u2026|…|\.\.\.)?[0-9a-fA-F]*|\b[\w-]+\.sui\b', 'another wallet', t)


def moves(m):
    return ', '.join('%s %s %s' % (who(x.get('who')), rnd(x.get('amt')), x.get('coin')) for x in (m or [])) or 'none'


# the Walrus Memory namespace a record goes into: one per wallet, a RANDOM id kept only in spaces.jsonl (2026-11-18,
# W-4 A+ step 2). It used to be wallet-<salted wallet hash>, which Kivy could recompute from an address at any time; with a
# random id, deleting the wallet's line in spaces.jsonl leaves its Walrus records unconnected, for Kivy too. Older records
# stay in their wallet-<hash> spaces. spaces.jsonl only grows here (one line {"wh", "space"} per wallet, appended before the
# record is printed, so an upload never uses an id that was not kept); tests point SPACES at a temp file.
SPACES = HERE / 'spaces.jsonl'
_SPACES = {'path': None, 'map': {}}


def space(wh):
    if not wh:
        return 'wallet-unknown'
    if _SPACES['path'] != SPACES:                      # (re)load when the file is new to this process (tests swap SPACES)
        m = {}
        for ln in SPACES.read_text().splitlines() if SPACES.exists() else []:
            r = j(ln)
            if r and r.get('wh') and r.get('space'):
                m[r['wh']] = r['space']
        _SPACES.update(path=SPACES, map=m)
    sp = _SPACES['map'].get(wh)
    if not sp:
        sp = 'wallet-' + secrets.token_hex(12)
        with SPACES.open('a') as f:                    # fails loudly when not writable: no record goes out under an id we did not keep
            f.write(json.dumps({'wh': wh, 'space': sp}) + '\n')
        _SPACES['map'][wh] = sp
    return sp


# one record per transaction Kivy built (pipeline table: plan, simulation, check 1, sign, chain result, check 2);
# also returns every digest seen, so check_records() skips those
PIPE_COLS = 'iid, ts, wh, kind, plan, sim, check1, sign, digest, chain, check2'


def tx_ready(r, done, now):
    return 'tx:' + r[0] not in done and (r[10] or now - float(r[1]) >= TX_WAIT)  # wait for check 2 (max 2 h)


def pipeline_records(done, chain_lookup=True):
    c = sqlite3.connect('file:%s?mode=ro' % METRICS_DB, uri=True)
    rows = c.execute('SELECT %s FROM pipeline ORDER BY ts' % PIPE_COLS).fetchall()
    now = time.time()
    ready = [r for r in rows if tx_ready(r, done, now)]
    return tx_records(ready, chain_lookup), {r[8] for r in rows if r[8]}


# the ready pipeline rows -> their records (wallet hash from the chain for old rows without one)
def tx_records(ready, chain_lookup=True):
    found = senders.fill([r[8] for r in ready if not r[2]]) if chain_lookup and any(not r[2] for r in ready) else {}
    out = []
    for iid, ts, wh, kind, plan, sim, c1, sign, digest, chain, c2 in ready:
        key = 'tx:' + iid
        wh = wh or found.get(digest) or ''
        plan, sim, c1, sign, chain, c2 = j(plan), j(sim), j(c1), j(sign), j(chain), j(c2)
        s = sign or {}
        text = ('Kivy security record. Transaction check, %s. Record %s. Type: %s. Plan: %s. Kivy fee: %s SUI. '
                'Simulated: %s. Check 1 (before signing): %s. Sign: %s%s. On chain: %s, moves %s. '
                'Check 2 (after signing, against the plan): %s.'
                % (when(ts), ref(key), kind or '?', '; '.join(rnd(scrub(x)) for x in (plan or {}).get('steps') or []) or 'not recorded',
                   rnd((plan or {}).get('fee_sui', 0)), moves(sim), verdict(c1), s.get('result') or 'not recorded',
                   (' (%s)' % scrub(str(s.get('wallet')))) if s.get('wallet') else '',
                   (chain or {}).get('status') or 'not recorded', moves((chain or {}).get('moves')), verdict(c2)))
        out.append({'key': key, 'digest': digest, 'ns': space(wh), 'text': text})
    return out


# older after-signing checks (the checks log) whose transaction has no pipeline row
def check_records(done, seen):
    out = []
    p = CHECKS
    for ln in p.read_text().splitlines() if p.exists() else []:
        r = j(ln)
        if not r or r.get('digest') in seen:
            continue
        if check_key(r) in done:
            continue
        out.append(check_record(r))
    return out


def check_key(r):
    return 'check:%s:%s' % (r.get('digest'), r.get('at'))


def check_record(r):
    text = ('Kivy security record. After-signing check, %s. Record %s. Type: %s. On chain: %s. '
            'Check 2: %s.%s'
            % (day(r.get('at')), ref(check_key(r)), r.get('kind') or 'unknown', r.get('status') or 'not recorded',
               verdict(r), (' Notes: ' + rnd(scrub('; '.join(r['notes']))) + '.') if r.get('notes') else ''))
    return {'key': check_key(r), 'digest': r.get('digest'), 'ns': space(metrics.wh(r['address']) if r.get('address') else ''), 'text': text}


def recheck_records(done):
    """A transaction re-checked later with a different verdict gets a CORRECTION record next to the original (2026-10-26:
    a check-2 false alarm was fixed after its record was already on Walrus, which cannot edit or delete). The original stays:
    the history shows both, and why it changed."""
    p = CHECKS
    by = {}
    for ln in p.read_text().splitlines() if p.exists() else []:
        r = j(ln)
        if r and r.get('digest'):
            by.setdefault(r['digest'], []).append(r)
    out = []
    for dg, rs in by.items():
        rec = recheck_record(dg, rs[0], rs[-1], len(rs), done)
        if rec:
            out.append(rec)
    return out


# one digest's CORRECTION record from its first and last check line (n = how many lines it has), or None
def recheck_record(dg, first, last, n, done):
    if n < 2 or verdict(first) == verdict(last):
        return None
    key = 'recheck:%s:%s' % (dg, last.get('at'))
    if key in done:
        return None
    wh = metrics.wh(last['address']) if last.get('address') else ''
    text = ('Kivy security record. CORRECTION, %s. Record %s: a transaction was checked again after signing '
            '(Kivy\'s own ledger links it to its first record). Earlier verdict (%s): %s. New verdict: %s. The earlier record stays on Walrus unchanged; this one '
            'supersedes its check 2 verdict.' % (day(last.get('at')), ref(key),
                                                 day(first.get('at')), verdict(first), verdict(last)))
    return {'key': key, 'digest': dg, 'ns': space(wh), 'text': text}


def terms_records(done):
    """Each signed Terms agreement (data/customers.db), in the wallet's space, as a FINGERPRINT only (2026-11-18, W-4 A+
    step 2; was the address + signature, 2026-10-24): sha256 of address, version, message and signature. The signed agreement
    stays in customers.db; anyone holding it can recompute the fingerprint and match the dated Walrus record, so the proof
    holds, and no address is on Walrus."""
    p = CUSTOMERS
    if not p.exists():
        return []
    c = sqlite3.connect('file:%s?mode=ro' % p, uri=True)
    return [terms_record(row) for row in c.execute('SELECT id, address, version, ts, message, signature FROM agreements ORDER BY id')
            if 'terms:%d' % row[0] not in done]


def terms_fingerprint(a, v, msg, sig):
    return hashlib.sha256('\n'.join([str(a or '').lower(), str(v or ''), str(msg or ''), str(sig or '')]).encode()).hexdigest()


def terms_record(row):
    i, a, v, ts, msg, sig = row
    text = ('Kivy Terms agreement, %s. A wallet signed Kivy\'s Terms of Service and Privacy Policy, version %s. Fingerprint '
            '(sha256 of address, version, message and signature, joined by newlines): %s. The signed agreement is kept by Kivy.'
            % (when(ts), v, terms_fingerprint(a, v, msg, sig)))
    return {'key': 'terms:%d' % i, 'digest': None, 'ns': space(metrics.wh(a)), 'text': text}


# the package allowlist as one record in kivy-rules, saved again only when the file's sha256 changes
def rules_record(done):
    raw = PKGS.read_bytes()
    h = hashlib.sha256(raw).hexdigest()
    if 'rules:' + h in done:
        return []
    d = json.loads(raw)
    names = d.get('names') or {}
    pk = ', '.join('%s (%s)' % (p, names.get(p, '?')) for p in d.get('packages') or [])
    text = ('Kivy security rule set, version sha256 %s, saved %s. Kivy refuses to sign any transaction that calls a Move '
            'package not on this list (a DEX step inside a swap route too: refused until vetted and listed). '
            'Allowed packages (%d): %s.' % (h, dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d'), len(d.get('packages') or []), pk))
    return [{'key': 'rules:' + h, 'digest': None, 'ns': 'kivy-rules', 'text': text}]



# ── the incremental read (--state) ──
# Exactly the records the full read prints, from only what is new. Why that is the same set (the test runs both side by side):
#   ledger    only grows (upload.mjs appends): keys are read from the saved byte offset on; a replaced or shorter file = rebuild.
#   pipeline  every row before the first row whose record is not saved yet (by rowid: new rows, back-filled ones too, always
#             come after) has its key in the ledger, so the full read skips it too; rows from there on are read each run.
#   checks    a line is a record when its digest has no pipeline row and its key is not saved; a line that failed either test
#             fails it for good (saved keys stay saved; a row's digest is set once, by the first check 2 of its plan), so only
#             new lines and the lines still waiting (cpend) are looked at. A CORRECTION depends on a digest's first and last
#             line and its line count only: kept per digest (first, lastl); only digests with a new line, or still waiting
#             (rpend), are looked at.
#   terms     as the pipeline, by agreement id.
STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta  (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS done  (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS first (digest TEXT PRIMARY KEY, pos INTEGER);
CREATE TABLE IF NOT EXISTS lastl (digest TEXT PRIMARY KEY, pos INTEGER, n INTEGER);
CREATE TABLE IF NOT EXISTS cpend (pos INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS rpend (digest TEXT PRIMARY KEY);
"""
STATE_REBUILD = 24 * 3600                              # once a day the state starts again from a full read


class Done:
    """The saved keys (the state's table, plus a last ledger line still without its newline), with a set's `in`."""
    def __init__(self, db, extra):
        self.db, self.extra = db, extra

    def __contains__(self, key):
        return key in self.extra or self.db.execute('SELECT 1 FROM done WHERE key = ?', (key,)).fetchone() is not None


def file_id(p):
    st = p.stat()
    return '%d:%d' % (st.st_dev, st.st_ino), st.st_size


def read_from(p, off):
    """([(byte offset, line)], new offset, the bytes after the last newline): the file from off on, whole lines only."""
    with open(p, 'rb') as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b'\n') + 1
    out, at = [], off
    for raw in data[:end].split(b'\n')[:-1]:
        out.append((at, raw.decode('utf-8', 'replace')))
        at += len(raw) + 1
    return out, off + end, data[end:]


def line_at(fh, pos):
    fh.seek(pos)
    return j(fh.readline().rstrip(b'\n').decode('utf-8', 'replace'))


def incremental_records(state_path, now=None):
    """The same list main() builds with the full read (rules, terms, transactions, checks, corrections), from the state."""
    now = time.time() if now is None else now
    db = sqlite3.connect(str(state_path), timeout=30, isolation_level=None)
    try:
        db.execute('PRAGMA journal_mode=MEMORY')        # the sandbox lets the saver write this one file, no -journal beside it
        db.execute('PRAGMA temp_store=MEMORY')
        db.executescript(STATE_SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        try:
            recs = _incremental(db, now)
        except BaseException:
            db.execute('ROLLBACK')
            raise
        db.execute('COMMIT')
        return recs
    finally:
        db.close()


def _incremental(db, now):
    meta = dict(db.execute('SELECT k, v FROM meta').fetchall())
    led_id, led_size = file_id(LEDGER) if LEDGER.exists() else ('', 0)
    chk_id, chk_size = file_id(CHECKS) if CHECKS.exists() else ('', 0)
    rebuild = (now - float(meta.get('built') or 0) >= STATE_REBUILD or meta.get('ledger_id', led_id) != led_id
               or int(meta.get('ledger_off') or 0) > led_size)
    if rebuild or meta.get('checks_id', chk_id) != chk_id or int(meta.get('checks_off') or 0) > chk_size:
        for t in ('first', 'lastl', 'cpend', 'rpend'):  # the checks log was replaced or shortened: its part starts again
            db.execute('DELETE FROM %s' % t)
        meta['checks_off'] = '0'
    if rebuild:                                       # once a day, or the ledger was replaced: everything starts again
        db.execute('DELETE FROM done')
        meta = {'built': str(now), 'checks_off': '0'}

    # ledger: the keys saved since the last run (a last line without its newline counts now and is read again next run)
    extra = set()
    if LEDGER.exists():
        lines, off, tail = read_from(LEDGER, int(meta.get('ledger_off') or 0))
        db.executemany('INSERT OR IGNORE INTO done(key) VALUES(?)', [(json.loads(x)['key'],) for _, x in lines if x.strip()])
        if tail.strip():
            extra.add(json.loads(tail)['key'])
        meta.update(ledger_off=str(off), ledger_id=led_id)
    done = Done(db, extra)

    c = sqlite3.connect('file:%s?mode=ro' % METRICS_DB, uri=True)
    fh = open(CHECKS, 'rb') if CHECKS.exists() else None
    try:
        # pipeline: the rows from the first one whose record is not saved yet
        r0 = int(meta.get('pipe_from') or 0)
        top = c.execute('SELECT max(rowid) FROM pipeline').fetchone()[0] or 0
        if top + 1 < r0:                              # fewer rows than before (a restored copy): from the start
            r0 = 0
        rows = c.execute('SELECT rowid, %s FROM pipeline WHERE rowid >= ? ORDER BY ts' % PIPE_COLS, (r0,)).fetchall()
        nxt = top + 1
        for r in sorted(rows):
            if 'tx:' + r[1] not in done:
                nxt = r[0]
                break
        rows = [r[1:] for r in rows]
        tx = tx_records([r for r in rows if tx_ready(r, done, now)])
        meta['pipe_from'] = str(nxt)
        here = {r[8] for r in rows if r[8]}            # digests of the rows read this run (from row 0: every digest there is)

        # checks: the lines still waiting, then the new whole lines (each digest's first / last line kept). A line is a record
        # when its digest has no pipeline row and its key is not saved; a digest outside `here` is looked up (rare)
        lines, off, _ = read_from(CHECKS, int(meta.get('checks_off') or 0)) if fh else ([], 0, b'')
        meta.update(checks_off=str(off), checks_id=chk_id)
        cands, ask, touched = [], [], set()

        def judge(pos, r):
            if not r or r.get('digest') in here or check_key(r) in done:
                db.execute('DELETE FROM cpend WHERE pos = ?', (pos,))
            elif r0 and r.get('digest'):
                ask.append((pos, r))
            else:
                cands.append((pos, r))
        for (pos,) in db.execute('SELECT pos FROM cpend ORDER BY pos').fetchall():
            judge(pos, line_at(fh, pos))
        for pos, ln in lines:
            r = j(ln)
            if r and r.get('digest'):
                dg = r['digest']
                if not isinstance(dg, str):
                    raise ValueError('a check line whose digest is not text')   # the full read decides what happens then
                db.execute('INSERT OR IGNORE INTO first(digest, pos) VALUES(?, ?)', (dg, pos))
                db.execute('INSERT INTO lastl(digest, pos, n) VALUES(?, ?, 1) '
                           'ON CONFLICT(digest) DO UPDATE SET pos = excluded.pos, n = n + 1', (dg, pos))
                touched.add(dg)
            if r:
                judge(pos, r)
        found, want = set(), sorted({r['digest'] for _, r in ask})
        for i in range(0, len(want), 500):
            part = want[i:i + 500]
            found.update(x for (x,) in c.execute('SELECT digest FROM pipeline WHERE digest IN (%s)' % ','.join('?' * len(part)), part))
        checks = []
        for pos, r in sorted(cands + [(pos, r) for pos, r in ask if r['digest'] not in found], key=lambda x: x[0]):
            checks.append(check_record(r))
            db.execute('INSERT OR IGNORE INTO cpend(pos) VALUES(?)', (pos,))
        for pos, r in ask:
            if r['digest'] in found:
                db.execute('DELETE FROM cpend WHERE pos = ?', (pos,))

        # corrections: the digests with a new line, and those still waiting (one line only: never a correction)
        fix = []
        for dg in touched | {d for (d,) in db.execute('SELECT digest FROM rpend').fetchall()}:
            (fpos,) = db.execute('SELECT pos FROM first WHERE digest = ?', (dg,)).fetchone()
            lpos, n = db.execute('SELECT pos, n FROM lastl WHERE digest = ?', (dg,)).fetchone()
            rec = recheck_record(dg, line_at(fh, fpos), line_at(fh, lpos), n, done) if n >= 2 else None
            if rec:
                fix.append((fpos, rec))
                db.execute('INSERT OR IGNORE INTO rpend(digest) VALUES(?)', (dg,))
            else:
                db.execute('DELETE FROM rpend WHERE digest = ?', (dg,))
        fix = [rec for _, rec in sorted(fix, key=lambda x: x[0])]
    finally:
        c.close()
        if fh:
            fh.close()

    # terms: the agreements from the first one not saved yet
    terms = []
    if CUSTOMERS.exists():
        t = sqlite3.connect('file:%s?mode=ro' % CUSTOMERS, uri=True)
        try:
            t0 = int(meta.get('terms_from') or 0)
            top = t.execute('SELECT max(id) FROM agreements').fetchone()[0] or 0
            t0 = 0 if top + 1 < t0 else t0
            rows = t.execute('SELECT id, address, version, ts, message, signature FROM agreements WHERE id >= ? ORDER BY id', (t0,)).fetchall()
        finally:
            t.close()
        nxt = top + 1
        for row in rows:
            if 'terms:%d' % row[0] not in done:
                nxt = row[0]
                break
        terms = [terms_record(row) for row in rows if 'terms:%d' % row[0] not in done]
        meta['terms_from'] = str(nxt)

    db.execute('DELETE FROM meta')
    db.executemany('INSERT INTO meta(k, v) VALUES(?, ?)', sorted(meta.items()))
    return rules_record(done) + terms + tx + checks + fix


def build_records(state=None):
    """Every record not saved yet: from the state file when one is given and usable, else the full read."""
    st = Path(state) if state else None
    if st and os.access(st if st.exists() else st.parent, os.W_OK):   # a state file the sandbox can't write: the full read, quietly
        try:
            return incremental_records(st)
        except Exception as exc:                       # never fewer records than the full read: fall back to it, and the
            try:                                       # state starts clean next run (an empty file is a new SQLite file)
                open(st, 'w').close()
            except OSError:
                pass
            try:
                with DROPPED.open('a') as f:
                    f.write('%s incremental export skipped (%s: %s), full read\n' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                                                                   type(exc).__name__, str(exc)[:120]))
            except OSError:
                pass
    done = {json.loads(x)['key'] for x in LEDGER.read_text().splitlines() if x.strip()} if LEDGER.exists() else set()
    tx, seen = pipeline_records(done)
    return rules_record(done) + terms_records(done) + tx + check_records(done, seen) + recheck_records(done)


# ledger keys already saved -> skip; build every kind of record; refuse to print a transaction record holding an address
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    recs = build_records(argv[argv.index('--state') + 1] if '--state' in argv[:-1] else None)
    # belt and braces: no wallet address in a transaction record. A bad record is dropped and named once in run.log,
    # never the whole batch: one visitor-set string used to stop every upload (review W2-3, 2026-11-02). Not stderr: the
    # sync script reads stdout+stderr as the JSON list.
    bad = [r for r in recs if not r['key'].startswith('rules:') and (re.search(r'0x[0-9a-fA-F]{4}', r['text'])
           or (r.get('digest') and r['digest'] in r['text']))]          # W-4 A+: no transaction id on Walrus either
    if bad:
        try:                                           # the note is best effort: it must never stop the good records
            old = DROPPED.read_text() if DROPPED.exists() else ''
            with DROPPED.open('a') as f:
                for r in bad:
                    if r['key'] not in old:
                        f.write('%s dropped (raw address): %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), r['key']))
        except OSError:
            pass
    print(json.dumps([r for r in recs if r not in bad], ensure_ascii=False))


if __name__ == '__main__':
    main()
