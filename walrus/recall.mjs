// recall.mjs — manual lookup tool (not run by cron or the server): reads back what upload.mjs saved.
// Args: query text, topK (default 5). KIVY_WAL_NS picks the namespace (default kivy-rules, the rule set). Prints distance,
// blob id and the start of each record.
// Search Kivy's security records on Walrus Memory.   KIVY_WAL_NS=wallet-<hash> node recall.mjs "failed check 2" [topK]
import fs from 'fs';
import { MemWal } from '@mysten-incubation/memwal';
// the key: systemd's credentials folder when the unit hands it in (root-only /opt/kivy/keys since 2026-11-14), else the data folder
const m = MemWal.create({ key: fs.readFileSync([process.env.CREDENTIALS_DIRECTORY && process.env.CREDENTIALS_DIRECTORY + '/memwal.key', '/opt/kivy/keys/memwal.key', '/opt/kivy/data/memwal.key'].find((p) => p && fs.existsSync(p)) || '/opt/kivy/data/memwal.key', 'utf8').trim(),
  accountId: '0x762470f60d8cb2b2dd88b68d2d2111d444050dc162e2272bb151cf18c8bd152e',
  serverUrl: 'https://relayer.memory.walrus.xyz', namespace: process.env.KIVY_WAL_NS || 'kivy-rules' });
// semantic search: the top K records closest to the query (distance 0 = same meaning; above 0.9 is dropped)
const r = await m.recall({ query: process.argv[2] || 'Kivy security record', topK: Number(process.argv[3] || 5), maxDistance: 0.9 });
for (const x of r.results) console.log(x.distance.toFixed(2), x.blob_id, '\n  ', x.text.slice(0, 260));
