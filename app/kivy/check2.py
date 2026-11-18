"""Plans and check 2: each card's plan is saved, then compared with what landed on chain (Section 11).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# remember_intent is fed by finish_build, verify_later by POST /api/tx/log and the start-up scan (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import calendar
import fcntl
import json
import math
import metrics
import re
import secrets
import sqlite3
import threading
import time
from . import ledger as LEDGER
from . import settings as SETTINGS
from .settings import DATA
from .config import K, disp, unknown_coin
from .patterns import LABEL_BAD_RE
from .policy import call_refusals, kivy_pkgs, normaddr, policy_consts, type_key, type_key_bare
from .upstream import lend_cli


KIVY_DIARY = SETTINGS.DIARY
_DIARY_SEEN = set()


# ── Section 11: plans and check 2 — every built transaction's plan is saved (IntentStore), then compared with the landed
#    transaction (check_tx via builder.ts 'tx-facts'). Fed by Handler._send (remember_intent) and POST /api/tx/log (verify_later) ──
# ── After-the-fact check: did the signed transaction do what Kivy's card promised? (2026-10-20) ──
# When Kivy builds a transaction the SERVER remembers its plan (the same limits the pre-sign check uses, plus the
# minimum the user should receive). After signing, the browser only hands back the digest and the plan's id; the
# server reads the real transaction from the chain and compares. Verdict -> Kivy's tx log (✅ / ⚠️) + data/checks.jsonl.
class IntentStore(dict):
    """Kivy's plans, also kept on disk (data/intents.db, 24 h): a restart or a flood that pushes a plan out of memory no longer
    leaves check 2 without it (red-team 2026-10-24). Memory keeps 2 h; a plan missing from memory is read back from disk.
    Changing a field of a stored plan needs .save(iid)."""
    DB = DATA / 'intents.db'

    # Loads the last 2 h of plans from data/intents.db (sqlite) at start-up.
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        try:
            with self._lock, sqlite3.connect(self.DB) as c:
                c.execute('CREATE TABLE IF NOT EXISTS intents(iid TEXT PRIMARY KEY, at REAL, data TEXT)')
                for iid, data in c.execute('SELECT iid, data FROM intents WHERE at > ?', (time.time() - 7200,)):
                    super().__setitem__(iid, json.loads(data))
        except Exception as exc:
            print('kivy intents: disk store unavailable (%s)' % exc, flush=True)

    def _db(self, sql, args=()):
        try:
            with self._lock, sqlite3.connect(self.DB, timeout=5) as c:
                return c.execute(sql, args).fetchall()
        except Exception as exc:
            print('kivy intents: %s' % str(exc)[:120], flush=True)
            return []

    def __setitem__(self, iid, it):
        super().__setitem__(iid, it)
        self.save(iid)

    def save(self, iid):
        it = dict.get(self, iid)
        if it is not None:
            self._db('INSERT OR REPLACE INTO intents(iid, at, data) VALUES(?,?,?)', (iid, float(it.get('at') or time.time()), json.dumps(it, default=str)))

    # Memory first; a plan older than 2 h (or lost in a restart) is read back from disk for up to 24 h.
    def get(self, iid, default=None):
        it = dict.get(self, iid)
        if it is None and re.match(r'^[0-9a-f]{16}$', str(iid or '')):
            rows = self._db('SELECT data FROM intents WHERE iid = ? AND at > ?', (iid, time.time() - 86400))
            if rows:
                it = json.loads(rows[0][0])
                super().__setitem__(iid, it)            # back in memory (not written again)
        return it if it is not None else default

    def prune_disk(self):
        self._db('DELETE FROM intents WHERE at < ?', (time.time() - 86400,))


_INTENTS = IntentStore()
CHECKS = DATA / 'checks.jsonl'
# check 2 on a wallet that never made a verified Kivy transaction, when it is not green: kept here, not in checks.jsonl, so a
# stranger's plan for someone else's wallet writes no diary line, label, greeting, Walrus record or per-tx alert; the watchdog
# sends one combined line, so a real new user's failed first transaction is still seen (review W3-2, 2026-11-03)
CHECKS_NEW = DATA / 'checks_newwallet.jsonl'


OTHER_SHARE_MAX = 0.01                                # others (pools, protocol fee wallets) may get at most 1% of what was paid
PAY_OVER_UNITS = 1                                    # a planned recipient may be over its maximum by one base unit (rounding), no more


def pay_caps(pays, norm, ntype):
    """{(address, coin type): the most the plan lets it receive, in base units} from the guard's payments (check_tx, S2).
    Each payment's `max`, added up per address and coin; None when any payment of that pair carries none (an older plan)."""
    caps = {}
    for pay in pays or []:
        key = (norm(pay.get('address')), ntype(pay.get('coin_type')))
        mx = pay.get('max')
        ok = str(mx).isdigit() if mx is not None else False
        caps[key] = None if (not ok or (key in caps and caps[key] is None)) else caps.get(key, 0) + int(mx)
    return caps


def fee_share_max():
    """The share of a coin's volume Kivy's treasury may receive as the % swap / lend fee (knowledge/kivy.json revenue): 0 while
    both are off (today), so the treasury then receives the flat fee and nothing else. 2% headroom on the share for rounding."""
    r = K().get('revenue') or {}
    try:
        pct = max(float(r.get('swap_fee_pct') or 0), 0) + max(float(r.get('lend_fee_pct') or 0), 0)
    except (TypeError, ValueError):
        return 0.0
    return min(pct, 2.0) / 100 * 1.02 if r.get('enabled') is True else 0.0


def send_most(p, cplan, fan, most, slip, dec, top=0.0):
    """The most one send of a /compose plan may deliver, in base units — the guard payment's `max`, check_tx's upper bound
    (review scan S2, 2026-11-15). Called by server.py /api/tx/compose for each send row `p` of the builder's plan `cplan`.
    most: the server's own figure for it (a fixed piece: its amount; "the rest": the start or the last swap's estimate less
    the fixed pieces); top: that start / estimate; slip: the slippage cap as a fraction; dec: the coin's decimals.
      · a piece cut to an exact amount arrives exactly: its amount (+2 units for float rounding);
      · a coin that arrives WHOLE — "the rest", or a split's swapped coin (its plan amount is the swap's minimum) — can land
        above its estimate when the price moves the user's way: the estimate plus twice the slippage cap, 2% at least.
    A split row whose swap can't be found raises: the build is then refused, never handed out with a guessed limit."""
    head = max(2 * float(slip or 0), 0.02)
    amt = float(most or 0)
    if p.get('rest'):
        return int((amt + max(float(top or 0), amt) * head) * 10 ** dec) + 2
    swaps = [s for s in cplan if s.get('op') == 'swap' and s.get('in') is not None]
    if fan and swaps and p.get('coin') != swaps[0].get('from'):   # a split's swapped coin: the i-th row of that coin <-> its i-th swap
        rows = [x for x in cplan if x.get('op') in ('lend', 'send', 'keep', 'perps') and x.get('coin') == p.get('coin')]
        legs = [s for s in swaps if s.get('to') == p.get('coin')]
        i = next(k for k, x in enumerate(rows) if x is p)
        return int(max(amt, float(legs[i]['expected'])) * (1 + head) * 10 ** dec) + 2
    return int(round(amt * 10 ** dec)) + 2


