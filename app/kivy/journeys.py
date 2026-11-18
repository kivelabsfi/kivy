"""User journeys: whole-wallet questions answered from live data, no AI: journey_answers() (Section 5).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# journey_answers / amount_followup are called by widget_reply; recent_activity and coin_rates by the AI tools.
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import re
import time
from . import perpacct as PERPACCT
from .config import K, disp, unknown_coin
from .policy import type_key
from .upstream import lend_cli

widget_reply = None        # server.py's (Section 7): set by server.py once it is defined (a module never imports server.py)


# ── Section 5: user journeys — whole-wallet questions answered from live data, no AI; entry point journey_answers(),
#    called by widget_reply. Reads builder.ts (positions, perps), Sui GraphQL (activity), lend_rates_all ──
# ── User journeys answered by Kivy itself (2026-10-24, user-scenario review: these all went to the AI with little data) ──
# 1 exit everything · 2 best rate / move to the best market (every coin) · 3 activity + earnings · 4 common questions by code
EXIT_RE = re.compile(r"^\s*(?:please\s+)?(?:(?:get|take)\s+me\s+out(?:\s+of)?(?:\s+(?:everything|it\s+all|all(?:\s+of\s+it)?|all\s+my\s+positions|my\s+positions|this))?"
                     r"|i\s+want\s+(?:to\s+get\s+)?out(?:\s+of\s+everything)?|exit\s+(?:everything|all(?:\s+(?:my\s+)?positions)?|it\s+all)"
                     r"|(?:cash|pull)\s+out\s+(?:of\s+)?(?:everything|it\s+all|all)|close\s+(?:out\s+)?everything"
                     r"|(?:withdraw|take\s+out|pull)\s+(?:out\s+)?(?:everything|it\s+all|all\s+my\s+(?:money|funds|positions))"
                     r"|(?:sell|convert|turn|move|put)\s+(?:everything|it\s+all|all\s+my\s+(?:money|funds|coins|positions))\s+(?:in)?to\s+(?:usdc|stables?|stablecoins?|cash)"
                     r")(?:\s+(?:in)?to\s+(?:usdc|stables?|stablecoins?|cash))?(?:\s+(?:now|please|asap|right\s+now))*\s*[?.!]*\s*$", re.I)


def exit_reply(address):
    """"Get me out of everything": every position as one ordered list, each step one approval with Kivy's usual checks.
    Order: close perps (frees the margin), margin to the wallet, then each lent coin withdrawn as USDC."""
    if not address:
        return {'text': K()['no_wallet']}
    opts = []
    try:
        pp = lend_cli('perp-positions', address, timeout=40).get('positions') or []
    except Exception:
        pp = []
    if pp:
        opts.append({'label': 'Close %s' % ('your %s %s' % (pp[0]['symbol'], pp[0]['side']) if len(pp) == 1 else 'all %d perps positions' % len(pp)),
                     'sub': 'at market price', 'ask': 'close all my positions'})
    try:
        with PERPACCT.quiet():                           # the list of steps: each perps step asks which account itself (BLD-V1)
            free = float(perp_account(address, fresh=True).get('collateral') or 0)
    except Exception:
        free = 0
    if pp or free >= 0.01:
        opts.append({'label': 'Move perps margin to your wallet', 'sub': ('%s USDC free now' % dec(free, 6)) if free >= 0.01 else 'after closing',
                     'value': ('$%s' % dec(free, 4)) if free >= 0.01 else '',
                     'ask': 'withdraw all my margin from perps'})
    try:
        for p, v in (lend_positions(address, fresh=True) or {}).items():
            amt = float((v or {}).get('usdc') or 0)
            if amt >= 0.01:
                opts.append({'label': 'Withdraw USDC from %s' % PROTOS.get(p, p), 'sub': '%s USDC' % dec(amt, 6), 'value': '$%s' % dec(amt, 4),
                             'ask': 'withdraw all USDC from %s' % PROTOS.get(p, p)})
        for r in other_lent(address, fresh=True):
            opts.append({'label': 'Withdraw %s from %s as USDC' % (r['coin'], r['name']), 'sub': '%s %s' % (dec(r['lent'], 6), r['coin']),
                         'value': '$%s' % dec(r['lent'] * (r.get('px') or 0), 4), 'ask': 'withdraw all %s from %s as USDC' % (r['coin'], r['name'])})
    except Exception:
        pass
    if not opts:
        return {'text': "You have nothing in lending or perps — everything is already in your wallet."}
    n = len(opts)
    return {'text': ('Here is everything, in the order to do it. Each step is its own transaction: tap one, check it, approve it in your wallet.' if n > 1
                     else 'One step gets you out:') + ' Nothing moves until you approve.',
            'widget': {'type': 'choose', 'options': [dict(o, label='%d. %s' % (i + 1, o['label']) if n > 1 else o['label']) for i, o in enumerate(opts)]}}


BEST_RE = re.compile(r"\b(?:(?:move|switch|shift)\s+(?:all\s+)?(?:of\s+)?(?:my\s+)?(?P<c1>[a-z][a-z0-9]{1,9})\s+(?:in)?to\s+(?:the\s+)?(?:best|highest|top)\b"
                     r"|(?:is\s+there|any|find(?:\s+me)?|got)\s+(?:a\s+)?(?:better|higher)\s+(?:rate|yield|apy|apr|market|place)\s+(?:for|on)\s+(?:my\s+)?(?P<c2>[a-z][a-z0-9]{1,9})"
                     r"|(?:where|which\s+market)\s+(?:can|should|do)\s+i\s+(?:earn|get|make)\s+(?:the\s+)?(?:most|best|highest)(?:\s+(?:rate|yield|apy|interest))?\s+(?:on|for|with)\s+(?:my\s+)?(?P<c3>[a-z][a-z0-9]{1,9})"
                     r"|(?:best|highest|top)\s+(?:rate|yield|apy|apr|market)\s+(?:for|on)\s+(?:my\s+)?(?P<c4>[a-z][a-z0-9]{1,9}))", re.I)


def coin_rates(asset=None):
    """{market name: % a year} for one coin (any coin a market lists, 2026-10-24: the AI only knew USDC rates)."""
    tok = 'USDC' if not asset or str(asset).upper() == 'USDC' else lend_token(str(asset))
    if not tok:
        return {'error': '%s is not a coin any lending market lists' % asset}
    rows = [r for r in lend_rates_all() if not r.get('rate_only') and
            str({'USDSUI': 'USDsui'}.get(r.get('token'), r.get('token')) or r['asset']).lower() == tok.lower()]
    return {'coin': tok, 'apr_pct_by_market': {LEND_MKTS.get(r['market'], r.get('name') or r['market']): round(float(r['total_apr']), 2)
                                               for r in sorted(rows, key=lambda r: -float(r['total_apr']))}}


def best_reply(msg, address):
    """"Is there a better rate for my USDsui?", "move my USDsui to the best market": every market for that coin from live
    rates, where the user has it now, and the move one tap away when it pays (any coin, not only USDC)."""
    m = BEST_RE.search(msg)
    if not m:
        return None
    word = next(g for g in m.groups() if g)
    tok = 'USDC' if word.lower() == 'usdc' else lend_token(word)
    if not tok:
        return None
    nt = type_key
    rows = [r for r in lend_rates_all() if not r.get('rate_only') and
            str({'USDSUI': 'USDsui'}.get(r.get('token'), r.get('token')) or r['asset']).lower() == tok.lower()]
    if not rows:
        return None
    rows.sort(key=lambda r: -float(r['total_apr']))
    name = lambda r: LEND_MKTS.get(r['market'], r.get('name') or r['market'])
    best = rows[0]
    mine = []
    if address:
        try:
            if tok == 'USDC':
                mine = [{'id': p, 'name': PROTOS[p], 'lent': float(v.get('usdc') or 0), 'total': apr_of(p) or 0}
                        for p, v in (lend_positions(address) or {}).items() if float((v or {}).get('usdc') or 0) >= 0.01]
            else:
                mine = [r for r in other_lent(address) if str(r['coin']).lower() == tok.lower()]
        except Exception:
            mine = []
    top = '\n'.join('• %s — %.2f%% a year' % (name(r), float(r['total_apr'])) for r in rows[:4])
    opts = []
    if mine:
        lines = []
        for x in mine:
            gain = float(best['total_apr']) - float(x.get('total') or 0)
            if x['id'] == best['market'] or gain < 0.1:
                lines.append('Your %s %s on %s already earns about the best rate (%.2f%%).' % (dec(x['lent'], 6), disp(tok), x['name'], float(x.get('total') or 0)))
            else:
                yr = x['lent'] * (x.get('px') or 1) * gain / 100
                lines.append('Your %s %s on %s earns %.2f%%; %s pays %.2f%% — about $%s more a year.' % (
                    dec(x['lent'], 6), disp(tok), x['name'], float(x.get('total') or 0), name(best), float(best['total_apr']), dec(yr, 3)))
                opts.append({'label': 'Move it to %s' % name(best), 'sub': '%.2f%% a year · one approval' % float(best['total_apr']),
                             'ask': 'move all %s from %s to %s' % (disp(tok), x['name'], name(best))})
        text = '%s rates right now:\n%s\n\n%s' % (disp(tok), top, '\n'.join(lines))
    else:
        text = '%s rates right now:\n%s\n\nThe best is %s.' % (disp(tok), top, name(best))
        opts.append({'label': 'Lend %s on %s' % (disp(tok), name(best)), 'sub': '%.2f%% a year' % float(best['total_apr']), 'ask': 'lend %s on %s' % (disp(tok), name(best))})
    opts.append({'label': 'See all %s markets' % disp(tok), 'sub': 'with collateral', 'ask': 'lending markets for %s' % disp(tok)})
    return {'text': text + '\nRates change; the collateral column in the table shows each market\'s risk.', 'widget': {'type': 'choose', 'options': opts}}


ACT_RE = re.compile(r"^\s*(?:(?:show|list|see|check)\s+(?:me\s+)?)?(?:my\s+)?(?:recent\s+|latest\s+|last\s+(?:few\s+)?)?(?:history|activity|transactions|txs?|moves)\s*[?.!]*\s*$"
                    r"|\bwhat\s+(?:did|have)\s+i\s+(?:do|done|did)\b|\bwhere\s+(?:did|has|is)\s+(?:all\s+)?my\s+(?:money|funds|usdc|sui|coins?|\w{2,10})\s+(?:go|gone)\b"
                    r"|\bwhat\s+happened\s+(?:to|with)\s+my\s+(?:money|funds|coins?|usdc|sui|\w{2,10})\b|\bi\s+(?:didn'?t|did\s+not|never)\s+(?:get|receive)\s+(?:my\s+)?\w+"
                    r"|\bmy\s+(?:last\s+)?(?:transaction|txn|tx|swap|send|order)\s+(?:failed|didn'?t\s+work|did\s+not\s+work)\b|"
                    r"\b(?:latest|last|recent)\s+(?:tx|txn|transaction|swap|send)s?\s+status\b|\bstatus\s+of\s+my\s+(?:last\s+|latest\s+)?(?:tx|txn|transaction|swap|send)\b|"
                    r"\b(?:was|is)\s+(?:that|my|the)\s+(?:last\s+|latest\s+)?(?:tx|txn|transaction|swap|send|transfer)\s+(?:confirmed|through|done|stuck|pending)\b|"
                    r"\bwhat\s+transactions\s+did\s+i\b|(?:^|[,;.]\s*)did\s+(?:that|it)\s+(?:go\s+through|work|land|confirm)\b|\bwhy\s+was\s+my\s+(?:transaction|tx|txn|swap|send|withdrawal?|deposit|transfer)\s+(?:rejected|refused|declined|failed)\b|"
                    r"\b(?:last|latest)\s+\d{1,2}\s+(?:things|moves|transactions|txs|txns)\b|\b(?:check|see|show)\s+(?:me\s+)?my\s+(?:recent\s+|latest\s+|last\s+(?:\d+\s+|few\s+)?)?(?:wallet\s+)?(?:activity|transactions|txs|txns|history)\b|\bwhy\s+did\s+(?:my|the)\s+(?:last\s+|latest\s+|recent\s+)?(?:transaction|tx|swap|send|order|transfer|deposit|withdrawal?|move)\s+fail\b"
                    r"|\bwhy\s+(?:has|have|is|was)\s+(?:my|the)\s+(?:last\s+|latest\s+)?(?:transaction|tx|swap|send)\s+(?:been\s+)?fail(?:ed|ing)?\b"
                    r"|\bdid\s+(?:my|the)\s+(?:last\s+|latest\s+|recent\s+)?(?:transaction|tx|swap|send|transfer|deposit|withdrawal?|move)\s+(?:go\s+through|work|succeed|land|arrive|confirm)\b"
                    r"|^\s*(?:show\s+)?my\s+last\s+(?:transaction|tx|swap|send|transfer|deposit|withdrawal|move)\s*[?.!]*\s*$|\bmy\s+(?:transaction|tx|swap|send)\s+(?:is\s+)?(?:stuck|pending)\b"
                    r"|\bdid\s+(?:she|he|they|it|the\s+money|the\s+coins?)\s+(?:get|receive)\s+(?:it|them|the\s+\w+|my\s+\w+)\b|\b(?:has|did)\s+(?:it|my\s+(?:send|transfer|payment))\s+(?:arrived?|landed?)\b", re.I)


def coin_name(ct):
    """A coin type NOT on Kivy's list, for the AI, the chat, receipts and Kivy's diary: 'unknown coin 0xdead…::usdc::USDC'
    (config.unknown_coin). Never its bare struct name: anyone can publish a coin named USDC and send it, and it read as
    "Received +1,000 USDC" (QA 2026-11-17 OPS-V1 / ATK-V3); never an instruction-shaped name either (review W2-14)."""
    return unknown_coin(ct)


def recent_activity(address, n=10):
    """The wallet's latest transactions from the chain: time, status, what moved for THIS wallet, Kivy's label."""
    q = ('query($a: SuiAddress!, $n: Int) { transactions(last: $n, filter: {affectedAddress: $a}) { nodes { digest sender { address } '
         'effects { status timestamp executionError { message abortCode module { name } } balanceChanges { nodes { owner { address } coinType { repr } amount } } } } } }')
    nodes = ((gql(q, {'a': address, 'n': n}) or {}).get('transactions') or {}).get('nodes') or []
    shadow_note('history', {'a': address, 'n': n, 'kind': 'affected', 'detail': 2}, nodes)
    norm = lambda x: re.sub(r'^0x0*', '0x', x or '')
    syms = {norm(v['type']): (sym, int(v['decimals'])) for sym, v in COINS().items()}
    try:
        for r in lend_rates_all():                    # lending coins' names too (USDsui, sSUI…)
            if r.get('type') and r.get('token'):
                syms.setdefault(norm(r['type']), ({'USDSUI': 'USDsui'}.get(r['token'], r['token']), int(r.get('decimals') or 9)))
    except Exception:
        pass
    try:                                              # and every other coin on Kivy's list (reward coins like BLUE, coin pins), as
        for sym, v in ecoins().items():               # tx_detail and the page do: a type off the list is now an "unknown coin",
            syms.setdefault(norm(v['type']), (sym, int(v['decimals'])))   # so a listed one must never fall through (fix package 4)
    except Exception:
        pass
    labels = tx_labels([x['digest'] for x in nodes], owner=address)   # only what THIS wallet signed (audit nv #8)
    out = []
    for x in reversed(nodes):
        e = x.get('effects') or {}
        mv = {}
        for b in (e.get('balanceChanges') or {}).get('nodes') or []:
            if norm((b.get('owner') or {}).get('address')) != norm(address):
                continue
            ct = norm(b['coinType']['repr'])
            if '::reserve::CToken<' in ct:
                continue
            sym, dp = syms.get(ct, (coin_name(ct), 9))
            mv[sym] = mv.get(sym, 0) + int(b['amount']) / 10 ** dp
        out.append({'digest': x['digest'], 'time': e.get('timestamp'), 'ok': str(e.get('status')).upper() == 'SUCCESS',
                    'mine': norm((x.get('sender') or {}).get('address')) == norm(address), 'label': labels.get(x['digest']), 'err': tx_err_plain(e.get('executionError')),
                    'moves': {k: v for k, v in mv.items() if abs(v) > 1e-9}})
    return out


def tx_err_plain(err):
    """A failed transaction's executionError from the chain, in plain words (never the raw abort text)."""
    if not err:
        return None
    m, mod = str(err.get('message') or ''), str(((err.get('module') or {}).get('name')) or '')
    step = re.search(r'Error in (\d+)\w* command', m)
    at = ' (step %s)' % step.group(1) if step else ''
    # first: Sui cancels a transaction when too many want the same market or pool at once (ExecutionCancelledDueTo
    # SharedObjectCongestion); its error lists the busy objects, which can look like a swap or pool failure (review study 2 §4)
    if re.search(r'Cancel\w*DueTo|SharedObjectCongestion|shared object|congest|locked', m, re.I):
        return 'Sui cancelled it because the network was busy with the same market at that moment' + at + ' — nothing moved except the network fee; try again in a minute'
    if re.search(r'InsufficientGas|gas', m, re.I):
        return 'there was not enough SUI left for the network fee' + at
    if re.search(r'Insufficient coin balance|InsufficientCoinBalance', m, re.I):
        return 'a coin balance was too small by the time it ran' + at + ' — usually a price moved or another transaction spent those coins first'
    if re.search(r'router|swap|aggregator|slippage|cetus|pool', mod + m, re.I):
        return 'the swap price moved past the minimum you approved' + at + ', so the swap stopped instead of paying you less'
    if mod:
        return 'the %s program refused it%s' % (mod, at)
    return 'the network stopped it' + at


