"""The confirm card plan() for one swap/send/lend/withdraw, amount words, multi-send (Section 17).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# plan() is called by reply(), the AI's plan tools, the send readers and again by the /api/tx/* routes (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import math
import re
from . import amounts as AM
from .config import K, SPOT_IN, cap_usd, disp, disp_text


WORD_AMOUNTS = {'all': 1.0, 'max': 1.0, 'everything': 1.0, 'entire': 1.0, 'half': 0.5, 'a quarter': 0.25, 'quarter': 0.25, 'a third': 1 / 3, 'third': 1 / 3}


def share_said(msg):
    """The share of a holding a request names — "50%" -> 0.5, "half", "a third", "a quarter" — else None. For card presets: a
    percent was read as that many coins and capped at the whole balance ("lend 50% of my usdc on navi" opened at ALL, 2026-11-13)."""
    m = re.search(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(?:%|percent\b|pct\b)', AM._mask(msg or '', prices=False), re.I)   # never a rate ("at 5% apy")
    if m:
        return float(m.group(1)) / 100 if 0 < float(m.group(1)) <= 100 else None
    w = re.search(r'\b(half|(?:a|one)\s+third|(?:a|one)\s+quarter)\b', msg or '', re.I)
    return {'h': 0.5, 't': 1 / 3, 'q': 0.25}[w.group(1).split()[-1][0].lower()] if w else None


def word_amount(word, coin, holdings):
    """'all BTC' -> the wallet's whole BTC (SUI keeps a little back for gas). None if unknown."""
    w = ' '.join(str(word).lower().split())
    pc = re.match(r'^(\d+(?:\.\d+)?)\s*%$', w)          # "10%" of the coin (2026-10-25: it was read as 10 coins)
    share = (float(pc.group(1)) / 100 if 0 < float(pc.group(1)) <= 100 else None) if pc else WORD_AMOUNTS.get(w)
    if share is None or holdings is None:
        return None
    have = float(holdings.get(coin) or 0.0)
    if coin == 'SUI':
        have = max(0.0, have - max(float(K()['gas_sui']) * 3, 0.01))
    return have * share


