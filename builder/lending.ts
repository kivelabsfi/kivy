/*
 * lending.ts — lending across protocols: rate, position, lend / withdraw, rewards + claim, coin list, rates-all, coinOf().
 * In detail: one market's rate and position, plain lend / withdraw (GET /build), every coin and every position Kivy can see,
 * rewards and the claim transaction, /rates-all, /collateral and coinOf() (symbol -> type + decimals). Each function picks the
 * protocol from the market (Suilend, NAVI lending, NAVI vault) and calls suilend.ts / navi.ts.
 * Called by: builder.ts handle(), compose.ts (build(..., keep) for withdrawals, depositCoin, coinOf).
 * Imports: core.ts, suilend.ts, navi.ts — no protocol SDK. State: the coin list (1 h), the rates rows (5 min), LST yields (1 h).
 *
 * The protocol adapters: the same jobs for each protocol, picked here by the market (an SDK upgrade touches only its adapter file)
 *   job                  Suilend (suilend.ts)        NAVI lending (navi.ts)      NAVI vault (navi.ts)
 *   rate                 suilendRate                 naviRate                    vaultRate
 *   position             suilendPosition             naviPosition                vaultPosition
 *   deposit a coin       suilendDeposit              naviDeposit                 vaultDeposit
 *   lend / withdraw      suilendLend(LendStep)       naviLend(LendStep)          vaultLend(LendStep)
 *   rewards, claim       suilendRewards, -Claim      naviRewards, -Claim         vaultRewards, -Claim
 *   coin list            suilendKnownCoins           naviKnownCoins              -
 *   all positions        suilendPositionsAll         naviPositionsAll            vaultPositionsAll
 *   /rates-all rows      suilendRateRows             naviRateRows                rate() + suiVaultRateRows
 *   /collateral          suilendCollateral           naviCollateral              vaultCollateral, suiVaultCollateral
 */
import { Transaction } from '@mysten/sui/transactions';
import { grpc } from './chain.ts';
import { normType } from './txfacts.ts';
import { byKey, type Coin, DEC, KIVY_COINS, type LendStep, type Market, MARKETS, type Reward, settle, simulate, SUI_VAULT_MARKETS, USDC } from './core.ts';
import { suilendClaim, suilendCollateral, suilendDeposit, suilendKnownCoins, suilendLend, suilendPosition, suilendPositionsAll, suilendRate, suilendRateRows, suilendRewards } from './suilend.ts';
import { naviClaim, naviCollateral, naviDeposit, naviKnownCoins, naviLend, naviPosition, naviPositionsAll, naviRate, naviRateRows, naviRewards, suiVaultCollateral, suiVaultRateRows, vaultClaim, vaultCollateral, vaultDeposit, vaultLend, vaultPosition, vaultPositionsAll, vaultRate, vaultRewards } from './navi.ts';

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S8] RATES, POSITIONS, PLAIN LEND / WITHDRAW
// rate(), position(), depositCoin() (into Suilend / NAVI / a vault), build() (GET /build): each picks the protocol adapter
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── rates (yearly %, as each app shows it) ──
export async function rate(m: Market) {
  if (m.protocol === 'suilend') return suilendRate(m);
  if (m.kind === 'lending') return naviRate();
  return vaultRate(m);
}

// how much of coin T this wallet has lent on market m. For Suilend it also returns the accounts holding it
// (single-coin first) and the old cTokens, which build() withdraws from. Callers: build, positionsAll, the positions command
export async function position(m: Market, addr: string, T = USDC, dec = DEC) {
  if (m.protocol === 'suilend') return suilendPosition(m, addr, T, dec);
  if (m.kind === 'lending') return naviPosition(addr, T);
  return vaultPosition(m, addr);
}

// Put a coin (already in the transaction) into a market. Shared by plain deposits and swap-then-lend.
export async function depositCoin(tx: Transaction, m: Market, C: Coin, coin: any, addr: string, base?: bigint) {
  if (m.protocol === 'suilend') await suilendDeposit(tx, m, C, coin, addr);
  else if (m.kind === 'lending') await naviDeposit(tx, C, coin, base);
  else await vaultDeposit(tx, m, C, coin, addr);
}

