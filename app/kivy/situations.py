"""More situations Kivy answers itself: staked SUI, dust, claim card, quick/source answers (Section 6).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# situations / quick_answers / source_answers / claim_card are called by widget_reply (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import re
from . import perpacct as PERPACCT
from . import amounts as AM
from .config import disp
from .intentwords import IF_Q_RE, QUESTION_RE
from .patterns import CLAIM_SCOPES, SUILEND_MKT_RE, TICKER_WORD_RE
from .policy import type_key
from .upstream import lend_cli, lend_post

widget_reply = None        # server.py's (Section 7): set by server.py once it is defined (a module never imports server.py)


# ── Section 6: more situations + input hygiene — staked SUI, dust sweep, claim card, amount/wording tidying, seed-phrase
#    guard, quick_answers; situations() and quick_answers() are called by widget_reply, tidy_*/looks_secret by /api/chat ──
# ── More situations, answered by Kivy itself (2026-10-24 "think about new more situations", 30-case review) ──
# Most rewrite the message into a form Kivy already builds and checks (one approval where the builder allows it);
# the rest are honest answers. Rebalance / gather / dollar + "except" amounts / one coin into several / several into one /
# staked SUI / close-then-lend / automation / no gas / put it back.
STAKED_SUI = ('vSUI', 'haSUI', 'sSUI', 'afSUI', 'stSUI')
LATER_RE = re.compile(r"\b(?:tomorrow|tonight|later\s+today|next\s+(?:week|month|year|(?:mon|tues|wednes|thurs|fri|satur|sun)day)|this\s+(?:evening|afternoon|weekend)"
                      r"|in\s+(?:an?|\d+|a\s+few|few)\s+(?:minutes?|mins?|hours?|days?|weeks?)|at\s+\d{1,2}(?::\d\d)?\s*(?:am|pm)|at\s+(?:noon|midnight))\b", re.I)   # an order for later (2026-11-01)
AUTO_RE = re.compile(r"\b(?:(?:every|each)\s+(?:day|week|month|hour|morning|night|time|weekend|(?:mon|tues|wednes|thurs|fri|satur|sun)day|\d+\s+(?:days?|weeks?|months?|hours?))"
                     r"|on\s+(?:mon|tues|wednes|thurs|fri|satur|sun)days|daily|weekly|monthly|hourly|automatic(?:ally)?|auto[- ]?(?:compound|rebalance|swap|lend)\w*|whenever"
                     r"|remind\s+me|alert\s+me|notify\s+me|set\s+(?:up\s+)?(?:an?\s+)?(?:alert|reminder|rule))\b"
                     r"|\bif\b.{0,60}\b(?:drops?|falls?|goes\s+(?:up|down|below|above)|rises?|reach(?:es)?|hits?|below|above|under|over)\b.{0,60}\b(?:move|swap|sell|buy|withdraw|lend|close|switch|put)\b", re.I)
GAS_RE = re.compile(r"\b(?:no|not\s+enough|out\s+of|zero|0)\s+(?:sui\s+)?(?:for\s+)?gas\b|\bno\s+sui\s+(?:for|to\s+pay)\b|\bcan'?t\s+pay\s+(?:the\s+)?gas\b|\bneed\s+gas\b"
                    r"|\b(?:pay|cover|sponsor|front)\s+(?:the\s+|my\s+)?gas\b|\bgas\s*(?:-|\s)?(?:free|less)\b|\bfree\s+gas\b", re.I)   # "can you pay gas for me" (2026-10-25)
PUTBACK_RE = re.compile(r"\b(?:put|move|send)\s+(?:it|them|everything|my\s+money)\s+back\b|\bback\s+(?:to\s+)?where\s+it\s+was\b|\bgo\s+back\s+to\s+how\s+it\s+was\b", re.I)
MONEY_RE = r'(?:\$\s*(%s)|(%s)\s*(?:usd|dollars?|bucks)\b)' % (AM.NUM, AM.NUM)   # a $ amount inside a sentence shape (the AI gates read AM.said)
# all / max / half as an AMOUNT only when it is attached to the money: "send 1 sui to 0x…, that is all" built a card for the whole
# balance, and an idiom list leaked ("after all", "thanks for everything", "better half") — a reviewer W3-1 (round 3, 2026-11-03)
MONEY_NOUNS = {'balance', 'balances', 'funds', 'fund', 'money', 'coin', 'coins', 'token', 'tokens', 'wallet', 'holdings', 'stack', 'bag',
               'position', 'positions', 'savings', 'assets', 'crypto', 'rest', 'everything', 'lot'}
AMT_VERB = r'(?:send|swap|lend|withdraw|deposit|transfer|move|sell|convert|pay|stake|unstake|supply|give|redeem|exchange|put|take|pull|repay|close|trade|use|spend)'


def amount_pairs(text):
    """Each number in text that is an AMOUNT, with the coin it sits on: (value, is_percent, coin or None). A number counts when it
    sits on a coin or money noun ("10 sui", "10 of my usdc", "usdc 10", "50% of it") or right after an action verb ("send 5 to …");
    "room 12", "100 % sure", "since 2019" do not (review W3-3/W3-4, round 3, 2026-11-03). $ amounts are their own kind.
    Since round 2 (2026-11-16) this is the amount reader's reading (AM.amounts) — one reading for the cards and the AI gates."""
    return [(a['v'], a['kind'] == 'pct', a['coin'] or a.get('pc')) for a in AM.amounts(text)
            if a['kind'] != 'usd' and (a['kind'] != 'pct' or a.get('ctx'))]


def amount_words(text):
    """The all / max / half / everything / entire words in text that are an amount: right after an action verb ("send all to …",
    "withdraw max", "swap everything") or attached to a coin or money noun ("all my sui", "half of it", "max usdc", "my entire
    balance"). Anything else ("after all", "max speed", "half past three") is not."""
    t = str(text or '')
    out = []
    for m in re.finditer(r'(?i)\b(all|max|half|everything|entire|whole)\b', t):
        w, before, after = m.group(1).lower(), t[:m.start()], t[m.end():]
        if re.search(r'(?i)\b%s\s+(?:it\s+|them\s+|out\s+|over\s+)?$' % AMT_VERB, before):
            if not re.match(r'(?i)\s+(?:of\s+)?(?:(?:the|my|your|our|this|that)\s+)?([a-z]{2,12})\b', after) or \
                    (lambda x: ckey(x) or lend_token(x) or x.lower() in MONEY_NOUNS | {'it', 'them', 'to', 'into', 'for', 'from', 'on', 'in', 'out', 'back', 'of', 'my', 'the', 'please', 'pls', 'now'})(
                        re.match(r'(?i)\s+(?:of\s+)?(?:(?:the|my|your|our|this|that)\s+)?([a-z]{2,12})\b', after).group(1)):
                out.append(w)
            continue
        a = re.match(r'(?i)\s+(?:of\s+)?(?:(?:the|my|your|our|this|that)\s+)?([a-z]{2,12})\b(.{0,12})', after)
        if a and (ckey(a.group(1)) or lend_token(a.group(1)) or a.group(1).lower() in MONEY_NOUNS):
            out.append(w)
        elif a and a.group(1).lower() in ('it', 'them') and re.match(r'(?i)\s*(?:$|[.,!?;]|to\b|into\b|for\b|from\b|on\b|in\b|out\b|back\b|please\b)', a.group(2)):
            out.append(w)                                # "send all of it", "swap them all" stays below
        elif re.search(r'(?i)\b(?:it|them|coins?|tokens?|%s)\s+$' % '|'.join(['sui', 'usdc', 'usdt', 'deep', 'wal']), before) and \
                re.match(r'\s*(?:$|[.,!?;]|to\b|into\b|for\b|from\b|on\b|in\b|out\b|back\b|please\b)', after, re.I):
            out.append(w)                                # "send it all to …", "my sui, all"
        elif re.search(r'(?i)\b(?:my|the|your)\s+$', before) and w in ('entire', 'whole') and a and (ckey(a.group(1)) or lend_token(a.group(1)) or a.group(1).lower() in MONEY_NOUNS):
            out.append(w)
    return out
# "swap everything / all my coins / my leftover coins / half of everything to X": one swap per coin into X (review P-28)
ALL_COINS_RE = re.compile(r'^\s*(?:swap|convert|turn|change|sell|trade|exchange)\s+(?:(?P<share>half|\d{1,3}(?:\.\d+)?\s*%)\s+of\s+)?(?:all\s+(?:of\s+)?)?(?:my\s+|the\s+)?'
                          r'(?:everything|(?:other\s+|leftover\s+|remaining\s+)?(?:coins|tokens|crypto|assets|holdings))'
                          r'(?:\s+(?:i\s+(?:have|hold|own)|in\s+my\s+wallet))?(?:\s+(?:(?:in)?to|for|as)\s+(?P<to>[a-z][a-z0-9]{1,9}))?\s*[.!]*$', re.I)


def mk_name(key):
    """A market as the chain reader reads it back ("navi main", "suilend main", "Suilend Ember")."""
    return MKT_EXACT.get(key, LEND_MKTS.get(key, key))


REAL_COINS = {t.lower() for t in STAKED_SUI}         # real coins Kivy may not list: never auto-"corrected" into another coin (2026-10-25)
STAKED_ASK = re.compile(r'\b(?:the\s+)?(?:(?:best|highest(?:[\s-]+paying)?|top)\s+)?(?:liquid(?:ly)?\s+)?staked\s+sui\b|\b(?:an?\s+)?lsts?\b', re.I)
STAKED_NAMED = re.compile(r'\b(?:to|into|for)\s+(?:my\s+)?(?:%s)\b' % '|'.join(STAKED_SUI), re.I)   # a staked coin named as the TARGET ("withdraw my ssui…" is a source)


