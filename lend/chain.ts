// Kivy's Sui chain connection with a backup (2026-10-24: "use Tatum as our backup data").
// Every gRPC call goes to Mysten's fullnode first; if Mysten can't answer (down, timeout, overloaded), the same call is sent
// to Tatum's Sui gRPC and Mysten is skipped for 60 s. The answers are the same chain data, so builds, simulations and the
// guard work unchanged. Tatum needs native gRPC (it refuses grpc-web), its API key (/opt/kivy/data/tatum.key, never
// printed) and IPv4 (the key's allowlist has only the server's IPv4 address).
import fs from 'fs';
import dns from 'dns';
import { SuiGrpcClient } from '@mysten/sui/grpc';
import { GrpcWebFetchTransport } from '@protobuf-ts/grpcweb-transport';
import { GrpcTransport } from '@protobuf-ts/grpc-transport';
import { UnaryCall, RpcError } from '@protobuf-ts/runtime-rpc';
import type { RpcTransport, RpcOptions, MethodInfo } from '@protobuf-ts/runtime-rpc';
import { ChannelCredentials } from '@grpc/grpc-js';

const MYSTEN = process.env.KIVY_MYSTEN_URL || 'https://fullnode.mainnet.sui.io:443';   // tests point this at a dead address
const TATUM_HOST = 'sui-mainnet-grpc.gateway.tatum.io';
const TATUM_KEY = '/opt/kivy/data/tatum.key';
const SKIP_MS = 60_000;
const FAILOVER = new Set(['UNAVAILABLE', 'DEADLINE_EXCEEDED', 'INTERNAL', 'UNKNOWN', 'RESOURCE_EXHAUSTED', 'ABORTED']);

export const stats = { mysten_ok: 0, mysten_fail: 0, tatum_ok: 0, tatum_fail: 0, last_failover: 0, last_error: '' };
let mystenDownUntil = 0;

const mysten = new GrpcWebFetchTransport({ baseUrl: MYSTEN });

let _tatum: { t: GrpcTransport; ip: string; at: number } | null = null;
async function tatum(): Promise<GrpcTransport | null> {
  if (_tatum && Date.now() - _tatum.at < 10 * 60_000) return _tatum.t;      // re-resolve the IPv4 address every 10 minutes
  let key = '';
  try { key = fs.readFileSync(TATUM_KEY, 'utf8').trim(); } catch { return null; }
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

const shouldFailover = (e: any) => !(e instanceof RpcError) || FAILOVER.has(String(e.code));
const quiet = (p: Promise<any>) => { p.catch(() => {}); return p; };

class FailoverTransport implements RpcTransport {
  mergeOptions(o?: Partial<RpcOptions>): RpcOptions { return mysten.mergeOptions(o); }
  unary<I extends object, O extends object>(method: MethodInfo<I, O>, input: I, options: RpcOptions): UnaryCall<I, O> {
    const viaTatum = async () => {
      const t = await tatum();
      if (!t) throw new Error('Sui chain unavailable (Mysten down, no Tatum key)');
      for (let i = 0; ; i++) {
        await slot();
        const c = t.unary(method, input, t.mergeOptions({ timeout: options.timeout }));
        [c.headers, c.status, c.trailers].forEach(quiet);
        try { const r = await c.response; stats.tatum_ok++; return { c, r }; }
        catch (e: any) {
          if (limited(e) && i < 5) { await new Promise((r) => setTimeout(r, 500 * (i + 1))); continue; }
          stats.tatum_fail++; throw e;
        }
      }
    };
    let res: Promise<{ c: any; r: O }>;
    if (Date.now() < mystenDownUntil) res = viaTatum();
    else {
      const c = mysten.unary(method, input, options);
      [c.headers, c.status, c.trailers].forEach(quiet);
      res = c.response.then((r) => { stats.mysten_ok++; return { c, r }; }, (e: any) => {
        if (!shouldFailover(e)) throw e;
        stats.mysten_fail++; stats.last_failover = Date.now(); stats.last_error = String(e?.code ?? e?.message ?? e).slice(0, 120);
        mystenDownUntil = Date.now() + SKIP_MS;
        return viaTatum();
      });
    }
    quiet(res);
    return new UnaryCall<I, O>(method, options.meta ?? {}, input, quiet(res.then((x) => x.c.headers)), res.then((x) => x.r),
      quiet(res.then((x) => x.c.status)), quiet(res.then((x) => x.c.trailers)));
  }
  serverStreaming(method: any, input: any, options: RpcOptions) { return mysten.serverStreaming(method, input, options); }
  clientStreaming(method: any, options: RpcOptions) { return mysten.clientStreaming(method, options); }
  duplex(method: any, options: RpcOptions) { return mysten.duplex(method, options); }
}

// the client every part of kivy-lend uses
export const grpc = new SuiGrpcClient({ network: 'mainnet', transport: new FailoverTransport() });

// Tatum alone, for the health checks (kivy_watch, the Monday services report)
export async function tatumOnly(): Promise<SuiGrpcClient> {
  const t = await tatum();
  if (!t) throw new Error('no Tatum key');
  return new SuiGrpcClient({ network: 'mainnet', transport: t });
}