# Called by Handler._send (Kivy's fee applies only to 2+ actions) and remember_intent (metrics: the PTB's steps).
def intent_actions(path, req, resp):
    """The steps of a transaction Kivy built, for the numbers: [{service, coin, amount, usd}] (one PTB can hold several)."""
    def act(service, coin, amount):
        try:
            a = float(amount)
        except Exception:
            a = 0.0
        c = ckey(str(coin or '')) or str(coin or '')
        px = 1.0 if c in ('USDC', 'USDsui', 'USDT') else (coin_px(c) or 0)
        return {'service': service, 'coin': c, 'amount': a, 'usd': round(a * px, 6) if math.isfinite(a) else 0}
    k = path.rsplit('/', 1)[-1]
    try:
        if k == 'swap':
            return [act('swap', req.get('from'), req.get('amount'))]
        if k == 'dust':                               # one swap per dust coin (2026-10-25)
            return [act('swap', l.get('sym'), l.get('amount')) for l in (resp.get('legs') or [])]
        if k == 'check-send':
            items = req.get('items') or [{'coin': req.get('coin'), 'amount': req.get('amount')}]
            return [act('send', x.get('coin'), x.get('amount')) for x in items]
        if k == 'sendmany':                           # every withdrawal and every transfer in the multi-send
            return [act('withdraw' if p['op'] == 'withdraw' else 'send', p.get('coin'), p.get('amount')) for p in (resp.get('plan') or [])]
        if k == 'lend':
            a = str(req.get('action') or 'deposit')
            svc = {'deposit': 'lend', 'withdraw': 'withdraw', 'claim': 'claim'}.get(a, a)
            amt = resp.get('amount') if str(req.get('amount')) == 'all' else req.get('amount')
            if a == 'claim':                              # a claim's amount comes from the request, not the chain: never volume (audit nv #10)
                amt = 0
            return [act(svc, req.get('coin') or 'USDC', amt or 0)]
        if k == 'swaplend':
            return [act('swap', req.get('from'), req.get('amount')), act('lend', req.get('to'), resp.get('expected_out') or 0)]
        if k == 'withdrawswap':
            out = [act('withdraw', req.get('coin') or 'USDC', resp.get('amount') or req.get('amount') or 0)]
            if req.get('to') and str(req.get('to')).lower() != str(req.get('coin') or 'USDC').lower():
                out.append(act('swap', req.get('coin') or 'USDC', resp.get('amount') or 0))
            if resp.get('lend_to'):
                out.append(act('lend', req.get('to') or req.get('coin'), resp.get('expected_out') or resp.get('amount') or 0))
            return out
        if k == 'compose':                            # follow the coin through the plan: start -> swaps -> sinks (a 'rest' sink takes what's left)
            plan = resp.get('plan') or []
            out, cur, have = [], None, 0.0
            for p in plan:
                op = p.get('op')
                if op == 'dust':                       # the sweep at the start: one swap per dust coin, then the coin in hand
                    out += [act('swap', l.get('sym'), l.get('amount')) for l in (p.get('legs') or [])]
                    cur, have = p.get('coin'), float(p.get('min') or p.get('amount') or 0)
                    continue
                if op in ('wallet', 'withdraw', 'perps_out'):
                    more = op == 'withdraw' and out and out[-1]['service'] == 'withdraw' and cur == (p.get('coin') or 'USDC')
                    cur, have = p.get('coin') or 'USDC', (have if more else 0.0) + float(p.get('amount') or 0)   # several markets: they add up
                    if op != 'wallet':
                        out.append(act('withdraw' if op == 'withdraw' else 'perps_withdraw', cur, have))
                elif op == 'plus':
                    have += float(p.get('amount') or 0)
                elif op == 'swap' and p.get('in') is not None:   # one piece of a split: its own input; the start coin stays in hand
                    out.append(act('swap', cur, float(p['in'])))
                elif op == 'swap':
                    out.append(act('swap', cur, have))
                    cur, have = p.get('to') or cur, float(p.get('min') or 0)
            fixed = sum(float(p.get('amount') or 0) for p in plan if p.get('op') in ('lend', 'send', 'keep', 'perps') and p.get('amount') is not None)
            for p in plan:
                op = p.get('op')
                if op in ('lend', 'send', 'perps'):
                    amt = float(p['amount']) if p.get('amount') is not None else max(have - fixed, 0)
                    out.append(act({'lend': 'lend', 'send': 'send', 'perps': 'perps_deposit'}[op], p.get('coin') or cur, amt))
            return out
        if k == 'perp':
            a, o = str(req.get('action') or ''), req.get('order') or {}
            if a == 'order':
                return [{'service': 'perps_open', 'coin': str(o.get('symbol') or ''), 'amount': float(o.get('usd') or 0), 'usd': float(o.get('usd') or 0)}]
            if a == 'close':
                pv = resp.get('preview') or {}
                usd = float(pv.get('order_value') or 0) if isinstance(pv, dict) else 0.0
                return [{'service': 'perps_close', 'coin': str(o.get('symbol') or ''), 'amount': usd, 'usd': usd}]
            if a in ('deposit', 'withdraw', 'margin'):
                amt = req.get('amount') if a != 'margin' else o.get('amount')
                return [act('perps_' + a, 'USDC', resp.get('amount') or amt or 0)]
            if a in ('tpsl', 'cancel', 'open'):
                return [{'service': {'open': 'perps_account'}.get(a, 'perps_' + a), 'coin': str(o.get('symbol') or ''), 'amount': 0, 'usd': 0}]
    except Exception:
        pass
    return []


# Called by Handler._send for every built transaction (the guard's steps / swap / perps); tests: tests/test_callrules_20261113.py.
def plan_steps(path, req, resp):
    """The kinds of step a built plan has — 'swap', 'lend', 'withdraw', 'claim', 'perps' (a send or a keep needs none): what the
    call rules let its transaction call. Read from the request the handler checked, not from the builder's answer — except
    whether a withdraw-then-swap really swaps, which the builder's coin types also tell."""
    k = path.rsplit('/', 1)[-1]
    req = req if isinstance(req, dict) else {}
    resp = resp if isinstance(resp, dict) else {}
    if k in ('swap', 'dust'):
        return {'swap'}
    if k == 'perp':
        return {'perps'}
    if k == 'swaplend':
        return {'swap', 'lend'}
    if k == 'lend':
        s = {'deposit': 'lend', 'withdraw': 'withdraw', 'claim': 'claim'}.get(str(req.get('action') or ''))
        return {s} if s else set()
    if k == 'sendmany':                               # a line sent straight from a lending market withdraws it first
        lines = req.get('lines') if isinstance(req.get('lines'), list) else []
        return {'withdraw'} if any(isinstance(x, dict) and str(x.get('source') or 'wallet') != 'wallet' for x in lines) else set()
    if k == 'withdrawswap':
        s, coin = {'withdraw'}, str(req.get('coin') or 'USDC')
        coin = 'USDC' if coin.upper() == 'USDC' else lend_token(coin)
        if ckey(req.get('to')) != coin or type_key(resp.get('coin_type')) != type_key(resp.get('to_type')):
            s.add('swap')
        if req.get('lend_to'):                        # a move: lent again on the other market
            s.add('lend')
        return s
    if k == 'compose':
        spec = req.get('spec') if isinstance(req.get('spec'), dict) else {}
        src = spec.get('source') if isinstance(spec.get('source'), dict) else {}
        sinks = [x for x in spec.get('sinks') or [] if isinstance(x, dict)] if isinstance(spec.get('sinks'), list) else []
        s = set({'withdraw': ['withdraw'], 'perps_out': ['perps'], 'dust': ['swap']}.get(src.get('op'), []))
        if spec.get('swaps') or any((x.get('to') and not str(x.get('to')).lower().startswith('0x')) or x.get('coin') for x in sinks):
            s.add('swap')                             # a split names each piece's own coin: those pieces are swapped
        return s | {{'lend': 'lend', 'perps': 'perps'}[x['op']] for x in sinks if x.get('op') in ('lend', 'perps')}
    return set()


# check-send / sendmany: an amount below the coin's smallest unit (review W3-9).
def too_small(coin, decimals):
    return 'That amount is below the smallest unit of %s (%s), so nothing would arrive. Try a bigger amount.' % (disp(coin), plain_num(10 ** -int(decimals)))


def intent_label(path, req, resp):
    """What the Activity tab calls a transaction, written by Kivy from the plan it built — never text from the request
    (red-team 2026-10-24: /api/tx/log took any label, so anyone could write into a user's Activity, greeting and AI notes).
    Same wording the page used to send."""
    k = path.rsplit('/', 1)[-1]
    nm = lambda m: LEND_MKTS.get(str(m or ''), str(m or ''))
    c = lambda x: disp(ckey(str(x or '')) or lend_token(str(x or '')) or 'USDC')
    try:
        if k == 'compose':
            def step(p):
                op, a = p.get('op'), ('%s ' % dec(p['amount'])) if p.get('amount') is not None else ''
                if op == 'wallet':
                    return 'Take %s%s from your wallet' % (a, c(p.get('coin')))
                if op == 'withdraw':
                    return 'Withdraw %s%s from %s' % (a, c(p.get('coin')), nm(p.get('market')))
                if op == 'plus':
                    return 'Add %s%s from your wallet' % (a, c(p.get('coin')))
                if op == 'perps_out':
                    return 'Withdraw %sUSDC from your perps account' % a
                if op == 'dust':
                    return 'Sweep %d dust coins into %s' % (len(p.get('legs') or []), c(p.get('coin')))
                if op == 'swap':
                    return 'Swap %s to %s' % (c(p.get('from')), c(p.get('to')))
                if op == 'lend':
                    return 'Lend %s%s on %s' % (a, c(p.get('coin')), nm(p.get('market')))
                if op == 'send':
                    to = str(p.get('to') or '')
                    return 'Send %s%s to %s' % (a, c(p.get('coin')), (to[:6] + '…' + to[-4:]) if ADDR_RE.match(to) else 'an address')
                if op == 'keep':
                    return 'Keep %s%s in your wallet' % (a, c(p.get('coin')))
                if op == 'perps':
                    return 'Deposit %sUSDC into your perps account' % a
                return None
            return 'chain ' + ' → '.join(x for x in (step(p) for p in resp.get('plan') or []) if x)
        if k == 'perp':
            a, o = str(req.get('action') or ''), req.get('order') or {}
            sym = re.sub(r'[^A-Za-z0-9-]', '', str(o.get('symbol') or ''))[:20]
            if a == 'order':
                return 'Perps %s %s%s' % ('limit' if str(o.get('type') or '') == 'limit' else 'market', 'short ' if o.get('side') else 'long ', sym)
            return 'Perps %s %s' % (re.sub(r'[^a-z]', '', a)[:12], sym)
        if k == 'withdrawswap':
            if resp.get('lend_to'):
                return 'move %s %s→%s → %s' % (nm(req.get('protocol')), c(req.get('coin')), c(req.get('to') or req.get('coin')), nm(resp.get('lend_to')))
            return 'withdrawswap %s %s→%s' % (nm(req.get('protocol')), c(req.get('coin')), c(req.get('to')))
        if k == 'swaplend':
            return 'swaplend %s %s→%s' % (nm(req.get('protocol') or req.get('market')), c(req.get('from')), c(req.get('to')))
        if k == 'lend':
            return 'lend %s %s' % ({'deposit': 'deposit', 'withdraw': 'withdraw', 'claim': 'claim'}.get(str(req.get('action') or ''), 'deposit'), nm(req.get('protocol')))
        if k == 'swap':
            return 'swap %s→%s' % (c(req.get('from')), c(req.get('to')))
        if k in ('check-send', 'sendmany'):
            return 'send'
        if k == 'dust':
            return 'dust sweep %d coins→%s' % (len(resp.get('legs') or []), c(req.get('to')))
    except Exception:
        pass
    return k


