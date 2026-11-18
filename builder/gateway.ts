/*
 * gateway.ts — Kivy's chain gateway: every chain READ the site server needs, behind one local HTTP service.
 * In operation: kivy-chain.service runs `tsx gateway.ts serve` on 127.0.0.1:8798 (never routed by the tunnel). server.py
 * asks it in SHADOW mode since 2026-11-12 (its own GraphQL reads stay the source of truth until the owner's cutover OK).
 * Input: GET /<route>?… from server.py and tools/watchdog.py. Output: JSON, {ok:true,…} or {ok:false,error} with an HTTP code.
 * Links: chain.ts (the ONE provider list: Chainstack -> Triton One -> Mysten -> Tatum; gRPC failover + GraphQL helpers),
 *        txfacts.ts (check 2's facts). Design: ../reports/2026-11-12-chain-gateway.md. Tests: ../tests/test_gateway_20261112.py.
 *
 * Routes (live state over gRPC; history over GraphQL, because gRPC has no "transactions of an address"):
 *   GET /balances?a=<addr>[&fresh=1]   every coin balance of a wallet                      gRPC   cache 2 s
 *   GET /name?n=<name.sui>             a SuiNS name's address (null = no such name)         gRPC   60 s (30 s when none)
 *   GET /object-kind?a=<addr>          'package' | 'object' | 'account' (= no object)       gRPC   60 s (10 min package)
 *   GET /tx?d=<digest>                 one transaction: status, error, balance changes, fee gRPC, then GraphQL when the nodes
 *                                                                                           have pruned it; 10 min once found
 *   GET /tx-facts?d=<digest>           check 2's facts (txfacts.ts)                         gRPC   10 min once found
 *   GET /history?a=&n=&kind=&detail=   a wallet's last n transactions (affected | sent)     GraphQL 5 s
 *   GET /used?a=<addr>                 has this address ever appeared in a transaction      GraphQL 1 h yes / 20 s no
 *   POST /verify-signature             {address, message, signature}: zkLogin/passkey/multisig personal-message check   gRPC, never cached
 *   GET /probe?p=<provider>[&gql=1]    one provider alone (health; after a new key)         5 s
 *   GET /chain-stats · GET /health     counters per provider + cache + routes · liveness
 * Rules: an answer is cached only when it is complete and not an error; identical requests in flight share one upstream
 * call; at most MAX_INFLIGHT upstream computations at once (more = 503 at once, never a queue); every computation has one
 * total deadline. A GraphQL provider that keeps only a window of history is believed only when its answer cannot be
 * cut short by that window (it returned as many rows as asked); otherwise the full-history provider is asked.
 * Nothing here logs an address, a name, a digest or a key.
 */
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { grpc, stats as chainStats, providers, paidActive, gqlSources, gqlVia, only, config, startStatsLog, scrub, MAINNET_ID, KEY_DIR } from './chain.ts';
import { txFacts } from './txfacts.ts';

const PORT = Number(process.env.KIVY_GATEWAY_PORT || 8798);
const ROUTE_MS = Number(process.env.KIVY_GATEWAY_ROUTE_MS || 12_000);      // one computation, all providers and pages together
const MAX_INFLIGHT = Number(process.env.KIVY_GATEWAY_MAX_INFLIGHT || 32);
// the answer cache's bounds: entries, total size (an answer's JSON length; a 50-row history is far larger than a balance) and
// one answer's size (bigger is never kept). Expired answers are dropped every 30 s at most, not only when the cache is full
// (fix package 4, 2026-11-15; a reviewer builder scan S1: it was capped by count only, and kept expired answers until 5,000).
const CACHE_MAX = 5000;
const CACHE_BYTES = Number(process.env.KIVY_GATEWAY_CACHE_BYTES || 32 * 1024 * 1024), ENTRY_BYTES = CACHE_BYTES / 32;
config.attemptMs = Number(process.env.KIVY_GATEWAY_ATTEMPT_MS || 5_000);   // one provider, one call (reads are small)
const g: any = grpc;

const ADDR = /^0x[0-9a-fA-F]{64}$/, DIGEST = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/, NAME = /^[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63}){0,3}\.sui$/;
const B64 = /^[A-Za-z0-9+/]{1,12000}={0,2}$/;