# 'what did I do lately', 'my last transactions': the wallet's recent transactions as plain lines (journey_answers).
def activity_reply(msg, address):
    if not address:
        return {'text': K()['no_wallet']}
    try:
        acts = recent_activity(address, 8)
    except Exception:
        return {'text': "I couldn't read your activity from the chain just now — try again in a moment, or open the wallet's Activity tab."}
    if not acts:
        return {'text': 'No transactions for this wallet yet.'}
    acts = sorted(acts, key=lambda a: str(a.get('time') or ''))
    def line(a):
        t = str(a.get('time') or '')[:16].replace('T', ' ')
        mv = ', '.join('%s%s %s' % ('+' if v > 0 else '−', dec(abs(v), 6), disp(k)) for k, v in a['moves'].items()) or 'no coin change'
        what = re.sub(r'^(?:chain|compose)\s+', '', a.get('label') or ('sent by you' if a['mine'] else 'received'))
        what = {'sendmany': 'Send'}.get(what, what[:1].upper() + what[1:])
        return '• %s UTC — %s%s: %s' % (t, what, '' if a['ok'] else ' — FAILED (only the network fee was spent)', mv)
    day = re.search(r'\b(today|yesterday)\b', msg, re.I)
    if day:
        want = time.strftime('%Y-%m-%d', time.gmtime(time.time() - (86400 if day.group(1).lower() == 'yesterday' else 0)))
        on = [a for a in acts if str(a.get('time') or '').startswith(want)]
        if not on:
            return {'text': 'No transactions %s (UTC). Your latest:\n' % day.group(1).lower() + '\n'.join(line(a) for a in acts[-5:])}
        acts = on
    fails = [a for a in acts if not a['ok']]
    head = 'Your latest transactions (newest last):'
    if re.search(r'\bfail|didn\'?t\s+work|did\s+not\s+work', msg, re.I):
        f = fails[-1] if fails else None
        head = ('Your last failed one (%s UTC) failed because %s. A failed transaction changes nothing except the network fee. Your latest transactions:'
                % (str(f.get('time') or '')[:16].replace('T', ' '), f.get('err') or 'the network stopped it') if f else 'None of your recent transactions failed. Your latest:')
    elif re.search(r'\b(?:go\s+through|work|succeed|land|arrive|confirm\w*)\b', msg, re.I) and any(x['mine'] for x in acts):
        a = [x for x in acts if x['mine']][-1]           # "did my last transaction go through": the last one YOU sent, the answer first
                                                         # (an incoming transfer on top hid the answer — 2026-11-08)
        head = ('Yes — your last transaction (%s UTC) went through.' % str(a.get('time') or '')[:16].replace('T', ' ') if a['ok'] else
                'No — your last transaction (%s UTC) failed because %s. Only the network fee was spent.' % (str(a.get('time') or '')[:16].replace('T', ' '), a.get('err') or 'the network stopped it'))
        head += ' Your latest transactions:'
    elif re.search(r"\b(?:didn'?t|did\s+not|never)\s+(?:get|receive)\b", msg, re.I):
        head = ('Every coin that reached this wallet shows as + below. If something is missing, check the sender used this exact address; '
                'a transaction to another address can\'t be pulled back. Your latest:')
    return {'text': head + '\n' + '\n'.join(line(a) for a in acts[-8:]) + '\nTap a transaction in the wallet\'s Activity tab for every step.'}


