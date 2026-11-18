"""AI budget and abuse limits: per minute, day, visitor and real IP address; the AI day count kept across restarts (ai_day.json).

Moved verbatim out of site/server.py (architecture programme change 3, 2026-11-14); server.py imports these names back,
so callers and tests that use them through server.py are unchanged.
The per-address limits (ip_quota) and the AI answer gate (ai_allow) count in SQLite when the running service turned the store
on (kivy/limitstore.py, limits.db, 2026-11-14): they survive a restart and two workers could share them. Off (tests, tools) or
failing, the in-memory code below counts exactly as before."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
import calendar
import json
import os
import re
import threading
import time
from pathlib import Path
from .settings import DATA
from . import limitstore as STORE

BRAIN = None                                         # set by server.py at start-up


# ── Section 2: AI budget + abuse limits — per minute / day / visitor / real IP; used by _ai_reply and Handler._heavy ──
_RATE = []                         # timestamps of AI calls, a simple per-minute budget
AI_PER_MIN = int(os.environ.get('KIVY_AI_PER_MIN', 60))          # everyone together (was 12: a busy minute sent every user the help menu, 2026-10-23)
AI_PER_VISITOR_MIN = int(os.environ.get('KIVY_AI_PER_VISITOR_MIN', 10))   # one visitor can't use up everyone's share
AI_PER_DAY = int(os.environ.get('KIVY_AI_PER_DAY', 3000))            # hard ceiling, all models: Haiku has its own 600/day
                                                                     # (brain.CLAUDE_PER_DAY, ~$1), DeepSeek ~$0.0005 an answer after it
# Abuse limits by the REAL internet address (Cloudflare's CF-Connecting-IP; the visitor id comes from the browser and a script can
# rotate it). Bad-actor review 2026-10-24: one script could spend the whole day's AI in ~50 minutes and leave everyone "busy".
AI_PER_IP_MIN = int(os.environ.get('KIVY_AI_PER_IP_MIN', 12))
# AI answers at once (review scaling study 2026-11-10, 2026-11-11 "go"): they run inside the 16 chat slots, so a slow AI
# provider may hold at most half of them and code answers stay instant. Full: wait AI_SLOT_WAIT s, then AI_BUSY.
AI_SLOTS = int(os.environ.get('KIVY_AI_SLOTS', 8))
AI_SLOT_WAIT = 2
_AI_SLOTS = threading.BoundedSemaphore(AI_SLOTS)
AI_PER_IP_DAY = int(os.environ.get('KIVY_AI_PER_IP_DAY', 250))      # generous for a shared office/mobile address, small next to 3,000
HEAVY_PER_IP_MIN = int(os.environ.get('KIVY_HEAVY_PER_IP_MIN', 240))   # cards, quotes, builds, chat: per address per minute
AUTH_PER_IP_MIN = int(os.environ.get('KIVY_AUTH_PER_IP_MIN', 20))     # sign-in (challenge + signed reply) per address per minute (W-6)
AUTH_PATHS = {'/api/auth/challenge', '/api/auth/wallet'}
WALLETS_PER_IP_HOUR = int(os.environ.get('KIVY_WALLETS_PER_IP_HOUR', 40))   # different wallets one address may look up per hour
# Transaction builds per address (QA 2026-11-17 SRV-V5, fix package 5): a build costs ~15 chain calls, and 240 a minute from one
# address used up the chain plan in hours. A person builds a few a minute at most; a shared office or phone network still fits.
BUILDS_PER_IP_MIN = int(os.environ.get('KIVY_BUILDS_PER_IP_MIN', 30))
BUILDS_PER_IP_DAY = int(os.environ.get('KIVY_BUILDS_PER_IP_DAY', 600))
_IPQ = {'ai_min': {}, 'ai_day': {}, 'heavy': {}, 'wal': {}, 'day': ''}
_IPQ_LOCK = threading.Lock()


_LIMIT_SEEN = {}


def limit_hit(kind, ip):
    """One log line per address and kind every 10 minutes when a limit stops someone — watchdog alerts on these."""
    k, now = (kind, ip), time.time()
    if now - _LIMIT_SEEN.get(k, 0) > 600:
        _LIMIT_SEEN[k] = now
        if len(_LIMIT_SEEN) > 20000:
            _LIMIT_SEEN.clear()
        print('kivy limit: %s stopped for %s' % (kind, re.sub(r'(\d+)\.(\d+)$', 'x.x', str(ip))), flush=True)   # the address shortened


# The limit check every route uses (Handler._heavy, /api/chat, /api/tx/log, /api/feedback, _ai_reply); logs when it stops someone.
def ip_quota(kind, ip, key=None):
    ok = _ip_quota(kind, ip, key)
    if not ok:
        limit_hit(kind, ip)
    return ok


def _ip_quota(kind, ip, key=None):
    """True when this real IP may do one more `kind` ('ai', 'heavy', 'wallet' with the address as key); records it."""
    if not ip:
        return True                                  # local scripts and tests (no Cloudflare header)
    ok = _store_quota(kind, ip, key) if STORE.ON else None
    if ok is not None:                               # counted in limits.db (the service); None = store off or failing: memory below
        return ok
    now = time.time()
    with _IPQ_LOCK:
        today = time.strftime('%Y-%m-%d', time.gmtime(now))
        if _IPQ['day'] != today:
            _IPQ.update(ai_day={}, day=today)
        for d in ('ai_min', 'ai_day', 'heavy', 'wal'):
            if len(_IPQ[d]) > 50000:                  # drop expired counters first; a wholesale clear reset everyone's limits (review K-20)
                win = 3600 if d == 'wal' else 60
                if d != 'ai_day':
                    for k in [k for k, v in _IPQ[d].items() if not any(now - t < win for t in (v.values() if isinstance(v, dict) else v))]:
                        _IPQ[d].pop(k, None)
                if len(_IPQ[d]) > 200000:
                    _IPQ[d].clear()
        if kind == 'ai':
            m = _IPQ['ai_min'].setdefault(ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= AI_PER_IP_MIN or _IPQ['ai_day'].get(ip, 0) >= AI_PER_IP_DAY:
                return False
            m.append(now); _IPQ['ai_day'][ip] = _IPQ['ai_day'].get(ip, 0) + 1
            return True
        if kind == 'heavy':
            m = _IPQ['heavy'].setdefault(ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= HEAVY_PER_IP_MIN:
                return False
            m.append(now)
            return True
        if kind == 'auth':                            # sign-in: its own count, so it neither spends nor rides the 240/min reads (W-6)
            m = _IPQ['heavy'].setdefault('auth:' + ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= AUTH_PER_IP_MIN:
                return False
            m.append(now)
            return True
        if kind == 'fb':                              # feedback: 20 a minute per address (rotating visitor ids can't flood the log)
            m = _IPQ['heavy'].setdefault('fb:' + ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= 20:
                return False
            m.append(now)
            return True
        if kind == 'm':                               # anonymous numbers (/api/m): 60 a minute per address (review K-12, 2026-10-30)
            m = _IPQ['heavy'].setdefault('m:' + ip, [])
            m[:] = [t for t in m if now - t < 60]
            if len(m) >= 60:
                return False
            m.append(now)
            return True
        if kind == 'build':                           # transaction builds: per minute and per day (SRV-V5, package 5)
            m = _IPQ['heavy'].setdefault('build:' + ip, [])
            m[:] = [t for t in m if now - t < 86400]
            if len(m) >= BUILDS_PER_IP_DAY or sum(1 for t in m if now - t < 60) >= BUILDS_PER_IP_MIN:
                return False
            m.append(now)
            return True
        if kind == 'wallet' and key:
            w = _IPQ['wal'].setdefault(ip, {})
            for a in [a for a, t in w.items() if now - t > 3600]:
                w.pop(a, None)
            if key.lower() not in w and len(w) >= WALLETS_PER_IP_HOUR:
                return False
            w[key.lower()] = now
            return True
    return True


_WALLET_RE = re.compile(r'^0x[0-9a-fA-F]{64}$')


def query_wallets(path, url):
    """The wallets a GET looks up: its ?address=, and the Send card's ?q= when it is an address (2 chain reads each, ATK-S2)."""
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(url).query)
    out = [(qs.get('address') or [''])[0]]
    if path == '/api/recipient':
        out.append((qs.get('q') or [''])[0].strip())
    return [a for a in out if _WALLET_RE.match(a)]


