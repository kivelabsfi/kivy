/*
 * suilend.ts — the Suilend adapter: every call into the Suilend SDK (markets, reserves, accounts, rewards).
 * In detail: Suilend markets loaded and cached, a market's reserve for a coin, the wallet's Suilend accounts (one per coin), reward
 * reads, then the adapter jobs lending.ts calls (rate, position, deposit, lend / withdraw step, claim, coin list, all positions,
 * /rates-all rows, collateral; the table is in lending.ts's header). A Suilend SDK upgrade should touch this file only.
 * Called by: lending.ts, routers.ts (slPkg, tests only), builder.ts (keeps the markets warm). Imports: core.ts, warm.ts.
 * State: the 60 s market cache (_sl; reloaded ahead of expiry while Kivy is in use — warm.ts).
 */
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import { Side, SuilendClient, formatRewards, getFilteredRewards, getTotalAprPercent, initializeObligations, initializeSuilend, initializeSuilendRewards } from '@suilend/sdk';
import { getPublishedAt } from '@suilend/sdk/_generated/_framework/env';
import { grpc } from './chain.ts';
import { normType } from './txfacts.ts';
import { type Coin, coinBalance, DEC, type LendStep, type Market, MARKETS, readCtx, type Reward, shared, takeLendFee, USDC, VARIANT_OF, webIcon } from './core.ts';
import { type Warm, warmAhead, warmGet } from './warm.ts';

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S6] SUILEND
// the Suilend SDK: markets loaded (cached 60 s), reserves, accounts (one per coin), rewards; then the adapter lending.ts calls
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
export const U64_MAX = 18446744073709551615n;
// the Suilend package to call: whatever the SDK resolved at initialize (v25 0x5967… was hard-coded here; Suilend's markets
// moved to version 8 in 2026-11 and only the newest package accepts them)
export const slPkg = () => getPublishedAt('suilend');
const CTOKEN_ORIGIN = '0xf95b06141ed4a174f239417323bde3f209b972f5930d8521ea38a52aff3a6ddf';

export const ctokenType = (m: Market, t = USDC) => `${CTOKEN_ORIGIN}::reserve::CToken<${m.type}, ${t}>`;

// Pyth's public Hermes now refuses keyless requests; supply rates and cToken values only need
// Suilend's on-chain cached prices, so reads never ask Hermes.
const NO_HERMES = {
  getLatestPriceFeeds: async () => undefined,
  getLatestPriceFeedsPartial: async (ids: string[]) => ({ feeds: [], missing: ids }),
};

// ── Suilend markets: heavy to load, kept for a minute each (warm.ts: reloaded in the background before they expire) ──
type SlMarket = { sl: any; init: any; rewards: any[]; rm: any };
const _sl = new Map<string, Warm<SlMarket>>();
// one Suilend market loaded through its SDK (client, reserves, reward APRs), shared for 60 s; used by rate, position,
// build, depositCoin, rewards, ratesAll, collateralAll and knownCoins
export function suilend(m: Market) { return warmGet(_sl, m.key, () => loadSuilend(m)); }
// the keep-warm tick (builder.ts): reload this market in the background shortly before its copy expires
export function suilendAhead(m: Market) { warmAhead(_sl, m.key, () => loadSuilend(m)); }
function loadSuilend(m: Market): Promise<SlMarket> {
  return (async () => {
    const sl = await SuilendClient.initialize(m.id!, m.type!, grpc as any);
    const init: any = await initializeSuilend(grpc as any, sl, undefined, undefined,
      { pythConnection: NO_HERMES, tolerateMissingPriceFeeds: true } as any);
    let rewards: any[] = [], rm: any = null;
    try {
      const { rewardPriceMap } = await initializeSuilendRewards(init.reserveMap, init.activeRewardCoinTypes);
      rm = formatRewards(init.reserveMap, init.rewardCoinMetadataMap, rewardPriceMap, []);
      rewards = getFilteredRewards(rm[USDC]?.deposit ?? []);
    } catch { /* rewards are a bonus; the base rate still shows */ }
    return { sl, init, rewards, rm };
  })();
}
// the market's reserve for coin t (default USDC); throws when the market does not list that coin
export function usdcReserve(init: any, t = USDC) {
  const r = init.reserveMap[t] ?? Object.values(init.reserveMap).find((x: any) => normType(x.coinType) === normType(t));
  if (!r) throw new Error('this market does not list ' + (t === USDC ? 'USDC' : t.split('::').pop()));
  return r;
}

