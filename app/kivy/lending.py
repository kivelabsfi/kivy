"""Lending and move: markets, builder.ts reads (rates, positions), lend cards, move (Sections 15-16).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# Called by the readers, the AI tools and the Handler (server.py); every read goes through kivy/upstream.py.
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import math
import re
import threading
import time
from pathlib import Path
from . import amounts as AM
from .config import K, SPOT_IN, cap_usd, disp, ui_cap
from .upstream import _DIRTY, lend_cli

widget_reply = None        # server.py's (Section 7): set by server.py once it is defined (a module never imports server.py)


# ── Section 15: lending — the USDC markets (PROTOS), market names, builder.ts reads (rates, collateral, positions), the
#    lend card and spend checks; used by widget_reply, chains, the AI tools and /api/lend*, /api/tx/lend ──
PROTOS = {'suilend': 'Suilend Main Market', 'suilend-ember': 'Suilend Ember', 'suilend-matrixdock': 'Suilend Matrixdock',
          'navi': 'NAVI', 'navi-highyield': 'NAVI High Yield', 'navi-prime': 'NAVI Prime'}
VAULTS = {'navi-highyield', 'navi-prime'}
# most specific first: 'suilend ember' must not stop at 'suilend'
MARKET_ALIASES = [('navi-sui-yield', r'(?:navi\s+)?sui\s+high\s*yield'), ('navi-sui-prime', r'(?:navi\s+)?sui\s+prime'),   # NAVI's SUI vaults before 'high yield' / 'prime' ("navi sui high yield" read as the USDC vault, 2026-10-31)
                  ('suilend-matrixdock', r'matri\w*(?:\s*do[ckx]\w*)?'), ('suilend-ember', r'emb[ae]r'),
                  ('navi-highyield', r'high\s*yield'), ('navi-prime', r'prime'),
                  ('navi', r'navi'), ('suilend', r'suilend\s+main'), ('suilend', r'suilend'), ('suilend', r'main')]


# The first market a text names (most specific alias first), or None.
def market_in(text):
    for key, pat in MARKET_ALIASES:
        if re.search(r'\b' + pat + r'\b', text, re.I):
            return key
    return None


def market_only(text):
    """The market the message names when it names nothing else ('suilend', 'lend on navi prime')."""
    t = re.sub(r'^\s*(?:lend\s+(?:on|with|in|via|to|into)\s+|use\s+|deposit\s+(?:on|in|into|to)\s+)', '', text.strip(), flags=re.I)
    t = re.sub(r'[.!?]+$', '', t).strip().lower()
    t = re.sub(r'\b(usdc|market|vault|the)\b', ' ', t)
    key = market_in(t)
    if not key:
        return None
    left = t
    for k, pat in MARKET_ALIASES:
        left = re.sub(r'\b' + pat + r'\b', ' ', left, flags=re.I)
    return key if not left.strip() else None
_LEND = {'rates': (0, None), 'pos': {}}


# USDC lending rates per market (builder.ts 'rates'), cached 5 min: the USDC table and apr_of / best_proto.
def lend_rates():
    t, v = _LEND['rates']
    if v and time.time() - t < 300:
        return v
    out = lend_cli('rates').get('rates') or []
    v = {r['market']: r for r in out if 'market' in r and 'total_apr' in r}
    _LEND['rates'] = (time.time(), v)
    return v


def lend_collateral():
    """{market: {'coins': [{sym, icon, ltv, liq, usd}], 'n'} | {'vault': True, 'risk'}} — what borrowers post in each market,
    biggest first. A lender's risk is that collateral: if it falls faster than liquidations sell it, lenders take the loss
    (2026-10-23). Read-only; cached 10 min."""
    t, v = _LEND.get('collat') or (0, None)
    if v and time.time() - t < 600:
        return v
    v = lend_cli('collateral').get('markets') or {}
    for m in v.values():
        for c in m.get('coins') or []:
            c['icon'] = coin_icon(c['sym']) or c.get('icon')     # Kivy's own icon when it has one
    _LEND['collat'] = (time.time(), v)
    return v


LEND_WORD_RE = re.compile(r'\b(?:lend\w*|ember|matrix\s*dock|suilend|navi|vaults?|collateral)\b', re.I)   # lending words, not finance terms (APR, APY, yield); a bare 'market' could be perps
LEND_MKT_NAMES = [('suilend-ember', r'\bember\b'), ('suilend-matrixdock', r'\bmatrix\s*dock\b'), ('navi-highyield', r'\bhigh\s*yield\b'),
                  ('navi-prime', r'\bprime\b'), ('suilend', r'\bsuilend(?:\s+main)?\b'), ('navi', r'\bnavi(?:\s+main)?\b')]


def lend_ctx(msg, history):
    """Is this question about the LENDING markets? Its own words, or a lending table just shown (not the perps markets)."""
    if LEND_WORD_RE.search(msg):
        return True
    if re.search(r'\bmarkets?\b', msg, re.I):
        last = next((h.get('text') or h.get('content') or '' for h in reversed(history or []) if (h.get('role') in ('assistant', 'kivy', 'bot'))), '')
        return bool(re.search(r'lending market', str(last), re.I))
    return False


def lend_risk_reply(msg, address, history):
    """"What's the difference between the markets?", "which is safest?", "why does Ember pay more?": answered from the
    markets' REAL collateral, never from the AI's memory (it once said Ember takes DEEP and WAL — it takes only eEARN)."""
    col = lend_collateral()
    if not col:
        return None
    named = [k for k, rx in LEND_MKT_NAMES if re.search(rx, msg, re.I)]
    if 'suilend' in named and any(k.startswith('suilend-') for k in named) and not re.search(r'\bmain\b', msg, re.I):
        named.remove('suilend')                      # "suilend ember" names Ember, not Suilend Main
    if 'navi' in named and any(k.startswith('navi-') for k in named) and not re.search(r'\bmain\b', msg, re.I):
        named.remove('navi')                         # "navi prime" names the Prime vault, not NAVI's main market
    keys = named if len(named) >= 1 else ['suilend-ember', 'suilend-matrixdock', 'suilend', 'navi']
    lines = []
    for k in keys:
        c = col.get(k) or {}
        nm = LEND_MKTS.get(k, k)
        if c.get('vault'):
            lines.append('• %s: a curator vault — its collateral isn\'t published.' % nm)
            continue
        coins = c.get('coins') or []
        if not coins:
            continue
        top = [x['sym'] for x in coins[:5]]
        lines.append('• %s: %s' % (nm, ('only ' + ' and '.join(top)) if len(coins) <= 2 else '%d coins — %s and more' % (len(coins), ', '.join(top))) + '.')
    if not named and any(col.get(k, {}).get('vault') for k in ('navi-highyield', 'navi-prime')):
        lines.append('• NAVI High Yield and Prime: curator vaults — their collateral isn\'t published.')
    head = ('No lending market is risk-free. ' if re.search(r'\bsafe', msg, re.I) else '') + \
        ('What matters is the collateral borrowers post there' if len(named) == 1 else 'The difference is the collateral borrowers post in each market') + \
        ' — that is the lender\'s risk: if it falls faster than it can be liquidated, lenders can lose part of their deposit.'
    tail = 'One or two niche coins means concentrated risk, usually paid with a higher rate; many large coins spread it. The Collateral column shows it for every market.'
    out = {'text': head + '\n' + '\n'.join(lines) + '\n' + tail}
    try:
        t = widget_reply('lending markets', address, history)
        if t and t.get('widget'):
            out['widget'] = t['widget']
    except Exception:
        pass
    return out


def collat_ai():
    """For the AI: each market's collateral (biggest first, with LTV / liquidation %), so it can explain the risk difference."""
    try:
        out = {}
        for k, m in lend_collateral().items():
            name = LEND_MKTS.get(k, k)
            out[name] = 'curator vault: a curator decides where it lends; its collateral is not published' if m.get('vault') else \
                [('%s (LTV %s%%, liquidation %s%%, $%s posted)' % (c['sym'], c['ltv'], c['liq'], format(c['usd'], ','))) for c in (m.get('coins') or [])[:8]]
        return out
    except Exception:
        return 'not available right now'


