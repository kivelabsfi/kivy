/*
 * routers.ts — swaps: Aftermath router, then Cetus; route checks; checkAppended(); the plain swap card's check + backup.
 * In detail: Aftermath's router first, the Cetus aggregator as backup (routed() runs the whole build again), the route block
 * list, the allowlist check of every route, checkAppended() (what a router or the Aftermath perps API appended must fit Kivy's
 * call rules), and the plain swap card's check + backup (swapCheck / swapAlt).
 * Called by: compose.ts (routeQuote, routeTrade, routeOut, minAfter, floorOut, checkAppended), perps.ts (checkAppended), builder.ts
 * (routed() around every swap build, POST /swapcheck + /swapalt). Imports: core.ts, guard.ts, suilend.ts (slPkg, tests only).
 * State: the Cetus client (_cetus). routerCtx (the router a build is on) is in core.ts, because simulate() reads it too.
 */
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import * as CET from '@cetusprotocol/aggregator-sdk';
// the Cetus SDK carries its own copy of the Sui library: its transactions are passed as bytes, never as objects
import { Transaction as CetusTx } from './node_modules/@cetusprotocol/aggregator-sdk/node_modules/@mysten/sui/dist/transactions/index.mjs';
import { readFileSync, statSync } from 'node:fs';
import { grpc } from './chain.ts';
import { normType } from './txfacts.ts';
import { aggPkgs, allowedPkgs, callWhy, coinArg, revenueFee, routeFeeWallets, routerCtx, simulate, treasury } from './core.ts';
import { isRouteFailure, isSwapAbort, routeError, ruleError } from './guard.ts';
import { slPkg } from './suilend.ts';

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S11] SWAP ROUTERS
// Aftermath router, then Cetus (routed / routeQuote / routeTrade), the route block list, the allowlist check of a route
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── swap then lend, one transaction (2026-10-20) ──
// Aftermath's router appends the swap to our transaction kind (v1/transactions/add-trade, from the coin Kivy
// built) and returns the output coin as a transaction argument; that coin goes straight into the market, so
// the user approves once and nothing is left over. If any step fails, nothing happens.
// Aftermath's router API: 'trade-route' quotes a swap, 'v1/transactions/add-trade' appends it to a transaction kind
async function afRouter(path: string, body: any) {
  const r = await fetch('https://aftermath.finance/api/router/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
                                                                         signal: AbortSignal.timeout(20_000) })   // a hung router moves on to the backups (review 2026-10-26)
    .catch((e: any) => { throw routeError('Aftermath router: ' + String(e?.message ?? e).slice(0, 120)); });
  const t = await r.text();
  let j: any; try { j = JSON.parse(t); } catch { throw routeError('Aftermath router: ' + t.slice(0, 160)); }
  if (!r.ok || typeof j === 'string' || j?.error) throw routeError('Aftermath router: ' + String(j?.error ?? j).slice(0, 200));
  return j;
}

// every route: Kivy's fee, and never through a protocol on the block list (Metastable is winding down — 2026-10-25)
function routeBlock(): string[] {
  try { const v = JSON.parse(readFileSync(process.env.KIVY_JSON_TEST || new URL('../knowledge/kivy.json', import.meta.url), 'utf8')).route_blocklist; return Array.isArray(v) ? v : []; }
  catch { return []; }
}
const withFee = (body: any) => { const f = revenueFee(), bl = routeBlock(); return { ...body, ...(f ? { externalFee: f } : {}), ...(bl.length ? { protocolBlacklist: bl } : {}) }; };

// ── swap routers (2026-10-26; Bluefin 7k dropped 2026-11-04: "we would do only aftermath and cetus") ──
// Aftermath first. When its route fails (no route, or the test run aborts in the route — e.g. 1.3 USDC -> SUI aborted in
// router::assert_acceptable_slippage_on_amount_out while other amounts worked), the WHOLE build runs again with the Cetus
// aggregator: same coins, same amount, same slippage, one approval. Left out of the backups: pools
// priced by Pyth (a paid feed now), Cetus Tide (order-book quotes that expire before the user approves) and blocked
// protocols. A backup route may only call programs on Kivy's allowlist (/opt/kivy/kivy_pkgs.json, the list the server and
// the page enforce): anything else and that router is skipped. Every swap site goes through routeQuote + routeTrade.
const ROUTERS = ['aftermath', 'cetus'] as const;
const ROUTER_NAME: Record<string, string> = { aftermath: 'Aftermath', cetus: 'Cetus' };