DEFINE_Q_RE = re.compile(r"\bmeans?\s*[?.!]*\s*$|\bmeaning\b|\bstands?\s+for\b|\bdefin(?:e|ition)\b|\bwhat\s+does\b.{0,80}\bmean\b", re.I)
EARN_RE = re.compile(r"\bhow\s+much\s+(?:have\s+i|did\s+i|am\s+i|do\s+i|will\s+i)\s+(?:earn|earned|earning|make|made|making|get|getting)\b"
                     r"|\b(?:what(?:'s|\s+is|\s+are)\s+)?my\s+(?:earnings|interest|yield|returns?|apy|apr|income|profit|pnl|p&l|p\s*n\s*l)\b"
                     r"|\bhow\s+(?:am\s+i|are\s+my\s+(?:positions|investments))\s+doing\b", re.I)


WALLET_BAL_RE = re.compile(r"^\s*(?:(?:what(?:'s|\s+is|\s+are)|show(?:\s+me)?|check)\s+)?(?:my\s+)?(?:wallet\s+)?(?:balances?|holdings|coins)(?:\s+in\s+my\s+wallet)?\s*[?.!]*\s*$|^\s*what(?:'s|\s+is)\s+in\s+my\s+wallet\s*[?.!]*\s*$", re.I)
TOTAL_RE = re.compile(r"^\s*what\s+(?:do\s+i\s+(?:hold|have|own)|am\s+i\s+holding)(?:\s+(?:right\s+)?now)?(?:\s*(?:,|and)\s+(?:what(?:'s|\s+is)\s+it\s+worth|how\s+much\s+is\s+it(?:\s+worth)?))?\s*[?.!]*\s*$"
                      r"|^\s*(?:(?:what(?:'s|\s+is)|show(?:\s+me)?|check)\s+)?(?:my\s+)?(?:total|overall|whole|full|net)\s+(?:balance|worth|value|portfolio)\s*[?.!]*\s*$"
                      r"|^\s*(?:what(?:'s|\s+is)\s+)?my\s+(?:net\s*worth|portfolio(?:\s+value)?)\s*[?.!]*\s*$|^\s*how\s+much\s+(?:money\s+)?do\s+i\s+have(?:\s+in\s+total|\s+altogether|\s+overall)?\s*[?.!]*\s*$"
                      # 2026-11-13: "total", "what's my total", "how much is my wallet worth", "how much am i worth" went to the AI, which added it up wrong
                      r"|^\s*(?:(?:what(?:'s|\s+is)\s+)?(?:my\s+|the\s+)?(?:grand\s+)?total|(?:my\s+)?total\s+(?:in\s+(?:usd|dollars))?)\s*[?.!]*\s*$"
                      r"|^\s*(?:how\s+much|what)\s+(?:is|are|'s)\s+(?:my\s+(?:wallet|account|portfolio|everything|stuff|coins|holdings)|everything|it\s+all|all\s+(?:of\s+)?(?:it|my\s+(?:coins|stuff|money)))\s+worth(?:\s+(?:now|today|right\s+now|in\s+total|in\s+(?:usd|dollars)))?\s*[?.!]*\s*$"
                      r"|^\s*how\s+much\s+am\s+i\s+worth\s*[?.!]*\s*$|^\s*what(?:'s|\s+is)\s+my\s+(?:wallet|account)\s+worth\s*[?.!]*\s*$", re.I)