def collat_row(mkt):
    """The table's Collateral cell: every collateral coin, biggest first, or a curator vault (no list published)."""
    try:
        c = lend_collateral().get(mkt) or {}
    except Exception:
        return None
    if c.get('vault'):
        return {'vault': True, 'risk': c.get('risk')}
    coins = c.get('coins') or []
    return {'coins': [{'sym': x['sym'], 'icon': x.get('icon')} for x in coins]} if coins else None   # all of them: the cell shows a few, the panel all


def lend_rates_all():
    """Yield of every Kivy coin on Suilend (3 markets) and NAVI (+ its USDC vaults). Read-only; cached 5 min."""
    t, v = _LEND.get('rates_all') or (0, None)
    if v and time.time() - t < 300:
        return v
    v = [r for r in (lend_cli('rates-all').get('rates') or []) if (r.get('market') in PROTOS or r.get('vault')) and r.get('asset')]
    icons = Path(__file__).resolve().parent.parent / 'static' / 'icons'
    for r in v:                                          # LSTs (vSUI, haSUI, sSUI): the icon Kivy keeps locally
        tok = r.get('token')
        r['icon'] = next(('/icons/%s.%s' % (tok, e) for e in ('png', 'svg') if tok and re.match(r'^[A-Za-z]{2,12}$', tok) and (icons / ('%s.%s' % (tok, e))).exists()), None)
    _LEND['rates_all'] = (time.time(), v)
    return v


