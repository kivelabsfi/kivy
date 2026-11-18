/*
 * navi.ts — the NAVI adapter: every call into NAVI's lending and vault SDKs (vaults, positions, rewards).
 * In detail: the vault list, the wallet's lending and vault positions, reward reads, then the adapter jobs lending.ts calls for
 * NAVI lending (navi*) and NAVI vaults (vault*, suiVault*) — the same jobs as suilend.ts (table in lending.ts's header).
 * A NAVI SDK upgrade (@naviprotocol/lending, @naviprotocol/vault) should touch this file only.
 * Called by: lending.ts, builder.ts (clears a wallet's cached positions, keeps the vault list warm, restarts when NAVI's own
 * stores are full). Imports: core.ts, warm.ts.
 * State: the 60 s vault list (_vaults, reloaded ahead of expiry — warm.ts), NAVI's config (60 s, _cfg — warm.ts), the 15 s
 * per-wallet position caches (_naviPos, _vaultPos; at most 5,000 wallets each, expired entries dropped), and the count of wallets
 * handed to NAVI's libraries (libWallets).
 * A NAVI withdrawal's price refresh is built here (naviRefresh, fix package 6, 2026-11-17), never with Pyth's own update step:
 * Pyth and Wormhole stay off Kivy's allowlist (the owner's D1); a coin NAVI's oracle then can't price is named and the build refused.
 */
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import * as navi from '@naviprotocol/lending';
import * as vault from '@naviprotocol/vault';
import { grpc } from './chain.ts';
import { normType } from './txfacts.ts';
import { allowedPkgs, type Coin, coinMeta, KIVY_COINS, lendFee, type LendStep, type Market, type Reward, settle, SUI_T, SUI_VAULT_MARKETS, takeLendFee, USDC, VARIANT_OF, webIcon } from './core.ts';
import { type Warm, WARM, warmAhead, warmGet } from './warm.ts';

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S7] NAVI
// the NAVI lending + vault SDKs: vault list (60 s), positions (15 s, capped), rewards; then the adapter lending.ts calls
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── NAVI vaults ──
// disableCache: the library keeps its own 60 s copy, which made a reload hand back the old list (one fixed key: no growth)
const _vaults = new Map<string, Warm<any[]>>();
const loadVaults = () => vault.getVaults({ disableCache: true } as any) as Promise<any[]>;
export function vaults() { return warmGet(_vaults, 'all', loadVaults); }
// the keep-warm tick (builder.ts): reload the list in the background shortly before it expires
export function vaultsAhead() { warmAhead(_vaults, 'all', loadVaults); }

// a NAVI vault market's vault object (from NAVI's vault SDK list, refreshed every 60 s)
export async function vaultOf(m: Market) {
  const v = (await vaults()).find((x: any) => x.name === m.vaultName);
  if (!v) throw new Error(m.name + ' is not listed by NAVI right now');
  return v;
}