# The confirm card ('plan') for one swap / send / USDC lend / withdraw: allowed coins, $ cap, balance, gas, recipient, then
# title, steps, risks and rows. Called by reply(), the AI's plan_* tools, send readers and again by POST /api/tx/swap,
# /api/tx/lend and /api/tx/check-send (the same rules re-checked at build time). Never builds or signs anything.
def plan(kind, amt, a, b=None, holdings=None, recipient=None, protocol=None, lent=None, all_=False, sender=None):
    k = K()
    ASSETS, CAP_USD, VENUES = k['assets'], cap_usd(), k['venues']
    GAS_SUI, SLIPPAGE_CAP = float(k['gas_sui']), float(k['slippage_cap_pct'])
    a, b = SPOT_IN.get(a, a), SPOT_IN.get(b, b) if b else b
    a, b = ckey(a) or a, (ckey(b) or b) if b else b       # 'USDSUI' -> 'USDsui'
    bad = [x for x in (a, b) if x and x not in ASSETS and not (kind in ('swap', 'send') and x in ecoins())]
    if bad:
        return {'text': "I can't do that: %s isn't on my allowed list (%s)."
                        % (', '.join(bad), ', '.join(ASSETS))}
    if kind in ('lend', 'withdraw'):
        if a != 'USDC':                                    # other coins use the lending card
            return {'text': 'Say "lend %s" to open its lending card (best market first), or tap it in "lending markets".' % a}
        protocol = (protocol or '').lower() or None
        if protocol and protocol not in PROTOS:
            protocol = market_in(protocol)
            if not protocol:
                return {'text': 'I can lend USDC on: %s.' % ', '.join(PROTOS.values())}
        if kind == 'lend' and not protocol:
            protocol = best_proto()
        if kind == 'withdraw':
            lent = lent or {}
            have_lent = {p: float((lent.get(p) or {}).get('usdc') or 0) for p in PROTOS}
            have_lent = {p: (v if v >= 0.01 else 0.0) for p, v in have_lent.items()}   # ignore rounding dust
            if not protocol:
                protocol = max(have_lent, key=have_lent.get)
            if have_lent.get(protocol, 0) <= 0:
                app = float((lent.get(protocol) or {}).get('in_app') or 0)
                return {'text': ('You have no USDC in %s through Kivy.' % PROTOS[protocol])
                                + (' Your %s USDC in a Suilend account is withdrawn in the Suilend app.' % dec(app) if app else '')}
            if all_:
                amt = have_lent[protocol]
            elif amt > have_lent[protocol] + 1e-9:
                return {'text': 'You have %s USDC lent on %s — try that much or less.' % (dec(have_lent[protocol]), PROTOS[protocol])}
    if kind == 'swap' and a == b:
        return {'text': 'Swapping %s to %s does nothing — pick two different coins.' % (a, b)}
    if not math.isfinite(amt):
        return {'text': 'That amount is not a number I can use.'}
    if amt <= 0:
        if holdings is not None and kind in ('swap', 'send', 'lend') and not float(holdings.get(a) or 0):
            return {'text': 'You have 0 %s, so there is nothing to %s.' % (a, kind)}
        return {'text': 'The amount has to be more than zero.'}
    px = prices()
    p_a = px.get(a) or (coin_px(a) if a not in ASSETS else None)
    if CAP_USD and kind != 'withdraw' and not (isinstance(p_a, (int, float)) and math.isfinite(p_a) and p_a > 0):
        return {'text': "I can't get a live %s price right now, so I can't check the $%.0f cap. Try again in a minute." % (a, CAP_USD)}
    usd = amt * p_a if p_a else None
    if CAP_USD and usd is not None and usd > CAP_USD and kind != 'withdraw':   # taking your own money back is never capped
        return {'text': "That's about $%.2f, over my $%.0f cap. I won't plan it — try a smaller amount."
                        % (usd, CAP_USD)}
    gas_usd = GAS_SUI * px.get('SUI', 1.0)
    have = None
    to_addr = None
    if kind == 'send':
        to_addr = resolve_recipient(recipient)
        if not to_addr:
            return {'text': "I couldn't read that recipient. Give a full 0x… address (64 characters) or a .sui name."}
        if sender and to_addr.lower() == sender.lower():   # never a card that sends to the sending account itself (2026-10-21)
            accts = [x for x in xw_accounts(sender) if x['a'].lower() != sender.lower()]
            return {'text': 'That address is this account (%s…%s) — sending there moves nothing.%s' % (sender[:6], sender[-4:],
                            (' To send from your other account %s…%s instead, switch to it in your wallet and ask again.' % (accts[0]['a'][:6], accts[0]['a'][-4:]))
                            if accts else ' Paste the address you want to send to.')}
    if holdings is not None and kind in ('swap', 'lend', 'send'):
        have = float(holdings.get(a) or 0.0)
        spare = have - (max(GAS_SUI * 3, 0.01) if a == 'SUI' else 0.0)     # keep a little SUI for gas
        if amt > spare + 1e-9:                         # float room: "all" = balance − 0.01 must pass (0.912597 − 0.01 = 0.90259699…)
            return {'text': 'Your wallet has %s %s%s, not enough for %s. Try a smaller amount.'
                            % (dec(have), a, ' (a little SUI is kept back for gas)' if a == 'SUI' else '', dec(amt))}
    held = ('Your wallet holds %s %s — enough ✓' % (dec(have), a)) if have is not None else None
    if kind == 'swap':
        p_b = px.get(b) or coin_px(b)
        out = (usd / p_b) if (usd is not None and p_b) else None
        steps = [held or 'Check the wallet holds %s %s' % (dec(amt), a),
                 'Get a route %s → %s from the %s' % (a, b, VENUES['swap']),
                 'Swap %s %s for about %s %s (refuse if slippage > %g%%)'
                 % (dec(amt), a, dec(out) if out else '?', b, SLIPPAGE_CAP),
                 'Confirm the %s arrived and report the real amount' % b]
        risks = ['Price can move before the swap lands — capped at %g%% slippage' % SLIPPAGE_CAP,
                 'Router fees are included in the quote, not on top']
        title = 'Swap %s %s → %s' % (dec(amt), a, b)
    elif kind == 'send':
        steps = [held or 'Check the wallet holds %s %s' % (dec(amt), a),
                 'Send %s %s to %s' % (dec(amt), a, to_addr),
                 'Confirm it left the wallet and report the transaction']
        risks = ['A transfer cannot be undone: check the full address above, character by character',
                 'Sending to an exchange? Make sure it supports %s on the Sui network' % a]
        title = 'Send %s %s' % (dec(amt), a)
        if recipient and not ADDR_RE.match(recipient.strip()):
            title += ' to %s' % recipient.strip()
    elif kind == 'lend':
        apr = apr_of(protocol)
        steps = [held or 'Check the wallet holds %s %s' % (dec(amt), a),
                 'Deposit %s %s into %s%s' % (dec(amt), a, PROTOS[protocol], (' — earns about %.2f%% a year' % apr) if apr else ''),
                 'Supply only: Kivy never borrows. Withdraw any time']
        risks = ['Rates change with demand', 'If %s were hacked, deposits could be lost' % PROTOS[protocol]]
        title = 'Lend %s %s on %s' % (dec(amt), a, PROTOS[protocol])
    else:
        steps = ['You have %s USDC lent on %s' % (dec(float((lent.get(protocol) or {}).get('usdc') or 0)), PROTOS[protocol]),
                 'Withdraw %s %s back to your wallet' % ('all' if all_ else dec(amt), a)]
        risks = ['A pool near full use may not return everything at once']
        title = 'Withdraw %s %s from %s' % ('all' if all_ else dec(amt), a, PROTOS[protocol])
    action = None
    if kind == 'swap':
        action = {'kind': 'swap', 'from': a, 'to': b, 'amount': amt, 'label': disp_text('Swap %s %s' % (dec(amt), a))}   # verb + amount (UI review 2026-11-04)
    elif kind == 'send':
        action = {'kind': 'send', 'coin': a, 'amount': amt, 'to': to_addr, 'label': disp_text('Send %s %s' % (dec(amt), a))}
    elif kind in ('lend', 'withdraw'):
        action = {'kind': 'lend', 'protocol': protocol, 'name': PROTOS[protocol], 'action': 'deposit' if kind == 'lend' else 'withdraw',
                  'amount': 'all' if all_ else amt,
                  'label': disp_text('%s %s %s' % ('Lend' if kind == 'lend' else 'Withdraw', 'all' if all_ else dec(amt), a))}
    title, steps = disp_text(title), [disp_text(x) for x in steps]
    rows = plan_rows(kind, a, b, amt, to_addr, protocol, all_, usd, px, lent)
    return {'text': k['plan_intro'],
            'plan': {'title': title, 'value_usd': round(usd, 2) if usd is not None else None,
                     'amount': None if all_ else dec(amt), 'coin': disp(a),
                     'action': action, 'to_full': to_addr, 'rows': rows,
                     'venue': (('%s · %.2f%% a year' % (PROTOS[protocol], apr_of(protocol) or 0)) if kind in ('lend', 'withdraw')
                               else VENUES.get(kind, 'Sui network')), 'steps': steps, 'risks': risks,
                     'cost': 'Free' if kind == 'send' and gasless_send(a, amt)
                             else 'Est. %s SUI' % dec(GAS_SUI) + (' + Kivy fee %g%%' % rev_fee_pct() if kind == 'swap' and rev_fee_pct() else '')
                                  + (' + Kivy fee %g%%' % rev_lend_pct() if kind == 'lend' and rev_lend_pct() else ''),
                     'status': k['plan_status']}}