# Every coin each market lists can be lent, not just USDC (2026-10-20). PROTOS stays the USDC list;
# NAVI's SUI vaults are SUI-only markets.
SUI_VAULT_MKTS = {'navi-sui-yield': 'NAVI SUI High Yield', 'navi-sui-prime': 'NAVI SUI Prime'}
LEND_MKTS = dict(PROTOS, **SUI_VAULT_MKTS)
# loose on purpose (2026-10-20: "swap usdc to usdsui then lend all of it in best amrket" fell through to the AI)
SWAPLEND_RE = re.compile(r'\b(?:swap|convert|change|exchange)\s+(?:([\d.]+|all|half|max)\s+)?(?:(?:my|of|the)\s+)*([A-Za-z]{2,10})\s+(?:to|for|into)\s+'
                         r'([A-Za-z]{2,10})\b(.*?\b(?:lend|supply|deposit|earn)\b(.*))$', re.I)
# After 'swap X to Y … lend', more than one plain lend: a part, a rest, a send, a keep or an amount -> the chain reader
# ("swap 5 sui to usdc, lend half on navi and send the rest to bob.sui" lost its send to the swap & lend card, 2026-10-29)
SWAPLEND_MORE = re.compile(r'\b(?:half|rest|remain\w*|quarter|third|percent|split|each|evenly|send|transfer|pay|give|keep|hold)\b'
                           r'|\d\s*%|\b(?:lend|supply|deposit|earn)\s+(?:only\s+)?[\d.]+'
                           r'|\bthen\s+(?:on|in)?to\s+[A-Za-z]|\b(?:swap|convert|exchange)\b'
                           r'|\b(?:aftermath|perps?|perpetuals?|margin|collateral)\b', re.I)   # "…and deposit it into perps": the perps account, not a lending market (2026-11-13)   # a 2nd swap: "…to usdc, then to usdsui, then lend" (2026-11-04)
WCOIN_RE = re.compile(r'^\s*(?:withdraw|redeem|take\s+(?:back|out))\s+(?:([\d.]+|all|half|max)\s+)?(?:(?:my|of|the)\s+)*([A-Za-z]{2,10})'
                      r'(?:\s+(?:from|on|in|at)\s+(.+?))?\s*[?.!]*\s*$', re.I)


def lend_token(word):
    """A coin Kivy can lend, spelled the way the markets list it ('vsui' -> 'vSUI', 'xbtc' -> 'BTC'); None if not one."""
    w = str(word or '').strip()
    up = SPOT_IN.get(w.upper(), w.upper())
    if up in K()['assets']:
        return up
    try:
        toks = [r['token'] for r in lend_rates_all() if r.get('token')]
    except Exception:
        return None
    hit = next((t for t in toks if t == w), None) or next((t for t in toks if t.lower() == w.lower()), None)
    return {'USDSUI': 'USDsui'}.get(hit, hit) if hit else None


def lend_rows_for(coin):
    """Rate rows for one coin, best first. A Kivy coin's own rows (SUI, not vSUI), or one version's rows."""
    rows = lend_rates_all()
    if coin in K()['assets']:
        out = [r for r in rows if r['asset'] == coin and not r.get('token')]
    else:
        out = [r for r in rows if str(r.get('token') or '').lower() == coin.lower()]
        exact = {r['market'] for r in out if r.get('token') == coin}
        out = [r for r in out if r.get('token') == coin or r['market'] not in exact]
    return sorted((r for r in out if not r.get('rate_only')), key=lambda r: -float(r['total_apr']))   # Volo: rates only