// ── positions (USDC) ──
// NAVI's lending library caches results FOREVER unless told otherwise, and this service runs non-stop — so a new
// deposit never showed up (the owner's WBTC on NAVI, 2026-10-20). Positions are read fresh and kept 15 s at most.
export const _naviPos = new Map<string, { at: number; v: Promise<any[]> }>();
export const _vaultPos = new Map<string, { at: number; v: Promise<any[]> }>();
const POS_MAX = 5_000, POS_TTL = 15_000;          // wallets kept per cache (zero-day review 2026-11-13: they grew without a bound)
export function capSet<V extends { at: number }>(m: Map<string, V>, k: string, v: V, now = Date.now()) {
  m.delete(k);                                      // re-inserted at the end: a Map keeps insertion order, so the first key is the oldest
  for (const [x, e] of m) { if (now - e.at < POS_TTL) break; m.delete(x); }   // expired ones go too (fix package 4): oldest first, stop at the first fresh
  while (m.size >= POS_MAX) m.delete(m.keys().next().value as string);
  m.set(k, v);
}
// NAVI's own libraries store one result per wallet for good: their cache writes on every call and disableCache only skips
// the read (@naviprotocol/lending getLendingPositions, also behind updateOraclePriceBeforeUserOperationPTB; @naviprotocol/vault
// getPositions; a reviewer builder scan S1). Nothing outside the library can empty those stores, so the builder counts the wallets
// it hands them; past LIB.wallets (20,000), or a day after the first, builder.ts restarts itself cleanly when no request is running
// (systemd's socket holds new requests meanwhile). Fix package 4, 2026-11-15.
export const LIB = { wallets: 20_000, ageMs: 24 * 3600_000, started: Date.now() };
const libWallets = new Set<string>();
export function libNote(addr: string) {
  if (libWallets.size >= LIB.wallets) return;
  if (!libWallets.size) LIB.started = Date.now();   // the stores' age runs from the first wallet in them
  libWallets.add(String(addr).toLowerCase());
}
export const libraryFull = (now = Date.now()) => libWallets.size >= LIB.wallets || (libWallets.size > 0 && now - LIB.started >= LIB.ageMs);
export const libraryCount = () => libWallets.size;
export function naviPositions(addr: string) {
  const h = _naviPos.get(addr);
  if (h && Date.now() - h.at < POS_TTL) return h.v;
  libNote(addr);
  const v = navi.getLendingPositions(addr, { disableCache: true, client: grpc } as any) as any as Promise<any[]>;
  capSet(_naviPos, addr, { at: Date.now(), v }); v.catch(() => _naviPos.delete(addr));
  return v;
}
export function vaultPositions(addr: string) {
  const h = _vaultPos.get(addr);
  if (h && Date.now() - h.at < POS_TTL) return h.v;
  libNote(addr);
  const v = vault.getPositions(addr, { disableCache: true } as any) as any as Promise<any[]>;
  capSet(_vaultPos, addr, { at: Date.now(), v }); v.catch(() => _vaultPos.delete(addr));
  return v;
}

export async function naviRewards(addr: string): Promise<Reward[]> {
  libNote(addr);
  const rw: any[] = ((await navi.getUserAvailableLendingRewards(addr, { disableCache: true, client: grpc } as any)) as any[]).filter((r) => Number(r.userClaimableReward) > 0);
  return Promise.all(rw.map(async (r) => {
    const t = navi.normalizeCoinType(r.rewardCoinType); const m = await coinMeta(t);
    return { market: 'navi', coinType: t, symbol: m.symbol, amount: Number(r.userClaimableReward), usd: 0, raw: r };
  }));
}
export async function vaultRewards(m: Market, addr: string): Promise<Reward[]> {
  const v: any = await vaultOf(m);
  const rw: any[] = ((await vault.getVaultRewards(v, addr, { client: grpc } as any)) as any[]).filter((r) => BigInt(r.claimable ?? 0) > 0n);
  return Promise.all(rw.map(async (r) => {
    const t = navi.normalizeCoinType(r.rewardCoinType); const md = await coinMeta(t);
    return { market: m.key, coinType: t, symbol: md.symbol, amount: Number(BigInt(r.claimable)) / 10 ** md.decimals, usd: 0, raw: r };
  }));
}

// ── the adapter: what lending.ts asks of NAVI lending (navi*) and NAVI vaults (vault*, suiVault*) — the same jobs as suilend.ts ──
// NAVI lending's USDC yearly % (supply + incentives) — rate() in lending.ts
export async function naviRate() {
  const p: any = await navi.getPool(USDC, { cacheTime: 60_000 } as any);
  const i = p.supplyIncentiveApyInfo || {};
  return Number(i.apy ?? 0) || Number(i.vaultApr ?? 0);
}

// a NAVI vault's yearly % as NAVI's app shows it — rate() in lending.ts
export async function vaultRate(m: Market) {
  const v: any = await vaultOf(m);
  return Number(v.apy?.instant ?? v.apy?.target ?? 0) * 100;   // the rate NAVI's app shows
}

// how much of coin T these NAVI positions supply (E-mode deposits included)
export function supplied(ps: any[], T: string) {
  let usdc = 0;
  for (const p of ps ?? []) {
    const s = p['navi-lending-supply'] ?? p['navi-lending-emode-supply'];   // E-mode deposits count too
    if (s && navi.normalizeCoinType(s.token?.coinType ?? s.pool?.coinType ?? s.pool?.suiCoinType ?? '') === navi.normalizeCoinType(T)) usdc += Number(s.amount ?? 0);
  }
  return usdc;
}