// ── Suilend accounts: ONE PER COIN (2026-10-20) ──
// A Suilend withdrawal must re-price every coin in the account it comes from; one stale price blocks it all.
// So each coin gets its own account: new deposits go to an account holding only that coin (or an empty one, or a
// new one), and a withdrawal touches only the accounts holding that coin. Older mixed accounts keep working.

// the wallet's Suilend accounts on market m: from the shared batch inside a read, else fresh
export function accounts(m: Market, addr: string) {
  if (!readCtx.getStore()) return accountsFresh(m, addr);
  return shared('acc:' + m.key + ':' + addr, async () => (await allAccounts(addr)).get(m.key) ?? []);
}
// every Suilend market's accounts for this wallet: one owner-card listing (the SDK shares it across markets) and ONE
// getObjects for all the account objects; the SDK then parses them from that batch instead of one fetch each
function allAccounts(addr: string) {
  return shared('allacc:' + addr, async () => {
    const sm = MARKETS.filter((m) => m.protocol === 'suilend');
    const caps = await Promise.all(sm.map(async (m) => ({ m, caps: (await SuilendClient.getObligationOwnerCaps(addr, [m.type!], grpc as any)) ?? [] })));
    const ids = caps.flatMap((x) => x.caps.map((c: any) => c.obligationId));
    const objs = new Map<string, any>();
    for (let i = 0; i < ids.length; i += 50) {
      const r: any = await (grpc as any).getObjects({ objectIds: ids.slice(i, i + 50), include: { content: true } });
      r.objects.forEach((o: any, k: number) => { if (!(o instanceof Error)) objs.set(ids[i + k], o); });
    }
    const batch = { getObject: async ({ objectId }: any) => {      // the SDK's getObligation reads through this
      const o = objs.get(objectId);
      return o ? { object: o } : (grpc as any).getObject({ objectId, include: { content: true } });
    } };
    const out = new Map<string, any[]>();
    for (const { m, caps: cs } of caps)
      out.set(m.key, await Promise.all(cs.map(async (cap: any) => ({ cap, ob: await SuilendClient.getObligation(cap.obligationId, [m.type!], batch as any) }))));
    return out;
  });
}
async function accountsFresh(m: Market, addr: string) {
  const { sl } = await suilend(m);
  const caps: any[] = await SuilendClient.getObligationOwnerCaps(addr, [m.type!], grpc as any);
  return Promise.all((caps ?? []).map(async (cap: any) => ({ cap, ob: await sl.getObligation(cap.obligationId) })));
}
export const obIdxs = (ob: any) => [...new Set([...(ob?.deposits ?? []), ...(ob?.borrows ?? [])].map((d: any) => BigInt(d.reserveArrayIndex)))];

// claimable rewards, one reader per protocol: Suilend SDK (per account), NAVI lending, NAVI vaults
export async function suilendRewards(m: Market, addr: string): Promise<Reward[]> {
  const caps: any[] = await SuilendClient.getObligationOwnerCaps(addr, [m.type!], grpc as any);
  if (!caps?.length) return [];
  const { sl, init } = await suilend(m);
  const obls: any = await initializeObligations(grpc as any, sl, init.refreshedRawReserves, init.reserveMap, addr);
  const { rewardPriceMap } = await initializeSuilendRewards(init.reserveMap, init.activeRewardCoinTypes);
  const rm: any = formatRewards(init.reserveMap, init.rewardCoinMetadataMap, rewardPriceMap, obls?.obligations ?? obls);
  const out: Reward[] = [];
  for (const cap of caps)                                    // every account (one per coin) earns its own rewards
  for (const ct of Object.keys(rm)) for (const side of ['deposit', 'borrow']) for (const r of getFilteredRewards(rm[ct]?.[side] ?? [])) {
    const amt = Number(r.obligationClaims?.[cap.obligationId]?.claimableAmount ?? 0);
    if (amt > 0) out.push({ market: m.key, coinType: r.stats.rewardCoinType, symbol: r.stats.symbol, amount: amt, usd: amt * Number(r.stats.price ?? 0),
      raw: { cap: cap.id, reserveArrayIndex: BigInt(r.stats.reserve?.arrayIndex ?? 0), rewardIndex: BigInt(r.stats.rewardIndex), rewardCoinType: r.stats.rewardCoinType, side: r.stats.side } });
  }
  return out;
}