def lend_pick(rows, where, coin):
    """The market row a message names ('on navi prime'), or the best one when it names none (or says 'best')."""
    w = (where or '').strip().lower()
    if not w or re.search(r'\bbest\b|\bhighest\b|\btop\b', w):
        fam = re.search(r'\b(suilend|navi)\b', w)            # "the best Suilend market": the best one of that protocol
        pick = [r for r in rows if r['market'].startswith(fam.group(1)) and not r.get('rate_only')] if fam else []
        return (pick or rows or [None])[0]
    for r in rows:
        if w in (LEND_MKTS.get(r['market'], '').lower(), str(r.get('name') or '').lower()):
            return r
    key = market_in(w)
    if coin == 'SUI':                                      # SUI's vaults share their names with the USDC vaults
        key = {'navi-highyield': 'navi-sui-yield', 'navi-prime': 'navi-sui-prime'}.get(key, key)
    hit = next((r for r in rows if r['market'] == key), None)
    if hit is None and w in ('navi', 'suilend'):            # a bare protocol whose main market doesn't list the coin: its best one, as in plans (review P-13)
        hit = fam_pick(rows, w)
    return hit


def coin_px(coin):
    """USD price for the cap check: a Kivy coin's own, or (vSUI, LBTC, USDsui…) its base coin's."""
    px = prices()
    c = SPOT_IN.get(coin, coin)
    if px.get(c):
        return float(px[c])
    try:
        t = cinfo(ckey(coin) or coin)['type']
        own = token_prices([t], max_age=600).get(t)
        if own:
            return float(own)
    except Exception:
        pass
    try:
        base = next((r['asset'] for r in lend_rates_all() if r.get('token') and r['token'].lower() == coin.lower()), None)
    except Exception:
        base = None
    return float(px.get(base) or 0) or None


def coin_change(coin):
    """24h price move, % — Bluefin's for Kivy's coins; a staked or bridged coin (vSUI, LBTC) moves with its base coin; a dollar coin is 0."""
    ch = _PX.get('c') or {}
    c = SPOT_IN.get(coin, coin)
    if c in ch:
        return ch[c]
    if c == 'USDC' or re.search(r'usd', c, re.I):
        return 0.0
    try:
        base = next((r['asset'] for r in lend_rates_all() if r.get('token') and r['token'].lower() == coin.lower()), None)
    except Exception:
        base = None
    return ch.get(base)


def spendable(coin, holdings):
    """What the wallet can put in of a Kivy coin (a little SUI kept back for gas)."""
    have = float((holdings or {}).get(coin) or 0)
    return max(0.0, have - max(float(K()['gas_sui']) * 3, 0.05)) if coin == 'SUI' else have   # room for gas


def spend_check(coin, amt, address):
    """Cap and balance for coin Kivy is about to spend (lend, or swap then lend). None = fine, else why not."""
    if not (math.isfinite(amt) and amt > 0):
        return 'The amount has to be more than zero.'
    p, cap = coin_px(coin), cap_usd()
    if cap and not p:
        return "I can't get a live %s price right now, so I can't check the $%.0f cap. Try again in a minute." % (coin, cap)
    if cap and amt * p > cap * 1.0001:
        return "That's about $%.2f, over my $%.0f cap. Try a smaller amount." % (amt * p, cap)
    hb = dict(all_balances(address) or {})
    if amt > spendable(coin, hb) + 1e-12:
        if coin == 'SUI':
            return ('You can use up to %s SUI — Kivy keeps a little SUI (%s) in your wallet for the network fee. Try %s or less.'
                    % (dec(spendable(coin, hb)), dec(0.05), dec(spendable(coin, hb))))
        return 'Your wallet has %s %s, not enough for %s.' % (dec(float(hb.get(coin) or 0)), coin, dec(amt))
    return None


