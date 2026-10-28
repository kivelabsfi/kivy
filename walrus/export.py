#!/usr/bin/env python3
"""Kivy security records -> plain-text records for Walrus Memory (2026-10-24).

One record per transaction Kivy built (metrics.db pipeline: plan, check 1, sign, chain, check 2), one per after-signing
check with no pipeline row (data/checks.jsonl, older ones), and one for the rule set (kivy_pkgs.json allowlist).
Each wallet has its own space on Walrus (namespace wallet-<hash>, hash = Kivy's salted metrics.wh); the rule set is in
kivy-rules. No raw address leaves the box; old records without a wallet get it from the chain (senders.py). Records already saved
(ledger.jsonl) are skipped, so this is safe to run again. A transaction waits for its check 2 (at most 2 hours).
Runs on its own: cron every minute (run.sh; nothing is sent when nothing is new), so a new transaction is on Walrus ~1 minute after check 2.
  python3 export.py            # print the new records as JSON (upload.mjs reads this)
"""
import datetime as dt
import hashlib
import json
import re
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


def j(v):
    try:
        return json.loads(v) if v else None
    except Exception:
        return None


def when(ts):
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def verdict(c):
    if not c:
        return 'not recorded'
    notes = ' (%s)' % '; '.join(c['notes']) if c.get('notes') else ''
    if c.get('ok') is None:
        return 'not run' + notes
    if c.get('ok'):
        return 'ok' + notes
    return 'FAILED: ' + '; '.join(c.get('problems') or [c.get('why') or 'no reason recorded']) + notes


def who(w):
    """'you' and 'Kivy' stay; any other wallet (shown shortened in the logs) becomes 'another wallet'."""
    return w if w in ('you', 'Kivy') else 'another wallet'


def scrub(t):
    """Recipients in plan steps (0x addresses, shortened or full, and .sui names) become 'another wallet'."""
    return re.sub(r'0x[0-9a-fA-F]+(?:\u2026|…|\.\.\.)?[0-9a-fA-F]*|\b[\w-]+\.sui\b', 'another wallet', t)


def moves(m):
    return ', '.join('%s %s %s' % (who(x.get('who')), x.get('amt'), x.get('coin')) for x in (m or [])) or 'none'


def space(wh):
    return 'wallet-' + wh if wh else 'wallet-unknown'


def pipeline_records(done, chain_lookup=True):
    c = sqlite3.connect('file:/opt/kivy/data/metrics.db?mode=ro', uri=True)
    rows = c.execute('SELECT iid, ts, wh, kind, plan, sim, check1, sign, digest, chain, check2 FROM pipeline ORDER BY ts').fetchall()
    ready = [r for r in rows if 'tx:' + r[0] not in done and (r[10] or time.time() - float(r[1]) >= 2 * 3600)]  # wait for check 2 (max 2 h)
    found = senders.fill([r[8] for r in ready if not r[2]]) if chain_lookup and any(not r[2] for r in ready) else {}
    out = []
    for iid, ts, wh, kind, plan, sim, c1, sign, digest, chain, c2 in ready:
        key = 'tx:' + iid
        wh = wh or found.get(digest) or ''
        plan, sim, c1, sign, chain, c2 = j(plan), j(sim), j(c1), j(sign), j(chain), j(c2)
        s = sign or {}
        text = ('Kivy security record. Transaction check, %s. Type: %s. Wallet hash: %s. Plan: %s. Kivy fee: %s SUI. '
                'Simulated: %s. Check 1 (before signing): %s. Sign: %s%s. Transaction: %s. On chain: %s, moves %s. '
                'Check 2 (after signing, against the plan): %s.'
                % (when(ts), kind or '?', wh or 'none', '; '.join(scrub(x) for x in (plan or {}).get('steps') or []) or 'not recorded',
                   (plan or {}).get('fee_sui', 0), moves(sim), verdict(c1), s.get('result') or 'not recorded',
                   (' (%s, %.1f s)' % (s.get('wallet'), (s.get('ms') or 0) / 1000)) if s.get('wallet') else '',
                   digest or 'none', (chain or {}).get('status') or 'not recorded', moves((chain or {}).get('moves')), verdict(c2)))
        out.append({'key': key, 'digest': digest, 'ns': space(wh), 'text': text})
    return out, {r[8] for r in rows if r[8]}