def total_reply(address):
    """Everything the wallet holds through Kivy, from live data: wallet coins, lent coins, perps margin (2026-10-25)."""
    if not address:
        return {'text': K()['no_wallet']}
    wal = 0.0
    try:
        b = all_balances(address) or {}
        wal = sum(float(v or 0) * float(coin_px(c) or 0) for c, v in b.items())
    except Exception:
        b = {}
    d = defi_positions(address)
    lent = sum(float(x.get('usd') or 0) for x in d.get('lent') or [])
    try:                                                 # perps: equity (margin + open PnL), the same number the wallet panel adds
        pp = lend_cli('perp-positions', address, timeout=40)
        perps = float(pp.get('equity') or 0) if pp.get('has') else 0.0
    except Exception:
        perps = float(d.get('perps') or 0)
    rw = float(d.get('rewards_usd') or 0)
    m = lambda v: '$%.2f' % v
    lines = ['• Wallet: %s' % m(wal), '• Lent: %s' % m(lent) + (' (%s)' % ', '.join('%s on %s' % (disp(x['coin']), x['market']) for x in d['lent'][:4]) if d.get('lent') else '')]
    if perps:
        lines.append('• Perps: %s' % m(perps))
    if rw > 0:
        lines.append('• Rewards to claim: %s' % usd_small(rw))
    return {'text': 'Total: about %s\n' % m(wal + lent + perps) + '\n'.join(lines) + '\nOpen the wallet panel for each coin.'}