def plan_rows(kind, a, b, amt, to_addr, protocol, all_, usd, px, lent):
    """The card's icon rows (2026-10-22: every confirm card in the icon style of the multi-step card)."""
    sh = lambda x: '%s…%s' % (x[:6], x[-4:])
    row = lambda c, t, sub='', proto=None: {'coin': disp(c), 'icon': coin_icon(c), 'title': disp_text(t), 'sub': sub, 'proto': proto}
    try:
        if kind == 'swap':
            p_b = px.get(b) or coin_px(b)
            out = (usd / p_b) if (usd is not None and p_b) else None
            return [row(a, 'Pay %s %s' % (dec(amt), a), ('≈$%.2f' % usd) if usd is not None else ''),
                    row(b, 'Receive ≈ %s %s' % (dec(out), b) if out else 'Receive %s' % b,
                        'at most %g%% slippage' % float(K()['slippage_cap_pct']))]
        if kind == 'send':
            return [row(a, 'Send %s %s' % (dec(amt), a), 'to %s' % sh(to_addr or ''))]
        if kind == 'lend':
            apr = apr_of(protocol)
            return [row(a, 'Lend %s %s on %s' % (dec(amt), a, PROTOS[protocol]), ('%.2f%% a year' % apr) if apr else '', protocol)]
        if kind == 'withdraw':
            have = float(((lent or {}).get(protocol) or {}).get('usdc') or 0)
            return [row(a, 'Withdraw %s %s from %s' % ('all' if all_ else dec(amt), a, PROTOS[protocol]),
                        'back to your wallet' + (' · %s lent now' % dec(have) if have else ''), protocol)]
    except Exception:
        pass
    return None