// ── cache + singleflight + the in-flight bound ──
class Refused extends Error { constructor(public http: number, m: string) { super(m); } }
type Entry = { at: number; ttl: number; v: any; n: number };
const cache = new Map<string, Entry>();
let cacheBytes = 0, lastSweep = Date.now();
const flights = new Map<string, Promise<any>>();
export const gw = { started: Date.now(), hits: 0, misses: 0, shared: 0, busy: 0, timeouts: 0, inflight: 0, peak: 0,
                    routes: {} as Record<string, { n: number; err: number; ms: number; max: number }> };

// a refused or timed-out read is never silent: one line per kind at most every 10 s (the watchdog alerts on them)
const lastSaid: Record<string, number> = {};
const say = (kind: string, line: string) => { if (Date.now() - (lastSaid[kind] ?? 0) > 10_000) { lastSaid[kind] = Date.now(); console.error(line); } };

function drop(key: string) { const e = cache.get(key); if (e) { cacheBytes -= e.n; cache.delete(key); } }
export function sweep(now = Date.now()) { lastSweep = now; for (const [k, e] of cache) if (now - e.at >= e.ttl) drop(k); }
export function put(key: string, v: any, ttl: number, now = Date.now()) {
  drop(key);
  const n = key.length + JSON.stringify(v).length;
  if (n > ENTRY_BYTES) return;                       // one answer this big is not kept
  const full = cache.size >= CACHE_MAX || cacheBytes + n > CACHE_BYTES;
  if (now - lastSweep >= 30_000 || (full && now - lastSweep >= 1_000)) sweep(now);   // full: at most one sweep a second
  for (const k of cache.keys()) {                    // still full: the oldest go first (a Map keeps insertion order)
    if (cache.size < CACHE_MAX && cacheBytes + n <= CACHE_BYTES) break;
    drop(k);
  }
  cache.set(key, { at: now, ttl, v, n }); cacheBytes += n;
}
export const cacheState = () => ({ size: cache.size, bytes: cacheBytes, max: CACHE_MAX, max_bytes: CACHE_BYTES });

// load(signal) runs at most once per key at a time; its answer is kept ttlOf(answer) ms (0 = not kept). `fresh` skips the
// cached copy (the server asks this right after a wallet signed) and shares only with other fresh calls in flight.
export async function shared(key: string, fresh: boolean, ttlOf: (v: any) => number, load: (signal: AbortSignal) => Promise<any>): Promise<any> {
  if (!fresh) {
    const e = cache.get(key);
    if (e && Date.now() - e.at < e.ttl) { gw.hits++; return { ...e.v, cached_ms: Date.now() - e.at }; }
  }
  const fkey = fresh ? key + '#fresh' : key;           // a fresh read never joins a flight that started before it was asked for
  const f = flights.get(fkey);
  if (f) { gw.shared++; return f; }
  if (gw.inflight >= MAX_INFLIGHT) { gw.busy++; say('busy', `kivy-chain busy: a read refused, all ${MAX_INFLIGHT} slots taken`); throw new Refused(503, 'the chain gateway is busy'); }
  gw.misses++; gw.inflight++; gw.peak = Math.max(gw.peak, gw.inflight);
  const ac = new AbortController();
  const work = load(ac.signal);
  work.then(() => {}, () => {}).finally(() => { gw.inflight--; });           // the slot is free when the upstream work has really ended
  let late = false;                                    // past its deadline: whatever arrives now is not cached
  const p = new Promise<any>((resolve, reject) => {
    const timer = setTimeout(() => { late = true; gw.timeouts++; say('slow', `kivy-chain slow: a read got no answer in ${ROUTE_MS / 1000} s`); ac.abort(); reject(new Refused(504, 'the chain did not answer in time')); }, ROUTE_MS);
    work.then((v) => { clearTimeout(timer); const ttl = late ? 0 : ttlOf(v); if (ttl > 0) put(key, v, ttl); resolve(v); },
              (e) => { clearTimeout(timer); reject(e); });
  }).finally(() => { flights.delete(fkey); });
  flights.set(fkey, p);
  return p;
}