def staked_ranked(mk=None):
    """The staked SUI coins Kivy can lend (today sSUI, vSUI, haSUI), best lending rate first; mk = only that market."""
    rows = [(t, r) for t in STAKED_SUI for r in lend_rows_for(t)
            if not mk or r['market'] == mk or (mk.startswith('fam:') and r['market'].split('-')[0] == mk[4:])]
    return sorted(rows, key=lambda x: -float(x[1]['total_apr']))


def staked_line(ranked, where=None):
    """'haSUI pays the most of the 3 staked SUI Kivy can lend (haSUI 2.46%, sSUI 2.35%, vSUI 2.22% a year).'"""
    seen = []
    for t, r in ranked:
        if t not in [x[0] for x in seen]:
            seen.append((t, r))
    rates = ', '.join('%s %.2f%%' % (t, float(r['total_apr'])) for t, r in seen)
    if where and len(seen) == 1:
        return '%s is the staked SUI you can lend on %s (%s a year).' % (seen[0][0], where, rates)
    return '%s pays the most of the %d staked SUI Kivy can lend%s (%s a year).' % (seen[0][0], len(seen), ' on ' + where if where else '', rates)


# The market a piece of text names (chain_market's reading), or None for 'best' / 'there' / nothing.
def _mk_of(text):
    k, _ = chain_market(text or '')
    return k if k and k not in ('best', 'there') else None


def _rows_on(rows, mk, coin=None):
    """The user's lent rows on a named market (exact key or protocol family), optionally one coin."""
    return [r for r in rows if (not coin or r['coin'].lower() == coin.lower())
            and (r['id'] == mk or (mk.startswith('fam:') and r['id'].split('-')[0] == mk[4:]))]


def safest_market(coin):
    """The best rate among markets whose borrowers post broad collateral (5+ coins): the lower-risk choice."""
    col = lend_collateral()
    rows = [r for r in lend_rows_for(coin) if len((col.get(r['market']) or {}).get('coins') or []) >= 5]
    return rows[0]['market'] if rows else None


DUST_USD = 0.01                                       # a coin worth less than this is dust: the wallet panel hides it
DUST_RE = re.compile(r'\bdust\b|\b(?:tiny|small|little)\s+(?:coins?|balances?|amounts?|bits|tokens?)\b', re.I)   # not "leftover": that is the rest of a coin
DUST_TO_RE = re.compile(r'\b(?:(?:in)?to|as)\s+(?:my\s+|some\s+)?([a-z][a-z0-9]{1,9})\b', re.I)


def dust_coins(address, to=None):
    """The verified coins (Kivy's list + every coin the lending markets list) worth under $0.01, SUI aside (it pays gas):
    ([{sym, type, dec, amount, usd, icon}], how many coins Kivy doesn't know, which it never touches)."""
    bal = balances(address) or {}
    out, seen = [], set()
    norm = type_key
    rows = [(c, float(v or 0), cinfo(c)['type'], int(cinfo(c)['decimals']), coin_px(c), coin_icon(c)) for c, v in bal.items() if c in COINS()]
    rows += [(x['sym'], float(x['amount']), x['type'], int(cinfo(x['sym'])['decimals']), x.get('px'), x.get('icon')) for x in other_balances(address)]
    for c, v, t, d, px, icon in rows:
        if c == 'SUI' or c == to or v <= 0 or norm(t) in seen:
            continue
        seen.add(norm(t))
        if px and 0 < v * float(px) < DUST_USD:
            out.append({'sym': c, 'type': t, 'dec': d, 'amount': v, 'usd': v * float(px), 'icon': icon})
    known = {norm(c['type']) for c in ecoins().values()}
    raw = (_BAL.get(('raw', NET(), address)) or (0, []))[1]
    unknown = sum(1 for t, amt in raw if int(amt or 0) > 0 and norm(t) not in known)
    return sorted(out, key=lambda x: -x['usd']), unknown


def dust_build(address, to, dust):
    """Build the sweep (simulated) and add up what it gets against what it costs. Returns the card, or a text reply."""
    body = {'address': address, 'to': to, 'items': [{'sym': x['sym'], 'type': x['type'], 'dec': x['dec']} for x in dust]}
    out = lend_post('/dust', body)
    if not out.get('ok') and re.search(r'would fail right now', str(out.get('error') or '')) and 'router' not in str(out.get('error') or ''):
        out = lend_post('/dust', body)                # a pool refused a tiny amount (lend_post already retries router aborts)
    if not out.get('ok'):
        return {'text': "I couldn't prepare the dust sweep right now (%s). Try again in a minute." % str(out.get('error') or '')[:120]}
    legs = out.get('legs') or []
    why = {}
    for x in out.get('skipped') or []:                # grouped: "sSUI and haSUI stay: their swap route uses…"
        why.setdefault(x['why'], []).append(disp(x['sym']))
    skipped = '; '.join('%s: %s' % (' and '.join(v) if len(v) < 3 else ', '.join(v[:-1]) + ' and ' + v[-1],
                                    re.sub(r'^its ', 'their ' if len(v) > 1 else 'its ', w)) for w, v in why.items())
    if not legs:
        return {'text': 'None of your dust can be swapped right now%s.' % (' (' + skipped + ')' if skipped else '')}
    psui, pto = coin_px('SUI') or 0, (1.0 if to in ('USDC', 'USDsui') else coin_px(to) or 0)
    g = out.get('gas') or {}
    gas_sui = (int(g.get('comp') or 0) + int(g.get('storage') or 0) - int(g.get('rebate') or 0)) / 1e9   # below zero: Sui pays back more than it costs
    fee = kivy_fee() if len(legs) >= 2 else None       # Kivy's flat fee: multi-step transactions (2026-10-22)
    fee_sui = float(fee['sui']) if fee else 0.0
    by = {x['type']: x for x in dust}
    got_usd = float(out['expected']) * pto
    net = got_usd - (gas_sui + fee_sui) * psui
    coins = [{'sym': l['sym'], 'icon': (by.get(l['type']) or {}).get('icon') or coin_icon(l['sym']), 'amount': l['amount'],
              'usd': (by.get(l['type']) or {}).get('usd') or 0, 'type': l['type']} for l in legs]
    verdict = ('Worth it: you end up about $%s ahead.' % dec(net, 2) if net > 0 else
               'Not worth it for the money: it costs about $%s more than you get. Approve only if you want a tidy wallet.' % dec(-net, 2))
    return {'text': '%d dust coin%s into %s, one approval. %s%s' % (len(legs), '' if len(legs) == 1 else 's', disp(to), verdict,
                                                                   (' Left out — %s.' % skipped) if skipped else ''),
            'widget': {'type': 'dust', 'to': to, 'to_icon': coin_icon(to), 'coins': coins, 'types': [c['type'] for c in coins],
                       'value_usd': sum(c['usd'] for c in coins), 'expected': out['expected'], 'got_usd': got_usd,
                       'objects': sum(int(l.get('objects') or 0) for l in legs), 'gas_sui': gas_sui, 'gas_usd': gas_sui * psui,
                       'fee_sui': fee_sui, 'fee_usd': fee_sui * psui, 'net_usd': net}}


def usd_small(v):
    """'$0.37', or '<$0.01' for a sum too small to show in cents (2026-10-25: show it, don't hide it)."""
    v = float(v or 0)
    return '$%.2f' % v if v >= 0.01 else '<$0.01' if v > 0 else '$0'


def claim_card(address, only='all'):
    """Every reward waiting in every market — even tiny ones — claimed in ONE transaction, with what it nets after the
    gas (and Kivy's fee, if any): the same sum as the dust sweep (2026-10-25)."""
    try:
        r = lend_cli('build', address, only, 'claim')
    except Exception:
        r = {}
    if not r.get('ok'):
        if 'No rewards' in str(r.get('error') or ''):
            return {'text': 'You have no %slending rewards to claim right now.' % ((CLAIM_SCOPES.get(only) + ' ') if CLAIM_SCOPES.get(only) else '')}
        return {'text': "I couldn't prepare the claim right now (%s). Try again in a minute." % str(r.get('error') or 'builder busy')[:100]}
    items = [{'name': LEND_MKTS.get(x['market'], x['market']), 'market': x['market'], 'symbol': x['symbol'], 'coin_type': x.get('coinType'),
              'icon': coin_icon(x['symbol']), 'amount': x['amount'], 'usd': round(x['usd'], 6)} for x in (r.get('rewards') or [])]
    if not items:
        return {'text': 'You have no lending rewards to claim right now.'}
    got = float(r.get('usd') or 0)
    psui = coin_px('SUI') or 0
    g = r.get('gas') or {}
    gas_sui = (int(g.get('comp') or 0) + int(g.get('storage') or 0) - int(g.get('rebate') or 0)) / 1e9
    fee = kivy_fee() if len(intent_actions('/api/tx/lend', {'action': 'claim'}, {})) >= 2 else None   # a claim is one step: free today
    fee_sui = float(fee['sui']) if fee else 0.0
    net = got - (gas_sui + fee_sui) * psui
    mk = sorted({x['market'] for x in items}, key=lambda m: -sum(x['usd'] for x in items if x['market'] == m))
    verdict = ('Worth it: you end up about %s ahead after gas.' % usd_small(net) if net > 0 else
               'Not worth it yet: the gas is about %s more than the rewards. They keep growing — claiming later pays more.' % usd_small(-net))
    who = CLAIM_SCOPES.get(only) or ''
    return {'text': '%s from %d market%s, %s in all — one approval claims them all. %s' % ((who + ' rewards') if who else 'Rewards', len(mk), '' if len(mk) == 1 else 's', usd_small(got), verdict),
            'widget': {'type': 'rewards', 'items': items, 'usd': round(got, 6), 'markets': mk, 'only': only, 'who': who,
                       'gas_sui': gas_sui, 'gas_usd': gas_sui * psui, 'fee_sui': fee_sui, 'fee_usd': fee_sui * psui, 'net_usd': net}}