_RCPT = '(' + AM.RCPT + ')'                       # the one recipient pattern: exactly 64 characters, never the first 64 of more (R3)


def send_multi_parse(msg, rcpts, address):
    """Several recipients in one message, each with an amount (or one amount "each"): one Send card, ONE transaction.
    None when the words don't pair every recipient with an amount — the caller then asks which one."""
    if AM.long_address(msg):
        return {'text': AM.long_address(msg)}
    two = AM.clause_conflict(msg)                    # "…, 2 sui to alice.sui and 3 4 sui to carol.sui": 3 or 4? never the one next to
    if two:                                          # the coin (round 2, 2026-11-16)
        return two
    bare = next((a for a in AM.amounts(msg) if a['kind'] == 'usd' and not a['coin']), None)
    if bare:                                         # "send $5 of sui to A and $3 to B" sent B 3 SUI: dollars of which coin? ("$N of
        n = AM._plain(bare['v'])                     # COIN" is converted before this reader runs — read_guard)
        return {'text': '"$%s" of which coin? Say it for each recipient — e.g. "$%s of SUI to …" — or give the amounts in coins.' % (n, n)}
    segs, last = [], None
    for a_, c_, w_ in re.findall(r'(\d+(?:\.\d+)?)\s+(?:(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+)?to\s+' + _RCPT, msg, re.I):
        c_ = c_ or last                                  # "send 1 sui to A and 2 to B": the bare 2 is SUI too (review P-11)
        if not c_:
            segs = []
            break
        last = c_
        segs.append((a_, c_, w_))
    sp = re.search(r'(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:(?:split|divided|shared)\s+)?(?:equally\s+|evenly\s+)?between\b', msg, re.I)
    if sp and len(segs) < 2 and (ckey(sp.group(2)) or lend_token(sp.group(2))):   # "5 usdc split between A and B": ask, never all to one
        c, a = ckey(sp.group(2)) or lend_token(sp.group(2)), float(sp.group(1))
        who = ' and '.join(rcpts)
        return {'text': 'How should %s %s be shared?' % (num_plain(a), disp(c)), 'widget': {'type': 'choose', 'options': [
            {'label': 'Split: %s %s each' % (_gnum(a / len(rcpts)), disp(c)), 'ask': 'send %s %s each to %s' % (_gnum(a / len(rcpts)), c, who)},
            {'label': '%s %s each' % (num_plain(a), disp(c)), 'sub': '%s %s in total' % (num_plain(a * len(rcpts)), disp(c)), 'ask': 'send %s %s each to %s' % (num_plain(a), c, who)}]}}
    each = re.search(r'(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:each\s+to|to\s+each\s+of)\s+(.+)$', msg, re.I)
    if each:
        segs = [(each.group(1), each.group(2), r) for r in rcpts]
    if len(segs) != len(rcpts) or len(segs) < 2:
        return None
    hold = dict(all_balances(address) or {})
    lines, need = [], {}
    for amt, coin, who in segs:
        c = ckey(coin) or lend_token(coin)
        if not c:
            return {'text': "%s isn't a coin I can send. Check the name and ask again." % coin}
        to = resolve_recipient(who)
        if not to:
            return {'text': 'I couldn\'t find %s. Give a full 0x… address or a .sui name that exists.' % who}
        if to.lower() == address.lower():
            return {'text': 'One of those is your own wallet (%s…%s) — sending there moves nothing. Remove it and ask again.' % (address[:6], address[-4:])}
        a = float(amt)
        if not a > 0:
            return {'text': 'Every transfer needs an amount above zero.'}
        need[c] = need.get(c, 0.0) + a
        lines.append({'source': 'wallet', 'coin': c, 'amount': a, 'to': to, 'name': who})
    if len(lines) > 12:
        return {'text': 'Send 1 to 12 transfers at a time — split it into two requests.'}
    for c, a in need.items():                        # all the transfers of one coin together must fit (SUI keeps a little for gas)
        have = spendable(c, hold)
        if a > have + 1e-9:
            return {'text': 'Those transfers need %s %s in total, but your wallet has %s%s.' % (dec(a), disp(c), dec(have), ' (a little SUI is kept for gas)' if c == 'SUI' else '')}
    usd = sum(l['amount'] * (1.0 if l['coin'] in ('USDC', 'USDsui') else float(coin_px(l['coin']) or 0)) for l in lines)
    if cap_usd() and usd > cap_usd() + 0.005:
        return {'text': "That's about $%.2f in total, over my $%.0f cap. Try smaller amounts." % (usd, cap_usd())}
    sh = lambda a: a if not ADDR_RE.match(a) else a[:6] + '…' + a[-4:]
    k = K()
    return {'text': 'Here\'s the plan: %d transfers in one transaction — you approve once, and if any part fails nothing is sent.' % len(lines),
            'plan': {'title': disp_text('Send ' + ' + '.join('%s %s to %s' % (dec(l['amount']), l['coin'], sh(l['name'] if not ADDR_RE.match(l['name']) else l['to'])) for l in lines)),
                     'value_usd': round(usd, 2), 'amount': None, 'coin': None,
                     'action': {'kind': 'sendmany', 'lines': [{'source': 'wallet', 'coin': l['coin'], 'amount': l['amount'], 'to': l['to']} for l in lines],
                                'label': 'Send all %d' % len(lines)},
                     'rows': [{'coin': disp(l['coin']), 'icon': coin_icon(l['coin']), 'title': disp_text('Send %s %s' % (dec(l['amount']), l['coin'])),
                               'sub': 'to %s' % sh(l['name'] if not ADDR_RE.match(l['name']) else l['to']), 'proto': None} for l in lines],   # icon rows like every confirm card
                     'venue': 'Sui network · one transaction',
                     'to_list': ['%s · %s %s' % (l['to'], dec(l['amount']), disp(l['coin'])) for l in lines],   # every full address, shown above Approve
                     'steps': [disp_text('Send %s %s to %s' % (dec(l['amount']), l['coin'], l['to'])) for l in lines] +
                              ['All in one transaction: one approval, and if any part fails nothing is sent'],
                     'risks': ['A transfer cannot be undone: check every full address above, character by character',
                               'Sending to an exchange? Make sure it supports the coin on the Sui network'],
                     'cost': 'Est. %s SUI' % dec(float(k['gas_sui'])), 'status': k['plan_status']}}