// how much of coin T this wallet has lent on NAVI (E-mode deposits included) — position() in lending.ts
export async function naviPosition(addr: string, T = USDC) {
  return { usdc: supplied(await naviPositions(addr), T) };
}

// what this wallet holds in a NAVI vault (coin amount + shares) — position() in lending.ts
export async function vaultPosition(m: Market, addr: string) {
  const v: any = await vaultOf(m);
  const p: any = (await vaultPositions(addr)).find((x: any) => x.vaultId === v.id);
  return { usdc: Number(p?.tokenBalance ?? 0), shares: p?.shares ?? '0' };
}

// ── NAVI's config, the build's one positions read, and the price refresh before a withdrawal (fix package 6, 2026-11-17) ──
// NAVI's own API answers in well under a second; past NAVI_WAIT a build says so instead of waiting on (the library's reads
// have no deadline of their own: BLD-W1)
export const NAVI = { wait: 15_000 };
export function within<T>(p: Promise<T>, what: string, ms = NAVI.wait): Promise<T> {
  let t: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<T>((_, no) => { t = setTimeout(() => no(new Error(`${what} did not answer in ${ms / 1000} s — try again in a minute.`)), ms); });
  return Promise.race([p, late]).finally(() => clearTimeout(t));
}

// NAVI's config (its oracle's objects and every coin's price feed): one read a minute at most, with a deadline. It is not a
// wallet's positions: disableCache, passed into the library's price helper, reached its config reader too, so every NAVI
// withdrawal build fetched the config 5 to 28 times (BLD-W1/S1). This fills the same library cache entry its deposit and
// withdraw helpers read (no options: one key), so they reuse it.
const _cfg = new Map<string, Warm<any>>();
const loadConfig = () => within((navi as any).getConfig({ cacheTime: WARM.ttl }) as Promise<any>, "NAVI's config");
export const naviConfig = () => warmGet(_cfg, 'main', loadConfig);

// a wallet's positions in one NAVI market, read fresh for a build — the ONE positions read of a withdrawal: its amount and its
// price refresh both come from it (the library's helper read them a second time, BLD-A5). disableCache: the library keeps a
// wallet's positions for good otherwise (2026-11-16, abort 1502 after a new deposit). One market: the withdrawal takes from it.
export async function buildPositions(addr: string, market: string): Promise<any[]> {
  libNote(addr);
  return ((await within(navi.getLendingPositions(addr, { disableCache: true, client: grpc, markets: [market] } as any) as any, "NAVI's positions read")) ?? []) as any[];
}

// a feed's coin name for a person: the position's own symbol, else the coin type's last part
const feedSym = (f: any, ps: any[] = []) => {
  for (const p of ps) { const s = p?.[p?.type]; if (s?.pool?.oracleId === f.oracleId && s?.token?.symbol) return String(s.token.symbol); }
  return String(f.coinType ?? '').split('::').pop() || 'a coin';
};
const names = (xs: string[]) => xs.length < 2 ? xs.join('') : xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1];

// the feeds whose Pyth price on chain is over 30 s old — the library's own test (one chain read), the one that made it add
// Pyth's update step
export async function pythOld(feeds: any[]): Promise<any[]> {
  const fs = feeds.filter((f) => f.pythPriceFeedId && f.pythPriceInfoObject);
  if (!fs.length) return [];
  const ids: string[] = await within((navi as any).getPythStalePriceFeedIdV2(fs.map((f) => ({ priceFeedId: f.pythPriceFeedId, priceInfoObject: f.pythPriceInfoObject, expiration: 30 })),
                                                                           { client: grpc }), "NAVI's price feeds read");
  return fs.filter((f) => ids.includes(f.pythPriceFeedId));
}