// tests only (tests/test_routers.py; command-line runs — the service never has this set): KIVY_ROUTER_FAULT="aftermath:abort,cetus:hang"
// makes a router fail on purpose. Kinds: quote (no route), abort (the route aborts in the test run), hang (no answer), pkgs (the
// route calls an unlisted program). "aftermath:abort@2" fails only the 2nd swap of a build.
function fault(r: string, stage: 'quote' | 'trade'): Promise<never> | null {
  const spec = (process.env.KIVY_ROUTER_FAULT ?? '').split(',').map((x) => x.trim()).find((x) => x.startsWith(r + ':'));
  if (!spec) return null;
  const [kind, nth] = spec.slice(r.length + 1).split('@');
  const ctx = routerCtx.getStore();
  if (nth && Number(nth) !== (ctx?.n ?? 0)) return null;
  const who = ROUTER_NAME[r];
  if (stage === 'quote' && kind === 'quote') return Promise.reject(routeError(r === 'aftermath' ? 'Aftermath router: no route (test fault)' : `${who}: no route (test fault)`));
  if (stage === 'quote' && kind === 'hang') return within(new Promise<never>(() => {}), r === 'aftermath' ? 20_000 : 10_000, r === 'aftermath' ? 'Aftermath router' : who).catch((e: any) => { throw routeError(String(e?.message ?? e)); });
  if (stage === 'trade' && kind === 'abort') return Promise.reject(routeError(`simulation failed: MoveAbort in 41st command, abort code: 5, in '${aggPkgs()[0] ?? '0x0'}::router::assert_acceptable_slippage_on_amount_out' (test fault, ${who})`));
  if (stage === 'trade' && kind === 'pkgs') return Promise.reject(routeError(`${who}: route calls 0xdeadbeef…::router, not on Kivy's list (test fault)`, { unlisted: true }));
  return null;
}
// A failure of the swap route (worth another router), never a balance, amount or lending-protocol error. Decided by
// guard.ts isRouteFailure since 2026-11-15 (review scan B4): a typed route error, or a failed step inside the steps a route
// added (routerCtx.ranges). It was a pattern over the error TEXT, cut at 160 characters by the builders around it — it matched
// today's abort with 7 characters to spare, never a named Move error, and it sent a NAVI `pool` abort to Cetus for nothing.
export async function routed<T>(f: () => Promise<T>, only?: string[]): Promise<T> {
  let first: any = null;
  const tried: string[] = [];
  // tests only: KIVY_ROUTERS=cetus (or a comma list) on a command-line run, or the file state/router-test (same text) for a live
  // test through the page — it counts only for 10 minutes after it was written, then everyone is back on Aftermath first
  let testOrder = process.env.KIVY_ROUTERS ?? '';
  try { const f = process.env.KIVY_ROUTER_TEST_FILE || new URL('./state/router-test', import.meta.url); if (Date.now() - statSync(f).mtimeMs < 600_000) testOrder = readFileSync(f, 'utf8').trim(); } catch { /* no test */ }
  const forced = testOrder.split(',').filter((x) => (ROUTERS as readonly string[]).includes(x));
  const order = only ?? (forced.length ? forced : [...ROUTERS]);
  for (const r of order) {
    if (r !== 'aftermath' && revenueFee()) break;       // a % swap fee is taken through Aftermath only: no backup that would skip it
    const ctx = { router: r, swapped: false };
    try {
      const o: any = await routerCtx.run(ctx, f);
      if (ctx.swapped && o && typeof o === 'object') { o.router = ROUTER_NAME[r]; if (tried.length) console.error(`route: ${tried.join(', ')} failed -> built with ${ROUTER_NAME[r]}`); }
      return o;
    } catch (e: any) {
      const m = String(e?.message ?? e);
      if (!ctx.swapped || !isRouteFailure(e, (ctx as any).ranges ?? [])) throw e;  // not about the route: the same error on every router
      first ??= e; tried.push(ROUTER_NAME[r]);
      console.error(`route ${ROUTER_NAME[r]} failed:`, m.slice(0, 300));
    }
  }
  if (first) first.message = String(first.message) + ` (tried ${tried.join(', ')})`;
  throw first ?? new Error('No swap route');
}

