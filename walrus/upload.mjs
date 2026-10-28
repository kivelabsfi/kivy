// Saves Kivy's security records (from export.py) to Walrus Memory, each in its wallet's space (wallet-<hash>; rules in kivy-rules).
// One space per wallet: 2026-10-24. The first 75 records also sit in the old shared space kivy-security (no delete on Walrus Memory).
// Each saved record goes into ledger.jsonl (key, digest, blob_id, at), so a re-run only sends what is new.
//   python3 export.py | node upload.mjs
import fs from 'fs';
import { MemWal } from '@mysten-incubation/memwal';

const ACCOUNT = '0x762470f60d8cb2b2dd88b68d2d2111d444050dc162e2272bb151cf18c8bd152e';
const LEDGER = new URL('./ledger.jsonl', import.meta.url);
const recs = JSON.parse(fs.readFileSync(0, 'utf8'));
const m = MemWal.create({ key: fs.readFileSync('/opt/kivy/data/memwal.key', 'utf8').trim(), accountId: ACCOUNT,
  serverUrl: 'https://relayer.memory.walrus.xyz', namespace: 'kivy-rules' });

let ok = 0, bad = 0;
for (let i = 0; i < recs.length; i += 10) {
  const part = recs.slice(i, i + 10);
  const r = await m.rememberBulkAndWait(part.map(x => ({ text: x.text, namespace: x.ns })), { timeoutMs: 300000 });
  r.results.forEach((x, k) => {
    if (x.status === 'done') {
      ok++;
      fs.appendFileSync(LEDGER, JSON.stringify({ key: part[k].key, digest: part[k].digest, blob_id: x.blob_id, ns: part[k].ns, at: new Date().toISOString() }) + '\n');
    } else { bad++; console.log('not saved', part[k].key, x.status, x.error || ''); }
  });
  console.log(`${Math.min(i + 10, recs.length)}/${recs.length} sent, ${ok} saved`);
}
console.log(JSON.stringify({ saved: ok, failed: bad }));