// which of these coins NAVI's oracle still can't price after a refresh WITHOUT Pyth's step: one simulated transaction, nothing
// signed — the refresh, then the getter NAVI's withdrawal itself reads (calculator::calculate_value -> oracle::get_token_price;
// "not valid" = abort 1502). A coin with a second source (Supra) is priced; one with neither is returned.
export async function unpriced(o: any, feeds: any[], addr: string): Promise<any[]> {
  const tx = new Transaction(); tx.setSender(addr);
  await within((navi as any).updateOraclePricesPTB(tx, feeds, { client: grpc, updatePythPriceFeeds: false }), "NAVI's price refresh");
  const n0 = (tx.getData().commands as any[]).length;
  for (const f of feeds) tx.moveCall({ target: `${o.packageId}::oracle::get_token_price`, arguments: [tx.object('0x6'), tx.object(o.priceOracle), tx.pure.u8(f.oracleId)] });
  const r: any = await within((grpc as any).core.simulateTransaction({ transaction: tx, checksEnabled: false, include: { effects: true, commandResults: true } }), "NAVI's price check");
  const t = r?.Transaction ?? r?.FailedTransaction ?? r;
  if (t?.effects?.status?.success === false || !Array.isArray(r?.commandResults))
    throw new Error("NAVI's prices could not be checked right now, so this withdrawal can't be prepared — try again in a minute.");
  return feeds.filter((_f, i) => {
    const b = r.commandResults[n0 + i]?.returnValues?.[0]?.bcs;
    return (typeof b === 'string' ? Buffer.from(b, 'base64')[0] : b?.[0]) !== 1;
  });
}

// the plain refusal (the owner's D1): which coin, why, and where the user can still withdraw
export function naviBlocked(blocked: string[], sym: string) {
  const others = [...new Set(blocked.filter((x) => x.toUpperCase() !== sym.toUpperCase()))];
  return others.length
    ? `Your NAVI account also holds ${names(others)}, whose price NAVI can't refresh right now without a price feed Kivy doesn't use, so NAVI withdrawals through Kivy are blocked for now — withdraw in NAVI's app.`
    : `NAVI can't refresh the price of ${sym} right now without a price feed Kivy doesn't use, so this withdrawal would fail — try again in a few minutes, or withdraw in NAVI's app.`;
}

// The price refresh before a NAVI lending withdrawal. NAVI's withdrawal values EVERY coin the wallet has on that market, so each
// one's price is refreshed in the same transaction. Built here, no longer by the library's updateOraclePriceBeforeUserOperationPTB,
// which read the positions a second time and fetched the config up to 28 times (BLD-W1/S1), returned the transaction with NO
// refresh when anything failed (BLD-A5: a raw abort for the user), and — for a coin whose Pyth price on chain was over 30 s old —
// added Pyth's and Wormhole's own programs, which Kivy never lists, so Kivy refused its own exit (BLD-A2; the owner's D1: Pyth +
// Wormhole stay off for good). Here the refresh never has the Pyth step: NAVI's oracle then uses its second source (Supra) when
// that one is fresh — NAVX and BUCK at times (priced at 09:58 UTC on 2026-11-17, not at 10:15) — and a coin it can't price that
// way (NS, BLUE, HAEDAL, IKA have no Supra at all; probe/navi_nopyth.mts) is found before building, by NAVI's own oracle in one
// simulated refresh, and named in a plain refusal.
export async function naviRefresh(tx: Transaction, ps: any[], pool: any, C: Coin, addr: string) {
  const o = (await naviConfig())?.oracle;
  const feeds: any[] = navi.filterPriceFeeds(o?.feeds ?? [], { lendingPositions: ps, pools: [pool] } as any);
  if (!o?.packageId || !o?.priceOracle || !feeds.some((f) => f.oracleId === pool.oracleId))
    throw new Error(`NAVI's price list does not name ${C.sym} right now, so this withdrawal can't be prepared — try again in a minute.`);
  const old = await pythOld(feeds);
  if (old.length) {
    const blocked = await unpriced(o, old, addr);
    if (blocked.length) throw new Error(naviBlocked(blocked.map((f) => f.oracleId === pool.oracleId ? C.sym : feedSym(f, ps)), C.sym));
  }
  const n0 = (tx.getData().commands as any[]).length;
  await within((navi as any).updateOraclePricesPTB(tx, feeds, { client: grpc, updatePythPriceFeeds: false }), "NAVI's price refresh");
  // one oracle_pro refresh per coin and nothing else (never a withdrawal without its refresh — BLD-A5)
  const added = (tx.getData().commands as any[]).slice(n0);
  const pkg = normalizeSuiAddress(o.packageId);
  if (added.length !== feeds.length || !added.every((c) => c.MoveCall && normalizeSuiAddress(c.MoveCall.package) === pkg && c.MoveCall.module === 'oracle_pro'))
    throw new Error("NAVI's price refresh could not be prepared, so this withdrawal can't be — try again in a minute.");
}