// ── the adapter: what lending.ts asks of Suilend (one function per job; navi.ts has the same jobs for NAVI) ──
// a Suilend market's USDC yearly % (base + rewards, as Suilend's app shows) — rate() in lending.ts
export async function suilendRate(m: Market) {
  const { init, rewards } = await suilend(m);
  const r: any = usdcReserve(init);
  return Number(getTotalAprPercent(Side.DEPOSIT, r.depositAprPercent, rewards));   // base + rewards, as Suilend's app shows
}

// how much of coin T this wallet has lent on a Suilend market, with the accounts holding it (single-coin first) and the
// old cTokens, which suilendLend() withdraws from — position() in lending.ts (same defaults)
export async function suilendPosition(m: Market, addr: string, T = USDC, dec = DEC) {
  const fromB = (x: any) => Number(x) / 10 ** dec;
  const { sl, init } = await suilend(m);
  const r: any = usdcReserve(init, T);
  const ctokens = await coinBalance(addr, ctokenType(m, T));
  const idx = BigInt(sl.findReserveArrayIndex(r.coinType));
  const accts: any[] = [];
  for (const a of await accounts(m, addr)) {                 // every account holding this coin, single-coin ones first
    const d = (a.ob?.deposits ?? []).find((x: any) => BigInt(x.reserveArrayIndex) === idx);
    if (d && Number(d.depositedCtokenAmount) > 0) accts.push({ ...a, amount: fromB(Number(d.depositedCtokenAmount)) * Number(r.cTokenExchangeRate) });
  }
  accts.sort((x, y) => obIdxs(x.ob).length - obIdxs(y.ob).length);
  const inAccount = accts.reduce((t, a) => t + a.amount, 0);
  const rate = Number(r.cTokenExchangeRate);
  return { usdc: inAccount + fromB(ctokens) * rate, in_account: inAccount, ctokens, rate, accts };
}

// which existing account a deposit may go into: one holding only this coin, else an empty one, else none (open a new one).
// Never an account with a loan: an account key can be sent into any wallet, and a deposit there would back someone's debt
// (QA 2026-11-17, same class as BLD-V1; locked by tests/test_suilendpick_20261118.py)
export function suilendPick(all: any[], idx: bigint) {
  const only = (a: any) => !(a.ob?.borrows ?? []).length && obIdxs(a.ob).every((i) => i === idx);
  return all.find((a) => only(a) && obIdxs(a.ob).length) ?? all.find((a) => !obIdxs(a.ob).length);   // this coin's account, or an empty one
}

// put a coin already in the transaction into a Suilend market: this coin's own account, else an empty one, else a new one
// — depositCoin() in lending.ts
export async function suilendDeposit(tx: Transaction, m: Market, C: Coin, coin: any, addr: string) {
  const { sl, init } = await suilend(m);
  usdcReserve(init, C.type);                      // the market must list this coin
  const idx = BigInt(sl.findReserveArrayIndex(C.type));
  const pick = suilendPick(await accounts(m, addr), idx);
  if (pick) sl.deposit(coin, C.type, pick.cap.id, tx);
  else {                                          // no account for this coin yet: open one
    const cap = sl.createObligation(tx);
    sl.deposit(coin, C.type, cap, tx);
    tx.transferObjects([cap], addr);
  }
}