# Called by Handler._send for every successful POST /api/tx/* build; the id goes back to the page as intent_id / guard.iid.
def narrowed(it, what, path, exc):
    """A part of the plan check 2 compares against couldn't be recorded: never silent (OWASP A10-4, 2026-10-29). The journal
    line is alerted by tools/watchdog.py; check 2 adds 'check narrowed: …' to the verdict's notes (checks.jsonl)."""
    it.setdefault('narrowed', []).append(what)
    print('kivy check narrowed: %s not recorded for %s (%s)' % (what, path, type(exc).__name__), flush=True)


def remember_intent(path, req, resp):
    """Store the plan behind a transaction Kivy just built; returns its id (kept 2 hours)."""
    now = time.time()
    for k in [k for k, v in list(_INTENTS.items()) if now - v['at'] > 7200]:
        _INTENTS.pop(k, None)
    g = resp.get('guard') or {}
    it = {'at': now, 'kind': 'move' if resp.get('lend_to') else path.rsplit('/', 1)[-1], 'address': str(req.get('address') or '').lower(), 'guard': g,
          'protocol': str(req.get('protocol') or ''), 'action': str(req.get('action') or ''), 'min_in': {}}
    if resp.get('router') in ('Aftermath', 'Cetus'):
        it['router'] = resp['router']                 # which swap router built it (backups since 2026-10-26) — kept, not shown on the card
    try:                                              # the minimum the card promised (swap / withdraw-then-swap)
        slip = float(K()['slippage_cap_pct']) / 100
        if it['kind'] == 'swap' and resp.get('min_out') and resp.get('coin_out'):
            c = cinfo(resp['coin_out'])
            it['min_in'][c['type']] = int(float(resp['min_out']) * 10 ** int(c['decimals']) * 0.999)
        if it['kind'] == 'withdrawswap' and resp.get('expected_out') and resp.get('to'):
            c = cinfo(resp['to'])
            low = float(resp['min_out']) if resp.get('min_out') else float(resp['expected_out']) * (1 - slip)   # the route's own minimum when sent
            it['min_in'][c['type']] = int(low * 10 ** int(c['decimals']) * 0.999)
    except Exception as exc:
        narrowed(it, 'minimum received', path, exc)
    try:                                              # a withdrawal promises (about) its amount, not 1 unit (review user-harm M1, 2026-10-29)
        t, units = withdraw_min(it['kind'], it['action'], g, req, resp)
        if t and units > 0:
            it['min_in'][t] = max(int(it['min_in'].get(t) or 0), units)
    except Exception as exc:
        narrowed(it, 'withdrawal minimum', path, exc)
    if it['kind'] == 'move':
        it['swap'] = bool(req.get('to')) and str(req.get('to')).lower() != str(req.get('coin') or '').lower()
        it['lend_to'] = str(req.get('lend_to') or '')
    if it['kind'] == 'compose':
        it['sinks'] = resp.get('check_sinks') or []
        it['final_type'] = resp.get('final_type')
        it['src_op'] = ((resp.get('plan') or [{}])[0] or {}).get('op')
        try:                                          # how much passes through the chain's swaps: a router fee is measured against this (2026-10-23)
            dec = next((int(v['decimals']) for v in COINS().values() if v['type'] == it['final_type']), None)
            mins = [float(p.get('min') or 0) for p in (resp.get('plan') or []) if p.get('op') in ('swap', 'dust')]
            if dec is not None and mins:
                it['volume'] = {it['final_type']: int(max(mins) * 10 ** dec)}
            tys = {p['coin']: (p['type'], int(p.get('dec') or 0)) for p in (resp.get('plan') or []) if p.get('type')}
            if any(p.get('in') is not None for p in (resp.get('plan') or [])):    # a split: each coin's own swap
                it['volume'] = {tys[p['to']][0]: int(float(p.get('min') or 0) * 10 ** tys[p['to']][1]) for p in resp['plan'] if p.get('op') == 'swap' and p.get('to') in tys}
        except Exception as exc:
            narrowed(it, 'chain swap volume', path, exc)
        try:                                          # the coins the plan TAKES IN count too: a swap's input that was withdrawn inside the transaction
            ec = ecoins()                             # never touches the wallet, so a 0.0001% router fee read as 1% of the leftover dust (2026-10-26)
            vol = it.get('volume') or {}
            for p in resp.get('plan') or []:
                c = ec.get(p.get('coin') or '') or {}
                if p.get('op') in ('wallet', 'withdraw', 'plus', 'perps_out') and c.get('type') and p.get('amount'):
                    vol[c['type']] = vol.get(c['type'], 0) + int(float(p['amount']) * 10 ** int(c['decimals']))
            if vol:
                it['volume'] = vol
        except Exception as exc:
            narrowed(it, 'chain input volume', path, exc)
    if it['kind'] == 'dust' and resp.get('to_type'):  # the router fee is measured against what the sweep delivers
        it['volume'] = {resp['to_type']: int(resp.get('min_units') or 0)}
    if it['kind'] in ('move', 'swaplend'):           # the coin that must go INTO the market, not to the wallet
        try:
            tc = req.get('to') or req.get('coin')
            ty = (cinfo(ckey(tc) or tc)['type'] if (ckey(tc) or tc) in ecoins() else None)
            it['lent_type'] = ty
            it['lent_units'] = int(float(resp.get('expected_out') or resp.get('amount') or 0) * 10 ** int(cinfo(ckey(tc) or tc)['decimals'])) if ty else 0
            if ty and it['lent_units']:
                it['volume'] = {ty: it['lent_units']}
            it['lend_to'] = it.get('lend_to') or str(req.get('protocol') or '')
        except Exception as exc:
            narrowed(it, 'lent coin', path, exc)
    it['ip'] = getattr(XW, 'ip', None)               # the visitor who built it: only they may report on it (review 2026-10-24)
    it['actions'] = intent_actions(path, req, resp)
    it['label'] = intent_label(path, req, resp)[:300]
    vol = {k: int(v) for k, v in (it.get('volume') or {}).items()}       # the most of each coin this plan moves (audit V01: the
    for k, v in (it.get('min_in') or {}).items():                         # browser caps what any unapproved address may get by it)
        vol[k] = max(vol.get(k, 0), int(v))
    for a in it['actions']:
        try:
            c = cinfo(a['coin'])
            vol[c['type']] = max(vol.get(c['type'], 0), int(float(a['amount']) * 10 ** int(c['decimals'])))
        except Exception:
            pass
    it['vol'] = vol
    it['fee_sui'] = int((g.get('fee') or {}).get('mist') or 0) / 1e9   # Kivy's flat fee in this transaction (verified on chain by the guard)
    exp = []                                          # what each lend should put into its market (S-06: check 2 only saw the call)
    try:
        if it['kind'] == 'compose':
            exp = [{'mk': k['market'], 'type': it.get('final_type'), 'amount': float(k['expect'])} for k in it.get('sinks') or []
                   if k.get('op') == 'lend' and k.get('expect') and it.get('final_type')]
        elif it['kind'] in ('move', 'swaplend') and it.get('lent_type') and it.get('lent_units') and it.get('lend_to'):
            exp = [{'mk': it['lend_to'], 'type': it['lent_type'], 'amount': it['lent_units'] / 10 ** int(cinfo(ckey(req.get('to') or req.get('coin')) or req.get('to') or req.get('coin'))['decimals'])}]
        elif it['kind'] == 'lend' and it.get('action') == 'deposit' and float(req.get('amount') or 0) > 0:
            c = 'USDC' if str(req.get('coin') or 'USDC').upper() == 'USDC' else lend_token(str(req.get('coin')))
            exp = [{'mk': it['protocol'], 'type': cinfo(c)['type'], 'amount': float(req['amount'])}]
    except Exception as exc:
        exp = []
        narrowed(it, 'lent amount (S-06)', path, exc)
    iid = secrets.token_hex(8)
    _INTENTS[iid] = it
    if exp:
        it['lend_expect'] = exp
        def snap():                                   # the market positions BEFORE the user signs, read in the background
            try:
                rows = lend_positions_rows(it['address'])
                it['lend_before'] = {'%s|%s' % (r['id'], lend_ntype(r.get('type') or cinfo('USDC')['type'])): float(r['lent']) for r in rows}
                it['lend_before_at'] = time.time()
                _INTENTS.save(iid)
            except Exception:
                pass
        th = threading.Thread(target=snap, daemon=True)
        th.start()
        th.join(timeout=5)                            # wait (usually 1-3 s) so the reading is from BEFORE the wallet can sign — a fast
    LEDGER.plan_put(iid, _INTENTS.get(iid))           # dual-write (change 8): the ledger beside intents.db
    return iid                                        # signer beat it on the first live test (2026-10-25); after 5 s it carries on alone