// ── the reads ──
async function balances(a: string, signal: AbortSignal) {
  const rows: any[] = []; let cursor: string | null = null, complete = false;
  for (let i = 0; i < 20; i++) {                     // 50 a page; `complete` travels with the data — a walk that stops at its cap says so
    const r: any = await g.core.listBalances({ owner: a, limit: 50, cursor, signal });
    rows.push(...r.balances.map((b: any) => ({ coinType: b.coinType, totalBalance: String(b.balance) })));
    if (!r.hasNextPage) { complete = true; break; }
    cursor = r.cursor;
  }
  return { ok: true, balances: rows, complete };
}

async function nameAddress(n: string, signal: AbortSignal) {
  const r: any = await g.core.resolveNameServiceAddress({ name: n, signal });
  return { ok: true, address: r?.address ?? null };
}

async function objectKind(a: string, signal: AbortSignal) {
  try {
    const r: any = await g.core.getObject({ objectId: a, signal });
    return { ok: true, kind: r?.object?.type === 'package' ? 'package' : 'object' };
  } catch (e: any) {
    if (e?.code === 'notExists') return { ok: true, kind: 'account' };   // no object there: a wallet address (or unused). Any other error is an error, never "a wallet"
    throw e;
  }
}

const iso = (ms: any) => (Number(ms) > 0 ? new Date(Number(ms)).toISOString() : null);
const ordinal = (m: string) => { const x = /in (\d+)\w* command/.exec(m); return x ? Number(x[1]) - 1 : null; };
// one transaction in one shape, whichever interface answered
function txFromGrpc(r: any) {
  const t = r.Transaction ?? r.FailedTransaction ?? r.transaction ?? r;
  const st = t.effects?.status ?? t.status;
  if (typeof st?.success !== 'boolean') throw new Error('the chain reply has no effects for this transaction yet');
  const err = st.success ? null : st.error ?? {};
  const gu = t.effects?.gasUsed ?? {};
  return { digest: t.digest, sender: t.transaction?.sender ?? null, status: st.success ? 'SUCCESS' : 'FAILURE', timestamp: iso(t.timestampMs),
    checkpoint: t.checkpoint != null ? String(t.checkpoint) : null,
    error: err && { message: String(err.message ?? '').slice(0, 400), kind: err.$kind ?? null, abortCode: err.MoveAbort?.abortCode != null ? String(err.MoveAbort.abortCode) : null,
                    module: err.MoveAbort?.location?.module ?? null, command: err.command ?? ordinal(String(err.message ?? '')) },
    balanceChanges: (t.balanceChanges ?? []).map((b: any) => ({ owner: b.address, coinType: b.coinType, amount: String(b.amount) })),
    gas: { computationCost: String(gu.computationCost ?? 0), storageCost: String(gu.storageCost ?? 0), storageRebate: String(gu.storageRebate ?? 0) } };
}
const TX_FIELDS = 'digest sender { address } effects { status timestamp checkpoint { sequenceNumber } executionError { message abortCode module { name } } '
  + 'balanceChanges { nodes { owner { address } coinType { repr } amount } } gasEffects { gasSummary { computationCost storageCost storageRebate } } }';
function txFromGql(t: any) {
  const e = t.effects ?? {}; const x = e.executionError; const gs = e.gasEffects?.gasSummary ?? {};
  return { digest: t.digest, sender: t.sender?.address ?? null, status: e.status ?? null, timestamp: e.timestamp ?? null,
    checkpoint: e.checkpoint?.sequenceNumber != null ? String(e.checkpoint.sequenceNumber) : null,
    error: x ? { message: String(x.message ?? '').slice(0, 400), kind: null, abortCode: x.abortCode != null ? String(x.abortCode) : null, module: x.module?.name ?? null,
                 command: ordinal(String(x.message ?? '')) } : null,
    balanceChanges: (e.balanceChanges?.nodes ?? []).map((b: any) => ({ owner: b.owner?.address ?? null, coinType: b.coinType?.repr, amount: String(b.amount) })),
    gas: { computationCost: String(gs.computationCost ?? 0), storageCost: String(gs.storageCost ?? 0), storageRebate: String(gs.storageRebate ?? 0) } };
}
const leftMs = (t0: number) => Math.max(1500, ROUTE_MS - (Date.now() - t0) - 500);

