/*
 * core.ts — shared by every builder module: constants, markets, units, kivy.json settings, allowlist + call rules, simulate().
 * In detail: token constants, the lending markets list, unit helpers, Kivy's settings from knowledge/kivy.json (coins, slippage
 * tiers, swap + lend fees), the allowlist + call rules (kivy_pkgs.json), one-lookup-per-request reads, and simulate() — the dry
 * run every build ends with (gas budget, wallet SUI check, expiry, route-only check).
 * Called by: every other builder module and builder.ts. Imports guard.ts only (plain rules, no state). Who imports whom (one way, no cycles):
 *   core -> guard     suilend.ts, navi.ts -> core     lending.ts -> core, suilend, navi     routers.ts -> core, guard, suilend (slPkg, tests only)
 *   perps.ts -> core, routers       compose.ts -> core, lending, routers, perps       builder.ts -> all of them
 * State kept here: the allowlist cache (_pkgs, 30 s), the epoch (10 min), coin metadata, and two per-request contexts:
 * readCtx (shared reads, sharedRead()) and routerCtx (the router a build is on: routers.ts sets it, simulate() reads it).
 * Links: chain.ts (grpc), txfacts.ts (normType), ../knowledge/kivy.json, kivy_pkgs.json (a release's site/ copy, else /opt/kivy).
 */
import { Transaction } from '@mysten/sui/transactions';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import { LENDING_MARKET_ID, LENDING_MARKET_TYPE } from '@suilend/sdk';
import { readFileSync } from 'node:fs';
import { AsyncLocalStorage } from 'node:async_hooks';
// the chain connection: one provider list with failover (chain.ts: Chainstack -> Triton One -> Mysten -> Tatum, 2026-11-12)
import { grpc } from './chain.ts';
// check 2's facts and the coin-type normaliser live in txfacts.ts: the chain gateway (gateway.ts) serves the same /tx-facts
import { normType } from './txfacts.ts';
// the builder's own fail-closed rules (fix package 3, 2026-11-15): typed failures, the perps event table, the object rule
import { objectProblem, perpsEventProblem, type PerpsTable, routeError, ruleError } from './guard.ts';

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S1] SETUP
// the release id, the allowlist path, token constants, the lending markets Kivy supports, unit helpers, the Coin type
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// The release this builder runs from (tools/release.py writes ../RELEASE into a release folder; 'none' outside one). Served on
// /health; the site compares it with its own (architecture programme 2026-11-14). Inside a release the allowlist is the
// release's own site/kivy_pkgs.json; outside one (today's in-place builder) it is the live /opt/kivy/kivy_pkgs.json.
export const RELEASE_ID: string = (() => { try { return String(JSON.parse(readFileSync(new URL('../RELEASE', import.meta.url), 'utf8')).id || 'none'); } catch { return 'none'; } })();
export const PKGS_LIVE: string | URL = RELEASE_ID !== 'none' ? new URL('../site/kivy_pkgs.json', import.meta.url) : '/opt/kivy/kivy_pkgs.json';

export const USDC = '0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC';
export const DEC = 6;

export type Market = { key: string; name: string; kind: 'lending' | 'vault'; protocol: 'suilend' | 'navi'; id?: string; type?: string; vaultName?: string };
export const MARKETS: Market[] = [
  { key: 'suilend', name: 'Suilend Main Market', kind: 'lending', protocol: 'suilend', id: LENDING_MARKET_ID, type: LENDING_MARKET_TYPE },
  { key: 'suilend-ember', name: 'Suilend Ember', kind: 'lending', protocol: 'suilend',
    id: '0x74d92cb095b618f344486f77bcce87fbffc0fe0b46584bb1828bb291a222dda8',
    type: '0x423f05ea163ae1bc5c03d75ef70d3e5d887455084118e46c4f76878530a03619::ember_market::EMBER_MARKET' },
  { key: 'suilend-matrixdock', name: 'Suilend Matrixdock', kind: 'lending', protocol: 'suilend',
    id: '0x8a8d8e138a28de1c637d0b0955e621b017da7010de388db5a18493eca99c5e82',
    type: '0xd382f7d6e7596585b3063d5f4607de30a7a81adeb521d6b862b4c57defca746e::Matrixdock_gold::MATRIXDOCK_GOLD' },
  { key: 'navi', name: 'NAVI', kind: 'lending', protocol: 'navi' },
  { key: 'navi-highyield', name: 'NAVI High Yield', kind: 'vault', protocol: 'navi', vaultName: 'NAVI USDC High Yield' },
  { key: 'navi-prime', name: 'NAVI Prime', kind: 'vault', protocol: 'navi', vaultName: 'NAVI USDC Prime' },
];
export const SUI_VAULT_MARKETS: Market[] = [
  { key: 'navi-sui-yield', name: 'NAVI SUI High Yield', kind: 'vault', protocol: 'navi', vaultName: 'NAVI SUI High Yield' },
  { key: 'navi-sui-prime', name: 'NAVI SUI Prime', kind: 'vault', protocol: 'navi', vaultName: 'NAVI SUI Prime' },
];
export const byKey = (k: string) => { const m = [...MARKETS, ...SUI_VAULT_MARKETS].find((x) => x.key === k); if (!m) throw new Error('unknown market ' + k); return m; };

