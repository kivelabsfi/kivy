"""Wallet memory + Activity labels, read back from Kivy's diary and the chain (Section 8).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# tx_labels feeds GET /api/tx/labels; wallet_memory / memory_text the AI's prompt (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import re
import time
from pathlib import Path


# ── Section 8: wallet memory + Activity labels — read back from Kivy's diary and the chain, never stored per user;
#    used by GET /api/tx/labels and the AI's prompt ──
def tx_labels(digests, owner=None):
    """{digest: what Kivy called it} from Kivy's own transaction log. Public: the transactions are public on chain and every
    Kivy transaction pays Kivy's fee on chain, so these labels reveal nothing new (2026-10-22). owner: only that sender's."""
    out = {}
    if not digests:
        return out
    me = '%s…%s' % (owner[:6], owner[-4:]) if owner else None
    try:
        text = Path(KIVY_DIARY).read_text()
        for m in re.finditer(r'^- \*\*[^*]+\*\* — (\S+) · ([^·\n]+?) · .*?/txblock/([1-9A-HJ-NP-Za-km-z]{32,44})\)', text, re.M):
            if m.group(3) in digests and (me is None or m.group(1).lower() == me.lower()) and m.group(2).strip() != 'transaction':
                out[m.group(3)] = m.group(2).strip() + (' ⚠️' if 'check ⚠️' in m.group(0) else '')   # 'transaction' = no Kivy plan: the chain's own title stays
    except Exception:
        pass
    return out


def nice_label(s):
    """'swaplend Suilend Ember USDC→USDsui' -> 'Swap & lend · Ember USDC→USDsui' (same wording as the Activity tab)."""
    for a, b in (('swaplend', 'Swap & lend ·'), ('withdrawswap', 'Withdraw & swap ·'), ('move', 'Move ·'), ('chain', 'Steps ·'), ('lend deposit', 'Lend ·'),
                 ('lend withdraw', 'Withdraw ·'), ('lend claim', 'Claim rewards')):
        if s.lower().startswith(a):
            s = b + s[len(a):]
            break
    s = re.sub(r'\s*\(backfilled\)', '', s, flags=re.I)
    s = re.sub(r'\b(Suilend|NAVI)\s+(?=\S)', '', s).replace('Main Market', 'Main')
    return s[:1].upper() + s[1:]


_MEM = {}


def wallet_memory(address):
    """What Kivy remembers about a wallet — rebuilt from public data each time, never stored (2026-10-20):
    its recent actions through Kivy (chain + Kivy's transaction log) and what it has lent right now. Cached 60 s."""
    hit = _MEM.get(address)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    acts, lent = [], []
    try:
        d = gql('query($a: SuiAddress!) { transactions(last: 30, filter: {affectedAddress: $a}) { nodes { digest effects { status timestamp } } } }',
                {'a': address})
        shadow_note('history', {'a': address, 'n': 30, 'kind': 'affected', 'detail': 1}, ((d.get('transactions') or {}).get('nodes')) or [])
        nodes = list(reversed(((d.get('transactions') or {}).get('nodes')) or []))
        L = tx_labels({n['digest'] for n in nodes}, owner=address)   # a Kivy tx someone else sent you is not yours (audit nv #8)
        for n in nodes:
            if n['digest'] in L and len(acts) < 6:
                e = n.get('effects') or {}
                acts.append({'what': nice_label(L[n['digest']]), 'when': str(e.get('timestamp') or '')[:16].replace('T', ' ') + ' UTC',
                             'ok': e.get('status') == 'SUCCESS'})
    except Exception:
        pass
    try:
        pos = lend_positions(address)
        for p in PROTOS:
            v = float((pos.get(p) or {}).get('usdc') or 0)
            if v >= 0.01:
                lent.append({'coin': 'USDC', 'amount': v, 'usd': v, 'market': PROTOS[p], 'apr': apr_of(p) or 0})
        for x in other_lent(address):
            lent.append({'coin': x['coin'], 'amount': x['lent'], 'usd': x['lent'] * x['px'], 'market': x['name'], 'apr': x['total']})
    except Exception:
        pass
    lent.sort(key=lambda x: -x['usd'])
    held = []
    try:
        for k, v in (all_balances(address) or {}).items():
            if float(v or 0) > 0 and float(v) * float(coin_px(k) or 0) >= 0.02:
                held.append((k, float(v)))
    except Exception:
        pass
    m = {'actions': acts, 'lent': lent, 'held': held}
    _MEM[address] = (time.time(), m)
    return m


def memory_text(m):
    """The memory as lines for the AI's prompt."""
    out = []
    if m.get('actions'):
        out.append('- Their recent actions through Kivy (newest first): ' + '; '.join(
            '%s %s%s' % (a['when'], a['what'], '' if a['ok'] else ' (failed)') for a in m['actions']))
    if m.get('held'):
        out.append('- In their wallet: ' + ', '.join('%s %s' % (dec(v), k) for k, v in m['held']))
    if m.get('lent'):
        out.append('- Lent right now: ' + '; '.join('%s %s on %s at %.2f%% a year' % (dec(x['amount']), x['coin'], x['market'], x['apr'])
                                                    for x in m['lent']))
    return '\n'.join(out)


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .check2 import KIVY_DIARY  # noqa: E402
from .chainreads import all_balances, dec, gql, shadow_note  # noqa: E402
from .lending import PROTOS, apr_of, coin_px, lend_positions, other_lent  # noqa: E402
