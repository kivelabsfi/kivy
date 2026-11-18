/*
 * chain.ts — Kivy's Sui chain connection: ONE provider list with failover, used by two processes
 *   · builder.ts (kivy-lend, :8799) imports `grpc` for every read, build and simulation;
 *   · gateway.ts (kivy-chain, :8798) imports `grpc` + the GraphQL helpers and serves chain reads to server.py.
 * Order (2026-11-12, "chain gateway A"): Chainstack -> Triton One -> Mysten public -> Tatum (last, until cutover).
 * A provider whose key file is absent is skipped (one log line, no crash). Each process keeps its own counters, served as
 * GET /chain-stats (health checks: tools/watchdog.py, tools/services_weekly.py); `tatumOnly()` backs GET /tatum-balances.
 * Input: gRPC calls from the Sui SDK / GraphQL queries from gateway.ts. Output: the same chain answers, from the first
 * provider that can give them. Keys: one line per file in KEY_DIR, never printed or logged.
 * Links: @mysten/sui (SuiGrpcClient), @protobuf-ts grpc-web + grpc transports, @grpc/grpc-js (TLS channels).
 */
// How a failed call moves on (per call, in list order):
//   can't answer (down, timeout, overloaded, throttled) -> next provider; this one is skipped for 60 s (5 s for a paid
//                                                          provider that only throttled)
//   key refused (UNAUTHENTICATED / PERMISSION_DENIED)   -> next provider; this one is skipped for 10 minutes
//   method not served (UNIMPLEMENTED)                   -> next provider for that method for 10 minutes (one log line), no penalty
//   "name has expired" (sent as RESOURCE_EXHAUSTED)     -> a real answer about a SuiNS name, passed through — not a throttle
//   transaction not found                               -> the next provider is asked too (nodes prune, and a fresh
//                                                          transaction reaches them at different times); never Tatum
//   any other answer (object not found, bad argument…)  -> a real chain answer, passed through
// When every provider is inside its skip window the list is tried again anyway — a skip is a preference, not an outage.
import fs from 'fs';
import dns from 'dns';
import { SuiGrpcClient } from '@mysten/sui/grpc';
import { GrpcWebFetchTransport } from '@protobuf-ts/grpcweb-transport';
import { GrpcTransport } from '@protobuf-ts/grpc-transport';
import { UnaryCall, RpcError } from '@protobuf-ts/runtime-rpc';
import type { RpcTransport, RpcOptions, MethodInfo } from '@protobuf-ts/runtime-rpc';
import { ChannelCredentials } from '@grpc/grpc-js';

export const KEY_DIR = process.env.KIVY_KEY_DIR || (fs.existsSync('/opt/kivy/keys') ? '/opt/kivy/keys' : '/opt/kivy/data');   // services: systemd's folder (%d); a command-line run as root: the root-only key folder (2026-11-14)                 // tests point this at a temp folder
const MYSTEN = process.env.KIVY_MYSTEN_URL || 'https://fullnode.mainnet.sui.io:443';   // tests point this at a dead address
const MYSTEN_GQL = process.env.KIVY_MYSTEN_GQL || 'https://graphql.mainnet.sui.io/graphql';
const CHAINSTACK_HOST = 'sui-mainnet.core.chainstack.com';                    // gRPC :443 + /graphql, token as x-token (docs.chainstack.com/docs/sui-tooling)
const CHAINSTACK_URL = process.env.KIVY_CHAINSTACK_URL || '';                 // set = grpc-web to this address instead of native gRPC (tests; an ops switch)
const CHAINSTACK_GQL = process.env.KIVY_CHAINSTACK_GQL || `https://${CHAINSTACK_HOST}/graphql`;
const TATUM_HOST = 'sui-mainnet-grpc.gateway.tatum.io';
export const MAINNET_ID = '4btiuiMPvEENsttpZC7CZ53DruC3MAgfznDbASZ7DR6S';     // Sui mainnet's chain id (GraphQL shows its first bytes: 35834a8a)
const ORDER = (process.env.KIVY_PROVIDERS || 'chainstack,triton,mysten,tatum').split(',').map((s) => s.trim()).filter(Boolean);
const SKIP_MS = 60_000, LIMIT_SKIP_PAID_MS = 5_000, AUTH_SKIP_MS = 600_000, NO_METHOD_MS = 600_000;
const DOWN = new Set(['UNAVAILABLE', 'DEADLINE_EXCEEDED', 'INTERNAL', 'UNKNOWN', 'RESOURCE_EXHAUSTED', 'ABORTED']);
const AUTH = new Set(['UNAUTHENTICATED', 'PERMISSION_DENIED']);
const KEY_RE = /^[A-Za-z0-9._~+=_-]{8,256}$/;                                  // one header-safe token; anything else = a bad key file
const BIG = { 'grpc.max_receive_message_length': 64 * 1024 * 1024, 'grpc.keepalive_time_ms': 60_000, 'grpc.keepalive_timeout_ms': 10_000 };