export const toBase = (x: number) => BigInt(Math.round(x * 10 ** DEC));
// an amount the user typed ('0.29', 0.29, '1e-7') -> exact base units, rounded DOWN, with decimal-string maths: never float
// flooring (Math.floor(0.29 * 1e8) = 28,999,999 — a reviewer L4, 2026-10-29). Same rule as server.py base_units().
export function toUnits(amount: any, dec: number): bigint {
  let t = String(amount ?? '').trim();
  if (/e/i.test(t)) t = Number(t).toFixed(Math.min(100, dec + 2));
  const m = /^(\d*)(?:\.(\d*))?$/.exec(t);
  if (!m || !(m[1] || m[2])) throw new Error('Not an amount: ' + String(amount).slice(0, 20));
  return BigInt((m[1] || '0') + (m[2] ?? '').padEnd(dec, '0').slice(0, dec));
}
// base units -> the exact decimal text ('29000000', 8 -> '0.29')
export function unitsText(u: bigint, dec: number): string {
  if (!dec) return u.toString();
  const t = u.toString().padStart(dec + 1, '0');
  return (t.slice(0, -dec) + '.' + t.slice(-dec)).replace(/\.?0+$/, '');
}
export const fromBase = (x: any) => Number(x) / 10 ** DEC;

export type Coin = { type: string; dec: number; sym: string };
// one lend / withdraw step of build() (lending.ts), handed to a protocol adapter (suilendLend, naviLend, vaultLend): it adds its
// calls to tx and returns the amount (an 'all' withdrawal returns what was lent)
export type LendStep = { addr: string; m: Market; C: Coin; tx: Transaction; tb: (x: number) => bigint; fb: (x: any) => number;
  give: (c: any) => void; amount: number; all: boolean; action: string; keep?: any[] };
export const SUI_T = normType('0x2::sui::SUI');

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S2] KIVY.JSON SETTINGS
// coins, slippage tiers, coin versions, Kivy's swap + lend fees (knowledge/kivy.json)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
const KIVY_JSON: any = JSON.parse(readFileSync(new URL('../knowledge/kivy.json', import.meta.url), 'utf8'));
export const KIVY_COINS: Record<string, any> = KIVY_JSON.coins ?? {};
const SWAP_SLIP = Math.min(0.01, Number(KIVY_JSON.slippage_cap_pct ?? 0.5) / 100);   // Kivy's ceiling (0.5%), same as plain swaps
// per pair, as low as it safely goes (2026-10-24): the tightest tier holding BOTH coins — stablecoins 0.05%, SUI and its
// staked versions 0.1%, big coins 0.2% — else the ceiling. Same rule as slip_for() in server.py (knowledge/kivy.json slippage_tiers).
export function slipFor(a: string, b: string): number {
  const tiers: any = KIVY_JSON.slippage_tiers ?? {};
  const A = String(a).toLowerCase(), B = String(b).toLowerCase();
  let best = SWAP_SLIP;
  for (const [name, t] of Object.entries(tiers) as any) {
    if (name.startsWith('_') || !t || !Array.isArray(t.coins)) continue;
    const cs = new Set(t.coins.map((c: string) => c.toLowerCase()));
    if (cs.has(A) && cs.has(B)) best = Math.min(best, Number(t.pct) / 100);
  }
  return best;
}
// Other versions of a coin, shown under it (2026-10-19): SUI's liquid-staking tokens, BTC's wrapped / staked kinds
const VARIANTS: Record<string, string[]> = { SUI: ['vSUI', 'haSUI', 'sSUI'], BTC: ['LBTC', 'wBTC', 'WBTC', 'suiWBTC'],
                                             USDC: ['USDsui', 'USDSUI'] };   // USDC row group = the 'Stable' filter