DUST_TARGETS = ('SUI', 'USDC', 'USDsui')


def dust_reply(msg, address):
    """'swap all my dust to usdc', 'sweep dust', 'clean up the tiny coins' (2026-10-25): every verified coin under
    $0.01 into one coin the user picks, in ONE transaction, with what it gets against what it costs."""
    if not DUST_RE.search(msg) or QUESTION_RE.search(msg) and not re.search(r'\b(?:swap|sweep|convert|sell|clean|clear|collect|gather|worth|how\s+much|value)\b', msg, re.I):
        return None
    if re.search(r'^\s*(?:what\s+is|what\'?s)\s+(?:a\s+)?dust(?:\s+coins?)?\s*[?.!]*\s*$|\bdust\s+means?\b', msg, re.I):
        return None                                   # the concept: the FAQ answers it
    if re.search(r'\b(?:withdr[ae]w\w*|margin|perps?|lend\w*|deposit\w*|supply|send|then)\b', msg, re.I):
        return None                                   # a step of something else ("…the dust from navi, then lend it"): the chain reader
    if not ADDR_RE.match(str(address or '')):
        return {'text': 'Connect your wallet and I\'ll find your dust — the coins worth under $0.01 that the wallet panel hides.'}
    m = DUST_TO_RE.search(msg)
    to = ((ckey(m.group(1)) or lend_token(m.group(1))) if m else None) or 'SUI'   # no coin named: straight to the card in SUI, with a picker on it (2026-10-25)
    dust, unknown = dust_coins(address, to)
    left = (' %d coin%s Kivy doesn\'t recognise %s left alone.' % (unknown, '' if unknown == 1 else 's', 'is' if unknown == 1 else 'are')) if unknown else ''
    if not dust:
        return {'text': 'No dust: every coin Kivy recognises in your wallet is worth $0.01 or more (SUI is kept for gas).' + left}
    if NET() != 'mainnet':
        return {'text': 'Dust sweeps need the swap router, which is mainnet-only.'}
    r = dust_build(address, to, dust)
    if AUTO_RE.search(msg) and r.get('text'):         # "every week": Kivy can't repeat it, but here is one now
        r['text'] = "Kivy can't sweep on a schedule — every sweep needs your approval. Here's one now: " + r['text']
    if r.get('widget'):
        r['widget']['to_opts'] = [to] + [c for c in DUST_TARGETS if c != to]   # the card's "Receive as" picker
    if left and r.get('text'):
        r['text'] += left
    return r


