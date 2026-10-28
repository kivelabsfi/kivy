#!/usr/bin/env python3
# senders.py — helper for export_records.py (imported), also runnable by hand: python3 senders.py
# In: transaction digests. Out: senders.json (digest -> wallet hash). Links: Sui GraphQL, metrics.py (metrics.wh).
"""Fills in the wallet hash for records saved before Kivy kept one: asks Sui GraphQL who signed each transaction and
keeps only Kivy's salted hash (metrics.wh), never the address. Cache: senders.json (digest -> wallet hash)."""
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, '/opt/kivy')
import metrics  # noqa: E402

CACHE = Path(__file__).resolve().parent / 'senders.json'


# digests -> wallet hash: asks Sui GraphQL for the sender, 20 transactions per query; only the hash is cached
# (called by export_records.py pipeline_records() for records with no wallet hash)
def fill(digests):
    have = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    need = [d for d in digests if d and d not in have]
    for i in range(0, len(need), 20):
        part = need[i:i + 20]
        q = '{ %s }' % ' '.join('t%d: transaction(digest: "%s") { sender { address } }' % (k, d) for k, d in enumerate(part))
        req = urllib.request.Request('https://graphql.mainnet.sui.io/graphql', data=json.dumps({'query': q}).encode(),
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'kivy-walrus/1'})
        data = json.load(urllib.request.urlopen(req, timeout=60)).get('data') or {}
        for k, d in enumerate(part):
            a = ((data.get('t%d' % k) or {}).get('sender') or {}).get('address')
            if a:
                have[d] = metrics.wh(a)
    CACHE.write_text(json.dumps(have, indent=1))
    return have


# CLI: report how many pipeline records lack a wallet hash and how many the chain could fill
if __name__ == '__main__':
    c = sqlite3.connect('file:/opt/kivy/data/metrics.db?mode=ro', uri=True)
    ds = [r[0] for r in c.execute("SELECT digest FROM pipeline WHERE digest IS NOT NULL AND (wh IS NULL OR wh = '')")]
    have = fill(ds)
    print(len(ds), 'without a wallet;', sum(1 for d in ds if d in have), 'found on chain;', len(set(have[d] for d in ds if d in have)), 'distinct wallets')
