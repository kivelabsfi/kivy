"""Kivy's policy readers: the program allowlist and the call rules (site/kivy_pkgs.json), checked on every built transaction before its card goes out (pkg_watch, call_watch), plus the address helpers they use.

Moved verbatim out of site/server.py (architecture programme change 3, 2026-11-14); server.py imports these names back,
so callers and tests that use them through server.py are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
import json
import os
import re
import threading
import time
from pathlib import Path
from . import settings as SETTINGS
from .settings import DATA


KIVY_PKGS_FILE = Path(os.environ.get('KIVY_PKGS_FILE') or SETTINGS.SITE / 'kivy_pkgs.json')   # the Move packages a Kivy transaction may call (audit V01); the page enforces the same list


# The allowlisted Move package ids from kivy_pkgs.json, normalised; read on every build and check 2, so parsed only when the
# file changes (same idea as K(); a reviewer code-waste CW-8, 2026-11-03). A copy each call; unreadable file -> empty set, as before.
_PKC = {'k': None, 'v': set()}


def kivy_pkgs():
    try:
        st = KIVY_PKGS_FILE.stat()
        k = (str(KIVY_PKGS_FILE), st.st_mtime_ns, st.st_size)
        if _PKC['k'] != k:
            _PKC['v'], _PKC['k'] = {normaddr(p) for p in json.loads(KIVY_PKGS_FILE.read_text())['packages']}, k
        return set(_PKC['v'])
    except Exception:
        return set()


# Call rules (the owner "Go" 2026-11-13, after a defensive review found that check 1, pkg_watch and the builder looked only at
# WHICH PACKAGES a transaction calls): each kind of plan step may call only the functions it needs — a lend plan can't call
# deposit_ctokens_into_obligation_by_id (someone else's account) or repay, a swap plan can't call lending at all.
# One table: kivy_pkgs.json "calls" (its _about has the format). Check 1 (the page, #kivy-calls), call_watch below and the
# builder enforce it. Parsed only when the file changes; a copy each call; unreadable -> {} (every call outside the framework
# list is then refused: fail closed).
_CLC = {'k': None, 'v': {}}


def kivy_calls():
    try:
        st = KIVY_PKGS_FILE.stat()
        k = (str(KIVY_PKGS_FILE), st.st_mtime_ns, st.st_size)
        if _CLC['k'] != k:
            c = json.loads(KIVY_PKGS_FILE.read_text()).get('calls')
            _CLC['v'], _CLC['k'] = (c if isinstance(c, dict) and isinstance(c.get('steps'), dict) else {}), k
        return json.loads(json.dumps(_CLC['v']))
    except Exception:
        return {}


def call_refusals(calls, steps):
    """Why each call breaks the call rules for a plan with these steps ([] = none). calls: [(package, module, function)].
    A package off the allowlist is pkg_watch's business (refused there). Same logic as callWhy() in index.html and
    callWhy() in builder.ts."""
    R, allow = kivy_calls(), kivy_pkgs()
    groups = {normaddr(p): g for g, ps in (R.get('groups') or {}).items() for p in (ps or [])}
    rules = list(R.get('fw') or [])
    for s in sorted(steps or ()):
        rules += (R.get('steps') or {}).get(s) or []
    try:
        never = re.compile(R['never'], re.I) if R.get('never') else None
    except (re.error, TypeError):
        never = re.compile('')                       # an unreadable rule refuses every call

    def part(want, got):
        return want == '*' or want == got or (want.endswith('*') and got.startswith(want[:-1]))

    def hit(r, pkg, mod, fn, grp):
        p = str(r).split('::')
        return len(p) == 3 and (normaddr(p[0]) == pkg if p[0].startswith('0x') else p[0] == grp) and part(p[1], mod) and part(p[2], fn)
    out = []
    for pkg, mod, fn in calls:
        pkg, mod, fn = normaddr(pkg), str(mod), str(fn)
        if pkg not in allow:
            continue
        # a listed program that is in NO group matches no group rule: refused in every plan (default deny, a reviewer scan 2026-11-15 —
        # it counted as a swap program, so a lending upgrade left out of groups.lend was open to every swap plan)
        grp = 'fw' if pkg in (normaddr('0x1'), normaddr('0x2')) else groups.get(pkg)
        if never is not None and never.search(fn):
            out.append('%s::%s (never allowed)' % (mod, fn))
        elif not any(hit(r, pkg, mod, fn, grp) for r in rules):
            out.append('%s::%s (not part of this plan)' % (mod, fn))
    return out


def call_watch(path, tx, steps):
    """The call rules on a built transaction before its card goes out (Handler._send, swap_tx): the reasons it breaks them,
    [] = none. Fails closed like pkg_watch: a transaction that can't be read is refused. path: the route (for symmetry with
    pkg_watch; the refusal is printed by the caller as "kivy refused build: call rule …", which the watchdog alerts on)."""
    if tx is None:                                   # no transaction in this answer (a plain send: the page builds it)
        return []
    try:
        cmds = tx_commands(tx)
        if any(k != 'MoveCall' and k not in CMD_KINDS for k, _ in cmds):
            raise ValueError('a %s command' % next(k for k, _ in cmds if k not in CMD_KINDS))
        return call_refusals([(c.get('package'), c.get('module'), c.get('function')) for k, c in cmds if k == 'MoveCall'], steps)
    except Exception as exc:
        unreadable(path, exc)
        return ['unreadable transaction']


# The command kinds a Kivy transaction is made of. Anything else — Publish, Upgrade, a kind a later Sui version adds — is
# refused by both readers: they can only vouch for what they can read (review scan S3, 2026-11-15).
CMD_KINDS = ('MoveCall', 'SplitCoins', 'MergeCoins', 'TransferObjects', 'MakeMoveVec')


def tx_commands(tx):
    """[(kind, body)] for every command of a built transaction (its JSON text or dict), or ValueError when it is not the shape
    the readers know: no `commands` list, an empty one, a command that is not one {kind: body} entry, a call without its
    package / module / function. Fails closed — before 2026-11-15 a transaction with no `commands` read as "nothing wrong"."""
    t = json.loads(tx) if isinstance(tx, str) else tx
    if not isinstance(t, dict):
        raise ValueError('not a transaction')
    cmds = t.get('commands')
    if not isinstance(cmds, list):
        raise ValueError('no commands list')
    if not cmds:
        raise ValueError('no commands')
    out = []
    for c in cmds:
        if not isinstance(c, dict):
            raise ValueError('a command that is not an object')
        kinds = [k for k in c if k != '$kind']
        if len(kinds) != 1 or c.get('$kind', kinds[0]) != kinds[0]:
            raise ValueError('a command with no single kind')
        k, body = kinds[0], c[kinds[0]]
        if k == 'MoveCall' and not (isinstance(body, dict) and all(isinstance(body.get(x), str) and body.get(x) for x in ('package', 'module', 'function'))):
            raise ValueError('a call without package, module and function')
        out.append((k, body if isinstance(body, dict) else {}))
    return out


def unreadable(path, exc):
    """One journal line with the reason a transaction could not be read (the caller refuses it as 'unreadable transaction')."""
    print('kivy refused build: unreadable transaction on %s (%s)' % (path, str(exc)[:80] or type(exc).__name__), flush=True)


# '0x2' -> the full 64-hex form, so ids compare equal however they were written.
def type_key(t):
    """A coin type or address for comparing: leading zeros dropped, lower-cased ('0x000…02::sui::SUI' -> '0x2::sui::sui').
    One copy for what was written inline 9 times (review code-waste CW-1, 2026-11-03). Case-keeping and no-'0x' variants
    stay where they are on purpose."""
    return re.sub(r'^0x0*', '0x', str(t or '').lower())


def type_key_bare(x):
    """A coin type keyed on its package without '0x' or leading zeros, the module and name as written ('0x0…2::sui::SUI' -> '2::sui::SUI')."""
    return re.sub(r'^0x0*', '', str(x or '').split('::')[0].lower()) + '::' + '::'.join(str(x or '').split('::')[1:])


def normaddr(a):
    try:
        return '0x%064x' % int(str(a), 16)
    except Exception:
        return str(a).lower()


_PKG_SEEN, _PKG_SEEN_LOCK = {}, threading.Lock()


def pkg_watch(path, tx):
    """Every package outside the allowlist that a built transaction calls is logged AND refused, before the card goes out
    (review F-04). No exemption by module name: an unlisted 'router' step is refused too — a module name is chosen by
    whoever publishes it (review user-harm H1, 2026-10-29). New Aftermath adapters are vetted and listed (kivy-vet-package).
    Fails closed (OWASP A10-2, 2026-10-29): the refusal is decided before the log line is written, a log that can't be
    written (disk full, permissions) never cancels it, and a transaction that can't be read is refused."""
    bad, lines = [], []
    if tx is None:                                   # no transaction in this answer (a plain send: the page builds it)
        return bad
    try:
        raw = (json.loads(tx) if isinstance(tx, str) else tx)
        for c in (raw.get('commands') if isinstance(raw, dict) and isinstance(raw.get('commands'), list) else []):
            # never publish or upgrade a program in a user's transaction (audit nv #2) — named before the shape check below
            if isinstance(c, dict) and ({'Publish', 'Upgrade'} & set(c) or c.get('$kind') in ('Publish', 'Upgrade')):
                bad.append('it publishes or upgrades a program')
        cmds = [] if bad else tx_commands(tx)        # no commands, or a shape this reader does not know: refused (S3, 2026-11-15)
        allow = kivy_pkgs()
        for k, mc in cmds:
            if k not in CMD_KINDS:                   # a command kind no Kivy plan uses (new in a later Sui version?)
                raise ValueError('a %s command' % k)
            if k == 'MoveCall' and normaddr(mc.get('package')) not in allow:
                bad.append('%s…::%s' % (normaddr(mc.get('package'))[:10], mc.get('module')))
                lines.append('%s %s %s::%s::%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), path, normaddr(mc.get('package')), mc.get('module'), mc.get('function')))
    except Exception as exc:
        unreadable(path, exc)
        bad.append('unreadable transaction')         # can't read it = can't vouch for it
    pkg_log(lines)                                   # the same call again within 10 minutes: refused again, logged once
    return bad


# Both writers of data/unknown_pkgs.log (pkg_watch and the page's /api/pipeline report): the same call again within 10 minutes
# is logged once (review K-20), and the file rotates at 1 MB (review W3-7: the page's writer had no cap).
def pkg_log(lines):
    now = time.time()
    with _PKG_SEEN_LOCK:
        if len(_PKG_SEEN) > 5000:
            _PKG_SEEN.clear()
        fresh = [x for x in lines if now - _PKG_SEEN.get(x.split(' ', 3)[-1], 0) > 600]
        for x in fresh:
            _PKG_SEEN[x.split(' ', 3)[-1]] = now
    if fresh:
        try:                                         # the watchdog's "unlisted program" alert reads this file
            log = DATA / 'unknown_pkgs.log'
            if log.exists() and log.stat().st_size > 1_000_000:   # bounded: the watchdog restarts from 0 when the file shrinks
                os.replace(log, log.with_suffix('.log.1'))
            with open(log, 'a') as f:
                f.write(''.join(fresh))
        except Exception as exc:
            print('kivy pkg log not written: %s' % type(exc).__name__, flush=True)


# Called by remember_intent; the label ends up in the Kivy diary line and the Activity tab (GET /api/tx/labels).


# ── shared values + the rules version (architecture programme change 5, 2026-11-14) ──
def policy_consts(name):
    """One shared value from kivy_pkgs.json calls.consts — the ONLY copy of the router fee wallets, aggregator entry programs,
    perps programs and the 'pulls value' pattern, which check 1, this server and the builder all read. [] / '' when missing,
    which makes every reader stricter."""
    v = (kivy_calls().get('consts') or {}).get(name)
    if v is None:
        return '' if name == 'pulls_value' else []
    return list(v) if isinstance(v, list) else v


_PV = {'k': None, 'v': ''}


def rule_files():
    """Everything check 1 runs on, in a fixed order: the rules table (kivy_pkgs.json), check 1's own code (src/check1.js) and
    the checksums of the libraries it runs on (static/vendor/SHA256SUMS: the Sui SDK, the wallet library)."""
    return [KIVY_PKGS_FILE, SETTINGS.SITE / 'src' / 'check1.js', SETTINGS.SITE / 'static' / 'vendor' / 'SHA256SUMS']


def policy_version():
    """The rules version: the first 12 hex of a SHA-256 over every file in rule_files(). Every guard carries it and the page
    refuses a guard judged by other rules than the ones it loaded (a page left open after a release); check records keep it.
    Until 2026-11-15 it covered the rules table only, so a tab left open kept its OLD check 1 code after a fix to it (review
    scan, page finding 10). A file that can't be read gives '' — the page refuses a guard without a version (fail closed)."""
    try:
        fs = rule_files()
        k = tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in fs)
        if _PV['k'] != k:
            import hashlib
            h = hashlib.sha256()
            for f in fs:
                b = f.read_bytes()
                h.update(b'%d\n' % len(b))            # each file's length first: two files can't run together
                h.update(b)
            _PV['k'], _PV['v'] = k, h.hexdigest()[:12]
    except OSError:
        _PV['k'], _PV['v'] = None, ''
    return _PV['v']