export const VARIANT_OF: Record<string, string> = Object.fromEntries(Object.entries(VARIANTS).flatMap(([a, ss]) => ss.map((x) => [x, a])));

// Kivy's own swap fee (knowledge/kivy.json 'revenue', read fresh each time so turning it on needs no restart).
// Off during testing (2026-10-20). Aftermath's router takes it out of the swap and pays the address.
export function revenueFee(): { recipient: string; feePercentage: number } | null {
  try {
    const r: any = JSON.parse(readFileSync(process.env.KIVY_JSON_TEST || new URL('../knowledge/kivy.json', import.meta.url), 'utf8')).revenue ?? {};
    const pct = Number(r.swap_fee_pct);
    if (r.enabled === true && /^0x[0-9a-fA-F]{64}$/.test(String(r.address)) && pct > 0 && pct <= 1)
      return { recipient: String(r.address).toLowerCase(), feePercentage: pct / 100 };
  } catch { /* no file, bad json: no fee */ }
  return null;
}

// the lending-deposit fee (same switch + address): a fraction of the deposit coin, paid to the address, before it goes in
export function lendFee(): { recipient: string; frac: number } | null {
  const f = revenueFee(); if (!f) return null;
  try {
    const p = Number(JSON.parse(readFileSync(process.env.KIVY_JSON_TEST || new URL('../knowledge/kivy.json', import.meta.url), 'utf8')).revenue?.lend_fee_pct);
    return p > 0 && p <= 1 ? { recipient: f.recipient, frac: p / 100 } : null;
  } catch { return null; }
}
export function takeLendFee(tx: Transaction, coin: any, base: bigint) {   // base = what the coin holds (or at least holds)
  const f = lendFee(); if (!f) return 0n;
  const fee = BigInt(Math.floor(Number(base) * f.frac));
  if (fee <= 0n) return 0n;
  const [cut] = tx.splitCoins(coin, [fee]);
  tx.transferObjects([cut], f.recipient);
  return fee;
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S3] ALLOWLIST + CALL RULES
// kivy_pkgs.json packages, call rules and shared values (re-read every 30 s), the treasury address
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
let _pkgs = { at: 0, set: new Set<string>(), calls: {} as any };
// Kivy's package allowlist and call rules (kivy_pkgs.json "packages" + "calls", re-read every 30 s): the same file server.py and
// the page enforce. No readable rules = {}: every appended call outside the framework list is then refused (fails closed).
export function allowedPkgs(): Set<string> {
  if (Date.now() - _pkgs.at > 30_000) {
    try {
      const d = JSON.parse(readFileSync(process.env.KIVY_PKGS_TEST || PKGS_LIVE, 'utf8'));   // KIVY_PKGS_TEST: tests only
      const calls = d.calls && typeof d.calls === 'object' && d.calls.steps && typeof d.calls.steps === 'object' ? d.calls : {};
      _pkgs = { at: Date.now(), set: new Set((d.packages as string[]).map((x) => normalizeSuiAddress(x))), calls };
    } catch { _pkgs = { at: Date.now(), set: new Set(), calls: {} }; }
  }
  return _pkgs.set;
}