def lend_card(coin, where=None, address=None, amt=None, src=None):
    """Lend any listed coin: a slider in that coin, or — when the wallet has none of it — swap another coin
    into it and lend, in one transaction (2026-10-20)."""
    rows = lend_rows_for(coin)
    if not rows:
        return {'text': "%s isn't listed on Suilend or NAVI right now." % coin}
    r = lend_pick(rows, where, coin)
    if not r:
        return {'text': '%s is not lendable on %s. It is on: %s.' % (coin, where, ', '.join(LEND_MKTS.get(x['market'], x['name']) for x in rows))}
    if not address:
        return {'text': K()['no_wallet']}
    key, name, apr = r['market'], LEND_MKTS.get(r['market'], r['name']), round(float(r['total_apr']), 2)
    if r.get('token') and coin not in K()['assets']:
        coin = {'USDSUI': 'USDsui'}.get(r['token'], r['token'])       # one spelling on screen; the builder matches either
    hold, cap = all_balances(address) or {}, ui_cap()
    px = {c: (prices().get(c) or coin_px(c) or 0) for c in hold if float(hold.get(c) or 0) > 0}
    p = coin_px(coin) or 0
    have = spendable(coin, hold)
    over = bool(amt) and float(amt) > have * 1.000001
    if have * p >= 0.01 and not src:
        mxl = round(min(have, cap / p) if p else have, 9)
        if over:                                     # "lend 5 sui" holding 1.04: the slider starts at the most, and says so (it opened at 5)
            amt = mxl
        return {'text': ('You can lend up to %s %s%s, so the slider starts there. ' % (dec(mxl, 6), disp(coin), ' (a little SUI stays for gas)' if coin == 'SUI' else '') if over else '')
                        + 'Slide to choose how much %s to lend on %s.' % (disp(coin), name),
                'widget': {'type': 'lend', 'coin': coin, 'protocol': key, 'name': name, 'apr': apr, 'px': p, 'lend_fee_pct': rev_lend_pct(),
                           'balance': have, 'max': round(min(have, cap / p) if p else have, 9), 'cap': cap, 'preset': amt or 0}}
    srcs = {c: {'bal': spendable(c, hold), 'px': float(px.get(c) or 0)} for c in px
            if c != coin and float(px.get(c) or 0) > 0 and spendable(c, hold) * float(px.get(c) or 0) >= 0.01}
    if not srcs:
        return {'text': 'You have no %s, and no other coin to swap into it. Deposit some first (tap your balance for your address).' % disp(coin)}
    fam = r['asset'] if r['asset'] != coin else None
    pick = src if src in srcs else fam if fam in srcs else 'USDC' if 'USDC' in srcs else max(srcs, key=lambda c: srcs[c]['bal'] * srcs[c]['px'])
    return {'text': ('You have no %s, so Kivy swaps your %s into it and lends it on %s in one transaction: one approval, and if any step fails nothing moves.'
                     % (disp(coin), disp(pick), name)) if not src else
                    'Swap %s to %s, then lend it on %s, in one transaction: one approval.' % (disp(pick), disp(coin), name),
            'widget': {'type': 'swaplend', 'to': coin, 'from': pick, 'protocol': key, 'name': name, 'apr': apr, 'px_to': p, 'fee_pct': rev_fee_pct(), 'lend_fee_pct': rev_lend_pct(),
                       'sources': srcs, 'cap': cap, 'preset': amt or 0}}


def mark_dirty(addr):
    """A transaction from this wallet landed: drop its cached positions and balances so the next read is new."""
    a = str(addr or '').lower()
    if a:
        _DIRTY[a] = time.time()
        for k in [k for k in _BAL if a in (k if isinstance(k, tuple) else (k,))]:
            _BAL.pop(k, None)


def pos_cached(hit, addr, fresh):
    """A cached position read is good for 30 s (10 s when the answer decides an amount, e.g. a withdrawal), and never
    once the wallet has signed a transaction since (scale: 200k users a day would otherwise mean ~7M chain calls)."""
    return bool(hit) and hit[0] > _DIRTY.get(str(addr or '').lower(), 0) and time.time() - hit[0] < (10 if fresh else 30)


_POS_LOCKS = {}


def lend_positions_all(addr, fresh=False):
    """[{market, token, asset, type, amount}] — every coin this wallet has lent. Cached 30 s. One read at a time per wallet:
    the wallet panel asks for USDC and other coins at once, and both are served by the same read (W-04)."""
    hit = _LEND.setdefault('posall', {}).get(addr)
    if pos_cached(hit, addr, fresh):
        return hit[1]
    lock = _POS_LOCKS.setdefault(addr, threading.Lock())
    if len(_POS_LOCKS) > 5000:
        _POS_LOCKS.clear()
    with lock:
        hit = _LEND['posall'].get(addr)              # someone else just read it while we waited
        if hit and time.time() - hit[0] < 5 and hit[0] > _DIRTY.get(str(addr or '').lower(), 0):
            return hit[1]
        v = lend_cli('positions-all', addr).get('positions') or []
        _LEND['posall'][addr] = (time.time(), v)
    return v


