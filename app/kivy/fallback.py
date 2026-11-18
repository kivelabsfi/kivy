"""The rule-based reply() fallback (AI off or busy), FAQ, recipients, SuiNS, gasless (Section 14).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# reply() is called by the Handler and intent; resolve_recipient / recipient_check by the send readers and routes.
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import re
import time
from . import amounts as AM
from .config import K, cap_usd


ORDER_RE = re.compile(r'^\s*(?:please\s+|pls\s+)?(?:swap|convert|trade|buy|sell|send|transfer|pay|lend|deposit|supply|withdraw|redeem)\b')
MKT_WORDS_RE = re.compile(r'\b(?:navi|suilend|ember|matrixdock|prime|high\s*yield|sui\s+prime|main\s+market)\b')
# an order the plain rules can't read in full: say so and how to say it for the exact readers — never a card from half a sentence
UNREAD = ("I can't read that request without the AI, and it isn't answering right now, so I haven't prepared anything. "
          'Say it as one plain order and I can do it without the AI — e.g. "swap 5 SUI to USDC", "send 1 USDC to name.sui", '
          '"lend 5 USDC on NAVI" or "withdraw 5 USDC from NAVI". Or try again in a minute.')


# ── Section 14: rule-based reply() fallback (used when the AI is off/busy), FAQ, recipient checks + SuiNS, gasless sends ──
# An amount word or number from reply()'s patterns -> a float ('all' / 'half' read against the wallet).
def _amt(v, coin, address):
    try:
        return float(v)
    except ValueError:
        w = word_amount(v, ckey(coin) or coin, all_balances(address) if address else None)
        return w if w is not None else 0.0


def reply(text, address=None):
    """Plain-rule order reader. Returns {'text', 'plan'?}. Never executes."""
    _g = guarded(reply, text, address)              # the read guard first (review 2026-10-29)
    if _g != 'go':
        return _g or {'text': 'That reads as a question or a "don\'t", so I haven\'t prepared anything to sign. Say the move plainly when you want it, e.g. "swap 5 SUI to USDC".'}
    t = ' '.join(text.strip().split())
    low = t.lower()
    num = r'(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?|all|max|everything|half|a\s+quarter|a\s+third)'
    asset = r'([a-zA-Z]+)'
    # The whole sentence or nothing (review scan R7, 2026-11-15): these patterns read the first words only, so "lend 3 to my
    # brother he needs it" became a Lend 3 USDC card while the AI was down. END = nothing left but a market name and a full stop.
    END = r'(?:\s+(?:please|pls|now|thanks))?\s*[.!]*\s*$'
    whole = lambda rest: not re.sub(r'\b(?:on|in|at|into|to|from|out\s+of|the|my|market|main|please|pls|now)\b|[.,!]', ' ', MKT_WORDS_RE.sub(' ', rest)).strip()
    m = (re.match(r'^(?:swap|convert|trade)\s+' + num + r'\s+' + asset + r'\s+(?:to|for|into)\s+' + asset + END, low)
         or re.match(r'^(?:buy)\s+' + asset + r'\s+with\s+' + num + r'\s+' + asset + END, low))
    if m and low.startswith('buy'):
        m = (m.group(2), m.group(3), m.group(1))
    elif m:
        m = (m.group(1), m.group(2), m.group(3))
    if m:
        a, b = m[1].upper(), m[2].upper()
        amt = _amt(m[0], a, address)
        return plan('swap', amt, a, b, holdings=all_balances(address) if address else None)
    m = re.match(r'^(?:send|transfer|pay)\s+' + num + r'\s+' + asset + r'\s+to\s+(.+)$', t, re.I)
    if m and AM.long_address(t):
        return {'text': AM.long_address(t)}
    if m and not re.fullmatch(r'(?:(?:this|that|the|my\s+friend(?:\'s)?|my|wallet|address|account)\s*:?\s*)*(?:' + AM.RCPT + r')\s*[.!]*', m.group(3).strip(), re.I):
        m = None                                     # words after the recipient, or no full address / name: not read here
    if m:   # 'to this wallet : 0x…' / 'to my friend alice.sui': take the address or name wherever it is
        tail = m.group(3)
        found = (re.search(AM.HEX_ADDR, tail) or re.search(r'@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b', tail, re.I)
                 or re.search(r'@[a-z0-9-]+', tail, re.I) or re.match(r'(\S+)', tail))
        return plan('send', _amt(m.group(1), m.group(2).upper(), address), m.group(2).upper(), recipient=found.group(0), sender=address,
                    holdings=all_balances(address) if address else None)
    pro = market_in(low)
    # lending is USDC only, so the coin may be left out: 'lend all', 'lend half my usdc', 'withdraw everything'
    filler = r'(?:\s+(?:of\s+)?(?:my|the|it))?'
    # only a real coin name counts ('lend all SUI' -> USDC-only reply); filler like 'amount', 'money', 'of it' means USDC
    lend_coin = lambda g: g.upper() if g and g.upper() in K()['assets'] else 'USDC'
    lend_word = lambda g: not g or g.upper() in K()['assets'] or g in ('amount', 'money', 'funds', 'coins', 'balance', 'savings', 'deposit', 'position')
    m = re.match(r'^(?:lend|deposit|supply)\s+' + num + filler + r'(?:\s+([a-z]+))?\b', low)
    if m and not whole(low[m.end():] if lend_word(m.group(2)) else low[m.start(2):]):
        m = None
    if m:
        c = lend_coin(m.group(2))
        return plan('lend', _amt(m.group(1), c, address), c,
                    holdings=balances(address) if address else None, protocol=pro)
    m = re.match(r'^(?:withdraw|redeem)\s+' + num + filler + r'(?:\s+([a-z]+))?\b', low)
    if m and not whole(low[m.end():] if lend_word(m.group(2)) else low[m.start(2):]):
        m = None
    if m:
        if not address:
            return {'text': K()['no_wallet']}
        w = m.group(1)
        if w.endswith('%'):
            return None                                  # a share of a position: the readers above own it (this crashed on '25%', 2026-10-31)
        if not pro and lend_coin(m.group(2)) == 'USDC':  # several USDC markets and none named: ask which (it picked one silently, 2026-11-13)
            mine = [r for r in lend_positions_rows(address) if r['coin'] == 'USDC']
            if len(mine) > 1:
                return {'text': 'You have USDC in %d markets. Which one?' % len(mine), 'widget': {'type': 'positions', 'rows': mine}}
        return plan('withdraw', 0.0 if w in WORD_AMOUNTS else float(w), lend_coin(m.group(2)), protocol=pro,
                    lent=lend_positions(address), all_=w in ('all', 'max', 'everything'))
    k = K()
    if ORDER_RE.search(low) and re.search(r'\d|\b(?:all|max|everything|half)\b', low):
        return {'text': UNREAD}
    # what can you do?
    if re.search(r"\bwhat (can|do) (you|u|kivy) do\b|\bwhat are you\b|\bcapabilit|\bhow can you help\b", low):
        return {'text': ('Here is what I can do on Sui, with your own wallet:\n'
                         '• Swap — e.g. "swap 5 SUI to USDC". I find the best price; your wallet approves it.\n'
                         '• Send — e.g. "send 1 USDC to name.sui". I show the full address first; your wallet approves it.\n'
                         '• Any coin Suilend or NAVI lists (USDsui, vSUI, BLUE…) can be swapped and sent too.\n'
                         '• Lend / withdraw — any coin Suilend or NAVI lists; "swap USDC to USDsui then lend" and "withdraw USDsui as USDC" are one approval each.\n'
                         '• Perps — long or short with leverage on Aftermath (market, limit, stop, scale, TWAP); your wallet approves each order.\n'
                         '• Prices and your balances — connect a wallet with the icon at the bottom left.\n\n'
                         'Coins: %s.%s I never hold your keys, and nothing moves without your yes.')
                        % (', '.join(k['assets']), (' Swaps, sends and lending up to about $%g per order; perps have no cap.' % cap_usd()) if cap_usd() else '')}
    # which coins can I use? ("what assets can I play with", "which tokens do you support")
    if re.search(r'\b(assets?|coins?|tokens?)\b', low) and re.search(r'\b(what|which|available|support(ed)?|can i|play)\b', low):
        px = prices()
        rows = ['• %s  $%s' % (x, ('%.4f' % px[x]) if x in px else '—') for x in k['assets']]
        lines = '\n'.join(rows)
        example = k['assets'][2] if len(k['assets']) > 2 else 'USDC'
        return {'text': ('Kivy can play with these %d coins on Sui: %s\n%s\n\n'
                         'Swap any of them into another, send them, or lend them. '
                         '%sTry: "swap 5 SUI to %s".')
                        % (len(k['assets']), ', '.join(k['assets']), lines, ('Each swap, send or loan is up to about $%g. ' % cap_usd()) if cap_usd() else '', example)}
    # price questions: "SUI price", "price of DEEP", "how much is WAL"
    if re.search(r'\b(price|prices|how much is|worth|rate of)\b', low):
        px = prices()
        asked = [a for a in k['assets'] if re.search(r'\b%s\b' % a.lower(), low)] or k['assets']
        rows = ['• %s  $%s' % (a, ('%.4f' % px[a]) if a in px else '—') for a in asked]
        return {'text': k['price_intro'] + '\n' + '\n'.join(rows)}
    if re.search(r'\b(balance|balances|wallet|holdings?|how much do i have)\b', low):
        return {'text': balances_text(address) if address else k['no_wallet']}
    if re.search(r'\b(rules?|policy|limits?|cap)\b', low):
        rules = [r.replace('{cap}', '%g' % (cap_usd() or 0)).replace('{assets}', ', '.join(k['assets']))
                  .replace('{slippage}', '%g' % k['slippage_cap_pct']) for r in k['rules'] if cap_usd() or '{cap}' not in r]   # no limit: no limit rule
        return {'text': 'The rules I work under:\n' + '\n'.join('• ' + r for r in rules)}
    ans = faq(low, k)
    if ans:
        return {'text': ans}
    return {'text': k['help']}


def faq(low, k):
    """Best FAQ answer from the knowledge file, by whole-word/phrase key matches."""
    best, score = None, 0
    words = ' ' + re.sub(r'[^a-z0-9$% -]+', ' ', low) + ' '
    for item in k.get('faq') or []:
        hits = sum(len(key) for key in item['keys'] if ' %s ' % key in words)
        if hits > score:
            best, score = item['answer'], hits
    if best and '{kivy_fee}' in best:
        kf = kivy_fee()                                  # the real fee, always said (it said nothing once the % fees went to 0, 2026-10-23)
        best = best.replace('{kivy_fee}', (' Kivy\'s own fee: simple transactions and all sends are free; a multi-step transaction (several actions in one approval) '
                                           'includes %g SUI, shown on its card. Asking me is free.' % kf['sui']) if kf else ' Kivy itself charges no fee.')
    if best == '@help':
        return k['help']
    if best == '@assets':
        return ('I work with these coins: %s — plus every coin Suilend or NAVI lists (USDsui, vSUI, haSUI, sSUI, LBTC, wBTC, BLUE, '
                'NAVX…): swap, send, lend, and withdraw as another coin in one approval. Anything unlisted I refuse.') % ', '.join(k['assets'])
    return best


_RCP = {}


def recipient_check(q, sender=''):
    """The Send card's live check of a recipient (2026-10-23): 'ok' (a .sui name that resolves, or an address that has
    been used), 'new' (a valid address with no activity yet — allowed: new wallets are real), 'bad' (unknown name, not an
    address, the sender itself). Cached one minute."""
    q = str(q or '').strip()
    key = (q.lower(), sender.lower())
    hit = _RCP.get(key)
    if hit and time.time() - hit[0] < 60:               # a minute: a name that moves shows its new address fast (audit nv #6)
        return hit[1]
    looks_name = bool(re.search(r'\.sui$|^@', q, re.I))
    to = resolve_recipient(q)
    if not to:
        out = {'state': 'bad', 'why': "No such .sui name" if looks_name else 'Not a valid address or .sui name'}
    elif sender and to == sender.lower():
        out = {'state': 'bad', 'address': to, 'why': 'That is the sending account itself'}
    else:
        kind = None
        try:                                             # a package / object id is not a wallet: coins sent there are lost (review P-33)
            ob = (gql('query($a: SuiAddress!) { object(address: $a) { asMovePackage { address } } }', {'a': to}) or {}).get('object')
            kind = 'package' if ob and ob.get('asMovePackage') else 'object' if ob else 'account'
            shadow_note('kind', {'a': to}, kind)
        except Exception:
            kind = None
        used = None
        try:
            d = gql('query($a: SuiAddress!) { transactions(first: 1, filter: {affectedAddress: $a}) { nodes { digest } } }', {'a': to})
            used = bool(((d.get('transactions') or {}).get('nodes') or []))
            shadow_note('used', {'a': to}, used)
        except Exception:
            pass
        out = {'state': 'ok' if (used or looks_name or used is None) else 'new', 'address': to, 'name': q if looks_name else None}
        if kind in ('package', 'object'):
            out = {'state': 'bad', 'address': to, 'why': ('That is a program (package) on Sui, not a wallet — coins sent there are lost' if kind == 'package'
                                                         else 'That is an on-chain object, not a wallet — coins sent there can be lost')}
        elif kind is None and out['state'] == 'ok' and not looks_name:
            out = dict(out, state='new', why="Couldn't check this address on chain right now — make sure it is a wallet address")
    if len(_RCP) > 2000:
        _RCP.clear()
    _RCP[key] = (time.time(), out)
    return out


from .names import cached as _name_cache  # noqa: E402  fix package 4 (2026-11-15): kept 60 s inside a chat answer only; every build asks fresh


@_name_cache
def resolve_recipient(r):
    """0x address as-is; a SuiNS name (alice.sui / @alice) via GraphQL (JSON-RPC fallback). None if invalid."""
    r = (r or '').strip()
    if ADDR_RE.match(r):
        return r.lower()
    name = r.lower()
    if name.startswith('@'):
        name = name[1:] + '.sui'
    if not re.match(r'^[a-z0-9-]+(\.[a-z0-9-]+)*\.sui$', name):
        return None
    try:
        try:                                             # GraphQL first, gRPC (via kivy-lend) as the fallback on mainnet
            d = gql('query($n: String!) { address(name: $n) { address } }', {'n': name})
            out = (d.get('address') or {}).get('address')
            shadow_note('name', {'n': name}, out)
        except Exception:
            if NET() != 'mainnet':
                raise
            out = None
        if not out and NET() == 'mainnet':
            try:
                out = grpc_backup('grpc-name', '0x0', name if name.endswith('.sui') else name.lstrip('@') + '.sui', timeout=10).get('address')
            except Exception:
                out = None
        return out.lower() if out and ADDR_RE.match(out) else None
    except Exception:
        return None


GASLESS = {'USDC'}          # Sui gasless stablecoin transfers (protocol 125, May 2026); Kivy's coins only
GASLESS_MIN = 0.01


# True when a send can go as a Sui gasless stablecoin transfer (the page then builds it without SUI for gas).
def gasless_send(coin, amt):
    return NET() == 'mainnet' and coin in GASLESS and amt >= GASLESS_MIN


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .readguard import guarded  # noqa: E402
from .chainreads import (  # noqa: E402
    ADDR_RE, NET, all_balances, balances, balances_text, ckey, gql, grpc_backup,
    prices, shadow_note)
from .lending import lend_positions, lend_positions_rows, market_in  # noqa: E402
from .cards import WORD_AMOUNTS, plan, word_amount  # noqa: E402
from .txreads import kivy_fee  # noqa: E402