// ── call rules (the owner "Go" 2026-11-13, after a defensive review found every check looked only at WHICH PACKAGES a transaction
// calls): each kind of plan step may call only the functions it needs — kivy_pkgs.json "calls" (format in its _about), the same
// table check 1 (callWhy in index.html) and server.py (call_refusals) enforce. Here it judges the part of a transaction Kivy did
// NOT write: what a swap router (Aftermath's reply, the Cetus SDK) or the Aftermath perps API appends. ──
// why one call (of a listed package) does not fit a plan with these steps; '' = it does
export function callWhy(pkg: string, mod: string, fn: string, steps: string[]): string {
  allowedPkgs();
  const R = _pkgs.calls ?? {}, p = normalizeSuiAddress(pkg);
  const grp = p === normalizeSuiAddress('0x1') || p === normalizeSuiAddress('0x2') ? 'fw'
    : Object.keys(R.groups ?? {}).find((g) => (R.groups[g] ?? []).some((x: string) => normalizeSuiAddress(x) === p)) ?? '';   // in no group: no group rule fits (default deny, 2026-11-15)
  let never: RegExp | null = null;
  try { never = R.never ? new RegExp(R.never, 'i') : null; } catch { never = new RegExp(''); }   // an unreadable rule refuses every call
  if (never && never.test(fn)) return `${mod}::${fn} is never allowed`;
  const part = (want: string, got: string) => want === '*' || want === got || (want.endsWith('*') && got.startsWith(want.slice(0, -1)));
  const rules: string[] = [...(R.fw ?? []), ...steps.flatMap((s) => R.steps?.[s] ?? [])];
  return rules.some((r) => { const q = String(r).split('::'); return q.length === 3 && (q[0].startsWith('0x') ? normalizeSuiAddress(q[0]) === p : q[0] === grp) && part(q[1], mod) && part(q[2], fn); })
    ? '' : `${mod}::${fn} is not part of this plan`;
}
// Aftermath's swap-route fee wallet (check 1 allows it too) and Kivy's treasury: the only addresses besides the user that an
// appended step may send a coin or object to
// Shared values (router fee wallets, aggregator entry programs, the 'pulls value' pattern): kivy_pkgs.json calls.consts is their
// ONLY copy — check 1 and the server read it too (architecture programme change 5, 2026-11-14). Missing = stricter.
function consts(): any { allowedPkgs(); return (_pkgs.calls as any)?.consts ?? {}; }
export const routeFeeWallets = (): string[] => ((consts().route_fee_wallets ?? []) as string[]).map((x) => normalizeSuiAddress(x));
export const aggPkgs = (): string[] => ((consts().aggregator_entry ?? []) as string[]).map((x) => normalizeSuiAddress(x));
const pullsValue = (): RegExp => { try { const v = consts().pulls_value; return v ? new RegExp(String(v), 'i') : /./; } catch { return /./; } };
export const perpsPkgs = (): string[] => ((consts().perps_programs ?? []) as string[]).map((x) => normalizeSuiAddress(x));   // Aftermath perps
export function treasury(): string | null {
  try {
    const a = String(JSON.parse(readFileSync(process.env.KIVY_JSON_TEST || new URL('../knowledge/kivy.json', import.meta.url), 'utf8')).revenue?.address ?? '');
    return /^0x[0-9a-fA-F]{64}$/.test(a) ? normalizeSuiAddress(a) : null;
  } catch { return null; }
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S4] SHARED READS + SMALL HELPERS
// one chain lookup per request (shared), coin balance / metadata, settle(), the coin Kivy built (coinArg)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// One position read shares its chain lookups (2026-10-24, scale): inside a 'positions' / 'positions-all' request each
// Suilend market's accounts are fetched once and all coin balances come from one call, instead of once per use.
// Builds run outside this and always read fresh.

export const readCtx = new AsyncLocalStorage<Map<string, Promise<any>>>();
// inside a sharedRead() request: run each keyed lookup once and hand every caller the same promise
export function shared<T>(key: string, f: () => Promise<T>): Promise<T> {
  const ctx = readCtx.getStore();
  if (!ctx) return f();
  if (!ctx.has(key)) ctx.set(key, f());
  return ctx.get(key)!;
}
export const sharedRead = <T>(f: () => Promise<T>) => readCtx.run(new Map(), f);
// the wallet's coin balances in one call (cTokens included), only inside a shared read; otherwise a single fresh balance
export async function coinBalance(addr: string, type: string): Promise<number> {
  if (!readCtx.getStore()) {
    const bal: any = await (grpc as any).core.getBalance({ owner: addr, coinType: type });
    return Number(bal?.balance?.balance ?? bal?.balance?.totalBalance ?? bal?.totalBalance ?? 0);
  }
  const all = await shared('bals:' + addr, async () => {
    const m = new Map<string, number>(); let cursor: string | null = null;
    for (let i = 0; i < 10; i++) {
      const r: any = await (grpc as any).core.listBalances({ owner: addr, limit: 50, cursor });
      for (const b of r.balances) m.set(normType(b.coinType), Number(b.balance ?? 0));
      if (!r.hasNextPage) break; cursor = r.cursor;
    }
    return m;
  });
  return all.get(normType(type)) ?? 0;
}