// keep: withdraw into the transaction (for withdraw-then-swap) instead of sending the coin to the wallet
// GET /build — plain lend (deposit) or withdraw of one coin on one market. Caller: server.py /api/tx/lend (lend_cli).
// Also the withdrawal step inside withdrawswap, compose and sendmany (keep / into).
export async function build(addr: string, key: string, action: string, amountArg: string, sym = 'USDC', keep?: any[], into?: Transaction) {
  const m = byKey(key);
  const C = await coinOf(key, sym);
  const tb = (x: number) => BigInt(Math.round(x * 10 ** C.dec)), fb = (x: any) => Number(x) / 10 ** C.dec;
  const tx = into ?? new Transaction();             // into: add this withdrawal to a transaction being built (multi-send)
  if (!into) tx.setSender(addr);
  const give = (c: any) => { if (keep) keep.push(c); else tx.transferObjects([c], addr); };
  let amount = Number(amountArg);
  const all = amountArg === 'all';
  if (action === 'deposit' && !(amount > 0)) throw new Error('Enter an amount to lend');
  const L: LendStep = { addr, m, C, tx, tb, fb, give, amount, all, action, keep };
  if (m.protocol === 'suilend') amount = await suilendLend(L);
  else if (m.kind === 'lending') amount = await naviLend(L);
  else amount = await vaultLend(L);
  if (keep) return { tx, amount, C };                 // withdraw-then-swap goes on from here
  let size = 0;
  try { size = await simulate(tx); }                 // never hand the user a transaction that would fail
  catch (e: any) {
    if (/unwrap_spot_price|price/i.test(String(e?.message)) && m.protocol === 'suilend')
      throw new Error(`Your ${m.name} account also holds coins whose price isn't fresh on chain right now — try again in a minute, or withdraw in the Suilend app.`);
    throw e;
  }
  return { ok: true, market: key, action, amount, coin: C.sym, coin_type: C.type, decimals: C.dec, tx: await tx.toJSON({ client: grpc as any }), bytes: size };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S9] COIN LIST, ALL POSITIONS, REWARDS
// /known-coins, /positions-all, /rewards and the claim transaction (build … claim)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── every coin the markets list (lending versions, NAVI/Suilend reserves, reward coins): Kivy can swap / send / show
// these, and nothing else — a protocol listing is the filter against airdropped junk tokens ──
let _known: { at: number; v: Promise<any[]> } | null = null;
// GET /known-coins — caller: server.py ecoins(). Also coinOf()'s last resort. Rebuilt once an hour.
export function knownCached() {
  if (!_known || Date.now() - _known.at > 3_600_000) { const v = knownCoins(); _known = { at: Date.now(), v }; v.catch(() => { _known = null; }); }
  return _known.v;
}
async function knownCoins() {
  const out: any[] = []; const seen = new Set<string>();
  const add = (sym: any, type: any, dec: any, icon?: any) => {
    if (!sym || !type || !Number.isFinite(Number(dec))) return;
    const t = normType(String(type)); if (seen.has(t)) return;
    seen.add(t); out.push({ sym: String(sym), type: t, dec: Number(dec), icon: typeof icon === 'string' && icon.startsWith('https://') ? icon : null });
  };
  for (const r of await rowsAll()) if (r.token && r.type) add(r.token, r.type, r.dec);
  await Promise.all(MARKETS.filter((m) => m.protocol === 'suilend').map((m) => settle(() => suilendKnownCoins(m, add))));
  await settle(() => naviKnownCoins(add));
  await settle(async () => { add('NAVX', '0xa99b8952d4f7d947ea77fe0ecdcc9e5fc0bcab2841d6e2a5aa00c3044e5544b5::navx::NAVX', 9); });   // NAVI's reward coin
  return out;
}

// ── everything this wallet has lent, any coin (for the 'You lent' column and withdrawals) ──
// GET /positions-all — caller: server.py lend_positions_all(). Runs inside sharedRead() (see shared()).
export async function positionsAll(addr: string) {
  const rows = await rowsAll();
  const label = (t: string) => { const r = rows.find((x) => x.type && normType(x.type) === normType(t)); return r ? { asset: r.asset, token: r.token ?? r.asset } : null; };
  const out: any[] = [];
  await Promise.all([...MARKETS, ...SUI_VAULT_MARKETS].map((m) => settle(async () => {
    if (m.protocol === 'suilend') await suilendPositionsAll(m, addr, label, out);
    else if (m.kind === 'lending') await naviPositionsAll(m, addr, label, out);
    else await vaultPositionsAll(m, addr, out);
  })));
  return out;
}