function checkPkgs(kind: Uint8Array, who: string) {             // every route calls only programs the page will sign
  const allow = allowedPkgs();
  const cmds = Transaction.fromKind(kind).getData().commands as any[];
  const unlisted = [...new Set(cmds.filter((c) => c.MoveCall && !allow.has(normalizeSuiAddress(c.MoveCall.package)))
    .map((c) => `${normalizeSuiAddress(c.MoveCall.package)}::${c.MoveCall.module}::${c.MoveCall.function}`))];
  if (unlisted.length) console.error(`route ${who} unlisted programs: ${unlisted.join(' ')}`);   // full ids for vetting (kivy-vet-package), 2026-11-14
  for (const c of cmds) {
    const pk = c.MoveCall?.package;
    if (pk && !allow.has(normalizeSuiAddress(pk)))     // unlisted: the dust sweep drops that coin, every other build tries the next router
      throw routeError(`${who}: route calls ${normalizeSuiAddress(pk).slice(0, 10)}…::${c.MoveCall.module}, not on Kivy's list`,
                       { unlisted: true, rule: `${normalizeSuiAddress(pk).slice(0, 10)}…::${c.MoveCall.module} is not on Kivy's list` });
  }
}

const asArg = (a: any) => a && !a.$kind ? (a.NestedResult ? { $kind: 'NestedResult', ...a } : a.Result != null ? { $kind: 'Result', ...a } : a.Input != null ? { $kind: 'Input', ...a } : a) : a;
const blockedHas = (name: string) => routeBlock().some((b) => String(b).toLowerCase().replace(/[^a-z0-9]/g, '') === name.toLowerCase().replace(/[^a-z0-9_]/g, '').replace(/_/g, ''));
const within = <T>(p: Promise<T>, ms: number, who: string) => Promise.race([p, new Promise<T>((_, no) => setTimeout(() => no(new Error(who + ': no answer in ' + ms / 1000 + ' s')), ms))]);
let _cetus: any = null;
// NAVI, NAVI vault and Cetus get OUR chain client (review scaling study 2, 2026-11-11): left alone they each open their own
// connection to Mysten's public node, outside chain.ts — no failover, no paid key. Calls that only use NAVI's own API
// (getPool, getVaults, getPositions, the deposit/withdraw/claim PTB builders) take no client.
const cetus = () => (_cetus ??= new CET.AggregatorClient({ env: CET.Env.Mainnet, client: grpc as any }));
// a route for `body` ({coinInType, coinOutType, coinInAmount: "123n"}) from this build's router; coinOut.amount = expected out
export async function routeQuote(body: any): Promise<any> {
  const ctx = routerCtx.getStore(); if (ctx) { ctx.swapped = true; ctx.n = (ctx.n ?? 0) + 1; (ctx.types ??= new Set()).add(normType(body.coinInType)).add(normType(body.coinOutType)); }
  const r = ctx?.router ?? 'aftermath';
  const f = fault(r, 'quote'); if (f) await f;
  const amt = String(body.coinInAmount).replace('n', '');
  if (r === 'aftermath') {
    const q: any = await afRouter('trade-route', withFee(body));
    // the route must be for the plan's own coins (review scan B5, 2026-11-15: it was used as given — the card's numbers, the
    // minimum and Kivy's floor all come from this answer)
    if (normType(String(q?.coinIn?.type ?? '')) !== normType(body.coinInType) || normType(String(q?.coinOut?.type ?? '')) !== normType(body.coinOutType))
      throw routeError('Aftermath router: its route is for other coins than this plan');
    return q;
  }
  if (r === 'cetus') {
    const skip = [...CET.PYTH_ORACLE_BASED_DEXES, CET.CETUS_TIDE, CET.OBRIC, CET.SEVENK, CET.STEAMM_OMM, CET.STEAMM_OMM_V2, CET.METASTABLE];
    const providers = CET.getProvidersExcluding(skip).filter((p: string) => !blockedHas(p));
    let q: any;
    try { q = await within(cetus().findRouters({ from: body.coinInType, target: body.coinOutType, amount: amt, byAmountIn: true, providers }), 10_000, 'Cetus'); }
    catch (e: any) { throw routeError('Cetus: ' + String(e?.message ?? e).slice(0, 160)); }
    if (!q || q.error || q.insufficientLiquidity || !(BigInt(q.amountOut?.toString() ?? '0') > 0n)) throw routeError('Cetus: no route' + (q?.error ? ' (' + JSON.stringify(q.error).slice(0, 80) + ')' : ''));
    return { coinOut: { amount: q.amountOut.toString() + 'n' }, __router: 'cetus', __q: q };
  }
  throw new Error('Unknown swap router: ' + r);
}
// append the swap of `body.completeRoute` to body.txKind (base64), from body.coinInId; returns {txKind (base64), coinOutId}
// small pieces every swap build shares (review code-waste CW-14, 2026-11-03): one copy each, same arithmetic as before
export const routeOut = (route: any): bigint => BigInt(String(route.coinOut?.amount ?? '0').replace('n', ''));   // the route's expected output, base units
export const minAfter = (exp: bigint, slip: number): bigint => exp * BigInt(Math.floor((1 - slip) * 1_000_000)) / 1_000_000n;   // the least it may deliver