const _meta = new Map<string, Promise<{ symbol: string; decimals: number }>>();
// a coin's symbol + decimals from Sui GraphQL (kept for the life of the process)
export function coinMeta(t: string) {
  if (!_meta.has(t)) _meta.set(t, (async () => {
    const q = { query: 'query($t: String!) { coinMetadata(coinType: $t) { symbol decimals } }', variables: { t } };   // a variable, not pasted in (2026-11-13)
    const r: any = await (await fetch('https://graphql.mainnet.sui.io/graphql', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(q), signal: AbortSignal.timeout(20_000) })).json();
    const m = r?.data?.coinMetadata ?? {};
    return { symbol: m.symbol ?? t.split('::').pop(), decimals: Number(m.decimals ?? 9) };
  })());
  return _meta.get(t)!;
}

export type Reward = { market: string; coinType: string; symbol: string; amount: number; usd: number; raw: any };

export const webIcon = (u: any) => (typeof u === 'string' && /^https:\/\//.test(u) && u.length < 400 ? u : null);   // no inline data: icons

// run f; an error becomes {error} so one market failing never fails a whole list
export const settle = async (f: () => Promise<any>) => { try { return await f(); } catch (e: any) { if (process.env.KIVY_DEBUG) console.error("settle:", e?.message ?? e); return { error: String(e?.message ?? e).slice(0, 200) }; } };

export function lastSplit(resolved: any): number {                          // our split = the last SplitCoins after intents resolve (-1: none)
  let at = -1;
  resolved.commands.forEach((c: any, i: number) => { if (c.SplitCoins) at = i; });
  return at;
}

// The USDC coin Kivy built: coinWithBalance resolves to a SplitCoins (from coins, or from the address
// balance via redeem_funds; send_funds may follow to return change), so the coin is that split's output.
export function coinArg(resolved: any) {
  const i = lastSplit(resolved);
  return i >= 0 ? { NestedResult: [i, 0] } : { Result: resolved.commands.length - 1 };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S5] SIMULATE
// the current epoch, simulate() (dry run + gas budget + expiry) and the route-only check it runs on every build
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// The current Sui epoch (24 h each on mainnet), cached 10 minutes; null when it cannot be read and no fresh value is cached.
const EPOCH = { n: null as number | null, at: 0 };
async function currentEpoch(): Promise<number | null> {
  if (EPOCH.n != null && Date.now() - EPOCH.at < 600_000) return EPOCH.n;
  try {
    const r: any = await (grpc as any).core.getCurrentSystemState();
    const n = Number(r?.systemState?.epoch);
    if (Number.isFinite(n) && n > 0) { EPOCH.n = n; EPOCH.at = Date.now(); }
  } catch (e: any) {
    console.error('kivy-lend: epoch read failed:', String(e?.message ?? e).slice(0, 120));
  }
  return EPOCH.n != null && Date.now() - EPOCH.at < 12 * 3600_000 ? EPOCH.n : null;   // a value older than half an epoch could already be stale
}
// dry-runs a built transaction on Sui (chain.ts gRPC) and throws if it would fail; then sets the gas budget and checks the
// wallet has the SUI for it, and bounds how long the signed bytes stay valid. Every builder calls this before handing a
// transaction back. Returns the size in bytes.
export async function simulate(tx: Transaction) {
  const bytes = await tx.build({ client: grpc as any });
  const d0: any = (tx as any).getData?.();
  const pp = perpsPkgs();
  const perps = (d0?.commands ?? []).some((c: any) => c.MoveCall && pp.includes(normalizeSuiAddress(c.MoveCall.package)));
  // effects must be asked for: without them Sui returns no status and no gas numbers (2026-10-20: the check and the
  // gas margin silently did nothing, and two NAVI moves ran out of gas). Object types: the object rule; events: the perps table.
  const sim: any = await (grpc as any).core.simulateTransaction({ transaction: bytes, include: { effects: true, balanceChanges: true, objectTypes: true, ...(perps ? { events: true } : {}) } });
  const t = sim.Transaction ?? sim.FailedTransaction ?? sim.transaction ?? sim;
  const st = t?.effects?.status ?? t?.status;
  if (!st) throw new Error('simulation returned no result');
  // the failed step travels with the error (guard.ts execErr): routed() reads it, not the text (review scan B4, 2026-11-15)
  if (st.success === false) throw Object.assign(new Error('simulation failed: ' + JSON.stringify(st.error ?? st, (_k, v) => typeof v === 'bigint' ? v.toString() : v).slice(0, 300)),
                                                  st.error && typeof st.error === 'object' ? { executionError: st.error } : {});
  routeOnlyCheck(t, (tx as any).getData?.());
  ownRules(tx, t, d0, perps);
  // gas budget: Sui needs the NET cost (computation + storage - refund) to fit. The library's own budget left only
  // ~0.0001 SUI spare, so when the real run cost a bit more than the dry run (route / price moved) it ran out of gas
  // (the owner's NAVI/Suilend moves, 2026-10-20). Now: 2x the net estimate + 0.02 SUI. Only the real cost is charged.
  const g = t?.effects?.gasUsed;
  if (g) {
    const comp = Number(g.computationCost ?? 0);
    const net = Math.max(comp + Number(g.storageCost ?? 0) - Number(g.storageRebate ?? 0), comp);
    (tx as any).__gas = { comp, storage: Number(g.storageCost ?? 0), rebate: Number(g.storageRebate ?? 0) };   // the real cost, for the dust sweep's sum
    const budget = BigInt(Math.ceil(net * 2)) + 20_000_000n;
    tx.setGasBudget(budget);
    const sender = (tx as any).getData?.().sender;
    if (sender) {
      const bal: any = await (grpc as any).core.getBalance({ owner: sender, coinType: '0x2::sui::SUI' }).catch(() => null);
      const have = BigInt(bal?.balance?.balance ?? bal?.balance?.totalBalance ?? bal?.totalBalance ?? 0);
      // SUI the transaction itself takes from the wallet (e.g. lending SUI), beyond the fee
      const me = normType(sender + '::a::b'), suiT = normType('0x2::sui::SUI');
      const suiOut = (t?.balanceChanges ?? []).filter((c: any) => normType(c.address + '::a::b') === me && normType(c.coinType) === suiT)
        .reduce((x: number, c: any) => x - Number(c.amount), 0) - net;
      const need = budget + BigInt(Math.max(0, Math.ceil(suiOut)));
      if (have && have < need)
        throw new Error(`This needs about ${(Number(need) / 1e9).toFixed(3)} SUI in the wallet (incl. about ${(Number(budget) / 1e9).toFixed(3)} SUI set aside for the network fee — only the real fee is charged); your wallet has ${(Number(have) / 1e9).toFixed(3)} SUI. Use a little less SUI and try again.`);
    }
  }
  // Expiry (security gap 2026-11-08 #9): a transaction paying gas from SUI coins had none, so bytes a wallet signed but never
  // sent stayed valid for ever. Now it is void after the NEXT epoch ends (24-48 h). Address-balance gas (no payment coins)
  // already carries the library's ValidDuring window and is left alone; the page keeps the bound (classicGas carries it over).
  const d: any = (tx as any).getData?.();
  const ex = d?.expiration;
  if ((d?.gasData?.payment?.length ?? 0) > 0 && (!ex || ex.None || ex.$kind === 'None')) {
    const ep = await currentEpoch();
    if (ep != null) tx.setExpiration({ Epoch: ep + 1 });
    else console.error('kivy-lend: no epoch, transaction built without an expiry');
  }
  return bytes.length;
}

// types: the coins this build's swaps go from and to; ranges: the steps each swap route (and Kivy's floor under it) added,
// [from, to) — a failed step inside one is that route's failure (guard.ts isRouteFailure)
export const routerCtx = new AsyncLocalStorage<{ router: string; swapped: boolean; n?: number; types?: Set<string>; ranges?: [number, number][] }>();

// ── Kivy's own rules on every simulated build (fix package 3, 2026-11-15; the rules themselves are in guard.ts) ──
// 1. the object rule: nothing of the wallet's that is not a coin leaves it — the perps account key, a lending key (review scan B2);
// 2. a transaction that calls the perps program must carry its event table (setPerps) and fit it: the perps check ran only on
//    perps commands, so a perps step inside a chain was never judged, and a build nobody gave a table is refused (B1).
// A refusal on a perps build prints "kivy perps refused:" (the watchdog's alert line), like perpGuard's own.
export function setPerps(tx: Transaction, table: PerpsTable | null) { if (table) (tx as any).__perps = table; }
export function perpsRefused(why: string, cmd: string): never {
  console.error(`kivy perps refused: ${why} | ${cmd}`);
  throw new Error(`This perps transaction did not match your request, so nothing was prepared (${why}). Kivy has been alerted.`);
}
function ownRules(tx: Transaction, t: any, data: any, perps: boolean) {
  if (!data?.sender) return;                                           // a kind without a sender: nothing is handed out from here
  const table = (tx as any).__perps as PerpsTable | undefined;
  const obj = objectProblem(t, data.sender, perps);
  if (obj && perps) perpsRefused(obj, table?.cmd ?? 'perps step');
  if (obj)   // a rule refusal: builder.ts prints its registered line (was "kivy-lend guard refused:", which alerted nobody — OPS-A1)
    throw ruleError(`This transaction would move something of yours that its plan does not include (${obj}), so nothing was prepared.`, obj);
  if (!perps) return;
  if (!table) perpsRefused('it calls the perps program in a build that has no perps step', 'no perps rule');
  const evs = (t?.events ?? []).map((e: any) => ({ type: String(e.eventType ?? e.type ?? '') }));
  const why = perpsEventProblem(evs, table!, perpsPkgs());
  if (why) perpsRefused(why, table!.cmd);
}

// ── route-only coins (2026-11-13, "Yes" to option A) ──
// Check 1 lets a coin that only the swap route touches go to another address (a pool's fee or burn wallet) only in a PURE swap.
// With a withdraw / redeem / claim / perps-collateral step in the same transaction it refuses that coin, because a hidden
// "withdraw extra, swap it, send it away" looks the same. Aftermath's route sometimes passes through such a coin (dSTBL paid to
// 0x0, 2026-11-13), so those builds were refused at signing. A build that swapped now checks its own simulation the same way
// and, when the route does that, fails as a ROUTE failure: routed() tries the next router (Cetus). Check 1 is not loosened.
function routeOnlyCheck(t: any, data: any) {
  const ctx = routerCtx.getStore();
  if (!ctx?.swapped || !data?.sender) return;                          // no swap in this build, or a kind without a sender
  const fw = [normalizeSuiAddress('0x1'), normalizeSuiAddress('0x2')];
  const calls = (data.commands ?? []).map((c: any) => c.MoveCall).filter(Boolean);
  const agg = aggPkgs(), pulls = pullsValue();
  const pure = calls.some((c: any) => agg.includes(normalizeSuiAddress(c.package))) &&
    !calls.some((c: any) => !fw.includes(normalizeSuiAddress(c.package)) && pulls.test(String(c.function)));
  if (pure) return;                                                    // check 1 allows route-only coins here
  const me = normalizeSuiAddress(data.sender), tr = treasury();
  const payees = new Set([me, ...routeFeeWallets(), ...(tr ? [tr] : [])]);
  const bc: any[] = [...(t?.balanceChanges ?? [])];
  if ((process.env.KIVY_ROUTER_FAULT ?? '').split(',').some((x) => x.trim() === `${ctx.router}:routeonly`))   // tests only (tests/test_routeonly_20261113.py):
    bc.push({ address: '0x0', coinType: '0x' + 'd'.repeat(64) + '::dstbl::DSTBL', amount: '1' });       // the route pays an unplanned coin to 0x0
  const planned = new Set(ctx.types ?? []);                            // the swaps' coins, and every coin the user's own balance moves in
  bc.forEach((c) => { if (normalizeSuiAddress(String(c.address)) === me) planned.add(normType(c.coinType)); });
  const bad = bc.find((c) => BigInt(c.amount) > 0n && !payees.has(normalizeSuiAddress(String(c.address))) && !planned.has(normType(c.coinType)));
  if (bad) { const why = `the route pays ${String(bad.coinType).split('::').pop()} to another address (${normalizeSuiAddress(String(bad.address)).slice(0, 10)}…), a coin this plan never mentions`;
             throw routeError(`${ctx.router === 'cetus' ? 'Cetus: ' : 'Aftermath router: '}${why}`, { rule: why }); }
}