// ── rewards: what each market has paid out that the user can claim ──
// USD prices from Aftermath's price-info API, for rewards the protocol SDK did not price
async function usdPrices(types: string[]) {
  if (!types.length) return {} as Record<string, number>;
  try {
    const r: any = await (await fetch('https://aftermath.finance/api/price-info', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ coins: types }), signal: AbortSignal.timeout(20_000) })).json();
    return Object.fromEntries(types.map((t) => [t, Math.max(0, Number(r?.[t]?.price ?? 0))]));
  } catch { return {} as Record<string, number>; }
}

// GET /rewards — callers: server.py defi_positions() and /api/lend/rewards. only = a market key, p-suilend / p-navi, or all
export async function rewards(addr: string, only?: string) {
  const jobs: Promise<Reward[]>[] = [];
  for (const m of [...MARKETS, ...SUI_VAULT_MARKETS]) {                 // NAVI's SUI vaults pay rewards too
    if (only && only !== 'all' && only !== m.key && only !== 'p-' + m.protocol) continue;   // 'p-suilend' / 'p-navi': one protocol's markets (2026-10-25)
    if (m.protocol === 'suilend') jobs.push(suilendRewards(m, addr).catch(() => []));
    else if (m.kind === 'lending') jobs.push(naviRewards(addr).catch(() => []));
    else jobs.push(vaultRewards(m, addr).catch(() => []));
  }
  const all = (await Promise.all(jobs)).flat();
  const px = await usdPrices([...new Set(all.filter((r) => !r.usd).map((r) => r.coinType))]);
  for (const r of all) if (!r.usd) r.usd = r.amount * (px[r.coinType] ?? 0);
  return all;
}

// GET /build with action "claim" — one transaction claiming every listed reward. Caller: server.py claim_card()
export async function buildClaim(addr: string, only: string) {
  const rs = await rewards(addr, only);
  if (!rs.length) throw new Error('No rewards to claim right now');
  const tx = new Transaction();
  tx.setSender(addr);
  for (const m of [...MARKETS, ...SUI_VAULT_MARKETS]) {
    const mine = rs.filter((r) => r.market === m.key);
    if (!mine.length) continue;
    if (m.protocol === 'suilend') await suilendClaim(tx, m, mine, addr);
    else if (m.kind === 'lending') await naviClaim(tx, mine, addr);
    else await vaultClaim(tx, mine, addr);
  }
  await simulate(tx);
  const usd = rs.reduce((a, r) => a + r.usd, 0);
  return { ok: true, market: only, action: 'claim', usd, rewards: rs.map(({ raw, ...r }) => r), gas: (tx as any).__gas ?? null,   // real gas: the card weighs it against the rewards
           tx: await tx.toJSON({ client: grpc as any }) };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S10] RATES-ALL, COLLATERAL, COIN LOOKUP
