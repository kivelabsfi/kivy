#!/usr/bin/env python3
"""Kivy's trusted list of Aftermath perps markets, kept up to date on its own (security audit 2026-10-27, finding 3;
"made #3 automatic fix when aftermath change format or add markets").

Every hour (cron): read Aftermath's market list, then let the CHAIN decide what is real —
  - a market is trusted only if its object on Sui is a shared ClearingHouse<USDC> of Aftermath's perps program;
  - the program's official upgrades (same lineage, proven on chain) are followed and added to Kivy's allowlist.
The result is builder/perps_markets.json, which the transaction builder checks every perps order against. New markets and
upgrades are NOT trusted automatically any more (review L3, 2026-10-28): a new program version goes to
tools/perps_pending_pkgs.json and a ⚠️ alert asks for a check; only --approve adds it to both lists. New markets of the
trusted program are still added automatically (Telegram FYI); anything the chain does not confirm is refused and alerted.
  python3 tools/sync_perps_markets.py           # update + tell the owner what changed
  python3 tools/sync_perps_markets.py --dry     # print, change nothing
  python3 tools/sync_perps_markets.py --approve # trust the pending program version(s) (after kivy-vet-package)
Since the release move (2026-11-14) the site's allowlist is site/kivy_pkgs.json in the repo and ships in a release: --approve
edits that file and prints the release steps; it never writes under /opt/kivy (review scan 2026-11-15 P4 — it used to replace
the read-only link /opt/kivy/kivy_pkgs.json with a stray file and say "approved" while live stayed unchanged)."""
# How it runs: crontab "17 * * * *" (every hour at :17), output appended to a log in the scratch folder; --dry is also
# run by tools/perps_fix_draft.sh; --approve by hand only.
# Reads: Aftermath's market list (AF) and Sui GraphQL (package versions, market objects).
# Writes: builder/perps_markets.json (read by perpGuard in builder/perps.ts before it builds any perps transaction — data
# outside the release, KIVY_PERPS_MARKETS), tools/perps_pending_pkgs.json (upgrades waiting for approval), and with --approve
# the repo's site/kivy_pkgs.json (the page's check 1 and site/server.py read the copy in the LIVE RELEASE, so it reaches them
# only with the next release). Messages go out through the shared alert sender.
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path('/srv/kivy/builder/perps_markets.json')
PKGS = Path('/srv/kivy/site/kivy_pkgs.json')   # the repo's allowlist (ships in a release); never /opt/kivy
RELEASE_STEPS = ('cd /srv/kivy && git add site/kivy_pkgs.json && git commit -m "allowlist: approve Aftermath perps upgrade"\n'
                 'python3 tools/release.py build   # then: stage <id>, test <id>, promote <id> (kivy-deploy skill)')
GQL = 'https://graphql.mainnet.sui.io/graphql'
AF = 'https://aftermath.finance/api/perpetuals/all-markets'
USDC = '0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC'
ORIGINAL = '0x3ec740df8428aa9c93aaef7f8cc1542ac3194fd014826b51bfe245346d64efc7'   # Aftermath perps, first version (checked 2026-10-27)
SENDER = '/srv/kivy/tools/send_alert.py'
DRY = '--dry' in sys.argv
APPROVE = '--approve' in sys.argv
PENDING = Path('/srv/kivy/tools/perps_pending_pkgs.json')


