"""Plan-first, step 1: SHADOW (architecture programme change 9, 2026-11-14). Today "what a card promised" is derived AFTER the
build, several times, partly from the builder's own answer (plan_steps, intent_actions, the guards) — the thing being judged
helps write the standard it is judged by. make_plan() writes the plan's SHAPE once, from the user's request alone, BEFORE the
build: its step kinds and the ordered services (withdraw, swap, lend, send, perps …). shadow() compares it with today's
derivation after every build and counts the differences per card kind (DATA/plan_shadow.json, "kivy plan shadow:" lines).
Nothing reads make_plan yet. When a card kind shows zero differences for a week, its views (steps, fee count, check 2
expectations) move onto the plan, one kind at a time, each with the owner's OK and a dry + small live round (tracker, phase 4).
"""
# How it runs: imported by site/server.py; Handler._send calls shadow() next to the old derivation (never changes the answer).
import json
import threading
import time

from . import settings

_LOCK = threading.Lock()
_SEEN = {}
SAVE = False                                         # the service only (server.py start-up), like ai_day.json
AMOUNTLESS = {'dust', 'perp'}                         # need the builder's quote or the order book: shape only partly known up front


def make_plan(path, req):
    """{'kind', 'steps': sorted step kinds, 'services': ordered services, 'known': bool} from the request alone."""
    k = path.rsplit('/', 1)[-1]
    req = req if isinstance(req, dict) else {}
    steps, services = set(), []
    if k == 'swap':
        steps, services = {'swap'}, ['swap']
    elif k == 'dust':
        steps, services = {'swap'}, []                 # one swap per dust coin: how many is the builder's answer
    elif k == 'check-send':
        items = req.get('items') if isinstance(req.get('items'), list) else [req]
        services = ['send'] * len(items)
    elif k == 'sendmany':
        lines = [x for x in req.get('lines') or [] if isinstance(x, dict)] if isinstance(req.get('lines'), list) else []
        for x in lines:
            if str(x.get('source') or 'wallet') != 'wallet':
                steps.add('withdraw')
                services.append('withdraw')
            services.append('send')
    elif k == 'lend':
        svc = {'deposit': 'lend', 'withdraw': 'withdraw', 'claim': 'claim'}.get(str(req.get('action') or 'deposit'))
        if svc:
            steps, services = {svc}, [svc]
    elif k == 'swaplend':
        steps, services = {'swap', 'lend'}, ['swap', 'lend']
    elif k == 'withdrawswap':
        steps, services = {'withdraw'}, ['withdraw']
        if req.get('to') and str(req.get('to')).lower() != str(req.get('coin') or 'USDC').lower():
            steps.add('swap')
            services.append('swap')
        if req.get('lend_to'):
            steps.add('lend')
            services.append('lend')
    elif k == 'compose':
        spec = req.get('spec') if isinstance(req.get('spec'), dict) else {}
        src = spec.get('source') if isinstance(spec.get('source'), dict) else {}
        sinks = [x for x in spec.get('sinks') or [] if isinstance(x, dict)] if isinstance(spec.get('sinks'), list) else []
        op = src.get('op')
        if op == 'withdraw':                           # one withdraw per market: the first + source.more (builder buildCompose)
            steps.add('withdraw')
            more = src.get('more') if isinstance(src.get('more'), list) else []
            services += ['withdraw'] * (1 + len(more[:4]))
        elif op == 'perps_out':
            steps.add('perps')
            services.append('perps_withdraw')
        elif op == 'dust':
            steps.add('swap')
        for _ in (spec.get('swaps') or []) if isinstance(spec.get('swaps'), list) else []:
            steps.add('swap')
            services.append('swap')
        start = str(src.get('coin') or '').lower()
        # a piece in its own coin is swapped first (all swaps come before the sinks). The piece's coin is 'coin' on a send (its
        # 'to' is the address) and 'to' on a lend / keep / perps part — as chains.py writes it and builder/compose.ts (fanCoin)
        # reads it. Only 'coin' was looked at, so every lent or kept part missed its swap (review scan F16, 2026-11-15).
        part_coin = lambda x: x.get('coin') if x.get('op') == 'send' else (x.get('to') if not str(x.get('to') or '').lower().startswith('0x') else None)
        for x in sinks:
            if part_coin(x) and str(part_coin(x)).lower() != start and not (spec.get('swaps') or []):
                services.append('swap')
        for x in sinks:
            if x.get('op') in ('lend', 'send', 'perps'):
                services.append({'lend': 'lend', 'send': 'send', 'perps': 'perps_deposit'}[x['op']])
                if x['op'] != 'send':
                    steps.add('lend' if x['op'] == 'lend' else 'perps')
            if (x.get('to') and not str(x.get('to')).lower().startswith('0x')) or x.get('coin'):
                steps.add('swap')
        if op == 'dust':
            services = []                              # how many dust coins is the builder's answer
    elif k == 'perp':
        a = str(req.get('action') or '')
        steps = {'perps'}
        services = [{'order': 'perps_open', 'close': 'perps_close', 'open': 'perps_account'}.get(a, 'perps_' + a)] if a else []
    known = k not in AMOUNTLESS and not (k == 'compose' and ((req.get('spec') or {}) if isinstance(req.get('spec'), dict) else {}).get('source', {}).get('op') == 'dust')
    return {'kind': k, 'steps': sorted(steps), 'services': services, 'known': known}


def shadow(path, req, old_steps, old_actions, log=print):
    """Compare the request-only plan with today's derivation; count per kind; one log line per kind and difference every hour."""
    try:
        p = make_plan(path, req)
        old_s = sorted(old_steps or [])
        old_v = [a.get('service') for a in old_actions or [] if isinstance(a, dict)]
        diffs = []
        if p['steps'] != old_s:
            diffs.append('steps %s vs %s' % (','.join(p['steps']) or '-', ','.join(old_s) or '-'))
        if p['known'] and p['services'] != old_v:
            diffs.append('services %s vs %s' % ('>'.join(p['services']) or '-', '>'.join(old_v) or '-'))
        _count(p['kind'], diffs)
        if diffs:
            key = (p['kind'], tuple(diffs))
            if time.time() - _SEEN.get(key, 0) > 3600:
                _SEEN[key] = time.time()
                log('kivy plan shadow: %s differs — %s' % (p['kind'], '; '.join(diffs))[:400], flush=True)
        return diffs
    except Exception as exc:
        log('kivy plan shadow: error %s' % type(exc).__name__, flush=True)
        return None


def _count(kind, diffs):
    if not SAVE:
        return
    f = settings.DATA / 'plan_shadow.json'
    day = time.strftime('%Y-%m-%d', time.gmtime())
    with _LOCK:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            d = {}
        k = d.setdefault(day, {}).setdefault(kind, {'n': 0, 'diff': 0})
        k['n'] += 1
        k['diff'] += bool(diffs)
        for old in sorted(d)[:-30]:
            d.pop(old, None)
        try:
            tmp = f.with_name(f.name + '.tmp')
            tmp.write_text(json.dumps(d))
            tmp.replace(f)
        except OSError:
            pass