# remember_intent (M1): the least a withdrawal must put in the wallet, as (coin type, base units) — floor(amount x 0.98) of the
# coin the guard names (lending; Suilend rounding is far under 2%) or USDC (perps). SUI pays gas from the same balance, so the
# guard's gas allowance comes off. (None, 0) = not a withdrawal / not readable.
def withdraw_min(kind, action, g, req, resp):
    if action != 'withdraw' or kind not in ('lend', 'perp'):
        return None, 0
    ins = list((g or {}).get('in') or {})
    t = ins[0] if kind == 'lend' and len(ins) == 1 else cinfo('USDC')['type'] if kind == 'perp' else None
    amt = resp.get('amount') if resp.get('amount') is not None else req.get('amount')
    if not t or amt is None or str(amt) == 'all':
        return None, 0
    nt = type_key_bare
    dec = next((int(c['decimals']) for c in ecoins().values() if nt(c.get('type')) == nt(t)), None)
    if dec is None:
        return None, 0
    return t, int(math.floor(float(amt) * 10 ** dec * 0.98))   # SUI: both checks count it before the network fee (K-8)


# A coin type with the leading zeros of its package id dropped, so '0x0..2::sui::SUI' and '0x2::sui::SUI' match.
def lend_ntype(t):
    return re.sub(r'^0x0*', '', str(t).split('::')[0].lower()) + '::' + '::'.join(str(t).split('::')[1:])


# Called by verify_later right after check_tx; reads positions through lend_positions_rows (builder.ts 'positions-all').
def lend_amount_check(intent, res, digest):
    """After check 2: did each lend's market really receive (about) what was planned? Positions after vs the reading taken
    before signing; short by more than 2% = a problem (S-06). Skipped, with a note, when the reading isn't usable."""
    exp = (intent or {}).get('lend_expect')
    if not exp or res.get('ok') is not True:
        return
    before, at = intent.get('lend_before'), intent.get('lend_before_at')
    try:                                              # the chain's time to the millisecond (whole seconds skipped a reading done 1.4 s before)
        tu = str(tx_detail(digest).get('time_utc') or '')
        ms = re.search(r'\.(\d+)', tu)
        t_tx = calendar.timegm(time.strptime(tu[:19], '%Y-%m-%dT%H:%M:%S')) + (float('0.' + ms.group(1)) if ms else 0.0)
    except Exception:
        t_tx = None
    if before is None or at is None or t_tx is None or at >= t_tx:
        res.setdefault('notes', []).append('lent amount not checked (no reading from before the transaction)')
        return
    try:
        after = {'%s|%s' % (r['id'], lend_ntype(r.get('type') or cinfo('USDC')['type'])): float(r['lent']) for r in lend_positions_rows(intent['address'])}
    except Exception:
        res.setdefault('notes', []).append('lent amount not checked (positions not readable)')
        return
    want = {}
    for e in exp:
        k = '%s|%s' % (e['mk'], lend_ntype(e['type']))
        want[k] = want.get(k, 0.0) + float(e['amount'])
    for _ in range(3):                                # vault shares show up late (NAVI vaults took >10 s, live 2026-10-25): up to 3 more reads, 10 s apart
        if all(k in after and after[k] - before.get(k, 0.0) >= amt * 0.98 - 1e-9 for k, amt in want.items()):
            break                                     # …also when the market was already held: a late top-up read +0 (live 2026-10-29)
        time.sleep(10)
        try:
            after = {'%s|%s' % (r['id'], lend_ntype(r.get('type') or cinfo('USDC')['type'])): float(r['lent']) for r in lend_positions_rows(intent['address'])}
        except Exception:
            pass
    for k, amt in want.items():
        mk = LEND_MKTS.get(k.split('|')[0], k.split('|')[0])
        if k not in after:                            # that market isn't in the reading at all: say so, never guess a shortfall
            res.setdefault('notes', []).append('lent amount on %s not checked (market not in the positions reading)' % mk)
            continue
        got = after[k] - before.get(k, 0.0)
        if got < amt * 0.98 - 1e-9:
            res.setdefault('problems', []).append('only %s of the planned %s reached %s' % (dec(max(got, 0), 6), dec(amt, 6), mk))
            res['ok'] = False
        else:
            res.setdefault('notes', []).append('lent %s on %s ✓' % (dec(got, 6), mk))