def other_lent(addr, fresh=False):
    """Lent coins other than USDC, worth at least a cent, as withdraw rows."""
    out = []
    try:
        for x in lend_positions_all(addr, fresh):
            tok = {'USDSUI': 'USDsui'}.get(x.get('token'), x.get('token'))
            if not tok or tok == 'USDC' or x['market'] not in LEND_MKTS:
                continue
            p = token_prices([x['type']]).get(x['type']) or coin_px(tok) or 0
            if float(x['amount']) * p < 0.01:
                continue
            apr = next((float(r['total_apr']) for r in lend_rows_for(tok) if r['market'] == x['market']), 0)
            out.append({'id': x['market'], 'name': LEND_MKTS[x['market']], 'coin': tok, 'type': x.get('type'), 'px': p, 'total': round(apr, 2), 'icon': coin_icon(tok),
                        'lent': round(float(x['amount']), 9), 'ask': 'withdraw %s from %s' % (tok, LEND_MKTS[x['market']])})
    except Exception:
        pass
    return out


def to_opts(coin):
    return [coin] + [a for a in K()['assets'] if a != coin]


# ── Section 16: move — withdraw a lent coin, swap it, lend it elsewhere, ONE approval; move_parse/move_card are called
#    by widget_reply, the card posts to /api/tx/withdrawswap (lend_to set) -> builder.ts 'withdrawswap' ──
# ── Move: withdraw a lent coin, swap it if needed, and lend it on another market — ONE transaction (2026-10-20) ──
MOVE_RE = re.compile(r'^\s*(?:withdraw|take|pull|move|shift)\s+(?:(all|half|max|[\d.]+)\s+(?:of\s+)?)?(?:(?:my|the)\s+)?([A-Za-z]{2,10})\s+'
                     r'(?:out\s+)?from\s+(.+?)\s*(?:,|\band\b|\bthen\b|&|\bto\b)\s*(.*)$', re.I)


def move_parse(msg):
    """{'coin', 'amount', 'src', 'to', 'where'} for "withdraw X from A (and swap it to Y) then lend it on B" or "move X from A to B"."""
    m = MOVE_RE.match(msg)
    if not m or re.search(r'\bwallet\b', m.group(3), re.I) or CHAIN_PERPS.search(msg):
        return None                                   # from the wallet, or into perps: the chain reader handles it
    rest = m.group(4)
    lend = re.search(r'\b(?:lend|supply|deposit|put|park|earn)\b(?:\s+(?:it|them|that|all|everything))*(?:\s+(?:in|on|at|to|into|with)\s+(.+?))?\s*[?.!]*\s*$', rest, re.I)
    plain_to = re.match(r'^\s*(.+?)\s*[?.!]*\s*$', rest) if re.match(r'^\s*move|^\s*shift', msg, re.I) or ' to ' in (' ' + m.group(0)[len(m.group(1) or '') :]) else None
    if not lend and not (re.match(r'^\s*(?:move|shift)\b', msg, re.I) and plain_to and market_in(plain_to.group(1))):
        return None                                   # no lending step: not a move (withdraw / withdraw-as handle it)
    coin = lend_token(m.group(2))
    if not coin:
        return None
    sw = re.search(r'\b(?:swap|convert|change|turn)\b(?:\s+\w+){0,3}?\s+(?:to|into)\s+([A-Za-z]{2,10})\b|\bas\s+([A-Za-z]{2,10})\s*[?.!]*\s*$', rest, re.I)
    if sw and sw.group(2):
        rest = rest[:sw.start()]
    sg = (sw.group(1) or sw.group(2)) if sw else None
    to = (ckey(sg) or lend_token(sg)) if sg else coin
    if not lend and sw and sw.group(2):
        plain_to = re.match(r'^\s*(.+?)\s*[?.!]*\s*$', rest)
    where = (lend.group(1) if lend else plain_to.group(1)) or ''
    return {'coin': coin, 'amount': m.group(1), 'src': m.group(3), 'to': to, 'where': where}