def build_gate(ip, raw):
    """A POST build from a real address (SRV-V5, fix package 5): the wallet in its body counts toward the different-wallets-an-hour
    rule like a GET's ?address=, and the build toward BUILDS_PER_IP_MIN / _DAY. Returns the refusal text, or None."""
    if not ip:
        return None
    try:
        a = str((json.loads(raw or b'{}') or {}).get('address') or '')
    except (ValueError, TypeError, AttributeError):
        a = ''
    if _WALLET_RE.match(a) and not ip_quota('wallet', ip, a):
        return 'Too many different wallets from here — try again later.'
    if not ip_quota('build', ip):
        return 'Too many transactions built from here — wait a minute and try again.'
    return None


def _day0(now):
    return calendar.timegm(time.gmtime(now)[:3] + (0, 0, 0))   # this UTC day's midnight (the day counts reset there)


def _store_quota(kind, ip, key):
    """_ip_quota's same limits and windows, counted in limits.db; None when the store is off or failing (or kind has no limit)."""
    now = time.time()
    per_min = {'heavy': HEAVY_PER_IP_MIN, 'auth': AUTH_PER_IP_MIN, 'fb': 20, 'm': 60}.get(kind)
    if per_min is not None:
        return STORE.count_add(kind, ip, per_min, 60, now)
    if kind == 'ai':
        return STORE.ai_ip_add(ip, AI_PER_IP_MIN, AI_PER_IP_DAY, now, _day0(now))
    if kind == 'wallet' and key:
        return STORE.seen_add(ip, key.lower(), WALLETS_PER_IP_HOUR, 3600, now)
    if kind == 'build':
        ok = STORE.count_add('build', ip, BUILDS_PER_IP_MIN, 60, now)
        if ok is None or not ok:
            return ok
        return STORE.count_add('build_day', ip, BUILDS_PER_IP_DAY, 86400, now)
    return None