// how long one provider may take over one call before the next is asked; 0 = no limit. The builder's simulations can be
// slow, so its default is generous; gateway.ts sets its own, shorter one.
export const config = { attemptMs: Number(process.env.KIVY_CHAIN_ATTEMPT_MS || 30_000) };

export { RpcError, UnaryCall };                         // for tests/test_chain_20261112.mts (fake providers)
const log = (m: string) => console.error('kivy chain: ' + m);
const quiet = (p: Promise<any>) => { p.catch(() => {}); return p; };
const codeOf = (e: any) => (e instanceof RpcError ? String(e.code) : '');
// an error text on its way to a log, /chain-stats or an alert: no address, no digest (upstream messages can quote the request)
export const scrub = (s: string) => String(s).replace(/0x[0-9a-fA-F]{8,}/g, '0x…').replace(/[1-9A-HJ-NP-Za-km-z]{32,}/g, '…');
const short = (e: any) => scrub(String(codeOf(e) || e?.message || e)).slice(0, 120);

// ── the provider list ──
export type Provider = {
  name: string;
  paid: boolean;                        // on a plan with our key: its capacity is ours (the server's shadow reads go to 100%)
  lastResort?: boolean;                 // a metered free backup (Tatum): never asked a second "not found" question
  keyFile?: string;                     // file name inside KEY_DIR; absent = the provider is skipped
  state: 'on' | 'no key' | 'bad key' | 'wrong chain';
  connect: () => Promise<RpcTransport | null> | RpcTransport | null;   // the gRPC transport (null = no key)
  verify?: boolean;                     // check the chain id once before the first use (keyed providers)
  verified?: boolean;
  wrap?: <T>(call: () => Promise<T>) => Promise<T>;                    // provider-specific pacing (Tatum's 3 calls/s)
  gql?: { url: string; windowDays: number | null; headers: () => Record<string, string> | null };   // its GraphQL side, if it has one
  ok: number; fail: number; downUntil: number; lastError: string; lastFailAt: number; noMethod: Map<string, number>;   // method -> not asked until
  gqlOk: number; gqlFail: number; gqlDownUntil: number;
};
const blank = () => ({ ok: 0, fail: 0, downUntil: 0, lastError: '', lastFailAt: 0, noMethod: new Map<string, number>(), gqlOk: 0, gqlFail: 0, gqlDownUntil: 0 });
export const newProvider = (p: Pick<Provider, 'name' | 'connect'> & Partial<Provider>): Provider => ({ paid: false, state: 'on', ...blank(), ...p });