// Pyth's or Wormhole's update steps (programs Kivy never lists) among the commands from n0 on — NAVI's vault library adds them
// on its own when the vault coin's Pyth price is over 30 s old
export function pythSteps(tx: Transaction, n0 = 0) {
  const allow = allowedPkgs();
  return (tx.getData().commands as any[]).slice(n0).some((c) => c.MoveCall && !allow.has(normalizeSuiAddress(c.MoveCall.package))
    && ['pyth', 'vaa', 'hot_potato_vector'].includes(String(c.MoveCall.module)));
}
const vaultBlocked = (sym: string) => `NAVI's price for ${sym} isn't fresh on chain right now, and NAVI's vault can only refresh it through a price feed Kivy doesn't use — try again in a minute, or withdraw in NAVI's app.`;

// put a coin already in the transaction into NAVI lending (base = the amount; none = the whole coin) — depositCoin()
export async function naviDeposit(tx: Transaction, C: Coin, coin: any, base?: bigint) {
  await navi.depositCoinPTB(tx, C.type, coin, base != null ? { amount: Number(base) } : {});   // no amount: the whole coin
}

// put a coin already in the transaction into a NAVI vault (it must be the vault's coin) — depositCoin() in lending.ts
export async function vaultDeposit(tx: Transaction, m: Market, C: Coin, coin: any, addr: string) {
  const v: any = await vaultOf(m);
  const vt = v.assets?.baseCoin?.coinType;
  if (!vt || normType(vt) !== normType(C.type)) throw new Error(`${m.name} takes ${v.assets?.baseCoin?.symbol ?? 'another coin'}, not ${C.sym}`);
  const value = tx.moveCall({ target: '0x2::coin::value', typeArguments: [C.type], arguments: [coin] });
  await vault.depositPTB(tx, v, addr, value as any, { coin, client: grpc } as any);
}

// build()'s NAVI lending step (lending.ts): a deposit, or a withdrawal after NAVI's oracle update; returns the amount
export async function naviLend(L: LendStep): Promise<number> {
  const { addr, C, tx, tb, give, all, action } = L;
  let amount = L.amount;
  if (action === 'deposit') {
    const coin = tx.add(coinWithBalance({ type: C.type, balance: tb(amount) }));
    const fee = takeLendFee(tx, coin, tb(amount));
    await naviDeposit(tx, C, coin, fee ? undefined : tb(amount));      // with a fee, NAVI reads the coin's value
  } else {
    const pool: any = await within(navi.getPool(C.type, { cacheTime: 60_000 } as any) as any, "NAVI's pool list");
    // positions read fresh (disableCache): the library read them once and reused that copy until a restart, so after a deposit of
    // a NEW coin no withdrawal refreshed its price and NAVI refused every withdrawal (abort 1502 in calculator::calculate_value —
    // live test 2026-11-16: WAL lent, then a USDC withdrawal refused on live and staging, built at once by a fresh process)
    const ps = await buildPositions(addr, String(pool?.market ?? 'main'));
    const have = supplied(ps, C.type);
    if (have <= 0) throw new Error(`No ${C.sym} lent on NAVI`);
    if (all) amount = have;
    await naviRefresh(tx, ps, pool, C, addr);         // every coin's price, never Pyth's step (D1); refused here when NAVI can't price one
    const coin: any = await navi.withdrawCoinPTB(tx, C.type, Number(tb(amount)));
    give(coin);
  }
  return amount;
}