# POST JSON, return the parsed JSON answer (Aftermath and GraphQL calls).
def post(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'User-Agent': 'kivy-perps-sync/1'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# One Sui GraphQL query; raises on GraphQL errors so a bad read never writes a list.
def gql(query):
    j = post(GQL, {'query': query})
    if j.get('errors'):
        raise RuntimeError(str(j['errors'])[:200])
    return j['data']


# Send one alert message (only printed with --dry).
def tell(text):
    if DRY:
        print('[would send] ' + text)
        return
    text = text.replace('_', '\\_').replace('*', '\\*')
    subprocess.run(['/usr/bin/python3', SENDER, text], check=False, timeout=60)


# A 0x id padded to 64 hex digits, so the same id always compares equal.
def norm(a):
    return '0x%064x' % int(a, 16)


def approve_pkgs(approved, at, path=None):
    """Add the approved program versions to the repo's allowlist (site/kivy_pkgs.json, same JSON layout: indent 1, newline at
    the end) — in ALL THREE places the rules need (fix package 6, 2026-11-17, BLD-A1; on 2026-11-16 a person added the v2 upgrade
    by hand to the last two): `packages` (+ its name), the call-rule group `calls.groups.perps` and the shared value
    `calls.consts.perps_programs` (the builder and check 1 decide "is this a perps transaction" from it). Refuses a link or
    anything under /opt/kivy: the allowlist ships in a release, never edited in place. Returns the versions that were not in
    `packages` yet. tools/release.py perps_lists_problem() refuses a release whose lists disagree."""
    path = Path(path or PKGS)
    if path.is_symlink() or str(path.resolve()).startswith('/opt/kivy/') or str(path).startswith('/opt/kivy/'):
        raise SystemExit('refused: %s is a link or inside /opt/kivy — the allowlist is site/kivy_pkgs.json in the repo' % path)
    p = json.loads(path.read_text())
    calls = p.setdefault('calls', {})
    group = calls.setdefault('groups', {}).setdefault('perps', [])
    progs = calls.setdefault('consts', {}).setdefault('perps_programs', [])
    added = []
    for a in approved:
        if a not in [norm(x) for x in p['packages']]:
            p['packages'].append(a)
            added.append(a)
        p.setdefault('names', {})[a] = 'Aftermath perps — official upgrade of %s…, confirmed on chain, approved %s' % (ORIGINAL[:10], at[:10])
        for lst in (group, progs):                     # the call rules: perps steps may call it, and it marks a perps transaction
            if a not in [norm(x) for x in lst]:
                lst.append(a)
    t2 = path.with_name(path.name + '.tmp')
    t2.write_text(json.dumps(p, indent=1) + '\n')
    os.chmod(t2, 0o644)
    t2.replace(path)
    return added


# Program versions from the chain -> markets from Aftermath, each confirmed on chain -> diff with the old list ->
# write the files -> alert about new versions, list changes and unconfirmed markets.
def main():
    old = json.loads(OUT.read_text()) if OUT.exists() else {'markets': {}, 'packages': [ORIGINAL]}
    # 1. the program and its official upgrades: every version after the original, as the chain lists them
    # 2026-11-16: package(address: ORIGINAL) now resolves to the LATEST version, so its packageVersionsAfter was empty and the v2
    # upgrade (0xe5475743…, 2026-11-15) was never seen — and this run would have dropped a v2 trusted by hand. packageVersions
    # lists the whole lineage by version.
    d = gql('{ v: packageVersions(address:"%s", first:50){ nodes { address version } } }' % ORIGINAL)
    nodes = sorted((d.get('v') or {}).get('nodes') or [], key=lambda n: int(n.get('version') or 0))
    if not nodes or norm(nodes[0]['address']) != norm(ORIGINAL):
        raise SystemExit('the program lineage did not start at the original version — keeping the old list')
    packages = [norm(n['address']) for n in nodes]
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
    doc = {'_about': 'Written by tools/sync_perps_markets.py: Aftermath perps markets and program versions, each confirmed on chain. '
                     'The builder refuses any perps transaction outside this list.',
           'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'original': norm(ORIGINAL), 'packages': packages, 'markets': ok}
    print('confirmed %d markets, %d trusted program version(s); new %s; changed %s; gone %s; refused %s; program waiting for approval %s; approved now %s'
          % (len(ok), len(packages), new_m, changed, gone, list(bad), new_p, approved if APPROVE else []))
    if DRY:
        return
    if APPROVE and approved:                            # before any write (a refused path changes nothing). The builder takes it with
        added = approve_pkgs(approved, doc['at'])        # perps_markets.json; check 1 and the server read the LIVE RELEASE's allowlist
        print('approved %s; added to %s: %s\nThe site accepts it only after a release:\n%s'
              % (', '.join(approved), PKGS, ', '.join(added) or 'nothing (already listed)', RELEASE_STEPS))
    tmp = OUT.with_suffix('.tmp'); tmp.write_text(json.dumps(doc, indent=1)); os.chmod(tmp, 0o644); tmp.replace(OUT)
    if new_p != [p for p in (json.loads(PENDING.read_text()) if PENDING.exists() else [])] and (new_p or PENDING.exists()):
        PENDING.write_text(json.dumps(new_p, indent=1))
    if old.get('markets'):                              # the first run only builds the list; later runs report what changed
        if new_p and set(new_p) != set(pending):         # say it once per new version, not every hour
            tell('⚠️ Kivy · Aftermath upgraded its perps program — needs your OK\n\nNew version (official lineage, confirmed on chain): %s\n\n'
                 'Kivy does NOT trust it yet, so perps orders that use it are refused. After checking it (kivy-vet-package), run:\n'
                 'python3 /srv/kivy/tools/sync_perps_markets.py --approve\n'
                 'then commit site/kivy_pkgs.json and release it (the allowlist ships in a release).' % ', '.join(x[:10] + '…' for x in new_p))
        if new_m or changed or gone or (APPROVE and approved):
            tell('🟢 Kivy · Perps list updated automatically\n\n' +
                 ('New markets (confirmed on chain): %s\n' % ', '.join(new_m) if new_m else '') +
                 ('Market object changed (confirmed): %s\n' % ', '.join(changed) if changed else '') +
                 ('No longer listed by Aftermath: %s\n' % ', '.join(gone) if gone else '') +
                 ('Perps program upgrade approved: %s — the builder accepts it now; the site\'s allowlist (site/kivy_pkgs.json in the '
                  'repo) changes with the next release: commit it, then release.py build / stage / test / promote.\n'
                  % ', '.join(x[:10] + '…' for x in approved) if APPROVE and approved else '') +
                 ('\nNothing to do — Kivy already accepts them.' if new_m or changed or gone else ''))
    if bad:
        tell('⚠️ Kivy · Perps market not confirmed on chain\n\nAftermath lists %s, but the chain does not show an Aftermath market there. '
             'Kivy refuses orders on it.\n\n%s\n\nWhat to do: nothing urgent; tell the operator if it is a market you want.'
             % (', '.join(sorted(bad)), '\n'.join('• %s %s… (%s, %s)' % (s, v[0][:10], v[1], v[2]) for s, v in sorted(bad.items())[:5])))


if __name__ == '__main__':
    main()