// One line from a key file. null = no file (the provider is skipped); a file that is not one clean token marks it 'bad key'.
const _keys = new Map<string, string | null>();
function readKey(p: Provider, parse: (line: string) => string = (s) => s): string | null {
  if (!p.keyFile) return '';
  if (_keys.has(p.name)) return _keys.get(p.name)!;
  let key: string | null = null;
  try {
    const line = fs.readFileSync(`${KEY_DIR}/${p.keyFile}`, 'utf8').trim();
    try { key = parse(line); if (!key) throw new Error('empty'); p.state = 'on'; }
    catch { key = null; p.state = 'bad key'; log(`${p.name} skipped: its key file is not in the expected form`); }
  } catch { p.state = 'no key'; log(`${p.name} skipped: no key file (${p.keyFile})`); }
  _keys.set(p.name, key);
  return key;
}
const token = (s: string) => { if (!KEY_RE.test(s)) throw new Error('bad'); return s; };

// Triton One gives each customer its own endpoint host plus a token (docs.triton.one/chains/sui/grpc: X-Token on every call).
// Its key file is one line: "host token", or the endpoint as a URL whose path is the token. No account yet (2026-11-12).
export function tritonParts(line: string): { host: string; token: string } {
  const s = String(line).trim();
  let host = '', tok = '';
  if (/^https?:\/\//i.test(s)) { const u = new URL(s); host = u.host; tok = u.pathname.replace(/^\/+|\/+$/g, ''); }
  else [host, tok = ''] = s.split(/\s+/);
  if (!/^[a-z0-9.-]{4,200}(:\d{1,5})?$/i.test(host) || !KEY_RE.test(tok)) throw new Error('bad');
  return { host: /:\d+$/.test(host) ? host : host + ':443', token: tok };
}

const mystenT = new GrpcWebFetchTransport({ baseUrl: MYSTEN });

// the Tatum connection: native gRPC over IPv4 with the API key in a header (its key's allowlist has only the server's IPv4
// address; it refuses grpc-web); null when no key is installed
let _tatum: { t: GrpcTransport; ip: string; at: number } | null = null;
async function tatum(p: Provider): Promise<GrpcTransport | null> {
  if (_tatum && Date.now() - _tatum.at < 10 * 60_000) return _tatum.t;      // re-resolve the IPv4 address every 10 minutes
  const key = readKey(p, token);
  if (!key) return null;
  const { address: ip } = await dns.promises.lookup(TATUM_HOST, { family: 4 });
  if (_tatum && _tatum.ip === ip) { _tatum.at = Date.now(); return _tatum.t; }
  try { _tatum?.t.close(); } catch {}
  const t = new GrpcTransport({ host: `ipv4:${ip}:443`, channelCredentials: ChannelCredentials.createSsl(),
    clientOptions: { 'grpc.ssl_target_name_override': TATUM_HOST, 'grpc.default_authority': TATUM_HOST }, meta: { 'x-api-key': key } });
  _tatum = { t, ip, at: Date.now() };
  return t;
}
// Tatum's free plan allows only a few calls a second (Suilend's reads fire dozens at once -> HTTP 429):
// space the calls out and retry the ones it turns away
const TATUM_RPS = Number(process.env.KIVY_TATUM_RPS || 3);
let nextSlot = 0;
const slot = () => { const now = Date.now(); const at = Math.max(now, nextSlot); nextSlot = at + 1000 / TATUM_RPS; return new Promise((r) => setTimeout(r, at - now)); };
const limited = (e: any) => /429|RESOURCE_EXHAUSTED|Too Many/i.test(String(e?.code ?? '') + ' ' + String(e?.message ?? e));
async function tatumPaced<T>(call: () => Promise<T>): Promise<T> {
  for (let i = 0; ; i++) {
    await slot();
    try { return await call(); }
    catch (e: any) { if (limited(e) && i < 5) { await new Promise((r) => setTimeout(r, 500 * (i + 1))); continue; } throw e; }
  }
}

// one transport per keyed provider, opened on first use and kept
const _open = new Map<string, RpcTransport | null>();
const once = (p: Provider, make: (key: string) => RpcTransport, parse?: (line: string) => string) => () => {
  if (_open.has(p.name)) return _open.get(p.name)!;
  const key = readKey(p, parse ?? token);
  const t = key ? make(key) : null;
  _open.set(p.name, t);
  return t;
};

const DEFS: Record<string, () => Provider> = {
  chainstack: () => {
    const p = newProvider({ name: 'chainstack', paid: true, keyFile: 'chainstack.key', verify: true, connect: () => null });
    p.connect = once(p, (key) => CHAINSTACK_URL
      ? new GrpcWebFetchTransport({ baseUrl: CHAINSTACK_URL, meta: { 'x-token': key } })
      : new GrpcTransport({ host: `${CHAINSTACK_HOST}:443`, channelCredentials: ChannelCredentials.createSsl(), clientOptions: BIG, meta: { 'x-token': key } }));
    // its GraphQL keeps filtered transaction history for a rolling 90 days only (docs.chainstack.com/docs/sui-graphql-endpoint)
    p.gql = { url: CHAINSTACK_GQL, windowDays: 90, headers: () => { const k = readKey(p, token); return k ? { 'x-token': k } : null; } };
    return p;
  },
  triton: () => {
    const p = newProvider({ name: 'triton', paid: true, keyFile: 'triton.key', verify: true, connect: () => null });
    p.connect = once(p, (line) => { const { host, token: tok } = tritonParts(line);
      return new GrpcTransport({ host, channelCredentials: ChannelCredentials.createSsl(), clientOptions: BIG, meta: { 'x-token': tok } }); },
      (line) => { tritonParts(line); return line; });
    return p;
  },
  mysten: () => newProvider({ name: 'mysten', connect: () => mystenT, gql: { url: MYSTEN_GQL, windowDays: null, headers: () => ({}) } }),
  tatum: () => { const p = newProvider({ name: 'tatum', lastResort: true, keyFile: 'tatum.key', connect: () => null, wrap: tatumPaced }); p.connect = () => tatum(p); return p; },
};
export const providers: Provider[] = ORDER.filter((n) => DEFS[n]).map((n) => DEFS[n]());
// which keyed providers have a key file, known at start (the file is only read when the provider is first used)
for (const p of providers) if (p.keyFile) { try { fs.accessSync(`${KEY_DIR}/${p.keyFile}`, fs.constants.R_OK); } catch { p.state = 'no key'; _keys.set(p.name, null); log(`${p.name} skipped: no key file (${p.keyFile})`); } }

// Chainstack's free plan allows 3M calls a month. The services' counts reach the watchdog through /chain-stats; a one-off
// process (the builder command line run by tests and tools) has no endpoint, so it appends its count when it exits
// (2026-11-13, usage alert at 60% / 85%). Runs against fake servers (KIVY_KEY_DIR / KIVY_CHAINSTACK_URL set) are skipped.
let _service = false;
const USAGE_FILE = process.env.KIVY_CHAIN_USAGE_FILE || '/srv/kivy/builder/state/chain_usage_cli.jsonl';
process.on('exit', () => {
  if (_service || process.env.KIVY_KEY_DIR || CHAINSTACK_URL) return;
  const p = providers.find((x) => x.name === 'chainstack');
  const n = p ? p.ok + p.fail + p.gqlOk + p.gqlFail : 0;
  if (n > 0) { try { fs.appendFileSync(USAGE_FILE, JSON.stringify({ t: Math.round(Date.now() / 1000), n }) + '\n'); } catch {} }
});

// ── counters (GET /chain-stats): <name>_ok / <name>_fail per provider (the old flat names stay), plus the list ──
export const stats: Record<string, any> = { mysten_ok: 0, mysten_fail: 0, tatum_ok: 0, tatum_fail: 0, last_failover: 0, last_error: '' };
function count(p: Provider, ok: boolean, e?: any) {
  if (ok) { p.ok++; stats[p.name + '_ok'] = p.ok; return; }
  p.fail++; stats[p.name + '_fail'] = p.fail; p.lastError = short(e); p.lastFailAt = Date.now();
  stats.last_failover = p.lastFailAt; stats.last_error = p.lastError;
}
export const providerStats = (list: Provider[] = providers) => list.map((p) => ({ name: p.name, state: p.state, paid: p.paid, ok: p.ok, fail: p.fail,
  skipped: Date.now() < p.downUntil, last_error: p.lastError, last_fail_at: p.lastFailAt, not_served: [...p.noMethod].filter(([, until]) => Date.now() < until).length,
  ...(p.gql ? { gql_ok: p.gqlOk, gql_fail: p.gqlFail, gql_skipped: Date.now() < p.gqlDownUntil } : {}) }));
Object.defineProperty(stats, 'providers', { enumerable: true, get: () => providerStats() });
// true when a paid provider is switched on and answering (not benched by a failure): its capacity carries the reads
export const paidActive = (list: Provider[] = providers) => list.some((p) => p.paid && p.state === 'on' && p.ok > 0 && Date.now() >= p.downUntil);

// every 5 minutes, one line with what each provider answered since the last line (only when something moved)
export function startStatsLog(tag: string) {
  _service = true;                                   // a service: its counts are read from /chain-stats, not appended at exit
  const last = new Map<string, number[]>();
  setInterval(() => {
    const parts: string[] = [];
    for (const p of providers) {
      const [o, f, go, gf] = last.get(p.name) ?? [0, 0, 0, 0];
      const d = [p.ok - o, p.fail - f, p.gqlOk - go, p.gqlFail - gf];
      last.set(p.name, [p.ok, p.fail, p.gqlOk, p.gqlFail]);
      if (d.some(Boolean)) parts.push(`${p.name} ok=${d[0]} fail=${d[1]}` + (d[2] || d[3] ? ` gql_ok=${d[2]} gql_fail=${d[3]}` : ''));
    }
    if (parts.length) console.error(`kivy chain: 5m ${tag} · ` + parts.join(' · '));
  }, 300_000).unref();
}

// ── one call to one provider, under a deadline of our own (grpc-web only sends the deadline as a header) ──
function attempt<I extends object, O extends object>(t: RpcTransport, method: MethodInfo<I, O>, input: I, base: RpcOptions, ms: number): Promise<{ c: UnaryCall<I, O>; r: O }> {
  const ac = new AbortController(); let timedOut = false;
  const outer = base.abort; const onOuter = () => ac.abort();
  if (outer) { if (outer.aborted) ac.abort(); else outer.addEventListener('abort', onOuter, { once: true }); }
  const timer = ms > 0 ? setTimeout(() => { timedOut = true; ac.abort(); }, ms) : null;
  const c = t.unary(method, input, { ...base, abort: ac.signal, ...(ms > 0 && !base.timeout ? { timeout: ms } : {}) });
  [c.headers, c.status, c.trailers].forEach(quiet);
  return c.response.then((r) => ({ c, r }), (e) => { throw timedOut ? new RpcError(`no answer in ${ms} ms`, 'DEADLINE_EXCEEDED') : e; })
    .finally(() => { if (timer) clearTimeout(timer); outer?.removeEventListener('abort', onOuter); });
}

// a keyed provider must answer as Sui mainnet before it is used (a testnet or wrong-chain endpoint would hand out wrong balances)
async function checkChain(p: Provider, t: RpcTransport): Promise<void> {
  if (!p.verify || p.verified) return;
  const one = new SuiGrpcClient({ network: 'mainnet', transport: t });
  const ac = new AbortController(); const timer = setTimeout(() => ac.abort(), 8000);
  try {
    const { response } = await (one as any).ledgerService.getServiceInfo({}, { abort: ac.signal });
    if (response.chainId !== MAINNET_ID) { p.state = 'wrong chain'; log(`${p.name} switched off: it did not answer as Sui mainnet`); throw new Error('wrong chain'); }
    p.verified = true; log(`${p.name} is on (answers as Sui mainnet)`);
  } finally { clearTimeout(timer); }
}

// A protobuf-ts transport that the Sui SDK calls for every gRPC method. Unary calls (all Kivy uses) walk the provider list
// as described at the top; streaming calls stay on Mysten.
export class FailoverTransport implements RpcTransport {
  constructor(private list: Provider[] = providers) {}
  mergeOptions(o?: Partial<RpcOptions>): RpcOptions { return mystenT.mergeOptions(o); }
  unary<I extends object, O extends object>(method: MethodInfo<I, O>, input: I, options: RpcOptions): UnaryCall<I, O> {
    const mkey = `${method.service.typeName}/${method.name}`;
    const isTxLookup = /LedgerService\/(GetTransaction|BatchGetTransactions)$/.test(mkey);
    const run = async () => {
      let lastErr: any = null, notFound: any = null;
      const tried = new Set<Provider>();
      for (const pass of [0, 1]) {                     // pass 0: providers not in a skip window; pass 1: the skipped ones, rather than no answer
        for (const p of this.list) {
          if (tried.has(p) || p.state !== 'on' || Date.now() < (p.noMethod.get(mkey) ?? 0)) continue;
          if (pass === 0 && Date.now() < p.downUntil) continue;
          if (notFound && p.lastResort) continue;
          tried.add(p);
          let t: RpcTransport | null;
          try { t = await p.connect(); if (t) await checkChain(p, t); }
          catch (e: any) { if (p.state === 'on') { count(p, false, e); p.downUntil = Date.now() + SKIP_MS; } lastErr = e; continue; }
          if (!t) continue;                            // no key
          // Mysten gets the caller's options as they are; a keyed provider gets its own defaults (the key header) plus the
          // caller's headers (the SDK's protocol-version one); Tatum, as before, only the deadline
          const base = p.name === 'mysten' ? options : t.mergeOptions({ abort: options.abort, timeout: options.timeout, ...(p.lastResort ? {} : { meta: options.meta }) });
          const call = () => attempt(t!, method, input, base, config.attemptMs);
          try {
            const out = await (p.wrap ? p.wrap(call) : call());
            count(p, true); p.downUntil = 0;
            return out;
          } catch (e: any) {
            const code = codeOf(e);
            if (e instanceof RpcError && code === 'UNIMPLEMENTED') {    // also what grpc-web makes of a stray HTTP 404, hence the expiry
              p.noMethod.set(mkey, Date.now() + NO_METHOD_MS); log(`${p.name} does not serve ${method.name}; the next provider is asked for 10 minutes`); lastErr = e; continue;
            }
            if (e instanceof RpcError && code === 'RESOURCE_EXHAUSTED' && /^name(%20| )has(%20| )expired$/.test(String(e.message))) throw e;   // SuiNS: a real answer
            if (e instanceof RpcError && code === 'NOT_FOUND' && isTxLookup) { notFound = e; continue; }
            if (e instanceof RpcError && !DOWN.has(code) && !AUTH.has(code)) throw e;      // a real chain answer
            count(p, false, e);
            p.downUntil = Date.now() + (AUTH.has(code) ? AUTH_SKIP_MS : code === 'RESOURCE_EXHAUSTED' && p.paid ? LIMIT_SKIP_PAID_MS : SKIP_MS);
            if (AUTH.has(code)) log(`${p.name} refused our key (${code}); skipped for 10 minutes`);
            lastErr = e;
          }
        }
        if (notFound) break;                           // the transaction is not on the providers that answered: that is the answer
      }
      throw notFound ?? lastErr ?? new Error('Sui chain unavailable (no provider is switched on)');
    };
    const res = run();
    quiet(res);
    return new UnaryCall<I, O>(method, options.meta ?? {}, input, quiet(res.then((x) => x.c.headers)), res.then((x) => x.r),
      quiet(res.then((x) => x.c.status)), quiet(res.then((x) => x.c.trailers)));
  }
  serverStreaming(method: any, input: any, options: RpcOptions): any { return (mystenT as any).serverStreaming(method, input, options); }   // never used by Kivy
  clientStreaming(method: any, options: RpcOptions): any { return (mystenT as any).clientStreaming(method, options); }
  duplex(method: any, options: RpcOptions): any { return (mystenT as any).duplex(method, options); }
}

// the client every part of kivy-lend and kivy-chain uses
export const grpc = new SuiGrpcClient({ network: 'mainnet', transport: new FailoverTransport() as any });

// One provider alone, for the health checks (watchdog, the Monday services report, the gateway's /probe)
export async function only(name: string): Promise<SuiGrpcClient> {
  const p = providers.find((x) => x.name === name);
  if (!p) throw new Error('no such provider');
  const t = p.state === 'on' ? await p.connect() : null;
  if (!t) throw new Error(`no ${name[0].toUpperCase() + name.slice(1)} key`);
  return new SuiGrpcClient({ network: 'mainnet', transport: t });
}
export const tatumOnly = () => only('tatum');

// ── GraphQL (the history part: what gRPC cannot answer) ──
// The providers that can take a GraphQL query now, in list order. windowDays = how far back its filtered transaction
// history goes (null = all of it); the caller decides whether a windowed answer is enough (gateway.ts).
export function gqlSources(list: Provider[] = providers): { name: string; windowDays: number | null }[] {
  const have = list.filter((p) => p.gql && p.state === 'on' && p.gql.headers());
  const up = have.filter((p) => Date.now() >= p.gqlDownUntil);
  return (up.length ? up : have).map((p) => ({ name: p.name, windowDays: p.gql!.windowDays }));
}
// One query to one provider; throws when it cannot answer (HTTP error, GraphQL errors, timeout).
export const GQL_FULL_MS = 8_000;                     // a full-length wait: only a timeout at least this long counts against the provider
export async function gqlVia(name: string, query: string, variables: Record<string, any> = {}, timeoutMs = GQL_FULL_MS, list: Provider[] = providers): Promise<any> {
  const p = list.find((x) => x.name === name);
  const headers = p?.gql?.headers();
  if (!p || !p.gql || !headers) throw new Error('no such GraphQL provider');
  const ac = new AbortController(); const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(p.gql.url, { method: 'POST', signal: ac.signal, body: JSON.stringify({ query, variables }),
      headers: { 'Content-Type': 'application/json', 'User-Agent': 'kivy/1', ...headers } });
    if (!r.ok) throw Object.assign(new Error('HTTP ' + r.status), { bench: r.status === 429 || r.status >= 500 || r.status === 401 || r.status === 403, refused: r.status === 401 || r.status === 403 });
    const out: any = await r.json();
    if (out?.errors || !out?.data) throw Object.assign(new Error('GraphQL: ' + String(out?.errors?.[0]?.message ?? 'no data').slice(0, 100)), { bench: false });   // this query, not the provider
    p.gqlOk++;
    return out.data;
  } catch (e: any) {
    // the provider is skipped only when IT is in trouble: throttled, erroring, unreachable, or silent for a full-length wait
    // (a caller short of time may pass a small timeoutMs — running out of that says nothing about the provider)
    const timeout = e?.name === 'AbortError';
    // its GraphQL refuses our key (a plan or node that serves gRPC only: Chainstack answered 403, 2026-11-13): skip its GraphQL
    // for 10 minutes and do not count it as a failure; a key that is bad everywhere is caught on the gRPC side
    if (e?.refused) { p.gqlDownUntil = Date.now() + AUTH_SKIP_MS; p.lastError = 'gql ' + e.message + ' (key not accepted for GraphQL)'; throw new Error(`${name} GraphQL: ${p.lastError}`); }
    if (!timeout || timeoutMs >= GQL_FULL_MS) { p.gqlFail++; p.lastFailAt = Date.now(); }
    if (timeout ? timeoutMs >= GQL_FULL_MS : e?.bench !== false) p.gqlDownUntil = Date.now() + SKIP_MS;
    p.lastError = 'gql ' + scrub(String(timeout ? 'timeout' : e?.message ?? e)).slice(0, 100);
    throw new Error(`${name} GraphQL: ${p.lastError}`);
  } finally { clearTimeout(timer); }
}