// build()'s NAVI vault step (lending.ts): a deposit (through depositCoin's path when a lend fee is on), or a withdrawal
// (kept in the transaction when keep is given); returns the amount
export async function vaultLend(L: LendStep): Promise<number> {
  const { addr, m, C, tx, tb, all, action, keep } = L;
  let amount = L.amount;
  const v: any = await vaultOf(m);
  if (normType(v.assets?.baseCoin?.coinType ?? '') !== normType(C.type)) throw new Error(`${m.name} takes ${v.assets?.baseCoin?.symbol ?? 'another coin'}, not ${C.sym}`);
  if (action === 'deposit' && lendFee()) {
    const coin = tx.add(coinWithBalance({ type: C.type, balance: tb(amount) }));
    takeLendFee(tx, coin, tb(amount));
    await vaultDeposit(tx, m, C, coin, addr);
  } else if (action === 'deposit') {
    await vault.depositPTB(tx, v, addr, String(amount), { useGasCoin: C.type === SUI_T, client: grpc } as any);
  } else {
    const pos = await vaultPosition(m, addr);
    if (pos.usdc <= 0) throw new Error(`No ${C.sym} in ${m.name}`);
    if (all) amount = pos.usdc;
    // NAVI's vault library refreshes the vault coin's price itself, WITH Pyth's step when its Pyth price on chain is over 30 s old
    // (no way to turn it off): said plainly before building, and caught after it if the price aged in between (the owner's D1)
    const feeds = ((await naviConfig())?.oracle?.feeds ?? []).filter((f: any) => normType(String(f.coinType ?? '')) === normType(C.type));
    if ((await pythOld(feeds)).length) throw new Error(vaultBlocked(C.sym));
    const n0 = (tx.getData().commands as any[]).length;
    const w: any = await vault.withdrawPTB(tx, v, addr, all ? { kind: 'all' } as any : { kind: 'amount', amount: String(amount) } as any,
                                           (keep ? { disableAutoTransfer: true, client: grpc } : { client: grpc }) as any);
    if (pythSteps(tx, n0)) throw new Error(vaultBlocked(C.sym));
    if (keep) { if (!w?.coin) throw new Error(`${m.name} can't withdraw-and-swap in one go`); keep.push(w.coin); }
  }
  return amount;
}

// every coin NAVI lending lists, each passed to add() — knownCoins() in lending.ts
export async function naviKnownCoins(add: (sym: any, type: any, dec: any, icon?: any) => void) {
  const ps: any = await (navi as any).getPools({ cacheTime: 60_000 });
  for (const p of (Array.isArray(ps) ? ps : Object.values(ps)) as any[]) add(p.token?.symbol, p.suiCoinType ?? p.coinType, p.token?.decimals, p.token?.logoUri);
}

// every coin this wallet has lent on NAVI, pushed to out — positionsAll() in lending.ts
export async function naviPositionsAll(m: Market, addr: string, label: (t: string) => any, out: any[]) {
  for (const p of (await naviPositions(addr)) ?? []) {
    const s = p['navi-lending-supply'] ?? p['navi-lending-emode-supply'];   // E-mode deposits count too
    if (!s || !(Number(s.amount) > 0)) continue;     // a borrow (or empty) entry has no supply: skip it — this guard had slipped into the comment above (2026-11-14)
    const t = normType(s.token?.coinType ?? s.pool?.coinType ?? s.pool?.suiCoinType ?? '');
    const lb = label(t) ?? { asset: s.token?.symbol ?? t.split('::').pop(), token: s.token?.symbol ?? t.split('::').pop() };
    out.push({ market: m.key, type: t, ...lb, amount: Number(s.amount) });
  }
}

// what this wallet holds in a NAVI vault (USDC or SUI), pushed to out — positionsAll() in lending.ts
export async function vaultPositionsAll(m: Market, addr: string, out: any[]) {
  const p: any = await vaultPosition(m, addr);
  const isSui = SUI_VAULT_MARKETS.includes(m);
  if (p.usdc > 0) out.push({ market: m.key, type: normType(isSui ? SUI_T : USDC), asset: isSui ? 'SUI' : 'USDC', token: isSui ? 'SUI' : 'USDC', amount: p.usdc });
}

// claim this wallet's listed NAVI lending rewards to the wallet — buildClaim() in lending.ts
export async function naviClaim(tx: Transaction, mine: Reward[], addr: string) {
  await navi.claimLendingRewardsPTB(tx, mine.map((r) => r.raw), { customCoinReceive: { type: 'transfer', transfer: addr } } as any);
}