def plan_send_many(items, recipient, holdings, sender):
    """Several coins to one recipient in ONE transaction, one approval (2026-10-22: 'send several assets at the same time').
    items = [(coin, amount)]. Each coin passes the single-send checks; the $ cap applies to the total."""
    k = K()
    parts, usd = [], 0.0
    for c, amt in items:
        one = plan('send', amt, c, recipient=recipient, holdings=holdings, sender=sender)
        if not one.get('plan'):
            return one
        parts.append(one['plan'])
        usd += one['plan'].get('value_usd') or 0.0
    if cap_usd() and usd > cap_usd() + 0.005:
        return {'text': "That's about $%.2f in total, over my $%.0f cap. I won't plan it — send one coin at a time or a smaller amount." % (usd, cap_usd())}
    to_addr = parts[0]['to_full']
    what = ' + '.join('%s %s' % (dec(a), disp(c)) for c, a in items)
    title = disp_text('Send %s' % what)
    if recipient and not ADDR_RE.match(recipient.strip()):
        title += ' to %s' % recipient.strip()
    return {'text': 'Here\'s the plan: every coin in one transaction. Nothing happens until you approve it in your wallet — once.',
            'plan': {'title': title, 'value_usd': round(usd, 2), 'amount': None, 'coin': None,
                     'action': {'kind': 'send', 'items': [{'coin': c, 'amount': a} for c, a in items], 'to': to_addr,
                                'label': 'Send %d coins' % len(items) if len(items) > 1 else 'Send'},
                     'to_full': to_addr, 'venue': 'Sui network · one transaction',
                     'rows': [r for p in parts for r in (p.get('rows') or [])],
                     'steps': [disp_text('Send %s %s to %s' % (dec(a), c, to_addr)) for c, a in items] +
                              ['All in one transaction: one approval, and if any part fails nothing is sent'],
                     'risks': ['A transfer cannot be undone: check the full address above, character by character',
                               'Sending to an exchange? Make sure it supports each of these coins on the Sui network'],
                     'cost': 'Est. %s SUI' % dec(float(k['gas_sui'])), 'status': k['plan_status']}}


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import num_plain  # noqa: E402
from .readguard import _gnum  # noqa: E402
from .chainreads import ADDR_RE, all_balances, ckey, coin_icon, dec, ecoins, prices  # noqa: E402
from .fallback import gasless_send, resolve_recipient  # noqa: E402
from .lending import PROTOS, apr_of, best_proto, coin_px, lend_token, market_in, spendable  # noqa: E402
from .txreads import rev_fee_pct, rev_lend_pct  # noqa: E402
from .sends import xw_accounts  # noqa: E402