def earn_reply(address):
    """What the wallet earns: each lent position at today's rate (per day / week / year), lending rewards waiting, perps PnL."""
    if not address:
        return {'text': K()['no_wallet']}
    rows, per_year, total = [], 0.0, 0.0
    try:
        for p, v in (lend_positions(address) or {}).items():
            amt = float((v or {}).get('usdc') or 0)
            if amt >= 0.01:
                rows.append((amt, 'USDC', PROTOS.get(p, p), float(apr_of(p) or 0), 1.0))
        for r in other_lent(address):
            rows.append((float(r['lent']), r['coin'], r['name'], float(r.get('total') or 0), float(r.get('px') or 0)))
    except Exception:
        pass
    lines = []
    for amt, coin, mk, apr, px in rows:
        usd = amt * px
        yr = usd * apr / 100
        per_year += yr; total += usd
        lines.append('• %s %s on %s at %.2f%% a year ≈ $%s a week, $%s a year' % (dec(amt, 6), disp(coin), mk, apr, dec(yr / 52, 3), dec(yr, 3)))
    perp = ''
    try:
        pp = lend_cli('perp-positions', address, timeout=40).get('positions') or []
        if pp:
            up = sum(float(x.get('upnl') or x.get('pnl') or 0) for x in pp)
            perp = '\nPerps: %d open position%s, %s$%s unrealised right now.' % (len(pp), '' if len(pp) == 1 else 's', '−' if up < 0 else '+', dec(abs(up), 4))
    except Exception:
        pass
    if not lines and not perp:
        return {'text': "You have nothing lent and no perps positions, so nothing is earning yet. Ask \"where can I earn the most on my USDC?\" to start."}
    text = ''
    if lines:
        text = 'At today\'s rates (they change):\n' + '\n'.join(lines)
        if len(lines) > 1:
            text += '\nTogether: about $%s a week on $%s (%.2f%% overall).' % (dec(per_year / 52, 3), dec(total, 4), per_year / total * 100 if total else 0)
        text += '\nThese are estimates from the current rate — Kivy doesn\'t track your exact earnings since you deposited yet.'
    text += perp
    opts = [{'label': 'Better rates?', 'sub': 'compare markets', 'ask': 'is there a better rate for my %s' % (disp(rows[0][1]) if rows else 'USDC')},
            {'label': 'Claim rewards', 'sub': 'if any are waiting', 'ask': 'claim my rewards'}]
    return {'text': text.strip(), 'widget': {'type': 'choose', 'options': opts} if rows else None}