# Check 2. Called by verify_later and scan_once (tests/test_check2.py too). Facts come from builder.ts 'tx-facts' (the chain):
# balance changes, called modules, moved objects; compared with the plan's guard, sinks, pays and the 1% 'others' rule.
def check_tx(digest, intent):
    """Compare the transaction on chain with the plan. Returns {'ok', 'problems', 'notes'}."""
    f = lend_cli('tx-facts', digest, timeout=60)
    if not f.get('ok'):
        raise RuntimeError(f.get('error') or 'chain read failed')
    shadow_note('facts', {'d': digest}, f)
    if not f.get('balance_changes') and not f.get('modules'):   # no effects to compare: unknown (retry / ok=None), never ok (review K-6)
        raise RuntimeError('the chain reply has no effects to compare yet')
    norm = lambda a: re.sub(r'^0x0*', '', str(a or '').lower())
    ntype = lambda t: re.sub(r'^0x0*', '', str(t).split('::')[0].lower()) + '::' + '::'.join(str(t).split('::')[1:])
    problems, notes = [], []
    me = norm((intent or {}).get('address') or f.get('sender'))
    if not f.get('success'):
        problems.append('the transaction failed on chain (%s) — nothing moved but the network fee' % (f.get('error') or '?'))
    if intent and norm(f.get('sender')) != me:
        problems.append('it was signed by %s, not the wallet the plan was for' % f.get('sender'))
    g = (intent or {}).get('guard') or {}
    out = {ntype(k): int(v) for k, v in (g.get('out') or {}).items()}
    sui = ntype('0x2::sui::SUI')
    # the network fee comes out of the same SUI: allow exactly the fee the chain charged, and count SUI received before it —
    # a flat 0.1 SUI let a route skim unseen, and a small SUI withdrawal read as short by its own fee (review K-8, 2026-10-30)
    gas_net = max(int(f['gas_net']), 0) if str(f.get('gas_net') or '').lstrip('-').isdigit() else None
    out[sui] = out.get(sui, 0) + (gas_net if gas_net is not None else int(g.get('gas') or 100_000_000))
    if (intent or {}).get('kind') == 'perp' and str(f.get('perp_ticket_gas') or '').isdigit():   # a stop-loss / take-profit ticket's SUI,
        out[sui] += int(f['perp_ticket_gas'])         # held for whoever triggers it (wallet is an executor): exactly that, perps only (2026-11-15)
    need = {ntype(k): int(v) for k, v in (g.get('in') or {}).items()}
    for k, v in ((intent or {}).get('min_in') or {}).items():
        need[ntype(k)] = max(need.get(ntype(k), 0), int(v))
    if gas_net is None and need.get(sui):             # no fee figure: the old allowance, taken off the SUI promised
        need[sui] = max(need[sui] - int(g.get('gas') or 100_000_000), 0)
    paid, got, others = {}, {}, {}
    for b in f.get('balance_changes') or []:
        t, v = ntype(b['coin_type']), int(b['amount'])
        if norm(b['address']) == me:
            (paid if v < 0 else got)[t] = (paid if v < 0 else got).get(t, 0) + abs(v)
        elif v > 0:
            others.setdefault(norm(b['address']), {})[t] = others.get(norm(b['address']), {}).get(t, 0) + v
    got_gross = dict(got)                             # SUI received before the fee: only for what the plan promised
    fee = g.get('fee') if isinstance(g.get('fee'), dict) else {}
    fee_back = int(fee.get('mist') or 0) if fee and (others.get(norm(fee.get('address'))) or {}).get(sui, 0) >= int(fee.get('mist') or 0) else 0
    if gas_net is not None:                           # Kivy's fee, paid from the same SUI (and received by Kivy), is added back too
        got_gross[sui] = max(got.get(sui, 0) - paid.get(sui, 0) + gas_net + fee_back, 0)
    try:                                              # a listed coin keeps its struct name in these lines; any other type is
        listed = {ntype(c['type']) for c in list(COINS().values()) + list(ecoins().values())}   # 'unknown coin 0xdead…::usdc::USDC',
    except Exception:                                 # never plain 'USDC' (fix package 4, QA 2026-11-17 OPS-V1 / ATK-V3)
        listed = {ntype(c['type']) for c in COINS().values()}
    sym = lambda t: t.split('::')[-1] if t in listed else unknown_coin(t)
    allow = kivy_pkgs()                               # every program it called must be on the allowlist (review user-harm M3, 2026-10-29)
    called = {normaddr(p) for p in f.get('packages') or []} | {normaddr(str(c).split('::')[0]) for c in f.get('calls') or [] if '::' in str(c)}
    unlisted = sorted(called - allow - {normaddr('0x1'), normaddr('0x2'), normaddr('0x3')}) if allow else (['(allowlist unreadable)'] if called else [])
    if unlisted:
        problems.append('it called a program Kivy has not verified (%s)' % ', '.join(p[:10] + '…' for p in unlisted))
    steps = g.get('steps') if isinstance(g.get('steps'), list) else None
    if intent and steps is not None:                  # the call rules on what LANDED too — check 1, the server and the builder apply
        calls = [tuple(str(c).split('::')[:3]) for c in f.get('calls') or [] if str(c).count('::') >= 2]   # them before signing (2026-11-14)
        bad = call_refusals(calls, set(steps))        # a program off the allowlist is the line above's business
        if bad:
            problems.append('it called %s, which this plan does not need' % ', '.join(x.split(' (')[0] for x in bad[:4]))
    credited = {k: dict(v) for k, v in others.items()}   # what each address received in full: the payment checks read this (a chain's send
                                                          # step pops its recipient from `others`, and the pay check then saw 0 — live test 2026-10-25)

    def units(t, v):                                  # 786 -> '0.000786' for USDC (it printed raw units)
        d = next((int(c['decimals']) for c in COINS().values() if ntype(c['type']) == t), None)
        return ('%.9f' % (v / 10 ** d)).rstrip('0').rstrip('.') if d is not None else str(v)
    if intent:
        for t, v in paid.items():
            if g.get('ctokens') and '::reserve::CToken<' in t:
                continue
            if v > out.get(t, 0):
                problems.append('it took %s %s from the wallet, the plan allowed %s' % (v, sym(t), out.get(t, 0)))
        for t, v in need.items():
            if got_gross.get(t, 0) < v:
                problems.append('the wallet received %s %s, the plan promised at least %s' % (got_gross.get(t, 0), sym(t), v))
        sends = {}                                    # a chain's sends: each recipient must get at least its planned minimums, added up
        for k in (intent.get('sinks') or []):        # (two sends to one address read 0 for the second one: a reviewer W2-4)
            if k['op'] == 'send':
                key = (norm(k['to']), ntype(k.get('type') or intent.get('final_type') or ''))
                sends[key] = sends.get(key, 0) + int(k.get('min') or 1)
        for (to, pt), mn in sends.items():
            if (others.get(to) or {}).get(pt, 0) < mn:
                problems.append('%s… got %s %s, the plan said at least %s' % (to[:8], (others.get(to) or {}).get(pt, 0), sym(pt), mn))
        pays = g.get('pay') or []
        pays = pays if isinstance(pays, list) else [pays]        # several coins to one address (one transaction)
        owed = {}                                     # two payments of one coin to one address: it must receive both (review W2-8)
        for pay in pays:
            key = (norm(pay.get('address')), ntype(pay.get('coin_type')))
            owed[key] = owed.get(key, 0) + int(pay.get('amount') or 0)
        for (to, pt), amt in owed.items():
            if (credited.get(to) or {}).get(pt, 0) < amt:
                problems.append('the recipient got %s %s, the plan said %s' % ((credited.get(to) or {}).get(pt, 0), sym(pt), amt))
        # the upper side (review scan S2, 2026-11-15: a planned recipient and the treasury had a lower bound only). Each payment
        # carries `max`, the most the plan lets that address receive of that coin (an exact send: its amount; "the rest" or an
        # estimate: the estimate plus slippage; Kivy's fee: exactly the fee). More than that is a problem — a coin that left a
        # lending market, the perps account or a swap inside the transaction never touched the wallet, so no wallet rule saw it.
        for key, mx in pay_caps(pays, norm, ntype).items():
            have = (credited.get(key[0]) or {}).get(key[1], 0)
            if mx is None:                            # a plan built before this rule (no `max` recorded): said, never guessed
                notes.append('upper limit not checked for %s to %s… (plan has no maximum)' % (sym(key[1]), key[0][:8]))
            elif have > mx + PAY_OVER_UNITS:
                problems.append('%s… got %s %s, the plan said at most %s' % (key[0][:8], have, sym(key[1]), mx))
        # only the planned (address, coin) pairs leave the "other addresses" rule — any OTHER coin a planned recipient or the
        # treasury receives is judged below like anyone else's (the whole address was skipped before)
        for key in list(sends) + list(owed):
            if key[0] in others:
                others[key[0]].pop(key[1], None)
    rev = norm(((K().get('revenue') or {}).get('address')) or '')
    via_agg = pure_agg_swap(f.get('calls'))       # a PURE swap through a trusted aggregator (review pre-launch H1, 2026-10-28)
    for who, coins in others.items():                 # pools and protocol fee wallets: small, else a problem
        for t, v in coins.items():
            # measured against the most of this coin the plan moved — not only what the wallet itself paid or kept
            # (a chain's swap output never touches the wallet: a 0.02% router fee read as 4% of the leftover, 2026-10-23)
            vol = {ntype(k): int(v) for k, v in ((intent or {}).get('volume') or {}).items()}
            base = max(paid.get(t, 0), got.get(t, 0), out.get(t, 0), need.get(t, 0), vol.get(t, 0))
            if who == rev and rev:
                # Kivy's flat fee is a planned payment (guard.pay, exact — checked above), so it never reaches this line. What
                # does: the treasury receiving something the plan does NOT include (S2: any coin and amount was a "Kivy fee"
                # note). Allowed only as the % swap / lend fee while that is switched on (knowledge/kivy.json revenue), within
                # that share of the coin's volume; with no plan on record the basics are all that can be said.
                if not intent:
                    notes.append('Kivy fee %s %s' % (units(t, v), sym(t)))
                elif base and v <= base * fee_share_max() + 1 and fee_share_max() > 0:
                    notes.append('Kivy fee %s %s' % (units(t, v), sym(t)))
                else:
                    problems.append('Kivy\'s treasury received %s %s that the plan does not include' % (units(t, v), sym(t)))
                continue
            if base and v > base * OTHER_SHARE_MAX:
                problems.append('another address (%s…) received %s %s' % (who[:8], units(t, v), sym(t)))
            elif not base and v > 0 and not via_agg and who not in {norm(x) for x in ROUTER_FEE_WALLETS_SRV}:   # a coin the plan never mentions, to anyone but the router's fee wallet (audit nv #1)
                problems.append('another address (%s…) received %s %s that the plan never mentions' % (who[:8], units(t, v), sym(t)))
            elif not base and v > 0 and route_only_too_big(t, v, intent):   # …but never worth more than 1% of the plan (review H1 (3), 2026-10-29)
                problems.append('another address (%s…) received %s %s inside the swap route, more than 1%% of the plan' % (who[:8], units(t, v), sym(t)))
            elif not base and v > 0:                     # a coin only the route touched (trusted, 2026-10-25): noted so it stays visible
                notes.append('route paid %s %s to %s…' % (units(t, v), sym(t), who[:8]))
    for o in f.get('moved') or []:
        if norm(o['from']) == me and norm(o['to']) != me:
            problems.append('one of the wallet\'s objects went to %s' % o['to'])
    for o in f.get('taken') or []:                    # wrapped, deleted, shared or put under another object (review K-9, 2026-10-30)
        problems.append('one of the wallet\'s objects (%s) left the wallet' % ('::'.join(str(o.get('type') or '').split('::')[-2:]) or 'an object'))
    for c in f.get('other_cmds') or []:               # Publish / Upgrade / anything new: never part of a Kivy plan
        problems.append('it ran a %s command, which no Kivy plan uses' % str(c)[:30])
    want = {'swap': 'router::', 'swaplend': 'router::', 'withdrawswap': 'router::', 'dust': 'router::'}.get((intent or {}).get('kind'))
    if (intent or {}).get('kind') == 'move' and intent.get('swap'):
        want = 'router::'
    proto = (intent or {}).get('protocol') or ''
    if (intent or {}).get('kind') == 'lend' and (intent or {}).get('action') in ('deposit', 'withdraw'):
        want = 'lending_market::' if proto.startswith('suilend') else None
    if want and not any(m.startswith(want) for m in f.get('modules') or []):
        problems.append('the planned protocol (%s) was not called' % want.rstrip(':'))
    if intent and called & PERPS_PKGS and not perps_plan(intent):   # allowlisted, but a perps call moves no coin or object (review K-4)
        problems.append('it called the perps program, which this plan does not use')
    if (intent or {}).get('kind') == 'compose':
        mods = f.get('modules') or []
        if intent.get('src_op') == 'perps_out' and not any(m.startswith('account::withdraw_collateral') for m in mods):
            problems.append('nothing was withdrawn from the perps account')
        if intent.get('src_op') == 'dust' and not any(m.startswith('router::') for m in mods):
            problems.append('the dust was not swapped (no router call)')
        for k in intent.get('sinks') or []:
            if k['op'] == 'lend':
                lt = k['market']
                dep = ('lending_market::deposit' if lt.startswith('suilend') else 'navi_vault::deposit' if lt in VAULTS or lt in SUI_VAULT_MKTS
                       else 'incentive_v3::')
                if not any(m.startswith(dep) for m in mods):
                    problems.append('nothing was lent on %s (no deposit call)' % LEND_MKTS.get(lt, lt))
            elif k['op'] == 'perps' and not any(m.startswith('account::deposit_collateral') for m in mods):
                problems.append('nothing was deposited into the perps account')
            elif k['op'] == 'keep' and not k.get('stays') and (k.get('type') or intent.get('final_type')) \
                    and got.get(ntype(k.get('type') or intent['final_type']), 0) < 1:   # a split's kept coin: its own type (2026-11-08)
                problems.append('the coin you kept never arrived in the wallet')
    if (intent or {}).get('kind') in ('move', 'swaplend') and intent.get('lend_to'):
        lt = intent['lend_to']
        dep = ('lending_market::deposit' if lt.startswith('suilend') else 'navi_vault::deposit' if lt in VAULTS or lt in SUI_VAULT_MKTS
               else 'incentive_v3::')
        if not any(m.startswith(dep) for m in f.get('modules') or []):
            problems.append('nothing was lent on %s (no deposit call)' % LEND_MKTS.get(lt, lt))
        ty, units = intent.get('lent_type'), int(intent.get('lent_units') or 0)
        if ty and units and got.get(ntype(ty), 0) > max(units * 0.01, 1):
            problems.append('%s %s went to the wallet instead of being lent' % (got.get(ntype(ty), 0), sym(ntype(ty))))
    if not intent:
        notes.append('no plan on record (server restarted?) — checked the basics only')
    moves = []                                        # what really moved, for the stats pipeline
    for b in f.get('balance_changes') or []:
        try:
            sy, amt = human_units(b['coin_type'], abs(int(b['amount'])))
            moves.append({'who': who_is(b['address'], me), 'coin': sy, 'amt': ('+' if int(b['amount']) > 0 else '−') + amt})
        except Exception:
            pass
    if not f.get('success'):                          # a failed transaction moved nothing but its network fee: recorded as FAILED, not as a
        problems = [p for p in problems if p.startswith(('the transaction failed', 'it was signed by'))]   # mismatch (2026-11-02)
    return {'ok': not problems, 'problems': problems, 'notes': notes, 'moves': moves[:12], 'status': 'SUCCESS' if f.get('success') else 'FAILED',
            **({} if f.get('success') else {'failed': True}), 'calls': (f.get('calls') or [])[:40]}   # calls: the scan's evidence rule (review K-7)