// ── Kivy's own floor under a swap leg (review scan B5, 2026-11-15) ──
// The minimum a swap must deliver sat only in arguments the ROUTER wrote into its answer; Kivy never read it. Now every swap
// leg is followed by two framework steps of Kivy's own: split `min` off the output coin and merge it back. The split aborts
// when the coin holds less, so what the card promised (the route's quote less Kivy's slippage, minAfter) is enforced by the
// chain whatever the router encoded. The two steps count as the route's (routerCtx.ranges): falling short is a route failure.
// tests only (KIVY_ROUTER_FAULT="aftermath:floor", command line): the floor is set far above the quote, so the build must fail
// at Kivy's floor step and move to the next router (tests/test_failclosed_20261115.py).
export function floorOut(tx: Transaction, coin: any, min: bigint): void {
  const ctx = routerCtx.getStore();
  if ((process.env.KIVY_ROUTER_FAULT ?? '').split(',').some((x) => x.trim() === `${ctx?.router ?? 'aftermath'}:floor`)) min = min * 1000n + 1_000_000n;
  if (min <= 0n) return;
  const at = (tx.getData().commands as any[]).length;
  const [piece] = tx.splitCoins(coin, [tx.pure.u64(min)]);
  tx.mergeCoins(coin, [piece]);
  if (ctx) (ctx.ranges ??= []).push([at, at + 2]);
}
// the output coin a router names must be made by the steps it added — never an earlier coin of the user's (B5)
function outIndex(a: any): number | null {
  try { const i = a?.NestedResult ? a.NestedResult[0] : a?.Result; return typeof i === 'number' && Number.isInteger(i) ? i : null; } catch { return null; }
}
function ownOutput(before: Uint8Array, after: Uint8Array, coinOut: any, who: string): void {
  const a = (Transaction.fromKind(before).getData().commands as any[]).length, b = (Transaction.fromKind(after).getData().commands as any[]).length;
  const i = outIndex(coinOut);
  if (i == null || i < a || i >= b) throw routeError(`${who}: its output coin is not a result of its own steps`, { unlisted: true });
  const ctx = routerCtx.getStore(); if (ctx) (ctx.ranges ??= []).push([a, b]);
}