# The move card for move_parse's reading: picks the lent row (market / protocol family), or asks which one.
def move_card(mv, address):
    coin, to = mv['coin'], mv['to']
    if not to:
        return {'text': "I can't swap into that coin. Which coin should it become?"}
    mine = [r for r in lend_positions_rows(address) if r['coin'].lower() == coin.lower()]
    src = mv['src'].strip().lower()
    fam = re.match(r'^(suilend|navi)\b(?!\s+(?:main|ember|matrix|high|prime|sui))', src)
    if fam:
        mine = [r for r in mine if r['id'].split('-')[0] == fam.group(1)]
    else:
        k = chain_market(src, coin)[0] or market_in(src)
        if k:
            mine = [r for r in mine if r['id'] == k]       # never fall back to another position (it picked NAVI main for 'NAVI SUI High Yield')
    if not mine:
        return {'text': 'You have no %s lent on %s through Kivy.%s' % (disp(coin), mv['src'], lent_hint(address, coin, lead=': '))}
    row = max(mine, key=lambda r: r['lent'] * (r.get('px') or 0))
    rows = [r for r in lend_rows_for(to) if not (r['market'] == row['id'] and to == coin)]
    w = mv['where'].lower()
    wf = re.search(r'\b(suilend|navi)\b', w)
    spec = market_in(w) if re.search(r'\b(main|ember|matrix|high\s*yield|prime)\b', w) else None
    if to == 'SUI' and spec:
        spec = {'navi-highyield': 'navi-sui-yield', 'navi-prime': 'navi-sui-prime'}.get(spec, spec)
    pick = next((r for r in rows if r['market'] == spec), None) if spec else \
        (fam_pick(rows, wf.group(1)) if wf else (rows[0] if rows else None))   # a bare protocol: its main market first (review P-13)
    if not pick:
        if not rows:                                  # the only market for it is where it already is
            return {'text': '%s can only be lent on %s, where it already is. To move it, swap it into another coin, e.g. "move my %s from %s to navi as USDC".'
                            % (disp(to), row['name'], disp(coin), mv['src'])}
        return {'text': '%s is not lendable on %s. It is on: %s.' % (disp(to), mv['where'] or 'that market',
                ', '.join(LEND_MKTS.get(r['market'], r['name']) for r in rows))}
    tgt = pick['market']
    card = withdraw_card(row, to if to != coin else None)
    wd = card['widget']
    wd.update(lend_to=tgt, lend_name=LEND_MKTS.get(tgt, pick['name']), lend_apr=round(float(pick['total_apr']), 2), to=to,
              lend_fee_pct=rev_lend_pct(), to_opts=[to])
    if mv['amount'] and mv['amount'].lower() in ('half',):
        wd['preset'] = round(row['lent'] / 2, 9)
    elif mv['amount'] and re.match(r'^[\d.]+$', mv['amount']):
        wd['preset'] = float(mv['amount'])
    else:
        wd['preset'] = row['lent']                      # "all of it" / no amount: the whole position
    steps = ['withdraw %s from %s' % (disp(coin), row['name'])] + (['swap it to %s' % disp(to)] if to != coin else []) + \
            ['lend it on %s at %.2f%% a year' % (wd['lend_name'], wd['lend_apr'])]
    card['text'] = 'Move in one approval: %s. If any step fails, nothing happens.' % ', '.join(steps)
    return card


def lent_hint(address, coin, skip=None, lead=' — withdraw it first, e.g. '):
    """' Your 1.995 USDsui is lent on Suilend Ember — withdraw it first, e.g. "…".' or '' (2026-11-03: "send 0.1 usdsui" only said none)."""
    try:
        rows = [r for r in lend_positions_rows(address) if str(r.get('coin')).lower() == str(coin).lower() and float(r.get('lent') or 0) > 0
                and r.get('id') != skip]
    except Exception:
        return ''
    if not rows:
        return ''
    r = max(rows, key=lambda x: float(x.get('lent') or 0))
    return ' Your %s %s is lent on %s%s"withdraw %s %s from %s".' % (_gnum(float(r['lent'])), disp(coin), r['name'], lead, _gnum(float(r['lent'])), disp(coin), r['name'])


def lend_positions_rows(address):
    """Every coin lent (USDC + others), as withdraw rows."""
    pos = lend_positions(address, fresh=True)
    rows = [{'id': p, 'name': PROTOS[p], 'total': round(apr_of(p) or 0, 2), 'lent': round(v, 6), 'coin': 'USDC', 'px': 1.0, 'icon': '/icons/USDC.svg'}
            for p, v in ((p, float((pos.get(p) or {}).get('usdc') or 0)) for p in PROTOS) if v >= 0.01]
    return rows + other_lent(address, fresh=True)