// build()'s Suilend step (lending.ts): a deposit, or a withdrawal from the accounts holding the coin and then the old
// cTokens; returns the amount ('all' = what was lent)
export async function suilendLend(L: LendStep): Promise<number> {
  const { addr, m, C, tx, tb, fb, give, all, action } = L;
  let amount = L.amount;
  const { sl } = await suilend(m);
  const idx = sl.findReserveArrayIndex(C.type);
  const market = tx.object(m.id!);
  const pos: any = await suilendPosition(m, addr, C.type, C.dec);
  if (action === 'deposit') {
    const coin = tx.add(coinWithBalance({ type: C.type, balance: tb(amount) }));
    takeLendFee(tx, coin, tb(amount));
    await suilendDeposit(tx, m, C, coin, addr);
  } else {
    // Audit T09: USDC can sit in two forms here — the user's Suilend account (obligation) and plain cTokens
    // from Kivy's first version. "all" empties both; an exact amount is taken from the account first, then
    // the cTokens, and is never silently cut down: asking for more than is lent is refused.
    const total = pos.usdc;
    if (!(total > 0)) throw new Error(`No ${C.sym} lent on ${m.name}`);
    if (!all && amount > total + 0.000001) throw new Error(`You have ${total.toFixed(6)} ${C.sym} lent on ${m.name} — withdraw that much or less.`);
    let left = all ? Infinity : amount;
    const fresh = new Set<bigint>();
    for (const a of pos.accts) {                    // single-coin accounts first: they only need this coin's price
      if (!(left > 0.000001)) break;
      for (const i of obIdxs(a.ob).sort()) {        // re-price what THIS account holds, from the price already on chain
        if (fresh.has(i)) continue; fresh.add(i);
        const rv = sl.lendingMarket.reserves[Number(i)];
        await sl.refreshReservePrices(tx, await sl.pythClient.getPriceFeedObjectId(Buffer.from(rv.priceIdentifier.bytes).toString('hex')), i);
      }
      const whole = left >= a.amount - 0.000001;
      const ct = whole ? U64_MAX : BigInt(Math.floor(Number(tb(left)) / pos.rate));
      const [coin] = await sl.withdraw(a.cap.id, a.cap.obligationId, C.type, ct.toString(), tx, false);
      give(coin);
      left = whole ? left - a.amount : 0;
    }
    if (pos.ctokens > 0 && left > 0.000001) {      // Kivy's first version held plain cTokens
      const ct = left >= fb(pos.ctokens) * pos.rate - 0.000001 ? pos.ctokens
               : Math.min(pos.ctokens, Math.ceil(Number(tb(left)) / pos.rate));
      const ctCoin = tx.add(coinWithBalance({ type: ctokenType(m, C.type), balance: BigInt(ct) }));
      const none = tx.moveCall({ target: '0x1::option::none',
        typeArguments: [`${slPkg()}::lending_market::RateLimiterExemption<${m.type}, ${C.type}>`] });
      const usdc = tx.moveCall({ target: `${slPkg()}::lending_market::redeem_ctokens_and_withdraw_liquidity`,
        typeArguments: [m.type!, C.type], arguments: [market, tx.pure.u64(idx), tx.object.clock(), ctCoin, none] });
      give(usdc);
    }
    if (all) amount = total;
  }
  return amount;
}

// every coin a Suilend market lists (reserves + reward coins), each passed to add() — knownCoins() in lending.ts
export async function suilendKnownCoins(m: Market, add: (sym: any, type: any, dec: any, icon?: any) => void) {
  const { init } = await suilend(m);
  for (const r of Object.values(init.reserveMap) as any[]) add(r.token?.symbol ?? r.symbol, r.coinType, r.mintDecimals, r.token?.iconUrl);
  for (const [t, md] of Object.entries(init.rewardCoinMetadataMap ?? {}) as any[]) add(md?.symbol, t, md?.decimals, md?.iconUrl);
}

