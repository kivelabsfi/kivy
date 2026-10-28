#!/usr/bin/env python3
"""Kivy's trusted list of Aftermath perps markets, kept up to date on its own (security audit 2026-10-27, finding 3;
"made #3 automatic fix when aftermath change format or add markets").

Every hour (cron): read Aftermath's market list, then let the CHAIN decide what is real —
  - a market is trusted only if its object on Sui is a shared ClearingHouse<USDC> of Aftermath's perps program;
  - the program's official upgrades (same lineage, proven on chain) are followed and added to Kivy's allowlist.
The result is lend/perps_markets.json, which the transaction builder checks every perps order against. New markets and
upgrades are NOT trusted automatically any more (review L3, 2026-10-28): a new program version goes to
tools/perps_pending_pkgs.json and a ⚠️ alert asks for a check; only --approve adds it to both lists. New markets of the
trusted program are still added automatically (Telegram FYI); anything the chain does not confirm is refused and alerted.
  python3 tools/perps_markets_sync.py           # update + tell the owner what changed
  python3 tools/perps_markets_sync.py --dry     # print, change nothing
  python3 tools/perps_markets_sync.py --approve # trust the pending program version(s) (after kivy-vet-package)"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path('/srv/kivy/lend/perps_markets.json')
PKGS = Path('/opt/kivy/kivy_pkgs.json')
GQL = 'https://graphql.mainnet.sui.io/graphql'
AF = 'https://aftermath.finance/api/perpetuals/all-markets'
USDC = '0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC'
ORIGINAL = '0x3ec740df8428aa9c93aaef7f8cc1542ac3194fd014826b51bfe245346d64efc7'   # Aftermath perps, first version (checked 2026-10-27)
SENDER = '/srv/kivy/tools/send_alert.py'
DRY = '--dry' in sys.argv
APPROVE = '--approve' in sys.argv
PENDING = Path('/srv/kivy/tools/perps_pending_pkgs.json')


def post(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'User-Agent': 'kivy-perps-sync/1'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def gql(query):
    j = post(GQL, {'query': query})
    if j.get('errors'):
        raise RuntimeError(str(j['errors'])[:200])
    return j['data']


def tell(text):
    if DRY:
        print('[would send] ' + text)
        return
    text = text.replace('_', '\\_').replace('*', '\\*')
    subprocess.run(['/usr/bin/python3', SENDER, text], check=False, timeout=60)


def norm(a):
    return '0x%064x' % int(a, 16)


def main():
    old = json.loads(OUT.read_text()) if OUT.exists() else {'markets': {}, 'packages': [ORIGINAL]}
    # 1. the program and its official upgrades: every version after the original, as the chain lists them
    d = gql('{ p: package(address:"%s"){ address packageVersionsAfter(first:50){ nodes { address version } } } }' % ORIGINAL)
    packages = [norm(ORIGINAL)] + [norm(n['address']) for n in (d['p'] or {}).get('packageVersionsAfter', {}).get('nodes', [])]
    # 2. the markets Aftermath lists, each confirmed on chain
    listed = {}
    for m in post(AF, {'collateralCoinType': USDC}).get('markets') or []:
        sym = str(m['marketParams']['baseAssetSymbol']).replace('USD', '', 1) if str(m['marketParams']['baseAssetSymbol']).endswith('USD') else str(m['marketParams']['baseAssetSymbol'])
        listed[sym.upper()] = norm(m['objectId'])
    want = 'ClearingHouse<%s>' % USDC
    ok, bad = {}, {}
    items = list(listed.items())
    for i in range(0, len(items), 25):                  # one query per 25 markets
        part = items[i:i + 25]
        q = ' '.join('m%d: object(address:"%s"){ asMoveObject{ contents{ type{ repr } } } owner{ __typename } }' % (k, mid) for k, (_, mid) in enumerate(part))
        d = gql('{ %s }' % q)
        for k, (sym, mid) in enumerate(part):
            o = d.get('m%d' % k) or {}
            t = (((o.get('asMoveObject') or {}).get('contents') or {}).get('type') or {}).get('repr') or ''
            shared = (o.get('owner') or {}).get('__typename') == 'Shared'
            if shared and t.startswith(norm(ORIGINAL) + '::clearing_house::') and t.endswith(want):
                ok[sym] = mid
            else:
                bad[sym] = (mid, t[:90] or 'not found', 'shared' if shared else 'not shared')
    if not ok:
        raise SystemExit('no market confirmed on chain — keeping the old list')
    new_m = sorted(set(ok) - set(old.get('markets') or {}))
    changed = sorted(s for s in ok if (old.get('markets') or {}).get(s) not in (None, ok[s]))
    gone = sorted(set(old.get('markets') or {}) - set(ok))
    trusted = set(norm(x) for x in old.get('packages') or [ORIGINAL])
    pending = [p for p in (json.loads(PENDING.read_text()) if PENDING.exists() else []) if p in packages]
    new_p = [p for p in packages if p not in trusted]     # chain-proven upgrades not trusted yet
    if APPROVE:
        trusted |= set(pending)
        new_p = [p for p in new_p if p not in pending]
    approved = [p for p in pending if p in trusted]
    packages = [p for p in packages if p in trusted]      # only trusted versions reach the builder and the allowlist
    doc = {'_about': 'Written by tools/perps_markets_sync.py: Aftermath perps markets and program versions, each confirmed on chain. '
                     'The builder refuses any perps transaction outside this list.',
           'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'original': norm(ORIGINAL), 'packages': packages, 'markets': ok}
    print('confirmed %d markets, %d trusted program version(s); new %s; changed %s; gone %s; refused %s; program waiting for approval %s; approved now %s'
          % (len(ok), len(packages), new_m, changed, gone, list(bad), new_p, approved if APPROVE else []))
    if DRY:
        return
    tmp = OUT.with_suffix('.tmp'); tmp.write_text(json.dumps(doc, indent=1)); os.chmod(tmp, 0o644); tmp.replace(OUT)
    if new_p != [p for p in (json.loads(PENDING.read_text()) if PENDING.exists() else [])] and (new_p or PENDING.exists()):
        PENDING.write_text(json.dumps(new_p, indent=1))
    if APPROVE and approved:                            # a checked upgrade: Kivy's allowlist follows it (the page and the server check it too)
        p = json.loads(PKGS.read_text())
        for a in approved:
            if a not in [norm(x) for x in p['packages']]:
                p['packages'].append(a)
            p['names'][a] = 'Aftermath perps — official upgrade of %s…, confirmed on chain, approved %s' % (ORIGINAL[:10], doc['at'][:10])
        t2 = PKGS.with_suffix('.tmp'); t2.write_text(json.dumps(p, indent=1)); os.chmod(t2, 0o644); t2.replace(PKGS)
    if old.get('markets'):                              # the first run only builds the list; later runs report what changed
        if new_p and set(new_p) != set(pending):         # say it once per new version, not every hour
            tell('⚠️ Kivy · Aftermath upgraded its perps program — needs your OK\n\nNew version (official lineage, confirmed on chain): %s\n\n'
                 'Kivy does NOT trust it yet, so perps orders that use it are refused. After checking it (kivy-vet-package), run:\n'
                 'python3 /srv/kivy/tools/perps_markets_sync.py --approve' % ', '.join(x[:10] + '…' for x in new_p))
        if new_m or changed or gone or (APPROVE and approved):
            tell('🟢 Kivy · Perps list updated automatically\n\n' +
                 ('New markets (confirmed on chain): %s\n' % ', '.join(new_m) if new_m else '') +
                 ('Market object changed (confirmed): %s\n' % ', '.join(changed) if changed else '') +
                 ('No longer listed by Aftermath: %s\n' % ', '.join(gone) if gone else '') +
                 ('Perps program upgrade approved: %s\n' % ', '.join(x[:10] + '…' for x in approved) if APPROVE and approved else '') +
                 '\nNothing to do — Kivy already accepts them.')
    if bad:
        tell('⚠️ Kivy · Perps market not confirmed on chain\n\nAftermath lists %s, but the chain does not show an Aftermath market there. '
             'Kivy refuses orders on it.\n\n%s\n\nWhat to do: nothing urgent; tell the operator if it is a market you want.'
             % (', '.join(sorted(bad)), '\n'.join('• %s %s… (%s, %s)' % (s, v[0][:10], v[1], v[2]) for s, v in sorted(bad.items())[:5])))


if __name__ == '__main__':
    main()