# Aftermath perps (allowlisted). Only a perps plan may call it: a perps build, or a chain that withdraws from or deposits into the
# perps account — an order slipped into a swap moves no coin and no object, so no other rule sees it (review K-4, 2026-10-30).
# The page refuses the same calls unless the guard says perps.
PERPS_PKGS = {normaddr(x) for x in policy_consts('perps_programs')}   # kivy_pkgs.json calls.consts (change 5, 2026-11-14)


def perps_plan(intent):
    it = intent or {}
    return it.get('kind') == 'perp' or (it.get('kind') == 'compose' and (it.get('src_op') == 'perps_out' or any(
        (k or {}).get('op') == 'perps' for k in it.get('sinks') or [])))


# The scan may hand a transaction to a plan only on evidence: a guard that promises something, or (claim, perps — empty guards
# pass on almost anything) a call into that plan's own program (review K-7, 2026-10-30).
def scan_evidence(it, res):
    g = it.get('guard') or {}
    if g.get('out') or g.get('in') or g.get('pay') or it.get('min_in') or it.get('sinks'):
        return True
    calls = [str(c).split('::') for c in res.get('calls') or [] if str(c).count('::') >= 2]
    if it.get('kind') == 'perp':
        return any(normaddr(c[0]) in PERPS_PKGS for c in calls)
    if it.get('kind') == 'lend' and it.get('action') == 'claim':
        return any('claim' in c[2] for c in calls)
    return False


# check_tx: a coin only an aggregator route touched may go to any address (2026-10-28), but a coin Kivy can price may not be
# worth more than 1% of the plan's dollars (min 1 cent) — a route that drains a lent coin the plan never names is caught even
# when the swap looks pure (review user-harm H1 (3), 2026-10-29). A coin Kivy can't price stays a note (pool fees in odd coins).
def route_only_too_big(t, v, intent):
    nt = type_key_bare
    try:
        c = next(((s, x) for s, x in ecoins().items() if nt(x.get('type')) == nt(t)), None)
        if not c:
            return False
        px = 1.0 if c[0] in ('USDC', 'USDsui', 'USDT') else coin_px(c[0])
        if not px:
            return False
        usd = int(v) / 10 ** int(c[1]['decimals']) * float(px)
        plan_usd = sum(float(a.get('usd') or 0) for a in (intent or {}).get('actions') or [])
        return usd > max(plan_usd * OTHER_SHARE_MAX, 0.01)
    except Exception:
        return False


_VERIFY_LOCK = threading.Lock()                      # makes the in-check + add below one step (review 2026-11-04 S-4)
_VERIFYING = set()                                   # digests being checked now: one checker each, a few at a time (audit V09)
_VERIFY_SLOTS = threading.BoundedSemaphore(4)


def human_units(t, v):
    """Raw coin units -> (symbol, plain amount) for the stats pipeline. A type off Kivy's list is 'unknown coin' with its raw
    units: never its struct name, which anyone chooses ('…::usdc::USDC' read as USDC — fix package 4, QA 2026-11-17 OPS-V1), and
    no address either (the Walrus export drops a record holding one and its cursor stuck there — OPS-B1)."""
    nt = type_key
    for sym, c in list(ecoins().items()):
        if nt(c['type']) == nt(t):
            return sym, ('%.9f' % (int(v) / 10 ** int(c['decimals']))).rstrip('0').rstrip('.')
    return 'unknown coin', str(v)


def who_is(addr, me):
    """'you' / 'Kivy' (treasury) / a short address, for the stats pipeline."""
    a = str(addr or '').lower()
    rev = str((K().get('revenue') or {}).get('address') or '').lower()
    nt = lambda x: re.sub(r'^0x0*', '', x)
    return 'you' if me and nt(a) == nt(str(me).lower()) else 'Kivy' if rev and nt(a) == nt(rev) else a[:6] + '…' + a[-4:]


def pipe_plan(it):
    """Kivy's plan in one line: the steps (service, amount, coin) and its fee."""
    steps = [' '.join(x for x in (a.get('service', '').replace('_', ' '), ('%g' % float(a['amount'])) if a.get('amount') else '', a.get('coin') or '') if x)
             for a in (it.get('actions') or [])]
    return {'steps': steps or [it.get('kind') or '?'], 'fee_sui': it.get('fee_sui') or 0, **({'router': it['router']} if it.get('router') else {})}


# Background thread started in main.
def unlogged_scan():
    """Every 10 min: a plan the user signed (or whose wallet reply got lost) but that the page never reported — tab closed,
    phone locked — is looked up on chain by its sender and checked like any other, so no signed transaction misses check 2.
    Nothing found: noted on the plan's row. Candidates that don't match the plan: an alert line for report_checks (2026-10-23)."""
    while True:
        time.sleep(600)
        scan_once()