# 4: common questions with one right answer, by code: instant, and no AI budget (they were most of the AI calls)
COMMON_QS = [
    # small talk and app questions: answered by code (2026-10-25 scenario pass: each cost an AI call of ~7k tokens)
    (r"^\s*(?:hi|hello|hey|yo|gm|good\s+(?:morning|afternoon|evening)|sup|hiya)(?:\s+(?:kivy|there|all))?\s*[!.👋]*\s*$",
     "Hi! Tell me what you want to do — e.g. \"swap 1 SUI to USDC\", \"lend my USDC\", \"what's my total balance\" — and I'll prepare it for your approval."),
    (r"^\s*(?:thanks?(?:\s+you)?|thx|ty|cheers|great|nice|cool|awesome|perfect|lol|haha+|👍|🙏)(?:\s+(?:kivy|so\s+much|a\s+lot))?\s*[!.]*\s*$",
     "Any time. Ask me whenever you want to move, lend or check something."),
    (r"\b(?:delete|clear|remove|erase|wipe)\s+(?:all\s+)?(?:my\s+)?(?:chats?|chat\s+history|conversations?|history)\b",
     "Open the menu (top left) — each saved chat has a trash button. Deleting removes it from Kivy's server too. Your transactions stay on the chain; nobody can delete those."),
    (r"^\s*(?:log\s*out|logout|sign\s*out|disconnect(?:\s+(?:my\s+)?wallet)?)\s*[.!]*\s*$",
     "Open the menu (top left), tap your wallet (\"… · connected\") › Disconnect all. That disconnects Kivy and signs you out on every device."),
    (r"^\s*(?:how\s+(?:do|can)\s+i\s+)?(?:connect|change|switch)\s+(?:my\s+|a\s+|the\s+|to\s+(?:another|a\s+different)\s+)?(?:wallet|account)s?\s*[?.!]*\s*$",
     "Open the menu (top left) › Connect wallet. To use another account, switch it in your wallet app — Kivy follows whichever account the wallet is set to."),
    (r"^\s*(?:what|which)\s+(?:coins?|tokens?|assets?|cryptos?)\s+(?:do\s+you\s+|can\s+you\s+|does\s+kivy\s+)?(?:support|have|trade|work\s+with|handle)\b.*$",
     "@assets"),
    (r"^\s*(?:what|which)\s+(?:network|chain|blockchain)\b.*$|^\s*(?:is\s+(?:this|kivy)\s+(?:on\s+)?)?(?:mainnet|testnet)\s*\??\s*$",
     "Kivy works on the Sui mainnet — real coins. Your wallet must be set to Sui mainnet too."),
    (r"^\s*(?:ok(?:ay)?,?\s+)?(?:cancel|stop|never\s*mind|nevermind|forget\s+it|don'?t\s+do\s+it|abort|no\s+thanks?)\s*[.!]*\s*$",
     "Okay — nothing happens. A card only does something when you approve it in your wallet."),
    (r"\bcancel\s+(?:the|my|that)\s+(?:last\s+)?(?:swap|send|transfer|transaction|tx|withdrawal|deposit)\s+(?:i\s+)?(?:just\s+)?(?:approved|signed|sent|made|did)\b|"
     r"\b(?:undo|reverse|revert|refund)\b.{0,30}\b(?:transaction|tx|swap|send|that|it|last)\b|^\s*undo\s*[?.!]*\s*$|\bcan\s+i\s+get\s+(?:it|my\s+money)\s+back\b",
     "A transaction on Sui can't be undone once it's confirmed — not by Kivy, not by anyone. What I can do is the opposite move: swap back, "
     "withdraw what you lent, or close a position. Tell me which."),
    (r"\b(?:sent|send|transferred)\b.{0,25}\bwrong\s+(?:address|wallet|person)\b|\bwrong\s+(?:address|wallet)\b.{0,25}\b(?:sent|send|transfer)",
     "Coins sent to the wrong address can't be pulled back — the chain has no undo. If you know who owns that address, ask them to send it back. "
     "Ask me \"show my last transaction\" to see exactly where it went. From now on: every send card shows the full address — check it before you approve."),
    (r"^\s*(?:is\s+(?:this|it|kivy)|are\s+you)\s+(?:safe|secure|legit|a\s+scam|trustworthy)\s*[?.!]*\s*$|\bcan\s+(?:you|kivy)\s+(?:steal|take|touch|move)\s+my\s+(?:money|funds|coins|crypto)\b"
     r"|\bdo\s+(?:you|kivy)\s+(?:hold|have|keep|store|custody)\s+my\s+(?:money|funds|coins|crypto|keys|wallet)\b|\bwho\s+(?:holds|has)\s+my\s+(?:money|funds|keys)\b"
     r"|\b(?:take|move|spend|steal|touch|drain)\s+(?:any\s+|my\s+)?(?:money|funds|coins|crypto)\b.{0,60}\bconnect|\bconnect\w*\b.{0,60}\b(?:take|move|spend|steal|drain)\s+(?:any\s+|my\s+)?(?:money|funds|coins|crypto)\b",
     "Kivy never holds your money or your keys — everything stays in your own wallet. Kivy prepares a transaction, checks it against what you asked "
     "(before you sign and again after), and nothing moves until you approve it in your wallet. The real risks are the markets themselves: prices, "
     "leverage, and the lending markets' collateral."),
    (r"^\s*(?:help|what\s+can\s+(?:you|kivy)\s+do|what\s+do\s+you\s+do|what\s+(?:are\s+)?(?:your|kivy'?s)\s+features|what\s+(?:things\s+)?can\s+i\s+(?:do|ask)(?:\s+here)?|commands|menu\s+of\s+commands|show\s+me\s+what\s+you\s+can\s+do)\s*[?.!]*\s*$", "@help"),
    (r"^\s*(?:(?:the\s+)?(?:gas|network|swap(?:ping)?|send(?:ing)?|lend(?:ing)?|withdraw(?:al|ing)?|deposit|transaction|tx|transfer)\s+)?(?:gas\s+)?(?:fees?|costs?|gas)\s*[?.!]*\s*$"   # "gas", "swap fee", "withdraw fee" (2026-11-13)
     r"|^\s*(?:(?:the\s+)?(?:costs?|fees?|gas)\s+(?:to|for|of|on)\s+(?:a\s+|an\s+)?(?:swap\w*|send\w*|lend\w*|withdraw\w*|transfer\w*|deposit\w*)|how\s+much\s+gas)\s*[?.!]*\s*$"
     r"|^\s*(?:(?:kivy'?s?\s+)?fees?|costs?|how\s+much\s+(?:does\s+(?:it|this|kivy)|do\s+you)\s+cost|is\s+(?:it|this|kivy)\s+free|what\s+(?:are\s+(?:the|your)\s+)?fees?|do\s+you\s+charge(?:\s+(?:a\s+)?fees?)?|any\s+fees?|pricing)\s*[?.!]*\s*$"
     r"|\b(?:is|are)\s+(?:it|this|kivy|transactions?|swap\w*|send\w*|lend\w*|withdraw\w*|everything)\b.{0,30}\bfree\b|\bfree\b.{0,30}\b(?:gas|fees?)\b"
     r"|\b(?:do|will|must)\s+i\s+(?:pay|need)\s+(?:for\s+|any\s+)?(?:gas|fees?)\b|\bhow\s+much\s+(?:is|does|will)\s+(?:the\s+)?gas\b|\bwho\s+pays\s+(?:the\s+)?gas\b"
     r"|^(?!\s*(?:swap|send|transfer|lend|deposit|withdraw|move|claim|long|short|close|buy|sell)\b)(?!.*\b(?:perps?|trading|funding|taker|maker|liquidation|borrow\w*|closing|earn\w*|profit\w*|worth\s+it)\b).*(?:\b(?:what|which|how\s+much|how\s+big|does|do|will|is|are|can|any)\b.{0,60}"
     r"\b(?:fees?|charges?|charging|subscriptions?)\b|\bkivy'?s?\s+(?:own\s+)?fees?\b|\b(?:do|will|must)\s+i\s+pay\s+anything\b|\bhow\s+do(?:es)?\s+(?:you|kivy)\s+make\s+money\b|\bwhat'?s\s+the\s+catch\b|\bcharge\s+me\b|\b(?:kivy|you)\s+(?:take|keep|add)\s+(?:anything|something|a\s+cut)(?:\s+extra)?\b|\bservice\s+charge\b|\bfree\s+on\s+your\s+side\b|\b(?:does|do|will|would)\s+(?:kivy|you)\s+charge\b|\b(?:your|kivy'?s)\s+(?:own\s+)?(?:fee|charge|cut)\b)",
     "@fees"),   # fee and gas questions: a fixed true answer (the AI once said Kivy's fee "covers the network gas", 2026-10-25)
    (r"^\s*(?:how\s+(?:do\s+i|to|can\s+i)\s+(?:start|begin|get\s+started|use\s+(?:this|kivy|it))|getting\s+started|where\s+do\s+i\s+start|what\s+(?:is\s+this|do\s+i\s+do(?:\s+here)?))\s*[?.!]*\s*$",
     "Three steps: 1) Connect your Sui wallet (bottom left). 2) Tell me what you want in plain words — \"swap 5 SUI to USDC\", \"where can I earn the most on my USDC?\", "
     "\"long BTC 3x with $10\". 3) Check the card I show and approve it in your wallet. Nothing moves without that last step."),
]