# Section 6's entry point (called by widget_reply): dust, advice questions, staked SUI, rebalance, gather, splits …
# Returns an answer, None, or {'_rewrite': simpler sentence, '_say': note} that widget_reply reads again once.
def situations(msg, address):
    low = msg.lower()
    try:
        dr = dust_reply(msg, address)
    except Exception as exc:
        print('kivy dust:', str(exc)[:160], flush=True)
        dr = {'text': "I couldn't check your dust right now. Try again in a minute."} if DUST_RE.search(msg) else None
    if dr:
        return dr
    adv = ADVICE_RE.search(msg)
    if adv:
        c = next((ckey(w) or lend_token(w) for w in re.findall(r'[A-Za-z]{2,10}', msg) if (ckey(w) or lend_token(w)) and w.lower() not in ('buy', 'sell', 'good', 'now')), None)
        px, ch = (coin_px(c), coin_change(c)) if c else (None, None)
        fact = (' %s is $%s right now%s.' % (disp(c), dec(px, 6), (' (%s%.2f%% in 24 hours)' % ('+' if ch >= 0 else '', ch)) if ch is not None else '')) if px else ''
        return {'text': "I can't tell you whether to buy, sell or hold — nobody can know where a price goes, and that call is yours." + fact +
                        ' I can show the chart, the lending rates, or prepare a trade you decide on.'}
    if SEED_ASK_RE.search(msg):
        return {'text': "Never share your seed phrase or private key — not with Kivy, not with \"support\", not with anyone. Whoever has it owns your wallet; "
                        "anyone asking for it is trying to steal your coins. Kivy never needs it: your own wallet signs every step. Keep it written down offline. "
                        "If you already shared it, move your coins to a new wallet right away."}
    if AUTO_RE.search(POLITE_RE.sub(' ', msg)) and not QUESTION_RE.search(msg) or re.match(r'^\s*(?:can|could)\s+(?:you|kivy)\b.*\b(?:every|automatic|whenever|if\b.*\b(?:drops?|falls?|below|above))', low):
        return {'text': "Kivy doesn't run automatic, repeating or if-this-then-that moves — every transaction waits for your own approval in "
                        "your wallet, so nothing can happen while you're away. Ask me any time and I'll check the rates and prepare it then."}
    if GAS_RE.search(msg):
        return {'text': "Every Sui transaction pays a small network fee (gas) in SUI — usually under 0.01 SUI. Sending USDC is free with Kivy "
                        "(the fee is covered), but swaps, lending and perps need a little SUI. Get about 0.1 SUI from an exchange or a friend "
                        "to this wallet; that covers many transactions. Kivy can't pay gas for you."}
    if PUTBACK_RE.search(msg):
        return {'text': "A finished transaction can't be undone, but I can do the opposite move. Say where it should go, e.g. "
                        "\"withdraw my USDC from NAVI and lend it on Ember\". Ask \"what did I do today?\" to see your last moves."}
    if not address:
        return None
    rows = None
    def lent():
        nonlocal rows
        if rows is None:
            rows = lend_positions_rows(address)
        return rows
    def coin_in(text):
        return next((c for c in (lend_token(w) or ckey(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', text)
                                 if w.lower() not in ('my', 'all', 'the', 'from', 'to', 'on', 'in', 'of', 'and', 'it', 'half', 'rest', 'lent', 'navi', 'suilend', 'ember', 'main', 'prime')) if c), None)

    # rebalance: "rebalance my usdc 50/50 between ember and navi", "even out my usdc across suilend and navi"
    m = re.search(r'\b(?:re-?balance|even\s+out|balance\s+out|equalize|equalise)\b(?P<mid>.*?)\b(?:between|across|over)\s+(?P<tgt>.+?)\s*[.!?]*$', msg, re.I)
    if m:
        parts = [x.strip() for x in re.split(r'\s*(?:,|&|\band\b)\s*', m.group('tgt')) if x.strip()]
        mks = [_mk_of(x) for x in parts]
        ratio = re.search(r'(\d{1,2})\s*(?:/|-|:)\s*(\d{1,2})', m.group('mid'))
        if len(parts) == 2 and all(mks):
            coin = coin_in(m.group('mid')) or next((r['coin'] for r in lent() if _rows_on([r], mks[0]) or _rows_on([r], mks[1])), None)
            if not coin:
                return {'text': 'You have nothing lent on those markets to rebalance.'}
            w1 = (int(ratio.group(1)) / 100.0) if ratio and int(ratio.group(1)) + int(ratio.group(2)) == 100 else 0.5
            a_rows, b_rows = _rows_on(lent(), mks[0], coin), _rows_on(lent(), mks[1], coin)
            a, b = sum(r['lent'] for r in a_rows), sum(r['lent'] for r in b_rows)
            total = a + b
            if total <= 0:
                return {'text': 'You have no %s lent on those markets.' % disp(coin)}
            diff = a - total * w1
            if abs(diff) < max(0.01, total * 0.005):
                return {'text': 'Already balanced: %s %s on %s and %s on %s.' % (dec(a, 6), disp(coin), parts[0], dec(b, 6), parts[1])}
            frm_rows, to_mk = (a_rows, mks[1]) if diff > 0 else (b_rows, mks[0])
            if len(frm_rows) != 1:
                return None
            to_key = to_mk if not to_mk.startswith('fam:') else (fam_pick(lend_rows_for(coin), to_mk[4:]) or {}).get('market')   # main first (review P-13)
            if not to_key:
                return None
            return {'_rewrite': 'withdraw %s %s from %s and lend it on %s' % (dec(abs(diff), 9, False), coin, mk_name(frm_rows[0]['id']), mk_name(to_key)),
                    '_say': 'To make it %d/%d: move %s %s from %s to %s.' % (round(w1 * 100), 100 - round(w1 * 100), dec(abs(diff), 6), disp(coin),
                                                                            frm_rows[0]['name'], LEND_MKTS.get(to_key, to_key))}

    # "move half of my ember usdc to navi": the market said before the coin
    m = re.match(r'^\s*(?P<v>move|shift|transfer|switch)\s+(?P<amt>half|all|everything|a\s+third|a\s+quarter|\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?(?P<mk>[a-z]+(?:\s+(?:main|prime|ember|market))?)\s+(?P<coin>[a-z][a-z0-9]{1,9})\s+(?:to|into|over\s+to)\s+(?P<to>.+?)\s*[.!]*$', msg, re.I)
    if m and _mk_of(m.group('mk')) and (lend_token(m.group('coin')) or ckey(m.group('coin'))):
        return {'_rewrite': '%s %s of my %s from %s to %s' % (m.group('v'), m.group('amt'), m.group('coin'), m.group('mk'), m.group('to'))}

    # gather: "move all my lent usdc to the best market", "put all my usdc in one place", "move my usdc to the safest market"
    m = re.search(r'\b(?:move|put|gather|consolidate|combine|bring|shift)\b(?P<mid>.*?)\b(?:to|into|in|on)\s+(?:the\s+|a\s+|one\s+)?(?P<where>best(?:\s+(?:market|rate|yield|place))?|highest\s+(?:rate|yield)|one\s+(?:place|market)|together|single\s+market|safest(?:\s+market)?|safe\s+market|lowest[- ]risk(?:\s+market)?|(?:a\s+)?market\s+with\s+broad\w*\s+collateral)\s*[.!]*$', msg, re.I)
    if m and not re.search(r'\b(?:wallet|perps?|margin)\b', msg, re.I):
        coin = coin_in(m.group('mid'))
        mine = [r for r in lent() if not coin or r['coin'].lower() == coin.lower()]
        coins = {r['coin'] for r in mine}
        if not mine:
            return {'text': 'You have %s lent to move.' % ('no ' + disp(coin) if coin else 'nothing')}
        if len(coins) > 1:
            return {'text': 'You have several coins lent (%s). Which one should I gather? e.g. "move all my %s to the best market".' % (', '.join(sorted(map(disp, coins))), disp(sorted(coins)[0]))}
        coin = mine[0]['coin']
        safe = re.search(r'saf|risk|broad', m.group('where'), re.I)
        best = safest_market(coin) if safe else next((r['market'] for r in lend_rows_for(coin)), None)
        if not best:
            return {'text': 'No market lists %s right now.' % disp(coin)}
        src = [r for r in mine if r['id'] != best]
        if not src:
            return {'text': 'All your %s is already on %s — the %s market for it right now.' % (disp(coin), LEND_MKTS.get(best, best), 'safest' if safe else 'best')}
        why = ('the safest choice: broad collateral' if safe else 'the best rate') + ' (%.2f%% a year)' % float(next((r['total_apr'] for r in lend_rows_for(coin) if r['market'] == best), 0))
        return {'_rewrite': 'withdraw all %s from %s and lend it on %s' % (coin, ' and '.join(mk_name(r['id']) for r in src), mk_name(best)),
                '_say': 'Gathering your %s on %s — %s.' % (disp(coin), LEND_MKTS.get(best, best), why)}

    # withdraw a dollar amount / everything except N / a plain amount: the withdraw card, starting at that amount
    wto, wmsg = None, msg                             # "... and swap to SUI" / "... as SUI": the card swaps it in the same approval (2026-10-26)
    mto = re.search(r'\s+(?:as|(?:and\s+|then\s+|,\s*)*(?:swap|convert|change)(?:\s+(?!\d|half\b|a\b|some\b|most\b|part\b)\w+){0,3}?\s+(?:to|into))\s+([A-Za-z]{2,10})\s*[?.!]*\s*$', msg, re.I)
    if mto and ckey(mto.group(1)) and not market_only(mto.group(1)):
        wto, wmsg = ckey(mto.group(1)), msg[:mto.start()]
    m = re.match(r'^\s*(?:withdraw|take\s+out|pull(?:\s+out)?|redeem|take)\s+(?P<what>.+?)\s+(?:from|out\s+of|off)\s+(?P<mk>[a-z]+(?:\s+(?:main|prime|ember|market|high\s*yield))?)'
                 r'(?:\s+(?:except|but\s+keep|leaving|leave|keeping)\s+(?P<keep>\$?\s*\d+(?:\.\d+)?)\s*(?P<kc>[a-z]{2,10})?(?:\s+(?:there|in\s+it|lent))?)?\s*[.!]*$', wmsg, re.I)
    if m and _mk_of(m.group('mk')):
        what = m.group('what')
        usd = re.search(MONEY_RE, what, re.I)
        num = None if usd else re.search(r'^(?:my\s+)?(\d+(?:\.\d+)?)\s*([a-z][a-z0-9]{1,9})?(?:\s+of\s+(?:my\s+)?([a-z][a-z0-9]{1,9}))?$', what.strip(), re.I)
        allw = re.match(r'^(?:all|everything|all\s+of\s+it|it\s+all)(?:\s+(?:my\s+)?([a-z][a-z0-9]{1,9}))?$', what.strip(), re.I) if m.group('keep') else None
        if usd or num or allw:
            coin = coin_in(what) or (coin_in(m.group('kc') or '') if m.group('kc') else None)
            here = _rows_on(lent(), _mk_of(m.group('mk')), coin)
            if len(here) != 1:
                if not here:
                    return {'text': 'You have no %s lent on %s.' % (disp(coin) if coin else 'coins', m.group('mk'))}
                return None                              # several coins there and none named: the normal withdraw choice asks
            row = here[0]
            px = 1.0 if row['coin'] in ('USDC', 'USDsui', 'USDT') else float(row.get('px') or coin_px(row['coin']) or 0)
            if usd:
                if not px:
                    return {'text': "I can't get a live %s price right now to turn dollars into %s." % (disp(row['coin']), disp(row['coin']))}
                amt = float(usd.group(1) or usd.group(2)) / px
            elif allw:
                k = m.group('keep').replace('$', '').strip()
                keep = float(k) / px if '$' in m.group('keep') and px else float(k)
                amt = row['lent'] - keep
                if amt <= 0:
                    return {'text': 'You have %s %s lent on %s — nothing above %s to withdraw.' % (dec(row['lent'], 6), disp(row['coin']), row['name'], dec(keep, 6))}
            else:
                amt = float(num.group(1))
            if amt > row['lent'] + 1e-9:
                return {'text': 'You have %s %s lent on %s — less than %s.' % (dec(row['lent'], 6), disp(row['coin']), row['name'], dec(amt, 6))}
            card = withdraw_card(row, wto if wto and wto != row['coin'] else None)
            card['widget']['preset'] = round(amt, 6)
            if usd:
                card['text'] = '$%s of %s is about %s %s. ' % (usd.group(1) or usd.group(2), disp(row['coin']), dec(amt, 6), disp(row['coin'])) + card['text']
            return card

    # lend a dollar amount: "lend $5 worth of sui on navi"
    m = re.match(r'^\s*(?P<v>lend|deposit|supply|put)\s+' + MONEY_RE + r'\s+(?:worth\s+)?(?:of\s+)?(?:my\s+)?(?P<coin>[a-z][a-z0-9]{1,9})\s+(?P<rest>(?:on|in|into|at)\s+.+?)\s*[.!]*$', msg, re.I)
    if m and (lend_token(m.group('coin')) or ckey(m.group('coin'))):
        c = lend_token(m.group('coin')) or ckey(m.group('coin'))
        px = 1.0 if c in ('USDC', 'USDsui', 'USDT') else float(coin_px(c) or 0)
        if px:
            return {'_rewrite': '%s %s %s %s' % (m.group('v'), dec(float(m.group(2) or m.group(3)) / px, 6, False), c, m.group('rest'))}

    # keep some for gas: "keep 0.1 sui for gas and lend the rest of my sui on navi"
    m = re.search(r'\b(?:keep|leave)\s+(?P<k>\d+(?:\.\d+)?)\s*(?P<c>sui)\b(?:\s+(?:for\s+gas|in\s+(?:my\s+)?wallet))?\s*(?:,|and|then)?\s*(?P<rest>.+)$', msg, re.I)
    if m and re.search(r'\b(?:the\s+rest|rest\s+of|everything\s+else|remaining)\b', m.group('rest'), re.I):
        have = float((all_balances(address) or {}).get('SUI') or 0)
        amt = have - float(m.group('k'))
        if amt <= 0:
            return {'text': 'You have %s SUI — nothing left after keeping %s for gas.' % (dec(have, 6), m.group('k'))}
        new = re.sub(r'\b(?:the\s+rest\s+of\s+(?:my\s+)?sui|the\s+rest|rest\s+of\s+(?:my\s+)?sui|everything\s+else|the\s+remaining\s+sui|remaining\s+sui)\b', '%s SUI' % dec(amt, 6, False), m.group('rest'), count=1, flags=re.I)
        if new != m.group('rest'):
            return {'_rewrite': new}

    # one coin into several: "swap half my sui to usdc and the other half to usdsui", "split my sui 70/30 into usdc and deep"
    m = re.search(r'\b(?:swap|convert|split|divide|turn)\b\s+(?:half\s+(?:of\s+)?)?(?:all\s+)?(?:my\s+)?(?P<a>[a-z][a-z0-9]{1,9})\s*(?P<r>\d{1,2}\s*(?:/|-|:)\s*\d{1,2})?\s+(?:in)?to\s+(?P<b>[a-z][a-z0-9]{1,9})\s+and\s+(?:the\s+other\s+half\s+(?:in)?to\s+|the\s+rest\s+(?:in)?to\s+)?(?P<c>[a-z][a-z0-9]{1,9})\s*[.!]*$', msg, re.I)
    if m:
        a, b, c = (lend_token(m.group(x)) or ckey(m.group(x)) for x in ('a', 'b', 'c'))
        if a and b and c and len({a, b, c}) == 3:
            have = max(float((all_balances(address) or {}).get(a) or 0) - (0.05 if a == 'SUI' else 0), 0)
            if have <= 0:
                return {'text': 'You have no %s in your wallet to swap.' % disp(a)}
            r = re.findall(r'\d{1,2}', m.group('r') or '')
            w1 = int(r[0]) / 100.0 if len(r) == 2 and int(r[0]) + int(r[1]) == 100 else 0.5
            x, y = round(have * w1, 6), round(have - have * w1, 6)
            return {'text': 'A swap gives one coin, so this is two swaps — two approvals:' + (' (keeping 0.05 SUI for gas)' if a == 'SUI' else ''),
                    'widget': {'type': 'choose', 'options': [{'label': 'Swap %s %s to %s' % (dec(x, 6), disp(a), disp(b)), 'sub': '%d%%' % round(w1 * 100), 'ask': 'swap %s %s to %s' % (dec(x, 9, False), a, b)},
                                                             {'label': 'Swap %s %s to %s' % (dec(y, 6), disp(a), disp(c)), 'sub': '%d%%' % (100 - round(w1 * 100)), 'ask': 'swap %s %s to %s' % (dec(y, 9, False), a, c)}]}}

    # several coins into one: "convert my vsui and hasui to sui", "clean up my wallet"
    m = re.match(r'^\s*(?:swap|convert|turn|change|sell)\s+(?:all\s+)?(?:my\s+)?(?P<list>[a-z][a-z0-9]{1,9}(?:\s*(?:,|and|&)\s*[a-z][a-z0-9]{1,9})+)\s+(?:in)?to\s+(?P<to>[a-z][a-z0-9]{1,9})\s*[.!]*$', msg, re.I)
    clean = re.search(r'\b(?:clean(?:\s*up)?|tidy(?:\s+up)?|sweep|consolidate)\s+(?:my\s+)?(?:wallet|dust|small\s+coins|leftovers?)\b|\b(?:dust|leftovers?)\s+(?:in)?to\s+usdc\b', msg, re.I)
    if clean and re.search(r'\b(?:then|lend\w*|deposit\w*|supply|send|put|park)\b', msg, re.I):
        clean = None                                 # "sweep my dust, then lend it": the chain reader builds both steps
    allc = None if (m or clean) else ALL_COINS_RE.match(msg)   # "swap everything / all my coins / my leftover coins to X" (review P-28)
    if allc and not allc.group('to'):
        base = msg.strip().rstrip('.!')
        return {'text': 'Which coin should it all become? It is one swap per coin, each its own approval.',
                'widget': {'type': 'choose', 'options': [{'label': 'Everything to %s' % disp(t), 'ask': '%s to %s' % (base, t)} for t in DUST_TARGETS]}}
    share = 1.0
    if allc:
        sh = (allc.group('share') or '').lower().replace(' ', '')
        share = 0.5 if sh == 'half' else (float(sh[:-1]) / 100 if sh.endswith('%') else 1.0)
        if not 0 < share <= 1:
            return None
    if m or clean or allc:
        bal = {k: float(v or 0) for k, v in (all_balances(address) or {}).items()}
        to = (lend_token(m.group('to')) or ckey(m.group('to'))) if m else (lend_token(allc.group('to')) or ckey(allc.group('to'))) if allc else 'USDC'
        if allc and not to:
            return {'text': 'I don\'t know the coin "%s". Say a coin Kivy lists, e.g. "swap everything to USDC".' % allc.group('to')}
        if m:
            src = [lend_token(x) or ckey(x) for x in re.findall(r'[a-z][a-z0-9]{1,9}', m.group('list'), re.I) if x.lower() not in ('and',)]
            if not to or not all(src):
                return None
        elif allc:
            src = [c for c, v in sorted(bal.items(), key=lambda kv: -kv[1] * float(coin_px(kv[0]) or 0)) if v > 0]   # every coin held, biggest first
        else:
            src = [c for c, v in bal.items() if c not in ('SUI', 'USDC') and v > 0]
        opts, tiny = [], []
        for c in src:
            if c == to:
                continue
            v = (bal.get(c, 0) - (0.05 if c == 'SUI' else 0)) * share
            usd = v * float(coin_px(c) or 0)
            if v <= 0:
                continue
            if usd < 0.01:
                tiny.append('%s (%s)' % (disp(c), dec(v, 4)))
                continue
            opts.append({'label': ('Swap all %s to %s' if share == 1 else 'Swap ' + ('half' if share == 0.5 else '%g%%' % (share * 100)) + ' of %s to %s') % (disp(c), disp(to)),
                         'sub': '%s %s · about $%s' % (dec(v, 6), disp(c), dec(usd, 3)), 'ask': 'swap %s %s to %s' % (dec(v, 9, False), c, to)})
        note = (' Too small to swap one by one: %s.' % ', '.join(tiny)) if tiny else ''
        if tiny and to and share == 1:              # the dust goes in one sweep instead (2026-10-25); not for "half of everything
            opts.append({'label': 'Sweep the dust into %s' % disp(to), 'sub': '%d coins under $0.01 · one approval' % len(tiny), 'ask': 'swap all my dust to %s' % to})
        if not opts:
            return {'text': ('Nothing worth swapping to %s.' % disp(to)) + note}
        if len(opts) == 1 and tiny and to:          # only dust: straight to the sweep card (2026-10-25: no one-option chooser)
            return dust_reply('sweep my dust into %s' % to, address)
        if allc and len(opts) == 1 and not tiny:    # "everything" is one coin here: its swap card, no one-option chooser (review P-28)
            return {'_rewrite': opts[0]['ask'], '_say': 'Read as: "%s" (the only coin to swap).' % opts[0]['ask']}
        return {'text': ('One swap per coin — %d approvals:' % len([o for o in opts if 'dust' not in o['ask']]) if len(opts) > 1 else 'One swap:') + note, 'widget': {'type': 'choose', 'options': opts}}

    # staked SUI: "swap my sui to the best staked sui and lend it", "stake my sui"
    if re.search(r'\b(?:stake|staking|staked\s+sui|liquid\s+stak\w*|lst)\b', msg, re.I) and re.search(r'\bsui\b', msg, re.I) and not re.search(r'\bunstake\b', msg, re.I) \
            and not re.search(r'\b(?:withdr[ae]w\w*|send|then|move)\b|,', msg, re.I) and not STAKED_NAMED.search(msg):   # more steps / a named LST: the chain reader (2026-10-25)
        lend_at = re.search(r'\b(?:lend|deposit|put|park|supply)\w*\b(.*)$', msg, re.I)
        mk = _mk_of(lend_at.group(1)) if lend_at else None
        ranked = staked_ranked(mk)                   # "…and lend it on suilend": the best staked SUI there
        if not ranked:
            return None
        t, r = ranked[0]
        where = LEND_MKTS.get(r['market'], r['market']) if mk else None
        am = re.search(r'(?<![\w.$])(\d+(?:\.\d+)?)\s+sui\b|\b(half|all)\s+(?:of\s+)?(?:my\s+)?sui\b', msg, re.I)   # "stake 0.5 sui": every option keeps the 0.5 (2026-11-13)
        qs = ('%s sui' % am.group(1)) if am and am.group(1) else ('%s my sui' % am.group(2).lower()) if am else 'my sui'
        if re.search(r'\b(?:lend|deposit|earn|and\s+put)\b', msg, re.I):
            return {'_rewrite': 'swap %s to %s and lend it on %s' % (qs, t, mk_name(r['market'])),
                    '_say': staked_line(ranked, where) + (' That is on top of the staking reward built into its price.' if where else
                                                          ' It lends on %s, on top of the staking reward built into its price.' % LEND_MKTS.get(r['market'], r['market']))}
        plain = next((x for x in lend_rows_for('SUI')), None)
        opts = [{'label': 'Swap to %s and lend it' % t, 'sub': 'staking reward + %.2f%% a year on %s' % (float(r['total_apr']), LEND_MKTS.get(r['market'], r['market'])),
                 'ask': 'swap %s to the best staked sui and lend it' % qs},
                {'label': 'Just swap to %s' % t, 'sub': 'staking reward only, kept in your wallet', 'ask': 'swap %s to %s' % (qs, t)}]
        if plain:
            opts.append({'label': 'Lend plain SUI', 'sub': '%.2f%% a year on %s' % (float(plain['total_apr']), LEND_MKTS.get(plain['market'], plain['market'])),
                         'ask': 'lend %s on %s' % (qs if am else 'sui', mk_name(plain['market']))})
        return {'text': 'Kivy stakes through liquid staking: swap SUI into sSUI, vSUI or haSUI (each earns the staking reward in its price), and lending it earns more on top.',
                'widget': {'type': 'choose', 'options': opts}}

    # close a position, then lend: the close is its own transaction — say the next step instead of dropping it
    m = re.search(r'^(?P<close>.*\bclose\b.*?)\s*(?:,|and\s+then|then|and)\s+(?:then\s+)?(?:lend|deposit|put|park)\s+(?:it|the\s+money|the\s+usdc|the\s+margin|that)\s+(?:on|in|into|at)\s+(?P<to>.+?)\s*[.!]*$', msg, re.I)
    if m and _mk_of(m.group('to')) and not re.search(r'\b(?:swap|send|convert)\b|\b(?:withdraw\w*|take|pull|get|cash)\s+(?:out\s+)?(?:\$?\d|half\b|a\s+(?:third|quarter)\b)', m.group('close'), re.I):   # "close X, withdraw HALF the margin
        r = widget_reply(m.group('close'), address)   # and lend it on navi": the close reader's own next step keeps the half (scan 2026-11-14 C4)
        if r and isinstance(r.get('widget'), dict) and str(r['widget'].get('type', '')).startswith('perp'):
            if r['widget'].get('type') == 'perpclose':     # the page sends it once the close has landed (2026-11-11)
                r['text'] = re.sub(r'\s*When it has closed,.*$', '', r.get('text') or '') + ' When it has closed, lending what it frees comes up for its own approval.'
                r['widget']['next'] = 'move my perps margin to %s' % m.group('to')
            else:
                r['text'] = (r.get('text') or '') + ' After it closes, say "move my perps margin to %s" to lend what it frees — that is a second approval.' % m.group('to')
            return r
    return None


RATE_Q_RE = re.compile(r"\b(?:rates?|apy|apr|interest|yield|pays?|paying|earns?|earning|returns?|better|higher|more)\b", re.I)
# Only "more" / "pays" / "better" in a why / explain / who-pays question is not a rate question (2026-11-10: "why did I get more
# USDC than the minimum?" got USDC rates, "explain what gas is on Sui and who pays it" got SUI rates).
STRONG_RATE_RE = re.compile(r"\b(?:rates?|apy|apr|interest|yield|earns?|earning|returns?)\b", re.I)
EXPLAIN_Q_RE = re.compile(r"^\s*(?:why|explain|how\s+come|what\s+(?:is|are)\s+(?:a\s+|an\s+)?(?:gas|slippage|a\s+minimum|the\s+minimum))\b|\bwho\s+pays\b"
                          r"|\bthan\s+(?:the\s+)?(?:minimum|min|quote|quoted|estimate|expected|card|preview)\b", re.I)
WORTH_RE = re.compile(r"^\s*(?:(?:what(?:'s|\s+is|\s+are)|how\s+much\s+(?:is|are))\s+(?:all\s+)?my\s+(?:\d+(?:\.\d+)?\s+)?([A-Za-z][A-Za-z0-9]{1,9})(?:\s+(?:lent|position|deposit))?\s+worth\b"
                      r"|(?:the\s+)?(?:value|worth)\s+of\s+my\s+([A-Za-z][A-Za-z0-9]{1,9})\b(?:\s+(?:now|today|right\s+now|rn|in\s+(?:usd|dollars)))?\s*[?.!]*\s*$"
                      r"|(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:holding\s+)?value(?:\s+(?:now|in\s+(?:usd|dollars)))?\s*[?.!]*\s*$)", re.I)
QUOTE_HINT_RE = re.compile(r"\bquote\b|\bwould\s+(?:become|be|turn\s+into|get\s+me|give\s+me)\b|\bhow\s+much\s+[A-Za-z][A-Za-z0-9]{1,9}\s+(?:would|will|do|can|could|should)\s+i\s+get\b"
                           r"|\bjust\s+a\s+price\b|\bworth\s+in\s+[A-Za-z]|\bwould\b.{0,30}\b(?:get|give)\s+me\b|\bhow\s+much\s+would\s+(?:that|it|this)\s+be\b"
                           r"|\bwhat\s+would\s+(?:arrive|come\s+out|i\s+get)\b|\bpricing\s+it\b|\bhow\s+much\s+[A-Za-z][A-Za-z0-9]{1,9}\s+is\s+\d"
                           r"|^\s*\$?\d+(?:\.\d+)?\s*[A-Za-z][A-Za-z0-9]{1,9}\s+(?:in|to|into)\s+[A-Za-z][A-Za-z0-9]{1,9}\s*\?\s*$", re.I)
CHART_WORD_RE = re.compile(r"\b(?:chart|graph|candles?|price\s+line)\b", re.I)
TF_WORDS = [(r'\b1\s*(?:m|min|minute)\b', '1m'), (r'\b15\s*(?:m|min|mins|minutes?)\b', '15m'), (r'\b30\s*(?:m|min|mins|minutes?)\b', '30m'),
            (r'\b(?:1\s*(?:h|hr|hour)|hourly|one\s+hour)\b', '1h'), (r'\b4\s*(?:h|hrs?|hours?)\b', '4h'), (r'\b(?:1\s*(?:d|day)|daily|one\s+day)\b', '1d'),
            (r'\b3\s*(?:d|days?)\b', '3d'), (r'\b(?:1\s*(?:w|wk|week)|weekly|one\s+week)\b', '1w')]


def holding_value(address, c):
    """One coin the wallet holds — in the wallet and lent in each market — at the live price."""
    px = float(coin_px(c) or 0)
    try:
        have = float((all_balances(address) or {}).get(c) or 0)
    except Exception:
        have = 0.0
    lent = [{'market': r['name'], 'amount': float(r['lent']), 'usd': float(r['lent']) * (px or float(r.get('px') or 0))}
            for r in lend_positions_rows(address) if str(r.get('coin')).lower() == str(c).lower() and float(r.get('lent') or 0) > 0]
    return {'coin': disp(c), 'price_usd': px, 'wallet': have, 'wallet_usd': have * px, 'lent': lent,
            'total_usd': have * px + sum(x['usd'] for x in lent)}


def source_answers(msg, address):
    """Common questions answered from the source — live rates, the wallet's own positions, the router's quote, Aftermath's
    candles — never the AI (2026-10-31: "it should be called from source"). None = not one of these."""
    low = msg.lower()
    words = re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', msg)
    trade = re.search(r'^\s*(?:please\s+)?(?:swap|send|transfer|lend|deposit|withdraw|move|claim|long|short|close|buy|sell|put|convert|shift|pull|grab|stick|take|bring|get|redeem|supply|stake|unstake|open|exit|rebalance|sweep)\b', msg, re.I) \
        or re.search(r'\b(?:from|out\s+of)\b.{1,60}\b(?:to|into|in|on)\b', msg, re.I) and G_VERB_RE.search(msg)   # a move from one place to another is an order
    # a chart with a timeframe, or in a beginner's words: "chart sui 4h", "btc daily chart", "could I see the SUI price line?"
    if CHART_WORD_RE.search(msg) and not trade and not re.search(r'\$\s*\d|\d\s*x\b|\b(?:long|short)\s+[a-z]', msg, re.I):
        sym = next((chart_symbol(w) for w in words if w.lower() not in TRADE_STOP | {'chart', 'graph', 'price', 'line', 'see', 'show', 'candle', 'candles',
                                                                                  'daily', 'weekly', 'hourly', 'could', 'can', 'the', 'not', 'buy', 'sell', 'asking',
                                                                                  'anything', 'ready', 'whenever', 'you', 'please', 'me', 'let', 'look'} and chart_symbol(w)), None)
        if sym:
            tf = next((t for pat, t in TF_WORDS if re.search(pat, low)), '15m')
            img = None
            try:
                img = perp_market(sym).get('image')
            except Exception:
                pass
            return {'text': 'Live %s / USD chart, %s candles (Aftermath perps price).' % (sym, tf), 'widget': {'type': 'chart', 'symbol': sym, 'tf': tf, 'image': img}}
    # "bring up my Aftermath perps account overview", "lemme see the positions in my perps account": the perps cards
    if address and not trade and re.search(r'\b(?:peek\s+at|look\s+at|check|see|show(?:\s+me)?)\s+my\s+perps?\s*[?.!]*\s*$', msg, re.I):
        return widget_reply('my perps account', address)
    if address and not trade and re.search(r'\b(?:bring\s+up|pull\s+up|show|see|view|check|look\s+at)\b.{0,30}\bperps?\s+(?:account|positions?)\b'
                                           r'|\bpositions?\s+in\s+my\s+(?:aftermath\s+)?perps?\s+account\b', msg, re.I) \
            and not re.search(r'\b(?:deposit|withdraw|close|add|fund|top\s*up|move)\b', msg, re.I):
        return widget_reply('my perps positions' if re.search(r'\bpositions?\b', msg, re.I) else 'my perps account', address)
    # "show my lending positions", "what have I lent": every position, live, one tap to withdraw
    if address and re.match(r"^\s*(?:(?:show|see|check|view|list)\s+(?:me\s+)?)?(?:all\s+)?my\s+(?:lending|lent|lend|savings?)\s+(?:positions?|deposits?|coins?)\s*[?.!]*\s*$"
                            r"|^\s*what\s+(?:do\s+i\s+have|have\s+i)\s+lent(?:\s+out)?\s*[?.!]*\s*$"
                            # 2026-11-13: "show my lends" opened the markets table; "how much have i lent", "what am i lending" went to the AI
                            r"|^\s*(?:(?:show|see|check|view|list)\s+(?:me\s+)?)?(?:all\s+)?my\s+(?:lends|lendings|loans|lent)\s*[?.!]*\s*$"
                            r"|^\s*how\s+much\s+(?:have\s+i|did\s+i|do\s+i\s+have|i\s+have)\s+(?:lent|lended|loaned)(?:\s+out)?(?:\s+(?:in\s+total|so\s+far|now))?\s*[?.!]*\s*$"
                            r"|^\s*what\s+am\s+i\s+lending(?:\s+(?:out|now|right\s+now))?\s*[?.!]*\s*$|^\s*what(?:'s|\s+is)\s+(?:lent|on\s+loan)(?:\s+out)?\s*[?.!]*\s*$", msg, re.I):
        rows = lend_positions_rows(address)
        if not rows:
            return {'text': 'You have nothing lent right now. Say "lend" to see the markets.'}
        return {'text': 'Here is what you have lent. Tap one to withdraw.', 'widget': {'type': 'positions', 'rows': rows}}
    um = re.match(r"^\s*(?:what(?:'s|\s+is)\s+(?:1|one)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+worth|what(?:'s|\s+is)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+at)(?:\s+(?:right\s+)?now)?\s*[?.!]*\s*$", msg, re.I)
    if not um:
        um = re.match(r"^\s*(?:what(?:'s|\s+is)\s+(?:1|one)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+worth|what(?:'s|\s+is)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+at)(?:\s+(?:right\s+)?now)?\s*[?.!]*\s*$",
                      POLITE_RE.sub('', msg).lstrip(' ,'), re.I)   # "if you don't mind, what's sui at right now?"
    if um and (ckey(um.group(1) or um.group(2)) or lend_token(um.group(1) or um.group(2))):   # "what's 1 sSUI worth", "what's sui at right now"
        c = ckey(um.group(1) or um.group(2)) or lend_token(um.group(1) or um.group(2))
        px = coin_px(c)
        if px:
            return {'text': '%s is $%s right now.' % (disp(c), dec(px, 6))}
    if address and re.search(r"\b(?:value|worth)\s+of\s+(?:everything|all)\s+(?:i(?:'ve|\s+have)?\s+)?lent\b|\bhow\s+much\s+is\s+(?:all\s+)?(?:my\s+)?(?:lent|lending|lend)\s+(?:money|stuff|coins|positions?)?\s*worth\b"
                             r"|\bwhat(?:'s|\s+is)\s+(?:all\s+)?my\s+(?:lending|lent\s+\w+)\s+worth\b", msg, re.I):
        rows, tot = [], 0.0                              # "value of everything I have lent": each position at the live price
        for r in lend_positions_rows(address):
            v = float(r['lent']) * float(coin_px(r['coin']) or r.get('px') or 0)
            rows.append('• %s %s on %s ≈ $%s' % (dec(r['lent'], 6), disp(r['coin']), r['name'], dec(v, 4))); tot += v
        if not rows:
            return {'text': 'You have nothing lent right now.'}
        return {'text': 'Everything you have lent is worth about $%s:\n%s' % (dec(tot, 4), '\n'.join(rows))}
    # "what is my sSUI worth": that coin in the wallet and in each market, at the live price
    wm = WORTH_RE.match(msg)
    if wm and address:
        w0 = wm.group(1) or wm.group(2) or wm.group(3)
        c = ckey(w0) or lend_token(w0)
        if c:
            h = holding_value(address, c)
            px = h['price_usd']
            lines = (['• %s %s in your wallet ≈ $%s' % (dec(h['wallet'], 6), disp(c), dec(h['wallet_usd'], 4))] if h['wallet'] > 0 else []) + \
                ['• %s %s lent on %s ≈ $%s' % (dec(x['amount'], 6), disp(c), x['market'], dec(x['usd'], 4)) for x in h['lent']]
            if not lines:
                return {'text': 'You don\'t hold any %s right now%s.' % (disp(c), (' (%s is $%s)' % (disp(c), dec(px, 6))) if px else '')}
            return {'text': 'Your %s is worth about $%s at $%s each:\n%s' % (disp(c), dec(h['total_usd'], 4), dec(px, 6), '\n'.join(lines))}
    # a quote without a card: "quote for $0.40 worth of my SUI in USDC", "what 0.19 SUI would become in USDC", "how much SUI do I get for my USDsui"
    if QUOTE_HINT_RE.search(msg) and not trade:
        a = b = None
        amt, usd, note = 0.0, None, ''
        um = re.search(r'\$\s*(\d+(?:\.\d+)?)\s*(?:worth\s+)?(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})', msg)
        nm = re.search(r'(?<![\w.$])(\d+(?:\.\d+)?)\s*([A-Za-z][A-Za-z0-9]{1,9})', msg)
        mm = re.search(r'\b(?:for|from)\s+(?:all\s+)?my\s+([A-Za-z][A-Za-z0-9]{1,9})', msg, re.I)
        if um and ckey(um.group(2)):
            a, usd = ckey(um.group(2)), float(um.group(1))
            amt = usd / float(coin_px(a) or 0) if coin_px(a) else 0
        elif nm and ckey(nm.group(2)):
            a, amt = ckey(nm.group(2)), float(nm.group(1))
        elif mm and ckey(mm.group(1)) and address:      # "for my USDsui": what they hold of it, wallet first, then what is lent
            a = ckey(mm.group(1))
            try:
                amt = float((all_balances(address) or {}).get(a) or 0)
            except Exception:
                amt = 0.0
            if amt <= 0:
                lent = [r for r in lend_positions_rows(address) if str(r.get('coin')).lower() == a.lower()]
                amt = sum(float(r.get('lent') or 0) for r in lent)
                if amt > 0:
                    note = ' That is your %s lent on %s; withdrawing and swapping is one approval ("withdraw my %s as %%s").' % (
                        disp(a), ' and '.join(r['name'] for r in lent), a)
            if amt <= 0:
                return {'text': 'You don\'t hold any %s right now.' % disp(a)}
        if a:
            gm = re.search(r'\bhow\s+much\s+([A-Za-z][A-Za-z0-9]{1,9})\s+(?:(?:would|will|do|can|could|should)\s+i\s+get|is\s+\d)', msg, re.I)
            if gm and ckey(gm.group(1)):
                b = ckey(gm.group(1))
            else:
                b = next((ckey(t) for t in re.findall(r'\b(?:in|into|to|for|as)\s+([A-Za-z][A-Za-z0-9]{1,9})', msg, re.I) if ckey(t) and ckey(t) != a), None)
        if a and b and a != b and amt > 0:
            try:
                out = float(swap_quote(a, b, amt)['out'])
            except Exception:
                return {'text': "I couldn't get a quote from the swap router right now. Try again in a minute."}
            what = ('$%s of %s (%s %s)' % (num_plain(usd), disp(a), dec(amt, 6), disp(a))) if usd else '%s %s' % (dec(amt, 6), disp(a))
            return {'text': '%s ≈ %s %s right now (the swap router\'s quote, before Kivy\'s fee and gas). Nothing moves until you ask for the swap and approve it.%s'
                            % (what, dec(out, 6), disp(b), (note % b) if note else '')}
    # "what rate does NAVI pay for USDsui": the live rate in that protocol (or every market), and the best one anywhere
    if RATE_Q_RE.search(re.sub(r'\b(?:sui\s+)?high\s+yield\b', ' ', msg, flags=re.I)) and not trade and not EARN_RE.search(msg) and not re.search(r'\d', msg) and not FUNDING_Q_RE.search(msg) \
            and not (not STRONG_RATE_RE.search(msg) and EXPLAIN_Q_RE.search(msg)) and not re.match(r'\s*(?:why|how\s+come|explain)\b', msg, re.I) \
            and (QUESTION_RE.search(msg) or msg.strip().endswith('?') or re.match(r'^\s*(?:which|where|does|do|is|are|whats|what)\b', msg, re.I)
                 or re.search(r'\b(?:apy|apr|rates?|interest)\b', msg, re.I)):
        tok = next(('USDC' if w.lower() == 'usdc' else lend_token(w) for w in words
                    if w.lower() not in ('rate', 'rates', 'pay', 'pays', 'apy', 'apr', 'yield', 'on', 'for', 'navi', 'suilend', 'ember', 'prime', 'main')
                    and ('USDC' if w.lower() == 'usdc' else lend_token(w))), None)
        if tok:
            both = re.search(r'\bnavi\b', low) and SUILEND_MKT_RE.search(low)   # "is ember or navi better": every market
            proto = None if both else 'navi' if re.search(r'\bnavi\b', low) else 'suilend' if SUILEND_MKT_RE.search(low) else \
                'other' if re.search(r'\b(?:scallop|bucket|aave|alphalend|kai)\b', low) else None
            rows = sorted([r for r in lend_rates_all() if not r.get('rate_only') and
                           str({'USDSUI': 'USDsui'}.get(r.get('token'), r.get('token')) or r['asset']).lower() == tok.lower()], key=lambda r: -float(r['total_apr']))
            name = lambda r: LEND_MKTS.get(r['market'], r.get('name') or r['market'])
            if rows and proto in ('navi', 'suilend'):
                sub_ = re.search(r'\b(ember|matrixdock|prime|high\s+yield|main)\b', low)
                mine = [r for r in rows if str(r['market']).startswith(proto) and (not sub_ or sub_.group(1).split()[0] in name(r).lower())]
                best = rows[0]
                if not mine:
                    return {'text': '%s doesn\'t list %s%s. The best %s rate right now is %s at %.2f%% a year.' % (
                        'NAVI' if proto == 'navi' else 'Suilend', disp(tok), (' in a %s market' % sub_.group(1).title() if sub_ else ''), disp(tok), name(best), float(best['total_apr'])),
                        'widget': {'type': 'choose', 'options': [{'label': 'Lend %s on %s' % (disp(tok), name(best)), 'sub': '%.2f%% a year' % float(best['total_apr']),
                                                                  'ask': 'lend %s on %s' % (disp(tok), name(best))}]}}
                text = '%s on %s right now:\n%s' % (disp(tok), 'NAVI' if proto == 'navi' else 'Suilend', '\n'.join('• %s — %.2f%% a year' % (name(r), float(r['total_apr'])) for r in mine[:4]))
                text += ('\nThat is the best %s rate anywhere.' % disp(tok)) if mine[0] is best else \
                    ('\nThe best anywhere is %s at %.2f%%.' % (name(best), float(best['total_apr'])))
                opts = [{'label': 'Lend %s on %s' % (disp(tok), name(mine[0])), 'sub': '%.2f%% a year' % float(mine[0]['total_apr']), 'ask': 'lend %s on %s' % (disp(tok), name(mine[0]))},
                        {'label': 'See all %s markets' % disp(tok), 'sub': 'with collateral', 'ask': 'lending markets for %s' % disp(tok)}]
                return {'text': text + '\nRates change with demand.', 'widget': {'type': 'choose', 'options': opts}}
            if rows:
                note = 'Kivy lends on Suilend and NAVI only. ' if proto == 'other' else ''
                br = best_reply('best rate for %s' % tok, address)
                if br:
                    return dict(br, text=note + br['text'])
    return None


def quick_answers(msg, address):
    """Plain questions with a definite answer, before any pattern could misread them (role-play review, 2026-10-23)."""
    low = msg.lower()
    # a bank, card or cash-out: Kivy moves coins between Sui addresses only
    if (BANK_RE.search(msg) or re.search(r'\b(?:dollar\s+bills|actual\s+(?:dollars|cash|money)|physical\s+(?:cash|money|dollars)|bank\s*notes|cash\s+in\s+hand)\b', low)) \
            and re.search(r'\b(?:send|transfer|withdraw|move|pay|cash|get|deposit|wire)\b', low) and not re.search(r'\b(?:un)?like\s+(?:a|at\s+a)\s+bank\b', low):
        return {'text': "I can only move coins between Sui addresses — I can't send to a bank account or card. To cash out, people usually "
                        "send USDC to their own account on an exchange that supports Sui, sell it there, and withdraw to their bank."}
    # "which is better, lending or perps?" — a comparison, not a market list
    if re.search(r'\blend\w*\b', low) and re.search(r'\bperps?\b|\btrad\w*\b|\bleverage\b', low) and re.search(r'\b(?:better|vs|versus|or|differen\w*|compare)\b', low) \
            and not re.search(r'\b(?:move|withdraw|deposit|swap|send|take|put|open|close)\b', low):
        try:
            rr = sorted(float(v.get('total_apr') or 0) for v in lend_rates().values() if float(v.get('total_apr') or 0) > 0)
            band = 'about %.0f–%.0f%% a year on USDC right now' % (rr[0], rr[-1]) if rr else 'a steady yearly rate'
        except Exception:
            band = 'a steady yearly rate'
        return {'text': "They're different jobs. Lending earns " + band + " and your deposit stays the same size — "
                        "the risk is the market's collateral. Perps is trading: with leverage your gains and losses are multiplied, and a big enough "
                        "move against you liquidates the position. For income, lending; for a view on a price, perps — with money you can afford to lose.",
                'widget': None}
    # "how close am i to liquidation?" — each open position: price, liquidation price, how far away
    if re.search(r'\bliquidat\w*|\bliq\b', low) and re.search(r'\b(?:how\s+(?:close|far|near)|distance|am\s+i|risk|safe|danger|near|liquidation\s+price|liq\s+price|get\s+liquidated|liquidated\s+at)\b', low) \
            and not LEND_WORD_RE.search(msg) and not IF_Q_RE.search(msg) and not DEFINE_Q_RE.search(msg) and not re.search(r'\bif\s+i\b', low) \
            and address:                              # "if I short BTC at 5x how far…?", "what does liquidation price mean?": not my positions (2026-11-10)
        try:
            r = PERPACCT.need(lend_cli('perp-positions', address, timeout=40))   # several perps accounts: which one? (BLD-V1)
            pos = r.get('positions') or []
            if not pos:
                return {'text': "You have no open perps positions, so nothing can be liquidated right now."}
            mk = {x['symbol']: x for x in perp_market_list()}
            lines = []
            for p in pos:
                px, lq = float((mk.get(p['symbol']) or {}).get('price') or 0), float(p.get('liquidation') or 0)
                if not (px and lq):
                    continue
                gap = (px - lq) / px * 100 if p['side'] == 'long' else (lq - px) / px * 100
                lines.append('• %s %s %gx: price %s, liquidation %s — %.1f%% %s.%s' % (p['symbol'], p['side'], float(p.get('leverage') or 0), fmt_px(px), fmt_px(lq),
                             abs(gap), 'away' if gap > 0 else 'PAST it', ' That is near — adding margin pushes it further away.' if 0 < gap < 5 else ''))
            if lines:
                return {'text': 'How far each position is from liquidation:\n' + '\n'.join(lines), 'widget': {'type': 'perppos', **r}}
        except Exception:
            pass
    # goals — "passive income", "earn something on my money", "what should I do with my idle usdc": a personal answer from what
    # they hold and today's rates, with the next step one tap away (role-play review 2026-10-23: the AI kept asking instead)
    if address and re.search(r'\bpassive\s+income\b|\b(?:earn|make)\s+(?:something|money|interest|yield|a\s+return)\b|\bidle\b|\bput\s+(?:my\s+)?(?:money|usdc|funds?)\s+to\s+work\b|\bgrow\s+my\s+(?:money|savings|funds?)\b', low) \
            and not re.search(r'\b\d', low) and not re.search(r'\b(?:swap|send|withdraw|remove|pull|move|then|close|long|short)\b|\btake\s+(?:\S+\s+){0,3}out\b', low) \
            and not re.search(r'\bhow\s+do(?:es)?\s+(?:you|kivy)\s+make\s+money\b', low):   # a stated move is a chain, not advice; "how do you make money" is about Kivy's fee
        try:
            hold = all_balances(address) or {}
            with PERPACCT.quiet():                       # advice from what they hold: never "which perps account?" (BLD-V1)
                perp_free = float(perp_account(address).get('collateral') or 0)
            stab = {c: float(hold.get(c) or 0) for c in ('USDC', 'USDsui') if float(hold.get(c) or 0) >= 0.5}
            best = {}
            for r in lend_rates_all():
                if not r.get('rate_only') and (r['asset'] not in best or float(r['total_apr']) > float(best[r['asset']]['total_apr'])):
                    best[r['asset']] = r
            bu = best.get('USDC')
            if re.search(r'\blow\s+risk|\bsafe|\bcareful|\bconservative', low):   # low risk: the best rate among markets with broad collateral (5+ coins)
                col = lend_collateral()
                broad = [r for r in lend_rates_all() if r['asset'] == 'USDC' and not r.get('rate_only') and len((col.get(r['market']) or {}).get('coins') or []) >= 5]
                if broad:
                    bu = max(broad, key=lambda r: float(r['total_apr']))
            lines, opts = [], []
            if bu and (perp_free >= 0.5 or stab):
                nm = LEND_MKTS.get(bu['market'], bu.get('name') or bu['market'])
                if stab:
                    c, a = max(stab.items(), key=lambda x: x[1])
                    lines.append('You have %s %s sitting in your wallet — lent on %s it would earn about %.2f%% a year (≈ $%.2f).' % (dec(a), disp(c), nm, float(bu['total_apr']), a * float(bu['total_apr']) / 100))
                    opts.append({'label': 'Lend my %s on %s' % (disp(c), nm), 'sub': '%.2f%% a year' % float(bu['total_apr']), 'ask': 'lend all my %s on %s' % (c, nm)})
                if perp_free >= 0.5:
                    lines.append('Your perps account has %s USDC free and not working — moved to %s it would earn about %.2f%% a year (≈ $%.2f).' % (
                        dec(perp_free), nm, float(bu['total_apr']), perp_free * float(bu['total_apr']) / 100))
                    opts.append({'label': 'Move perps USDC to %s' % nm, 'sub': 'one approval', 'ask': 'withdraw all my usdc from aftermath and lend it on %s' % nm})
            sui = spendable('SUI', hold)
            bs = best.get('SUI')
            if bs and sui * float(coin_px('SUI') or 0) >= 1:
                lines.append('Your %s SUI could earn about %.2f%% a year on %s (its price still moves with SUI).' % (dec(sui), float(bs['total_apr']), LEND_MKTS.get(bs['market'], bs.get('name') or bs['market'])))
                opts.append({'label': 'Lend my SUI', 'sub': '%.2f%% a year' % float(bs['total_apr']), 'ask': 'lend all my sui on %s' % LEND_MKTS.get(bs['market'], bs['market'])})
            if lines:
                low_risk = (' I picked %s for low risk: its borrowers post many large coins as collateral, so the risk is spread (no market is risk-free). '
                            'Ember pays more but takes only one collateral coin.' % LEND_MKTS.get(bu['market'], bu['market'])) if bu and re.search(r'\blow\s+risk|\bsafe|\bcareful|\bconservative', low) else ''
                opts.append({'label': 'See all lending markets', 'sub': 'rates and collateral', 'ask': 'lending markets'})
                return {'text': 'The steady way to earn is lending.\n' + '\n'.join('• ' + x for x in lines) + low_risk, 'widget': {'type': 'choose', 'options': opts[:4]}}
        except Exception:
            pass
    # "what is the funding rate on btc?" — the live number
    if FUNDING_Q_RE.search(msg) and re.search(r'\b(?:what|how\s+much|current|rate|now)\b', low):
        sym = next((chart_symbol(w) for w in TICKER_WORD_RE.findall(msg) if w.lower() not in TRADE_STOP | {'funding', 'what', 'rate', 'how', 'much', 'is', 'on', 'now', 'current'} and chart_symbol(w)), None)
        if sym:
            r = next((x for x in perp_market_list() if x['symbol'] == sym), None)
            if r and r.get('funding') is not None:
                f = float(r['funding'])
                who = 'longs pay shorts' if f > 0 else 'shorts pay longs' if f < 0 else 'no one pays right now'
                return {'text': '%s funding is %+.4f%% per hour right now (%s) — about %+.2f%% a day if it stays there. It changes every hour '
                                'with the gap between the perps price and the index.' % (sym, f, who, f * 24)}
    return None


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import (  # noqa: E402
    BANK_RE, FUNDING_Q_RE, TRADE_STOP, chart_symbol, fmt_px, num_plain, perp_account, perp_market,
    perp_market_list)
from .journeys import DEFINE_Q_RE, EARN_RE, best_reply  # noqa: E402
from .hygiene import ADVICE_RE, SEED_ASK_RE  # noqa: E402
from .readguard import G_VERB_RE, POLITE_RE, _gcoin  # noqa: E402
from .check2 import intent_actions  # noqa: E402
from .chainreads import (  # noqa: E402
    ADDR_RE, COINS, NET, _BAL, all_balances, balances, cinfo, ckey,
    coin_icon, dec, ecoins, other_balances)
from .lending import (  # noqa: E402
    LEND_MKTS, LEND_WORD_RE, coin_change, coin_px, lend_collateral, lend_positions_rows, lend_rates, lend_rates_all,
    lend_rows_for, lend_token, market_only, spendable, withdraw_card)
from .chains import MKT_EXACT, chain_market, fam_pick  # noqa: E402
from .txreads import kivy_fee, swap_quote  # noqa: E402