# One pass of the scan: Sui GraphQL lists the sender's last 10 transactions; the first that passes check_tx is the plan's.
# Also run by tests/test_scan.py.
SCAN_MAX, SCAN_PER_IP = 30, 2                       # plans one scan pass follows up, in all and per visitor (review W2-2)
SCAN_TRIES = 3                                      # passes a plan gets when the chain can't be read, before it is given up as "not read"


def scan_once(min_age=600):
    q = 'query($a: SuiAddress!) { transactions(last: 10, filter: {sentAddress: $a}) { nodes { digest effects { timestamp } } } }'
    claimed = {}                                      # digest -> the plan that already has it: one transaction, one plan (review K-7)
    for i2, d2 in _INTENTS._db('SELECT iid, data FROM intents WHERE at > ?', (time.time() - 86400,)):
        try:
            v = json.loads(d2)
        except Exception:
            continue
        for dg in (v.get('logged'), v.get('verified_digest')):
            if dg:
                claimed[dg] = i2
    for i2, v in list(_INTENTS.items()):
        for dg in (v.get('logged'), v.get('verified_digest')):
            if dg:
                claimed[dg] = i2
    # a reviewer W2-2 (2026-11-02): any visitor can mark a plan "signed" for any address, so one pass is capped: wallets that
    # already have a verified Kivy transaction go first, then at most SCAN_PER_IP plans per visitor and one per wallet,
    # SCAN_MAX in all; the rest wait for the next pass (10 minutes)
    due = [(iid, it) for iid, it in list(_INTENTS.items())
           if it.get('signed_at') and not it.get('logged') and not it.get('scanned') and time.time() - it['signed_at'] >= min_age
           and ADDR_RE.match(it.get('address') or '')]
    known = {}
    for _, it in due:
        if it['address'] not in known:
            try:
                known[it['address']] = metrics.had_kivy_tx(it['address'])
            except Exception:
                known[it['address']] = False
    due.sort(key=lambda x: (not known[x[1]['address']], x[1]['signed_at']))
    owner_ips = {}                                    # address -> IPs that built a plan for it whose transaction was verified (last 24 h)
    for _, d2 in _INTENTS._db('SELECT iid, data FROM intents WHERE at > ?', (time.time() - 86400,)):
        try:
            v = json.loads(d2)
        except Exception:
            continue
        if v.get('verified_digest') and v.get('ip') and v.get('address'):
            owner_ips.setdefault(v['address'], set()).add(v['ip'])
    take, per_ip, per_addr = [], {}, set()
    for iid, it in due:
        ipk = it.get('ip') or '-'
        if len(take) >= SCAN_MAX or per_ip.get(ipk, 0) >= SCAN_PER_IP or it['address'] in per_addr:
            continue
        per_ip[ipk] = per_ip.get(ipk, 0) + 1
        per_addr.add(it['address'])
        take.append((iid, it))
    if True:
        for iid, it in take:
            try:
                t0 = it.get('signed_at')
                # a plan is marked "scanned" only after a pass that READ the chain (review scan, server finding 10, 2026-11-15: it
                # was marked first, so one failed read — the builder or Sui GraphQL down for a minute — dropped it for good, and a
                # wallet Kivy knows got a false "none of its transactions matches" record). A pass that can't read counts a try;
                # after SCAN_TRIES the plan is closed as "not read", said apart from "not found" and "no match".
                it['scan_tries'] = int(it.get('scan_tries') or 0) + 1
                _INTENTS.save(iid)
                last_try = it['scan_tries'] >= SCAN_TRIES
                try:
                    nodes = ((gql(q, {'a': it['address']}).get('transactions') or {}).get('nodes')) or []
                except Exception as exc:
                    print('kivy scan: history not read for a signed plan (%s), try %d of %d' % (type(exc).__name__, it['scan_tries'], SCAN_TRIES), flush=True)
                    if last_try:
                        it['scanned'] = True
                        _INTENTS.save(iid)
                        metrics.pipe(iid, chain={'status': 'not read', 'moves': []})
                    continue
                shadow_note('history', {'a': it['address'], 'n': 10, 'kind': 'sent', 'detail': 1}, nodes)
                cand = [n['digest'] for n in nodes if n.get('effects') and
                        calendar.timegm(time.strptime(n['effects']['timestamp'][:19], '%Y-%m-%dT%H:%M:%S')) >= t0 - 60]
                cand = [dg for dg in cand if claimed.get(dg, iid) == iid]   # another plan's transaction is never this one's
                hit, unread = None, 0
                for dg in cand:
                    try:
                        res = check_tx(dg, it)
                        if res.get('ok') and scan_evidence(it, res):
                            hit = dg; break
                    except Exception:
                        unread += 1                   # this candidate could not be read: not the same as "does not match"
                if not hit and unread and not last_try:
                    print('kivy scan: %d of %d transactions not read for a signed plan, try %d of %d' % (unread, len(cand), it['scan_tries'], SCAN_TRIES), flush=True)
                    continue                          # the next pass reads it again; nothing is recorded on a half-read
                it['scanned'] = True
                _INTENTS.save(iid)
                if not hit and unread:                # the last try and still unread: closed as "not read", never as "no match"
                    metrics.pipe(iid, chain={'status': 'not read', 'moves': []})
                    continue
                if hit:
                    claimed[hit] = iid
                    it['logged'] = hit
                    _INTENTS.save(iid)
                    kivy_diary_add(hit, '%s · found by the scan (the page never reported it)' % it.get('kind'))
                    verify_later(hit, iid)
                elif cand and known.get(it['address']):    # a wallet that never used Kivy gets no record (W2-2: a stranger's plan is not evidence)
                    # a reviewer W3-6: "Kivy-known wallet" is not evidence that THIS plan was the owner's (anyone can build one for any
                    # address). Its record goes on the wallet only when the plan came from an IP that built a verified plan for it;
                    # otherwise into checks_newwallet.jsonl: the watchdog's one combined line, never the wallet's own record or Walrus
                    mine = it.get('ip') and it['ip'] in owner_ips.get(it['address'], ())
                    out = CHECKS if mine else CHECKS_NEW
                    if not mine and CHECKS_NEW.exists() and CHECKS_NEW.stat().st_size >= 1_000_000:
                        continue
                    with open(out, 'a') as fh:
                        fh.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'digest': cand[-1], 'kind': it.get('kind'),
                                             'address': it.get('address'), 'ok': False,
                                             'problems': ['a plan was signed but never reported, and none of the wallet\'s %d transactions since then matches it' % len(cand)]}) + '\n')
                else:
                    metrics.pipe(iid, chain={'status': 'not found', 'moves': []})
            except Exception:
                pass


_VERIFY_WAIT = []                                     # checks that found the queue full: run as soon as a slot frees (audit nv #9)