# The COMMON_QS table above: fixed answers (greetings, fees, safety, undo, help …); '@fees' / '@help' / '@assets' are filled live.
def common_answer(msg):
    for pat, ans in COMMON_QS:
        if re.search(pat, msg, re.I):
            if ans == '@fees':
                kf = kivy_fee()
                return {'text': 'Asking me is free. Every transaction pays a small Sui network fee (gas, usually under 0.01 SUI) from your wallet. '
                                + (('Kivy\'s own fee: simple swaps, sends and lending are free; a multi-step transaction (several actions in one approval) '
                                    'includes %g SUI, always shown on its card before you approve. They are two separate charges: Kivy\'s fee never pays '
                                    'the network, and a request you reject in your wallet instead of signing costs nothing.' % kf['sui']) if kf else 'Kivy itself charges no fee.')}
            if ans == '@help':
                return {'text': K()['help']}
            if ans == '@assets':
                k = K()
                return {'text': ('I work with these coins: %s — plus every coin Suilend or NAVI lists (USDsui, vSUI, haSUI, sSUI, LBTC, wBTC, BLUE, '
                                 'NAVX…): swap, send, lend, and withdraw as another coin in one approval. Anything unlisted I refuse.') % ', '.join(k['assets'])}
            return {'text': ans}
    return None


# plain swap orders ("swap 0.95 SUI to USDC"): the same card the AI's swap tool builds, without the AI (instant, no budget)
SWAP_EXACT_RE = re.compile(r'^\s*(?:swap|convert|trade)\s+(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?|all|max|half|a\s+quarter|a\s+third)\s+(?:of\s+)?(?:my\s+)?([a-z][a-z0-9]{1,9})\s+(?:to|for|into)\s+([a-z][a-z0-9]{1,9})\s*[.!]*\s*$', re.I)


AMOUNT_ONLY_RE = re.compile(r'^\s*(all|everything|max|half|a\s+quarter|\$?\d+(?:\.\d+)?%?)(?:\s+(?:of\s+it|please|pls|[a-z]{2,10}))?\s*[.!]*\s*$', re.I)


def amount_followup(msg, address, history):
    """A bare amount ("all", "half", "5") answering Kivy's "How much … ?": the user's previous request, with that amount put in
    (2026-10-24: "all" after "How much USDC should I start with?" got the help menu)."""
    m = AMOUNT_ONLY_RE.match(msg)
    if not m or not history:
        return None
    hs = [h for h in history if isinstance(h, dict)]
    last = next((i for i in range(len(hs) - 1, -1, -1) if hs[i].get('role') in ('model', 'assistant', 'kivy')), None)
    if last is None or not re.match(r'^\s*how\s+much\b', str(hs[last].get('text') or hs[last].get('content') or ''), re.I):
        return None
    prev = next((str(h.get('text') or h.get('content') or '') for h in reversed(hs[:last]) if h.get('role') == 'user'), '')
    if not prev or AMOUNT_ONLY_RE.match(prev):
        return None
    amt = m.group(1).replace('everything', 'all')
    new = re.sub(r'\b(?:my|that|the|some|this)\s+(?=[A-Za-z]{2,10}\b)', amt + ' ', prev, count=1, flags=re.I)
    if new == prev:
        new = re.sub(r'^(\s*\w+)\s+', lambda x: x.group(1) + ' ' + amt + ' ', prev, count=1)
    return widget_reply(new, address, hs[:last])