def check_records(done, seen):
    out = []
    p = Path('/opt/kivy/data/checks.jsonl')
    for ln in p.read_text().splitlines() if p.exists() else []:
        r = j(ln)
        if not r or r.get('digest') in seen:
            continue
        key = 'check:%s:%s' % (r.get('digest'), r.get('at'))
        if key in done:
            continue
        text = ('Kivy security record. After-signing check, %s. Type: %s. Wallet hash: %s. Transaction: %s. On chain: %s. '
                'Check 2: %s.%s'
                % (str(r.get('at')).replace('T', ' ').replace('Z', ' UTC'), r.get('kind') or 'unknown',
                   metrics.wh(r['address']) if r.get('address') else 'none', r.get('digest'), r.get('status') or 'not recorded',
                   verdict(r), (' Notes: ' + '; '.join(r['notes']) + '.') if r.get('notes') else ''))
        out.append({'key': key, 'digest': r.get('digest'), 'ns': space(metrics.wh(r['address']) if r.get('address') else ''), 'text': text})
    return out


def recheck_records(done):
    """A transaction re-checked later with a different verdict gets a CORRECTION record next to the original (2026-10-26:
    a check-2 false alarm was fixed after its record was already on Walrus, which cannot edit or delete). The original stays:
    the history shows both, and why it changed."""
    p = Path('/opt/kivy/data/checks.jsonl')
    by = {}
    for ln in p.read_text().splitlines() if p.exists() else []:
        r = j(ln)
        if r and r.get('digest'):
            by.setdefault(r['digest'], []).append(r)
    out = []
    for dg, rs in by.items():
        first, last = rs[0], rs[-1]
        if len(rs) < 2 or verdict(first) == verdict(last):
            continue
        key = 'recheck:%s:%s' % (dg, last.get('at'))
        if key in done:
            continue
        wh = metrics.wh(last['address']) if last.get('address') else ''
        text = ('Kivy security record. CORRECTION, %s. Transaction %s (wallet hash %s) was checked again after signing. '
                'Earlier verdict (%s): %s. New verdict: %s. The earlier record stays on Walrus unchanged; this one supersedes its '
                'check 2 verdict.' % (str(last.get('at')).replace('T', ' ').replace('Z', ' UTC'), dg, wh or 'none',
                                      str(first.get('at')).replace('T', ' ').replace('Z', ' UTC'), verdict(first), verdict(last)))
        out.append({'key': key, 'digest': dg, 'ns': space(wh), 'text': text})
    return out


def terms_records(done):
    """Each signed Terms agreement (data/customers.db), in the wallet's space. These carry the address and the signature on
    purpose (2026-10-24: tamper-proof evidence of who agreed to what, and when)."""
    p = CUSTOMERS
    if not p.exists():
        return []
    c = sqlite3.connect('file:%s?mode=ro' % p, uri=True)
    out = []
    for i, a, v, ts, msg, sig in c.execute('SELECT id, address, version, ts, message, signature FROM agreements ORDER BY id'):
        key = 'terms:%d' % i
        if key in done:
            continue
        text = ('Kivy Terms agreement, %s. Wallet %s (hash %s) agreed to Kivy\'s Terms of Service and Privacy Policy, version %s, '
                'by signing this message with its wallet: %s Signature: %s'
                % (when(ts), a, metrics.wh(a), v, ' / '.join(x for x in (msg or '').splitlines() if x.strip()), sig))
        out.append({'key': key, 'digest': None, 'ns': space(metrics.wh(a)), 'text': text})
    return out


def rules_record(done):
    raw = Path('/opt/kivy/kivy_pkgs.json').read_bytes()
    h = hashlib.sha256(raw).hexdigest()
    if 'rules:' + h in done:
        return []
    d = json.loads(raw)
    names = d.get('names') or {}
    pk = ', '.join('%s (%s)' % (p, names.get(p, '?')) for p in d.get('packages') or [])
    text = ('Kivy security rule set, version sha256 %s, saved %s. Kivy refuses to sign any transaction that calls a Move '
            'package not on this list, except a DEX router adapter inside a swap, which is allowed and reported. '
            'Allowed packages (%d): %s.' % (h, dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d'), len(d.get('packages') or []), pk))
    return [{'key': 'rules:' + h, 'digest': None, 'ns': 'kivy-rules', 'text': text}]


def main():
    done = {json.loads(x)['key'] for x in LEDGER.read_text().splitlines() if x.strip()} if LEDGER.exists() else set()
    tx, seen = pipeline_records(done)
    recs = rules_record(done) + terms_records(done) + tx + check_records(done, seen) + recheck_records(done)
    for r in recs:                                     # belt and braces: no wallet address in a transaction record
        assert r['key'].startswith(('rules:', 'terms:')) or not re.search(r'0x[0-9a-fA-F]{4}', r['text']), 'raw address in ' + r['key']
    print(json.dumps(recs, ensure_ascii=False))


if __name__ == '__main__':
    main()