export async function routeTrade(body: any): Promise<{ txKind: string; coinOutId: any }> {
  const q = body.completeRoute;
  const f = fault(q?.__router ?? 'aftermath', 'trade'); if (f) await f;
  if (!q?.__router) {
    // Aftermath's reply is checked like the backups': a route through a program not on Kivy's list goes to the next router
    // (Cetus) instead of reaching the page, whose check 1 refuses it (review H1, 2026-10-29 — it was never checked)
    const r: any = await afRouter('v1/transactions/add-trade', body);
    if (r?.txKind) {
      r.txKind = await tamper('aftermath', String(r.txKind));          // tests only (KIVY_ROUTER_FAULT=aftermath:tamper-…); the service never has it
      checkPkgs(Buffer.from(String(r.txKind), 'base64'), 'Aftermath router');
      checkAppended(Buffer.from(String(body.txKind), 'base64'), Buffer.from(String(r.txKind), 'base64'), 'Aftermath router', body.walletAddress, ['swap'], [body.coinInId]);   // 2026-11-13
      if (r.coinOutId) ownOutput(Buffer.from(String(body.txKind), 'base64'), Buffer.from(String(r.txKind), 'base64'), r.coinOutId, 'Aftermath router');   // 2026-11-15
    }
    return r;
  }
  const kind = Buffer.from(String(body.txKind), 'base64');
  if (q.__router !== 'cetus') throw new Error('Unknown swap router: ' + q.__router);
  let out: any, bytes: Uint8Array;
  try {
    const t = CetusTx.fromKind(kind); t.setSender(body.walletAddress);
    out = await within(cetus().routerSwap({ router: q.__q, txb: t as any, inputCoin: asArg(body.coinInId), slippage: Number(body.slippage) }), 15_000, 'Cetus');
    bytes = await t.build({ client: cetus().client, onlyTransactionKind: true } as any);
  } catch (e: any) { throw routeError('Cetus: ' + String(e?.message ?? e).slice(0, 200)); }
  bytes = Buffer.from(await tamper('cetus', Buffer.from(bytes).toString('base64')), 'base64');   // tests only (KIVY_ROUTER_FAULT=cetus:tamper-…)
  checkPkgs(bytes, 'Cetus');
  checkAppended(kind, bytes, 'Cetus', body.walletAddress, ['swap'], [body.coinInId]);   // 2026-11-13
  ownOutput(kind, bytes, out, 'Cetus');                                                // 2026-11-15
  return { txKind: Buffer.from(bytes).toString('base64'), coinOutId: out };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S12] CALL RULES FOR APPENDED STEPS