async function txByDigest(d: string, signal: AbortSignal) {
  const t0 = Date.now();
  let nodesDown: any = null;                           // the nodes could not answer at all (not the same as "not found")
  try {
    const r: any = await g.core.getTransaction({ digest: d, include: { effects: true, balanceChanges: true, transaction: true }, signal });
    return { ok: true, found: true, source: 'grpc', tx: txFromGrpc(r) };
  } catch (e: any) {
    if (e?.code === 'INVALID_ARGUMENT') throw e;
    if (e?.constructor?.name !== 'TransactionError' && e?.code !== 'NOT_FOUND') nodesDown = e;
  }
  // not on the nodes: pruned (Mysten's public node drops transactions after a few weeks) or not there yet — the indexers know
  let last: any = null, asked = 0;
  for (const s of gqlSources()) {
    try {
      const data = await gqlVia(s.name, `query($d: String!) { transaction(digest: $d) { ${TX_FIELDS} } }`, { d }, Math.min(8000, leftMs(t0)));
      asked++;
      if (data.transaction?.effects) return { ok: true, found: true, source: 'graphql:' + s.name, tx: txFromGql(data.transaction) };
    } catch (e) { last = e; }
    if (signal.aborted) break;
  }
  if (nodesDown) throw nodesDown;                      // nodes silent + not in an index: unknown, never "not found" (a fresh one may be on the nodes only)
  if (!asked && last) throw last;
  return { ok: true, found: false };
}

// A wallet's last n transactions, newest last (as GraphQL's `last: n` gives them). detail 0 = digests, 1 = + status and
// time, 2 = + sender, error and balance changes. `complete` is false only when a windowed provider answered alone.
async function history(a: string, n: number, kind: 'affected' | 'sent', detail: number, signal: AbortSignal, first = false) {
  const fields = detail >= 2 ? TX_FIELDS : detail === 1 ? 'digest effects { status timestamp }' : 'digest';
  const q = `query($a: SuiAddress!, $n: Int) { transactions(${first ? 'first' : 'last'}: $n, filter: {${kind === 'sent' ? 'sentAddress' : 'affectedAddress'}: $a}) { nodes { ${fields} } } }`;
  const t0 = Date.now();
  let part: any = null, last: any = null;
  for (const s of gqlSources()) {
    try {
      const data = await gqlVia(s.name, q, { a, n }, Math.min(8000, leftMs(t0)));
      const nodes = data.transactions?.nodes ?? [];
      const out = { ok: true, source: s.name, nodes: detail >= 2 ? nodes.map(txFromGql) : nodes.map((x: any) => ({ digest: x.digest, ...(detail === 1 ? { status: x.effects?.status ?? null, timestamp: x.effects?.timestamp ?? null } : {}) })) };
      if (s.windowDays === null || nodes.length >= n) return { ...out, complete: true };
      part = part ?? { ...out, complete: false };      // fewer rows than asked from a provider with a history window: ask the next
    } catch (e) { last = e; }
    if (signal.aborted) break;
  }
  if (part) return part;
  throw last ?? new Error('no GraphQL provider is switched on');
}

async function used(a: string, signal: AbortSignal) {
  const h: any = await history(a, 1, 'affected', 0, signal, true);
  return { ok: true, used: h.nodes.length > 0, complete: h.complete || h.nodes.length > 0, source: h.source };
}

async function verifySignature(b: any, signal: AbortSignal) {
  const r: any = await g.core.verifyZkLoginSignature({ bytes: b.message, signature: b.signature, intentScope: 'PersonalMessage', address: b.address, signal });
  return { ok: true, valid: r?.success === true };     // anything but a clear yes is a no
}

async function probe(name: string, viaGql: boolean) {
  const t0 = Date.now();
  if (viaGql) {
    const d = await gqlVia(name, '{ chainIdentifier }', {}, 8000);
    const id = String(d.chainIdentifier ?? '');
    return { ok: true, provider: name, interface: 'graphql', ms: Date.now() - t0, mainnet: id === MAINNET_ID || id === '35834a8a' };
  }
  const c: any = await only(name);
  const ac = new AbortController(); const timer = setTimeout(() => ac.abort(), 8000);
  try {
    const { response } = await c.ledgerService.getServiceInfo({}, { abort: ac.signal });
    return { ok: true, provider: name, interface: 'grpc', ms: Date.now() - t0, mainnet: response.chainId === MAINNET_ID, epoch: String(response.epoch ?? ''), checkpoint: String(response.checkpointHeight ?? '') };
  } finally { clearTimeout(timer); }
}