def wd_preset(card, msg):
    """"withdraw ALL my usdc from navi": the slider starts at the whole position (it started at 0 — live test 2026-10-25); "half" at half."""
    w = card.get('widget') or {}
    if w.get('type') == 'withdraw' and not w.get('preset'):
        if re.search(r'\b(?:all|everything|max|entire|whole|full)\b', msg, re.I):
            w['preset'] = w.get('max') or 0
        elif re.search(r'\bhalf\b', msg, re.I):
            w['preset'] = round(float(w.get('max') or 0) / 2, 9)
        elif AM.first_pct(msg) is not None and 0 < AM.first_pct(msg) <= 1:   # "withdraw 25% of my usdsui" — never a rate ("…, the apy is 5%")
            w['preset'] = round(float(w.get('max') or 0) * AM.first_pct(msg), 9)
        elif not re.search(r'\d|\b(?:some|part|bit|little)\b', AM.plain_text(msg), re.I) and not AM.unread(msg):   # a rate or a date is not an amount; "a fifth" is not all (RDR-V3)
            w['preset'] = w.get('max') or 0           # no amount = all of it (KIVYSOUL: "A withdrawal with no amount = all of it")
        else:                                         # "withdraw 0.05 sui from navi sui high yield": the typed amount of that coin (2026-10-27)
            m = re.search(r'(?<![\d.$])(\d+(?:\.\d+)?)\s*(?:%s)\b(?!\s*%%)' % re.escape(str(w.get('coin') or '')), msg, re.I)
            if m and 0 < float(m.group(1)) <= float(w.get('max') or 0) + 1e-9:
                w['preset'] = float(m.group(1))
    return card


# The withdraw slider for one lent row (optionally 'then swap to X'); the card posts to /api/tx/lend or /api/tx/withdrawswap.
def withdraw_card(row, to=None):
    to = to if to in to_opts(row['coin']) else row['coin']
    return {'text': 'Slide to choose how much %s to withdraw from %s%s.' % (disp(row['coin']), row['name'],
                                                                           (', then swap it to %s — one approval' % disp(to)) if to != row['coin'] else ''),
            'widget': {'type': 'withdraw', 'coin': row['coin'], 'protocol': row['id'], 'name': row['name'], 'apr': row['total'],
                       'px': row['px'], 'balance': row['lent'], 'max': row['lent'], 'to': to, 'to_opts': to_opts(row['coin']), 'fee_pct': rev_fee_pct()}}


def lend_positions(addr, fresh=False):
    """{market: {market, name, usdc}} — USDC lent per market, from the same read as every other coin: positions-all computes
    USDC with the same function as 'positions' (verified on a live NAVI deposit 2026-10-25) — one chain scan, not two (W-04)."""
    hit = _LEND['pos'].get(addr)
    if pos_cached(hit, addr, fresh):
        return hit[1]
    try:
        rows = lend_positions_all(addr, fresh)
    except Exception:                                    # one retry: these reads fail now and then (RPC hiccups)
        time.sleep(1)
        rows = lend_positions_all(addr, True)
    v = {p: {'market': p, 'name': PROTOS[p], 'usdc': round(sum(float(r.get('amount') or 0) for r in rows if r.get('market') == p and str(r.get('token')).upper() == 'USDC'), 9)}
         for p in PROTOS}
    _LEND['pos'][addr] = (time.time(), v)
    return v


# The USDC market paying the most right now (NAVI if rates can't be read).
def best_proto():
    try:
        r = lend_rates()
        return max(PROTOS, key=lambda p: float((r.get(p) or {}).get('total_apr') or 0))
    except Exception:
        return 'navi'


# A USDC market's total yearly rate, %, or None when unreadable.
def apr_of(p):
    try:
        return float((lend_rates().get(p) or {}).get('total_apr') or 0)
    except Exception:
        return None


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .readguard import _gnum  # noqa: E402
from .chainreads import _BAL, _PX, all_balances, cinfo, ckey, coin_icon, dec, prices, token_prices  # noqa: E402
from .chains import CHAIN_PERPS, chain_market, fam_pick  # noqa: E402
from .txreads import rev_fee_pct, rev_lend_pct  # noqa: E402