// /rates-all (every coin on every market), /collateral, coinOf() (symbol -> type + decimals)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── rates for every coin Kivy knows (2026-10-19); each row also carries the coin type + decimals Kivy lends with ──
// LST staking yield (e.g. sSUI ≈ 1.3%) — Suilend's lending rate leaves it out, NAVI's already includes it (2026-10-19).
// Suilend's public, keyless SpringSui endpoint; cached for an hour, and an outage just means the lending rate alone.
let _lstApy: { at: number; v: Record<string, number> } | null = null;
async function lstApys(): Promise<Record<string, number>> {
  if (_lstApy && Date.now() - _lstApy.at < 3_600_000) return _lstApy.v;
  const r = await fetch('https://api.suilend.fi/springsui/apy', { signal: AbortSignal.timeout(10_000) });
  const j: any = await r.json();
  const v: Record<string, number> = {};
  for (const [t, x] of Object.entries(j ?? {})) { const n = Number(x); if (Number.isFinite(n) && n > 0 && n < 50) v[normType(t)] = n; }
  _lstApy = { at: Date.now(), v };
  return v;
}
// GET /rates-all — one row per (market, coin): yearly %, coin type, decimals. Caller: server.py lend_rates_all().
// rowsAll() keeps it 5 minutes for coinOf() and positionsAll().
export async function ratesAll() {
  const stake = await lstApys().catch(() => ({} as Record<string, number>));
  const assets = Object.entries(KIVY_COINS).map(([sym, c]: any) => ({ sym, type: normType(c.type) }));
  const rows: any[] = [];
  await Promise.all(MARKETS.map((m) => settle(async () => {
    if (m.protocol === 'suilend') {
      await suilendRateRows(m, assets, stake, rows);
    } else if (m.kind === 'lending') {
      await naviRateRows(m, assets, rows);
    } else {
      rows.push({ market: m.key, name: m.name, protocol: 'navi', asset: 'USDC', type: normType(USDC), dec: DEC, total_apr: await rate(m) });   // NAVI's USDC vaults
    }
  })));
  await settle(() => suiVaultRateRows(rows));                                  // NAVI's own SUI vaults
  return rows.filter((r) => Number.isFinite(r.total_apr));
}

// What borrowers post as collateral in each market (2026-10-23): lenders' risk is the collateral behind the loans —
// if it crashes faster than liquidations can sell it, the market takes bad debt. A coin counts when its LTV > 0.
// NAVI's managed vaults publish no collateral list, only NAVI's own risk level.
// GET /collateral — caller: server.py lend_collateral()
export async function collateralAll() {
  const out: Record<string, any> = {};
  const num = (x: any) => { const n = Number(String(x ?? 0)); return Number.isFinite(n) ? n : 0; };
  await Promise.all(MARKETS.map((m) => settle(async () => {
    if (m.protocol === 'suilend') await suilendCollateral(m, out, num);
    else if (m.kind === 'lending') await naviCollateral(m, out, num);
    else await vaultCollateral(m, out);
  })));
  await settle(() => suiVaultCollateral(out));
  for (const k of Object.keys(out)) if (out[k].coins) out[k].coins.sort((a: any, b: any) => b.usd - a.usd);
  return out;
}

// A market + coin symbol -> its coin type and decimals. Kivy's own coins come from knowledge/kivy.json; the
// other versions (vSUI, LBTC, USDsui…) from the market's own listing, so the type is exactly what that market takes.
let _rowsAll: { at: number; v: Promise<any[]> } | null = null;
function rowsAll() {
  if (!_rowsAll || Date.now() - _rowsAll.at > 300_000) {
    const v = ratesAll();
    _rowsAll = { at: Date.now(), v };
    v.catch(() => { _rowsAll = null; });
  }
  return _rowsAll.v;
}
export async function coinOf(key: string, sym: string): Promise<Coin> {
  const s = String(sym || 'USDC');
  const base = KIVY_COINS[s.toUpperCase()];
  if (base?.type && Number.isFinite(Number(base.decimals))) return { type: normType(base.type), dec: Number(base.decimals), sym: s.toUpperCase() };
  const rows = await rowsAll();
  const same = (x: any, exact: boolean) => x.token && x.type && (exact ? x.token === s : x.token.toLowerCase() === s.toLowerCase());
  const r = rows.find((x) => x.market === key && same(x, true)) ?? rows.find((x) => x.market === key && same(x, false))
         ?? (key ? null : rows.find((x) => same(x, true)) ?? rows.find((x) => same(x, false)));
  if ((!r || !Number.isFinite(Number(r.dec))) && !key) {             // any coin the markets list (reward coins like BLUE)
    const k = (await knownCached()).find((x) => x.sym === s) ?? (await knownCached()).find((x) => x.sym.toLowerCase() === s.toLowerCase());
    if (k) return { type: normType(k.type), dec: Number(k.dec), sym: k.sym };
  }
  if (!r || !Number.isFinite(Number(r.dec))) throw new Error(`${s} is not listed${key ? ' on ' + byKey(key).name : ''}`);
  return { type: normType(r.type), dec: Number(r.dec), sym: r.token };
}