# Called by POST /api/tx/log and scan_once. Writes data/checks.jsonl, metrics.ptb + metrics.pipe, and the ✅/⚠️ mark on
# the Kivy diary line; at most 4 checks run at once, the rest wait in _VERIFY_WAIT.
def verify_later(digest, iid, quiet=False):
    """After a signed transaction: wait for it on chain, check it, record the verdict (never blocks the user). quiet: a wallet new
    to Kivy — its diary line and records are written only when check 2 is green (review W3-2)."""
    key = (digest, iid or '')                         # per plan: a check without a plan can't block the one with it (review 2026-10-24)
    with _VERIFY_LOCK:
        if key in _VERIFYING:
            return
        full = len(_VERIFYING) >= 32
        if not full:
            _VERIFYING.add(key)
    if full:                                          # never silent: watchdog alerts on this line (review 2026-10-24)
        print('kivy check dropped: %s… (%d checks waiting)' % (digest[:10], len(_VERIFYING)), flush=True)
        if len(_VERIFY_WAIT) < 500 and (digest, iid, quiet) not in _VERIFY_WAIT:
            _VERIFY_WAIT.append((digest, iid, quiet))   # delayed, never lost
        return

    jkey = '%s|%s' % (digest, iid or '')
    LEDGER.job_add('check2', jkey, {'digest': digest, 'iid': iid, 'quiet': quiet})   # durable (change 8): run again after a restart

    def run():
        with _VERIFY_SLOTS:
            try:
                work()
            finally:
                LEDGER.job_done('check2', jkey)
                _VERIFYING.discard(key)
                if _VERIFY_WAIT:
                    try:
                        verify_later(*_VERIFY_WAIT.pop(0))
                    except IndexError:
                        pass

    def work():
        intent = _INTENTS.get(iid) if iid else None
        res = None
        for i in range(6):
            try:
                res = check_tx(digest, intent); break
            except Exception as exc:
                res = {'ok': None, 'problems': [], 'notes': ['could not read it yet: %s' % str(exc)[:80]]}
                time.sleep(5)
        try:
            lend_amount_check(intent, res, digest)    # S-06: the amount that reached each lending market, not just the call
        except Exception as exc:                      # never silent (OWASP A10-4): the verdict says what wasn't checked
            res.setdefault('notes', []).append('lent amount not checked (error)')
            print('kivy check narrowed: lent amount (S-06) not checked for %s… (%s)' % (digest[:10], type(exc).__name__), flush=True)
        for what in (intent or {}).get('narrowed') or []:
            res.setdefault('notes', []).append('check narrowed: %s not recorded at build' % what)
        if quiet and res.get('ok') is not True:      # a wallet new to Kivy, not green: no diary, label, stats or Walrus record (W3-2)
            try:
                if not CHECKS_NEW.exists() or CHECKS_NEW.stat().st_size < 1_000_000:
                    with open(CHECKS_NEW, 'a') as fh:
                        fh.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'digest': digest,
                                             'kind': (intent or {}).get('kind'), 'address': (intent or {}).get('address'), **res}) + '\n')
                else:
                    print('kivy check new-wallet log full: %s… not recorded' % digest[:10], flush=True)
            except Exception:
                pass
            return
        if quiet:                                     # green: a real first Kivy transaction — logged like any other
            kivy_diary_add(digest, (intent or {}).get('label'))
        try:                                          # Kivy's numbers: one PTB, its steps
            dd = tx_detail(digest)
            if dd and dd.get('status'):
                sender = str(dd.get('sender') or '').lower()
                try:
                    sent_at = calendar.timegm(time.strptime(str(dd.get('time_utc') or '')[:19], '%Y-%m-%dT%H:%M:%S'))
                except Exception:
                    sent_at = None
                # verified = THIS card's transaction: its wallet sent it, check 2 passed, it came after the card was built, and no
                # other digest already claimed the card — an old or unrelated transaction can't inflate the numbers (review F-03)
                ver = bool(intent) and sender == str(intent.get('address') or '').lower() and res.get('ok') is True \
                    and sent_at is not None and sent_at >= float(intent.get('at') or 0) - 120 and intent.get('verified_digest') in (None, digest)
                if ver and intent.get('verified_digest') != digest:
                    intent['verified_digest'] = digest
                    _INTENTS.save(iid)
                metrics.ptb(digest, sender, str(dd['status']).upper(), (intent or {}).get('kind') or 'unknown', (intent or {}).get('actions') if ver else [], ver,
                            gas=float(dd.get('fee_sui') or 0), fee=(intent or {}).get('fee_sui', 0) if ver else 0)
        except Exception:
            pass
        try:                                          # stats pipeline: the real result and check 2 on this plan's row
            metrics.pipe(iid, digest=digest, chain={'status': res.get('status'), 'moves': res.get('moves') or []},
                         check2={'ok': res.get('ok'), 'problems': res.get('problems') or [], 'notes': res.get('notes') or [],
                                 **({'failed': True} if res.get('failed') else {})})
        except Exception:
            pass
        rec = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'digest': digest, 'kind': (intent or {}).get('kind'),
               'address': (intent or {}).get('address'), **({'router': intent['router']} if (intent or {}).get('router') else {}),
               **({'policy': intent['guard']['policy']} if ((intent or {}).get('guard') or {}).get('policy') else {}), **res}
        try:
            with open(CHECKS, 'a') as fh:
                fh.write(json.dumps(rec) + '\n')
        except Exception:
            pass
        LEDGER.check_put(rec, iid)                    # dual-write (change 8)
        mark = (' · failed on chain (only the network fee was spent)' if res.get('failed') and len(res.get('problems') or []) == 1 else
                ' · check ⚠️ ' + '; '.join(res['problems'])[:200] if res['ok'] is False else ' · check ? (chain not readable)' if not res['ok']
                else ' · check ✅' if intent else ' · check ? (no Kivy plan to compare)')   # unknown plan is never a green check (audit V10)
        LEDGER.tx_put(digest, iid=iid, address=(intent or {}).get('address'), verdict=mark.strip(' ·'))   # dual-write (change 8)
        for _ in range(6):                            # the log line may land a moment after us
            try:
                with open(KIVY_DIARY, 'r+') as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    text = fh.read()
                    key = '[%s…]' % digest[:10]
                    i = text.find(key)
                    if i >= 0:
                        e = text.find('\n', i)
                        line_start = text.rfind('\n', 0, i) + 1
                        head = text[line_start:i]
                        if ' · check ? (no Kivy plan to compare)' in head and intent:   # the plan's own check wins
                            head = head.replace(' · check ? (no Kivy plan to compare)', '')
                        if ' · check ' not in head:
                            text = text[:line_start] + head.rstrip().rstrip('·').rstrip() + mark + ' · ' + text[i:]
                            fh.seek(0); fh.truncate(); fh.write(text)
                        return
            except Exception:
                pass
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()


# Called by POST /api/tx/log and scan_once; writes diary.md in the Kivy folder (KIVY_DIARY), which tx_labels and
# wallet_memory read back. Chain facts come from tx_detail (Sui GraphQL).
def kivy_diary_add(digest, label=''):
    """Kivy's own diary: one line per on-chain transaction made through Kivy, newest on top under its UTC date.
    Every fact comes from the chain (status, sender, coins, fee, time) — the page only supplies the digest and a label."""
    if label:
        LEDGER.tx_put(digest, label=LABEL_BAD_RE.sub('', str(label))[:300])   # dual-write (change 8): the label beside the diary line
    if digest in _DIARY_SEEN and not label:
        return 'seen'
    if digest in _DIARY_SEEN or label:               # logged before without a plan: Kivy's own label replaces 'transaction'
        clean = LABEL_BAD_RE.sub('', str(label))[:300]
        with open(KIVY_DIARY, 'a+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0); text = f.read()
            if digest[:10] + '…]' in text:
                _DIARY_SEEN.add(digest)
                new = re.sub(r'(?m)^(- \*\*[^*]+\*\* — \S+ · )transaction( · [^\n]*\[%s…\])' % re.escape(digest[:10]), lambda m: m.group(1) + clean + m.group(2), text, count=1)
                if new != text and clean:
                    f.seek(0); f.truncate(); f.write(new)
                return 'seen'
    d = tx_detail(digest)
    if not d or d.get('error') or not d.get('status'):
        return 'not on chain yet'
    sender = d.get('sender') or ''
    short = lambda a: (a[:6] + '…' + a[-4:]) if a and len(a) > 12 else (a or '?')
    moves = '; '.join('%s %s %s' % (m['amount'], m['coin'], ('→ ' + short(m['to'])) if m.get('to') and m.get('to') != sender
                                                       else ('out' if m.get('from') == sender else 'in'))
                      for m in (d.get('moves') or [])[:6]) or 'no coin moves'
    t = str(d.get('time_utc') or '')
    day, hm = (t[:10], t[11:16]) if len(t) >= 16 else (time.strftime('%Y-%m-%d', time.gmtime()), time.strftime('%H:%M', time.gmtime()))
    ok = '✅' if str(d['status']).upper() == 'SUCCESS' else '❌ ' + str(d['status'])
    label = LABEL_BAD_RE.sub('', str(label or ''))[:300] or 'transaction'   # full step list (Activity detail, 2026-10-21); never '·' (it splits the line)
    line = '- **%s UTC** — %s · %s · %s · fee %s SUI · %s · [%s…](%s)' % (hm, short(sender), label, moves, d.get('fee_sui'), ok, digest[:10], d.get('explorer'))
    with open(KIVY_DIARY, 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0); text = f.read()
        if digest[:10] + '…]' in text:
            _DIARY_SEEN.add(digest); return 'seen'
        head = '# Kivy diary — on-chain transactions made through Kivy (newest on top)\n'
        body = text[len(head):] if text.startswith(head) else text
        sec = '## %s\n' % day
        body = body.replace(sec, sec + line + '\n', 1) if sec in body else sec + line + '\n\n' + body.lstrip('\n')
        # keep dates newest-first
        parts = re.split(r'(?m)^(?=## \d{4}-\d{2}-\d{2}\n)', body)
        parts = sorted([x for x in parts if x.strip()], key=lambda x: x[3:13], reverse=True)
        f.seek(0); f.truncate(); f.write(head + '\n' + ''.join(x if x.endswith('\n\n') else x.rstrip('\n') + '\n\n' for x in parts))
    _DIARY_SEEN.add(digest)
    return 'logged'


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .chainreads import ADDR_RE, COINS, cinfo, ckey, dec, ecoins, gql, plain_num, shadow_note  # noqa: E402
from .lending import LEND_MKTS, SUI_VAULT_MKTS, VAULTS, coin_px, lend_positions_rows, lend_token  # noqa: E402
from .cards import plan  # noqa: E402
from .txreads import ROUTER_FEE_WALLETS_SRV, pure_agg_swap, tx_detail  # noqa: E402
from .sends import XW  # noqa: E402