const stat = () => ({ ok: true, ...chainStats, paid: paidActive(),
  gateway: { up_s: Math.round((Date.now() - gw.started) / 1000), cache: { size: cache.size, bytes: cacheBytes, hits: gw.hits, misses: gw.misses, shared: gw.shared },
             inflight: gw.inflight, peak: gw.peak, max_inflight: MAX_INFLIGHT, busy: gw.busy, timeouts: gw.timeouts, routes: gw.routes } });

// ── request -> route ──
let sigSeq = 0;                                     // signature checks are never shared or cached: each gets its own key
const TTL = { balances: 2_000, name: 60_000, noName: 30_000, kind: 60_000, pkg: 600_000, tx: 600_000, history: 5_000, used: 3_600_000, unused: 20_000, probe: 5_000 };
export async function route(method: string, path: string, q: URLSearchParams, body: any): Promise<any> {
  const a = q.get('a') ?? '', d = q.get('d') ?? '', fresh = q.get('fresh') === '1';
  const need = (ok: boolean) => { if (!ok) throw new Refused(400, 'bad arguments'); };
  if (method === 'GET') switch (path) {
    case '/health': return { ok: true, service: 'kivy-chain', up_s: Math.round((Date.now() - gw.started) / 1000), paid: paidActive(), providers: providers.map((p) => ({ name: p.name, state: p.state })) };
    case '/chain-stats': return stat();
    case '/balances': need(ADDR.test(a)); return shared('bal:' + a.toLowerCase(), fresh, (v) => (v.complete ? TTL.balances : 0), (s) => balances(a, s));
    case '/name': { const n = (q.get('n') ?? '').toLowerCase(); need(n.length <= 80 && NAME.test(n)); return shared('name:' + n, fresh, (v) => (v.address ? TTL.name : TTL.noName), (s) => nameAddress(n, s)); }
    case '/object-kind': need(ADDR.test(a)); return shared('kind:' + a.toLowerCase(), fresh, (v) => (v.kind === 'package' ? TTL.pkg : TTL.kind), (s) => objectKind(a, s));
    case '/tx': need(DIGEST.test(d)); return shared('tx:' + d, fresh, (v) => (v.found && v.tx?.timestamp && v.tx?.checkpoint ? TTL.tx : 0), (s) => txByDigest(d, s));   // kept only once it is in a checkpoint
    case '/tx-facts': need(DIGEST.test(d)); return shared('facts:' + d, fresh, (v) => (v.ok && (v.balance_changes?.length || v.modules?.length) ? TTL.tx : 0), () => txFacts(g, d));   // an answer with nothing to compare yet is not kept
    case '/history': {
      const n = Number(q.get('n') ?? '10'), kind = q.get('kind') ?? 'affected', detail = Number(q.get('detail') ?? '1');
      need(ADDR.test(a) && Number.isInteger(n) && n >= 1 && n <= 50 && (kind === 'affected' || kind === 'sent') && [0, 1, 2].includes(detail));
      return shared(`hist:${a.toLowerCase()}:${n}:${kind}:${detail}`, fresh, (v) => (v.complete ? TTL.history : 0), (s) => history(a, n, kind as any, detail, s));
    }
    case '/used': need(ADDR.test(a)); return shared('used:' + a.toLowerCase(), fresh, (v) => (!v.complete ? 0 : v.used ? TTL.used : TTL.unused), (s) => used(a, s));
    case '/probe': { const p = q.get('p') ?? '', viaGql = q.get('gql') === '1'; need(providers.some((x) => x.name === p)); return shared(`probe:${p}:${viaGql}`, false, () => TTL.probe, () => probe(p, viaGql)); }
  }
  if (method === 'POST' && path === '/verify-signature') {
    need(body && ADDR.test(String(body.address ?? '')) && B64.test(String(body.message ?? '')) && B64.test(String(body.signature ?? '')));
    return shared('sig:' + ++sigSeq, true, () => 0, (s) => verifySignature({ address: String(body.address), message: String(body.message), signature: String(body.signature) }, s));
  }
  throw new Refused(404, 'not found');
}