# Section 5's entry point, called by widget_reply right after quick_answers: picks the journey whose pattern matches.
def journey_answers(msg, address):
    if EXIT_RE.match(msg):
        return exit_reply(address)
    if BEST_RE.search(msg):
        br = best_reply(msg, address)
        if br:
            return br
    if ACT_RE.search(msg):
        return activity_reply(msg, address)
    if EARN_RE.search(msg) and not re.search(r'\b(?:swap|send|withdraw|lend|deposit|long|short|close)\b', msg, re.I) \
            and not DEFINE_Q_RE.search(msg):          # "what does the % under my PnL mean?" is a definition, not my earnings (2026-11-10)
        return earn_reply(address)
    if TOTAL_RE.search(msg):
        return total_reply(address)
    if WALLET_BAL_RE.search(msg):                        # "what is my wallet balance": the coins, from live data
        return {'text': balances_text(address) if address else K()['no_wallet']}
    ca = common_answer(msg)
    if ca:
        return ca
    pm = re.match(r'^\s*(?:what(?:\'s|\s+is)\s+(?:the\s+)?)?(?:current\s+)?(?:price\s+of\s+|how\s+much\s+is\s+(?:1\s+|one\s+)?)?([a-z][a-z0-9]{1,9})(?:\s+price)?'
                  r'(?:\s+(?:now|today|right\s+now|rn|atm|currently|at\s+the\s+moment|these\s+days))?\s*[?.!]*$', msg, re.I)   # "…of sui rn?" reached the AI (2026-11-14)
    if pm and re.search(r'\bprice\b|\bhow\s+much\s+is\b', msg, re.I) and (ckey(pm.group(1)) or lend_token(pm.group(1))):
        c = ckey(pm.group(1)) or lend_token(pm.group(1))      # "what is the price of sui": Kivy's live price, no AI (2026-10-24)
        px = coin_px(c)
        if px:
            out = {'text': '%s is $%s right now.' % (disp(c), dec(px, 6))}
            sym = chart_symbol(c)
            if sym:                                  # its live chart too, like "sui price rn" gets
                try:
                    img = perp_market(sym).get('image')
                except Exception:
                    img = None
                out['widget'] = {'type': 'chart', 'symbol': sym, 'tf': '15m', 'image': img}
            return out
    if re.search(r'\bprices?\b', msg, re.I) and not re.search(r'\b(?:swap|send|lend|buy|sell|long|short|alert|when)\b', msg, re.I):
        cs = []                                          # "price of btc and eth": every coin named, live, no AI (2026-10-25)
        for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', msg):
            c = ckey(w) or lend_token(w)
            if c and c not in cs and w.lower() not in ('price', 'prices', 'of', 'and', 'the', 'now', 'today'):
                cs.append(c)
        if len(cs) >= 2:
            rows = []
            for c in cs[:8]:
                px, ch = coin_px(c), coin_change(c)
                rows.append('• %s  $%s%s' % (disp(c), dec(px, 6) if px else '—', ('  (%s%.2f%% 24h)' % ('+' if ch >= 0 else '', ch)) if px and ch is not None else ''))
            return {'text': 'Right now:\n' + '\n'.join(rows)}
    qm = re.match(r'^\s*(?:(?:get\s+(?:me\s+)?(?:a\s+)?)?quote\s+(?:for\s+)?(?P<a1>\d+(?:\.\d+)?)\s+(?P<c1>[a-z][a-z0-9]{1,9})\s+(?:to|for|in|into)\s+(?P<d1>[a-z][a-z0-9]{1,9})'
                  r'|how\s+much\s+(?P<d2>[a-z][a-z0-9]{1,9})\s+(?:(?:would|will|do|can|could)\s+i\s+get\s+)?(?:for|from)\s+(?P<a2>\d+(?:\.\d+)?)\s+(?P<c2>[a-z][a-z0-9]{1,9}))\s*[?.!]*\s*$', msg, re.I)
    if qm:                                               # a quote is the router's real answer, no AI and nothing to sign (2026-10-25)
        a, b = ckey(qm.group('c1') or qm.group('c2')), ckey(qm.group('d1') or qm.group('d2'))
        amt = float(qm.group('a1') or qm.group('a2'))
        if a and b and a != b and amt > 0:
            try:
                out = float(swap_quote(a, b, amt)['out'])
                return {'text': '%s %s ≈ %s %s right now (the swap router\'s quote, before Kivy\'s fee and gas). Say "swap %s %s to %s" to prepare it.'
                                % (num_plain(amt), disp(a), dec(out, 6), disp(b), num_plain(amt), a, b)}
            except Exception:
                return {'text': "I couldn't get a quote from the swap router right now. Try again in a minute."}
    sm = SWAP_EXACT_RE.match(msg)
    if sm and ckey(sm.group(2)) and ckey(sm.group(3)):
        a, b = ckey(sm.group(2)), ckey(sm.group(3))
        if not address:                                  # no wallet: say so, as "swap sui to usdc" does — not a plan nobody can approve (2026-11-13)
            return {'text': K()['no_wallet']}
        return plan('swap', _amt(sm.group(1), a, address), a, b, holdings=all_balances(address) if address else None)
    return None


def choose_icons(w):
    """Every choice card gets icons where its options name a thing (2026-10-24: "a table with icons"): a perps market's image,
    a lent coin = coin icon + protocol badge, a wallet coin = its icon, perps margin = Aftermath. The page draws a card whose
    options have icons as a table. Options that are just words (a leverage, "spot or perps") stay as they are."""
    if not isinstance(w, dict) or w.get('type') != 'choose':
        return w
    try:
        pm = {r['symbol']: r.get('image') for r in perp_market_list()}
    except Exception:
        pm = {}
    STOP = {'all', 'my', 'the', 'to', 'as', 'on', 'in', 'from', 'of', 'and', 'then', 'it', 'a', 'at', 'with', 'for', 'more', 'max', 'half', 'usd', 'spot',
            'send', 'swap', 'lend', 'buy', 'sell', 'withdraw', 'close', 'open', 'long', 'short', 'chart', 'price', 'see', 'move', 'claim', 'your', 'order',
            'market', 'markets', 'first', 'another', 'coin', 'or', 'quarter', 'deposit', 'trade', 'add', 'take', 'out', 'back', 'now', 'best', 'rate', 'rates'}
    for o in w.get('options') or []:
        if o.get('icon') or o.get('coin'):
            continue
        t = ' %s %s ' % (o.get('label') or '', o.get('ask') or '')   # the label first: "Buy xBTC" is about xBTC, not the USDC it's bought with
        low = t.lower()
        proto = 'navi' if re.search(r'\bnavi\b', low) else 'suilend' if re.search(r'\bsuilend\b', low) else None
        perpish = re.search(r'\b(?:chart|perps?|long|short|close|position|leverage|margin|aftermath|tp|sl|stop)\b', low)
        words = [x for x in re.findall(r'[A-Za-z][A-Za-z0-9.&]{1,11}', t) if x.lower() not in STOP]
        if not proto:                                  # a perps market: by context ("long BTC"), or a name that is only a market (TSLA, gold)
            sym = next((chart_symbol(x) for x in words if chart_symbol(x) and pm.get(chart_symbol(x)) and (perpish or not (lend_token(x) or ckey(x)))), None)
            if sym:
                o['icon'] = pm[sym]; o['coin'] = sym
                continue
            if perpish and re.search(r'\b(?:margin|perps?\s+account|aftermath)\b', low):
                o['icon'] = '/icons/aftermath.png'; o['coin'] = 'Aftermath'
                continue
        c = next((lend_token(x) or ckey(x) for x in words if (lend_token(x) or ckey(x))), None)
        if c:
            o['coin'] = disp(c)
            o['icon'] = coin_icon(c)
            if proto and re.search(r'\b(?:lent|lend|withdraw|on|from)\b', low):
                o['proto'] = proto
    if sum(1 for o in w.get('options') or [] if o.get('icon') or o.get('coin')) >= max(1, len(w.get('options') or []) // 2):
        w['table'] = True
    return w


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import chart_symbol, num_plain, perp_account, perp_market, perp_market_list  # noqa: E402
from .situations import usd_small  # noqa: E402
from .memory import tx_labels  # noqa: E402
from .chainreads import COINS, all_balances, balances_text, ckey, coin_icon, dec, defi_positions, ecoins, gql, shadow_note  # noqa: E402
from .fallback import _amt  # noqa: E402
from .lending import (  # noqa: E402
    LEND_MKTS, PROTOS, apr_of, coin_change, coin_px, lend_positions, lend_rates_all, lend_token,
    other_lent)
from .cards import plan  # noqa: E402
from .txreads import kivy_fee, swap_quote  # noqa: E402