_RATE_V, _RATE_DAY = {}, {'day': '', 'n': 0}
# AI answers today, kept across restarts (review scaling study 2 #3, 2026-11-11): every restart set the all-model count and
# Haiku's count (brain._CLAUDE_DAY) back to 0, so a day with restarts could pass AI_PER_DAY and CLAUDE_PER_DAY. Totals only, no
# addresses. Only the running service writes it (AI_DAY_SAVE is set in the start-up block), so tests and tools that import
# this file never change it; a file from another UTC day is ignored.
AI_DAY_FILE = Path(os.environ.get('KIVY_AI_DAY_FILE') or DATA / 'ai_day.json')
AI_DAY_SAVE = False
_AI_DAY_LOCK = threading.Lock()


def ai_day_save():
    if not AI_DAY_SAVE:
        return
    cd = getattr(BRAIN, '_CLAUDE_DAY', None) or {}
    d = {'day': _RATE_DAY.get('day', ''), 'all': int(_RATE_DAY.get('n', 0)), 'haiku_day': cd.get('day', ''), 'haiku': int(cd.get('n', 0))}
    try:
        with _AI_DAY_LOCK:
            tmp = AI_DAY_FILE.with_name(AI_DAY_FILE.name + '.tmp')
            tmp.write_text(json.dumps(d))
            os.replace(tmp, AI_DAY_FILE)
    except OSError as exc:
        print('kivy ai_day: not saved (%s)' % type(exc).__name__, flush=True)


def ai_day_load():
    """Today's AI counts back after a restart; never lowers a count already higher in memory."""
    try:
        d = json.loads(AI_DAY_FILE.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(d, dict):
        return
    today = time.strftime('%Y-%m-%d', time.gmtime())
    num = lambda v: max(0, min(int(v or 0), 10 ** 7)) if isinstance(v, (int, float, str)) and str(v).isdigit() else 0
    if d.get('day') == today:
        _RATE_DAY['n'] = max(_RATE_DAY['n'] if _RATE_DAY.get('day') == today else 0, num(d.get('all')))
        _RATE_DAY['day'] = today
    cd = getattr(BRAIN, '_CLAUDE_DAY', None)
    if isinstance(cd, dict) and d.get('haiku_day') == today:
        cd['n'] = max(cd.get('n', 0) if cd.get('day') == today else 0, num(d.get('haiku')))
        cd['day'] = today


# The AI answer gate (server.py's _ai_reply calls it; until the readers move its old in-memory block still runs there):
# everyone together AI_PER_MIN a minute, one browser id AI_PER_VISITOR_MIN a minute, AI_PER_DAY a UTC day for all models (the
# day count stays in _RATE_DAY + ai_day.json), and this address ip_quota('ai'). True = answer and count it; False = AI_BUSY.
def ai_allow(vid, ip):
    now = time.time()
    today = time.strftime('%Y-%m-%d', time.gmtime(now))
    if _RATE_DAY['day'] != today:
        _RATE_DAY.update(day=today, n=0)
    if _RATE_DAY['n'] >= AI_PER_DAY:
        return False
    got = STORE.ai_gate(vid, ip, AI_PER_MIN, AI_PER_VISITOR_MIN, AI_PER_IP_MIN, AI_PER_IP_DAY, now, _day0(now)) if STORE.ON else None
    if got is None:                                  # store off or failing: the in-memory block, as it ran in _ai_reply
        _RATE[:] = [t for t in _RATE if now - t < 60]
        mine = _RATE_V.setdefault(vid, [])
        mine[:] = [t for t in mine if now - t < 60]
        if len(_RATE) >= AI_PER_MIN or (vid and len(mine) >= AI_PER_VISITOR_MIN) or not ip_quota('ai', ip):
            return False
        _RATE.append(now)
        if vid:
            mine.append(now)
        if len(_RATE_V) > 5000:                      # drop idle visitors only (was: clear all; a reviewer 2026-11-04 W-8)
            for v_, ts in list(_RATE_V.items()):
                if not ts or now - ts[-1] >= 60:
                    _RATE_V.pop(v_, None)
    elif got != 'ok':
        if got == 'ip':
            limit_hit('ai', ip)                      # the same "kivy limit:" line ip_quota prints (watchdog)
        return False
    _RATE_DAY['n'] += 1
    ai_day_save()                                    # kept across restarts
    return True


AI_BUSY = "I'm getting a lot of questions right now — ask me again in a moment."