// The gateway's password (2026-11-13, "A ok"): every route that can reach a provider needs the x-kivy-chain-token header,
// the token kivy-site writes once (data/chain.token; this service gets it from systemd, LoadCredential, in KEY_DIR). /health
// and /chain-stats stay open: they never call a provider and the watchdog reads them. No token file = every other route refused.
const OPEN = new Set(['/health', '/chain-stats']);
const ROUTE_NAMES = new Set(['/health', '/chain-stats', '/balances', '/name', '/object-kind', '/tx', '/tx-facts', '/history', '/used', '/probe', '/verify-signature']);
const TOKEN = (() => { try { return fs.readFileSync(`${KEY_DIR}/chain.token`, 'utf8').trim(); } catch { return ''; } })();
const tokenOk = (h: unknown) => {
  const a = Buffer.from(String(h ?? '')), b = Buffer.from(TOKEN);
  return TOKEN.length >= 32 && a.length === b.length && crypto.timingSafeEqual(a, b);
};

// what an error may say to the caller or the journal: its first line, no upstream headers, no address or digest
const plain = (e: any) => scrub(String(e?.message ?? e).split('\n')[0].replace(/%20/g, ' ')).slice(0, 160);

export function serve(port = PORT) {
  const server = http.createServer(async (req, res) => {
    const t0 = Date.now();
    let u: URL;
    try { u = new URL((req.url ?? '/').slice(0, 2000), 'http://x'); } catch { u = new URL('http://x/bad-request-target'); }
    // only the gateway's own routes get a counter: made-up paths all count as 'other', so they can't grow the table (review 2026-11-13)
    const name = ROUTE_NAMES.has(u.pathname) ? u.pathname : 'other';
    const send = (code: number, o: any) => {
      const r = (gw.routes[name] ??= { n: 0, err: 0, ms: 0, max: 0 }); const ms = Date.now() - t0;
      r.n++; r.ms += ms; r.max = Math.max(r.max, ms); if (code >= 500) r.err++;
      res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(o));
    };
    try {
      // local callers only: anything that came through a proxy or the tunnel carries one of these headers
      if (req.headers['cf-connecting-ip'] || req.headers['x-forwarded-for'] || req.headers['cf-ray']) { req.resume(); return send(404, { ok: false, error: 'not found' }); }
      if (!OPEN.has(u.pathname) && !tokenOk(req.headers['x-kivy-chain-token'])) { req.resume(); return send(401, { ok: false, error: 'token required' }); }
      let body: any = null;
      if (req.method === 'POST') {
        let raw = '';
        for await (const ch of req) { raw += ch; if (raw.length > 40_000) throw new Refused(413, 'too large'); }
        try { body = JSON.parse(raw); } catch { throw new Refused(400, 'bad arguments'); }
      } else req.resume();
      send(200, await route(req.method ?? 'GET', u.pathname, u.searchParams, body));
    } catch (e: any) {
      if (e instanceof Refused) send(e.http, { ok: false, error: e.message });
      else send(e?.code === 'INVALID_ARGUMENT' ? 400 : 502, { ok: false, error: plain(e) });   // the chain itself calling the argument bad is the caller's 400
    }
  });
  server.headersTimeout = 5_000; server.requestTimeout = 10_000; server.keepAliveTimeout = 5_000; server.maxHeadersCount = 40; server.maxConnections = 256;
  server.listen(port, '127.0.0.1', () => console.error(`kivy-chain listening on 127.0.0.1:${port}` + (TOKEN ? '' : ' · no chain.token: every route but /health and /chain-stats is refused')));
  return server;
}

if (process.argv[2] === 'serve') {
  // one request's stray error must never stop the gateway for everyone; the watchdog counts these lines
  process.on('unhandledRejection', (e: any) => console.error('kivy-chain: stray error (kept running):', plain(e)));
  process.on('uncaughtException', (e: any) => console.error('kivy-chain: stray exception (kept running):', plain(e)));
  serve();
  setInterval(() => sweep(), 60_000).unref();         // a quiet gateway frees expired answers too
  startStatsLog('kivy-chain');
  // one call at start: the first provider that is switched on answers, so /health shows `paid` without waiting for traffic
  g.ledgerService.getServiceInfo({}).response.then(() => console.error('kivy-chain: chain reachable · paid provider ' + (paidActive() ? 'on' : 'off')),
    (e: any) => console.error('kivy-chain: chain not reachable at start:', plain(e)));
}