// claim this wallet's listed NAVI vault rewards — buildClaim() in lending.ts
export async function vaultClaim(tx: Transaction, mine: Reward[], addr: string) {
  await vault.claimRewardsPTB(tx, mine.map((r) => r.raw), addr, { client: grpc } as any);
}

// NAVI lending's rows for /rates-all: the coin versions and Kivy's coins, pushed to rows — ratesAll() in lending.ts
export async function naviRateRows(m: Market, assets: { sym: string; type: string }[], rows: any[]) {
  await settle(async () => {
    const ps: any = await (navi as any).getPools({ cacheTime: 60_000 });
    const seen = new Set<string>();
    for (const p of (Array.isArray(ps) ? ps : Object.values(ps)) as any[]) {
      const sym = p.token?.symbol ?? p.symbol; if (!VARIANT_OF[sym] || seen.has(sym)) continue; seen.add(sym);
      const i = p.supplyIncentiveApyInfo || {};
      rows.push({ market: m.key, name: m.name, protocol: 'navi', asset: VARIANT_OF[sym], token: sym, icon: p.token?.logoUri ?? null,
                  type: normType(p.suiCoinType ?? p.coinType), dec: Number(p.token?.decimals),
                  total_apr: Number(i.apy ?? 0) || Number(i.vaultApr ?? 0) });
    }
  });
  await Promise.all(assets.map((a) => settle(async () => {
    const p: any = await navi.getPool(a.type, { cacheTime: 60_000 } as any); if (!p) return;
    const i = p.supplyIncentiveApyInfo || {};
    rows.push({ market: m.key, name: m.name, protocol: 'navi', asset: a.sym, type: a.type, dec: Number(p.token?.decimals ?? KIVY_COINS[a.sym]?.decimals),
                total_apr: Number(i.apy ?? 0) || Number(i.vaultApr ?? 0) });
  })));
}

// NAVI's own SUI vaults' rows for /rates-all, pushed to rows — ratesAll() in lending.ts
export async function suiVaultRateRows(rows: any[]) {
  for (const v of (await vaults()) as any[]) {
    const sm = SUI_VAULT_MARKETS.find((x) => x.vaultName === v.name); if (!sm) continue;
    rows.push({ market: sm.key, name: v.name.replace('NAVI SUI', 'NAVI'), protocol: 'navi',
                asset: 'SUI', vault: true, type: SUI_T, dec: 9, total_apr: Number(v.apy?.instant ?? v.apy?.target ?? 0) * 100 });
  }
}

// what borrowers post as collateral on NAVI lending (live coins with LTV > 0), into out[m.key] — collateralAll()
export async function naviCollateral(m: Market, out: Record<string, any>, num: (x: any) => number) {
  const ps: any = await (navi as any).getPools({ cacheTime: 60_000 });
  out[m.key] = { coins: ((Array.isArray(ps) ? ps : Object.values(ps)) as any[]).filter((p) => !p.isDeprecated && num(p.ltv) / 1e27 > 0).map((p) => ({
    sym: p.token?.symbol ?? p.symbol, type: normType(p.suiCoinType ?? p.coinType), icon: webIcon(p.token?.logoUri),
    ltv: Math.round(num(p.ltv) / 1e25), liq: Math.round(num(p.liquidationFactor?.threshold) * 100), usd: Math.round(num(p.poolSupplyValue)) })) };
}

// a NAVI USDC vault: no collateral list, only NAVI's risk level, into out[m.key] — collateralAll() in lending.ts
export async function vaultCollateral(m: Market, out: Record<string, any>) {
  const v: any = await vaultOf(m); out[m.key] = { vault: true, risk: v.riskLevel ?? null };
}

// NAVI's SUI vaults: their risk levels into out — collateralAll() in lending.ts
export async function suiVaultCollateral(out: Record<string, any>) {
  for (const sm of SUI_VAULT_MARKETS) { const v: any = (await vaults()).find((x: any) => x.name === sm.vaultName); if (v) out[sm.key] = { vault: true, risk: v.riskLevel ?? null }; }
}