// checkAppended(): what a router or the perps API added must fit the plan; tamper() (tests only)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// an argument's identity, with or without the SDK's $kind
const argKey = (a: any) => a?.NestedResult ? `N${a.NestedResult[0]},${a.NestedResult[1]}` : a?.Result != null ? `R${a.Result}` : a?.Input != null ? `I${a.Input}` : a?.GasCoin ? 'G' : '?';
// every argument a command uses (Input / Result / NestedResult / GasCoin), wherever it sits in the command
function argsOf(v: any, out: any[] = []): any[] {
  if (Array.isArray(v)) { v.forEach((x) => argsOf(x, out)); return out; }
  if (v && typeof v === 'object') {
    if ('Input' in v || 'Result' in v || 'NestedResult' in v || 'GasCoin' in v) out.push(v);
    else Object.values(v).forEach((x) => argsOf(x, out));
  }
  return out;
}
const inKind = (inp: any) => inp?.$kind ?? Object.keys(inp ?? {}).find((k) => k !== '$kind');
const objKind = (inp: any) => inp?.Object ? (inp.Object.$kind ?? Object.keys(inp.Object).find((k) => k !== '$kind')) : null;
// What a router or the perps API returned (`after`), checked against the kind Kivy sent it (`before`; null = it wrote all of it):
//  · Kivy's own inputs and steps come back unchanged, as the start (nothing of ours rewritten or dropped);
//  · every new call is allowlisted and fits the call rules for `steps`; no 0x2::transfer / party call, no publish or upgrade;
//  · nothing new takes from the wallet: no new owned object, Receiving or account-balance withdrawal, no gas coin;
//  · new steps use only the coin Kivy handed over (`coinIn`), shared objects, plain values and their own results — never another
//    coin or object of the user's from earlier in the transaction (a lending key, a withdrawn coin);
//  · coins and objects go only to the sender, the route's fee wallet or Kivy's treasury.
// Throws {unlisted: true}: routed() then tries the next router, a dust sweep drops that coin; a perps build stops.
export function checkAppended(before: Uint8Array | null, after: Uint8Array, who: string, addr: string, steps: string[], coinIn: any[] = []) {
  // a router's reply (steps = swap): a route failure, routed() tries the next router; the perps API's reply: the build stops
  const refuse = (why: string): never => { throw ruleError(`${who}: its steps break Kivy's call rules (${why})`, why, { unlisted: true, ...(steps.includes('swap') ? { route: true } : {}) }); };
  const A: any = before ? Transaction.fromKind(before).getData() : { inputs: [], commands: [] };
  const B: any = Transaction.fromKind(after).getData();
  if (B.inputs.length < A.inputs.length || B.commands.length < A.commands.length) refuse('Kivy\'s own steps are missing');
  A.inputs.forEach((x: any, i: number) => { if (JSON.stringify(x) !== JSON.stringify(B.inputs[i])) refuse(`Kivy's input ${i} was changed`); });
  A.commands.forEach((x: any, i: number) => { if (JSON.stringify(x) !== JSON.stringify(B.commands[i])) refuse(`Kivy's step ${i} was changed`); });
  const plain = (inp: any) => inKind(inp) === 'Pure' || objKind(inp) === 'SharedObject';
  for (let i = A.inputs.length; i < B.inputs.length; i++)
    if (!plain(B.inputs[i])) refuse(`it adds a ${objKind(B.inputs[i]) ?? inKind(B.inputs[i])} input`);
  const me = normalizeSuiAddress(addr), t = treasury();
  const payees = new Set([me, ...routeFeeWallets(), ...(t ? [t] : [])]);
  const handed = new Set(coinIn.map(argKey));
  const argOk = (a: any) => {
    if (a.GasCoin) return false;                                       // the user's SUI
    if (typeof a.Input === 'number') return a.Input >= A.inputs.length || plain(A.inputs[a.Input]);
    const at = a.NestedResult ? a.NestedResult[0] : a.Result;
    return typeof at !== 'number' || at >= A.commands.length || handed.has(argKey(a));
  };
  const toAddr = (a: any): string | null => {                         // a recipient given as a plain address value
    const inp = typeof a?.Input === 'number' ? B.inputs[a.Input] : null;
    if (inKind(inp) !== 'Pure') return null;
    const b = Buffer.from(String(inp.Pure.bytes), 'base64');
    return b.length === 32 ? normalizeSuiAddress('0x' + b.toString('hex')) : null;
  };
  const sendsTo = (a: any, what: string) => { const to = toAddr(a); if (!to || !payees.has(to)) refuse(`${what} to ${to ? to.slice(0, 10) + '…' : 'an address Kivy cannot read'}`); };
  const fw2 = normalizeSuiAddress('0x2');
  for (let i = A.commands.length; i < B.commands.length; i++) {
    const c = B.commands[i], k = c.$kind ?? Object.keys(c).find((x) => x !== '$kind');
    if (k === 'Publish' || k === 'Upgrade') refuse('it publishes or upgrades a program');
    if (argsOf(c[k]).some((a) => !argOk(a))) refuse(`step ${i} uses a coin or object of yours it was not handed`);
    if (k === 'TransferObjects') sendsTo(c.TransferObjects.address, 'it transfers');
    if (k !== 'MoveCall') continue;
    const m = c.MoveCall, pk = normalizeSuiAddress(m.package);
    if (!allowedPkgs().has(pk)) refuse(`${pk.slice(0, 10)}…::${m.module} is not on Kivy's list`);
    if (pk === fw2 && (m.module === 'transfer' || m.module === 'party')) refuse(`it calls 0x2::${m.module}::${m.function}`);
    const why = callWhy(pk, m.module, m.function, steps);
    if (why) refuse(why);
    if (pk === fw2 && m.function === 'send_funds') sendsTo(m.arguments?.[1], 'it sends a coin');
  }
}
// tests only (the same switch as fault(): KIVY_ROUTER_FAULT="aftermath:tamper-xfer", command-line runs; the service never has it):
// the router's reply is changed before Kivy checks it, the way a compromised router could change it — xfer (a coin to a
// stranger), bycall (a call into someone else's Suilend account), owned (one of the wallet's objects added as an input), prefix
// (one of Kivy's own values rewritten), gas (the gas coin used). checkAppended must refuse each (tests/test_callrules_builder_20261113.py).
async function tamper(r: string, kindB64: string): Promise<string> {
  const spec = (process.env.KIVY_ROUTER_FAULT ?? '').split(',').map((x) => x.trim()).find((x) => x.startsWith(r + ':tamper-'));
  if (!spec) return kindB64;
  const how = spec.slice(r.length + ':tamper-'.length);
  let tx = Transaction.fromKind(kindB64);
  if (how === 'prefix') {
    const j = JSON.parse(await tx.toJSON());
    const i = j.inputs.findIndex((x: any) => x.Pure);
    if (i >= 0) j.inputs[i] = { Pure: { bytes: Buffer.alloc(8, 0xff).toString('base64') } };
    tx = Transaction.from(JSON.stringify(j));
  } else if (how === 'xfer') {
    const [c] = tx.moveCall({ target: '0x2::coin::zero', typeArguments: ['0x2::sui::SUI'] });
    tx.transferObjects([c], tx.pure.address('0x' + 'e'.repeat(64)));
  } else if (how === 'bycall') {
    tx.moveCall({ target: `${slPkg()}::lending_market::deposit_ctokens_into_obligation_by_id`, arguments: [tx.pure.u64(1)] });
  } else if (how === 'owned') {
    tx.moveCall({ target: '0x2::coin::value', typeArguments: ['0x2::sui::SUI'],
      arguments: [tx.objectRef({ objectId: '0x' + '1'.repeat(64), version: '1', digest: '11111111111111111111111111111111' })] });
  } else if (how === 'gas') {
    tx.splitCoins(tx.gas, [tx.pure.u64(1)]);
  }
  return Buffer.from(await tx.build({ client: grpc as any, onlyTransactionKind: true })).toString('base64');
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S13] PLAIN SWAP CARD
// the plain swap card's check + backup (POST /swapcheck, /swapalt)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── the plain swap card's backup (2026-10-26) ──
// The server builds a plain swap with Aftermath's trade endpoint and never ran it. swapCheck runs that transaction for the
// wallet first; when the route fails, swapAlt builds the same swap (same coins, amount, slippage) with Cetus.
export async function swapCheck(addr: string, kindB64: string) {
  const tx = Transaction.fromKind(Buffer.from(kindB64, 'base64')); tx.setSender(addr);
  try { await simulate(tx); return { ok: true, gas: (tx as any).__gas ?? null }; }   // its real gas: the swap card's fee line (fix package 4)
  catch (e: any) {
    const m = String(e?.message ?? e);
    const swapAbort = isSwapAbort(e);               // a Move abort outside the Sui framework, read from the error object (2026-11-15; was a text pattern)
    if (swapAbort) console.error('swap card route failed:', m.slice(0, 300));
    return { ok: false, route_fail: swapAbort, error: m.slice(0, 300), ...(e?.rule ? { rule: String(e.rule) } : {}) };   // a rule refusal: builder.ts's line
  }
}
export async function swapAlt(addr: string, fromType: string, toType: string, base: bigint, slip: number, minOut = 0n) {
  return routed(async () => {
    const tx = new Transaction(); tx.setSender(addr);
    tx.add(coinWithBalance({ type: fromType, balance: base }));
    const resolved = JSON.parse(await tx.toJSON({ client: grpc as any }));
    const kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
    const route = await routeQuote({ coinInType: fromType, coinOutType: toType, coinInAmount: base.toString() + 'n' });
    const exp0 = routeOut(route);
    if (minOut > 0n) {                                   // the minimum the user approved is what the CHAIN enforces (audit nv #5):
      if (exp0 * 9999n / 10000n < minOut) throw routeError(`${ROUTER_NAME[routerCtx.getStore()?.router ?? 'cetus']}: price moved — this route gives less than the minimum you approved`);
      slip = Math.min(slip, Math.max(0.0001, 1 - (Number(minOut) + 2) / Number(exp0)));   // tighter, never looser; +2 units so rounding never lands under
    }
    const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route, slippage: slip, walletAddress: addr, coinInId: coinArg(resolved) });
    const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
    floorOut(t2, r.coinOutId, minOut > 0n ? minOut : minAfter(exp0, slip));   // Kivy's own floor: the minimum the user approved (2026-11-15)
    t2.transferObjects([r.coinOutId as any], addr);
    await simulate(t2);
    const exp = routeOut(route);
    return { ok: true, tx_kind: Buffer.from(await t2.build({ client: grpc as any, onlyTransactionKind: true })).toString('base64'),
             expected_base: exp.toString(), min_base: minAfter(exp, slip).toString() };
  }, ['cetus']);
}