// every coin this wallet has lent on a Suilend market, pushed to out (USDC with its old cTokens) — positionsAll()
export async function suilendPositionsAll(m: Market, addr: string, label: (t: string) => any, out: any[]) {
  const { sl, init } = await suilend(m);
  const seen = new Set<string>(), sum = new Map<string, any>();
  for (const { ob } of await accounts(m, addr)) {
    for (const d of ob?.deposits ?? []) {
      const r: any = (Object.values(init.reserveMap) as any[]).find((x) => BigInt(x.arrayIndex) === BigInt(d.reserveArrayIndex));
      if (!r) continue;
      const amt = Number(d.depositedCtokenAmount) / 10 ** Number(r.mintDecimals) * Number(r.cTokenExchangeRate);
      const lb = label(r.coinType) ?? { asset: r.token?.symbol ?? r.symbol, token: r.token?.symbol ?? r.symbol };
      seen.add(normType(r.coinType));
      if (normType(r.coinType) === normType(USDC)) continue;          // USDC: counted below with its old cTokens
      if (amt > 0) { const t = normType(r.coinType), k = sum.get(t); if (k) k.amount += amt; else sum.set(t, { market: m.key, type: t, ...lb, amount: amt }); }
    }
  }
  out.push(...sum.values());
  const p: any = await suilendPosition(m, addr);                           // USDC, incl. Kivy's first-version cTokens
  if (p.usdc > 0) out.push({ market: m.key, type: normType(USDC), asset: 'USDC', token: 'USDC', amount: p.usdc });
}

// claim this wallet's listed rewards on a Suilend market: one claim per account — buildClaim() in lending.ts
export async function suilendClaim(tx: Transaction, m: Market, mine: Reward[], addr: string) {
  const { sl } = await suilend(m);
  for (const cap of [...new Set(mine.map((r) => r.raw.cap))])       // one claim per account
    sl.claimRewardsAndSendToUser(addr, cap, mine.filter((r) => r.raw.cap === cap).map((r) => ({ reserveArrayIndex: r.raw.reserveArrayIndex,
      rewardIndex: r.raw.rewardIndex, rewardCoinType: r.raw.rewardCoinType, side: r.raw.side })), tx);
}

// a Suilend market's rows for /rates-all: the coin versions (vSUI, LBTC…, + their staking yield) and Kivy's coins,
// pushed to rows — ratesAll() in lending.ts
export async function suilendRateRows(m: Market, assets: { sym: string; type: string }[], stake: Record<string, number>, rows: any[]) {
  const { init, rm } = await suilend(m);
  const byType = new Map(Object.values(init.reserveMap).map((r: any) => [normType(r.coinType), r]));
  const seen = new Set<string>();
  for (const r of Object.values(init.reserveMap) as any[]) {             // other versions (vSUI, LBTC…), shown under their coin
    const sym = r.token?.symbol ?? r.symbol; if (!VARIANT_OF[sym] || seen.has(sym)) continue; seen.add(sym);
    let rw: any[] = [];
    try { rw = getFilteredRewards(rm?.[r.coinType]?.deposit ?? []); } catch { /* base rate only */ }
    const st = stake[normType(r.coinType)] ?? 0;                       // + the token's own staking yield
    rows.push({ market: m.key, name: m.name, protocol: 'suilend', asset: VARIANT_OF[sym], token: sym, icon: r.token?.iconUrl ?? null,
                type: normType(r.coinType), dec: Number(r.mintDecimals),
                staking_apr: st || undefined, total_apr: Number(getTotalAprPercent(Side.DEPOSIT, r.depositAprPercent, rw)) + st });
  }
  for (const a of assets) {
    const r: any = byType.get(a.type); if (!r) continue;
    let rw: any[] = [];
    try { rw = getFilteredRewards((rm?.[r.coinType] ?? rm?.[a.type])?.deposit ?? []); } catch { /* base rate only */ }
    rows.push({ market: m.key, name: m.name, protocol: 'suilend', asset: a.sym, type: a.type, dec: Number(r.mintDecimals), total_apr: Number(getTotalAprPercent(Side.DEPOSIT, r.depositAprPercent, rw)) });
  }
}

// what borrowers post as collateral on a Suilend market (coins with LTV > 0), into out[m.key] — collateralAll()
export async function suilendCollateral(m: Market, out: Record<string, any>, num: (x: any) => number) {
  const { init } = await suilend(m);
  out[m.key] = { coins: (Object.values(init.reserveMap) as any[]).filter((r) => num(r.config?.openLtvPct) > 0).map((r) => ({
    sym: r.token?.symbol ?? r.symbol, type: normType(r.coinType), icon: webIcon(r.token?.iconUrl ?? r.iconUrl),
    ltv: num(r.config.openLtvPct), liq: num(r.config.closeLtvPct), usd: Math.round(num(r.depositedAmountUsd)) })) };
}
