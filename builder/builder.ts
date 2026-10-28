/**
 * Kivy lending builder — USDC on six markets, and every other coin each market lists (2026-10-20):
 *
 *   suilend             Suilend main market          (lending, via cTokens)
 *   suilend-ember       Suilend Ember market         (lending, via cTokens)
 *   suilend-matrixdock  Suilend Matrixdock market    (lending, via cTokens)
 *   navi                NAVI main market             (lending)
 *   navi-highyield      NAVI USDC High Yield         (vault, curated by NAVI)
 *   navi-prime          NAVI USDC Prime              (vault, curated by NAVI)
 *
 *   rates                                   yearly rate of each market
 *   positions <address>                     what this wallet has in each
 *   build <address> <market> <deposit|withdraw> <amount|all> [coin]
 *                                           coin = a Kivy coin (SUI, BTC…) or a listed version (vSUI, USDsui…); default USDC
 *   swaplend <address> <market> <from> <to> <amount>
 *                                           swap <amount> of <from> into <to> and lend it, in ONE transaction
 *   positions-all <address>                 every coin this wallet has lent, per market
 *                                           an UNSIGNED, fully built transaction (JSON) for the
 *                                           user's own wallet to sign, simulated first
 *   serve                                   the same over http on 127.0.0.1:8799 (markets kept loaded)
 *
 * Kivy never holds a key: this only reads the chain and builds transactions. The input
 * coin comes from coinWithBalance, so USDC in Sui's address balance (where free transfers
 * land) works as well as ordinary coin objects.
 *
 * Suilend: deposits go into an obligation (a Suilend account — ONE PER COIN since 2026-10-20, see accounts()) so the reward
 * tokens accrue — on Ember and Matrixdock they are a third to two thirds of the yield. A
 * withdrawal must refresh the price of every reserve in that account; Kivy does it from the
 * Pyth price already on chain (no paid Hermes update). USDC's on-chain price is refreshed by
 * others every ~5-15 s, so a USDC-only account always works. If the account also holds coins
 * whose on-chain price is stale, the simulation fails and the user is sent to the Suilend app.
 * Older cToken holdings (Kivy's first version) can still be redeemed.
 */
/*
 * ── HOW KIVY RUNS THIS FILE ────────────────────────────────────────────────────────────────────────────────────
 * Service kivy-lend: `tsx builder.ts serve` -> HTTP on localhost:8799 only. Its one caller is Kivy's web server (server.py,
 * service kivy-site): lend_cli() and grpc_backup() send GET /<cmd>?a=…&a=…, lend_post() sends JSON POSTs. Every answer is
 * JSON with HTTP 200: {ok: true, …} or {ok: false, error}. `tsx builder.ts <cmd> <args…>` runs one command and prints the same JSON.
 *   in : a wallet address + what the user asked (market, coin, amount, chain of steps, perps order)
 *   out: an UNSIGNED transaction (tx JSON; a plain swap's backup comes as a base64 kind), already simulated, plus the plan
 *        numbers (expected / minimum out, fees, gas) server.py turns into a card. Reads return plain data.
 *   links: chain.ts (Sui gRPC with failover) · merge.ts (joins two transaction kinds) · ../knowledge/kivy.json (coins, fees,
 *          slippage tiers, route blocklist) · kivy_pkgs.json (package allowlist) · perps_markets.json (verified perps
 *          markets, kept by tools/sync_perps_markets.py) · SDKs: Suilend, NAVI lending + vault, Cetus aggregator,
 *          Bluefin 7k · HTTP: Aftermath router / perpetuals / price-info, Suilend SpringSui APY, Sui GraphQL (coin metadata)
 *
 * ── ONE REQUEST (example: swap then lend) ──────────────────────────────────────────────────────────────────────
 *
 *   page ──POST /api/tx/swaplend──> server.py (kivy-site)
 *                                      │ lend_cli('swaplend', address, market, from, to, amount)
 *                                      v
 *                      GET localhost:8799/swaplend?a=…        (chains, dust, multi-send: POST via lend_post)
 *                                      │ HTTP server [S17] -> handle() [S16] -> routed() [S5]
 *                                      v
 *                      buildSwapLend() [S6]: routeQuote -> coinWithBalance -> routeTrade -> depositCoin
 *                                      │ Aftermath router, else Cetus, else Bluefin 7k; Suilend / NAVI SDKs
 *                                      v
 *                      simulate() [S4]: dry run on Sui through chain.ts, set the gas budget
 *                                      │ fails -> {ok:false, error} (a route failure tries the next router first)
 *                                      v
 *                      {ok:true, tx: <unsigned tx JSON>, expected_out, min_out, …}
 *                                      │
 *                                      v
 *   server.py adds plan + guard ──> page: check 1 (simulate + compare) ──> wallet signs ──> Sui
 *                               ──> later GET /tx-facts [S15] = check 2 (what the signed transaction really did)
 *
 * ── ENDPOINTS -> CALLER IN server.py ──────────────────────────────────────────────────────────────────────────
 *   GET /rates lend_rates() · /rates-all lend_rates_all() · /collateral lend_collateral() · /positions-all
 *       lend_positions_all() · /rewards defi_positions(), /api/lend/rewards · /known-coins ecoins() · /tx-facts check_tx()
 *   GET /grpc-balances balances() and /grpc-name resolve_recipient(), both via grpc_backup() when Sui GraphQL fails
 *   GET /tatum-balances, /chain-stats: health checks (tools/watchdog.py, tools/services_weekly.py); /positions: CLI
 *   GET /build /api/tx/lend (deposit | withdraw), claim_card() (claim) · /swaplend /api/tx/swaplend · /withdrawswap
 *       /api/tx/withdrawswap
 *   POST /compose /api/tx/compose · /dust dust_build() · /sendmany /api/tx/sendmany · /swapcheck + /swapalt swap_tx()
 *       · /kind-calls the allowlist check of a plain swap
 *   GET /perp-account perp_account() · /perp-params perp_params() · /perp-preview /api/perp/preview, perp_confirm() ·
 *       /perp-positions /api/perp/positions + wallet / chat readers · /perp-history /api/perp/history
 *   GET /perp, /perp-order, /perp-limit, /perp-stop, /perp-scale, /perp-twap, /perp-cancel, /perp-close, /perp-margin,
 *       /perp-tpsl, /perp-tpsl-multi: /api/tx/perp (/perp-cancel also /api/perp/cancel-preview)
 *   POST /perp-pro-orders /api/perp/pro-orders
 *
 * ── SECTIONS (line ranges) ─────────────────────────────────────────────────────────────────────────────────────
 *   [S1]   101-152   SETUP: imports, the token / package constants, the lending markets Kivy supports, small unit helpers
 *   [S2]   153-226   MARKET DATA: Suilend markets and NAVI vaults loaded through their SDKs (cached 60 s); rate() = one market's USDC yearly %
 *   [S3]   227-352   POSITIONS: what a wallet has lent: NAVI reads (15 s), Suilend accounts (one per coin), shared reads, position()
 *   [S4]   353-519   SIMULATE + PLAIN LEND / WITHDRAW: simulate() (dry run + gas budget), depositCoin() (into Suilend / NAVI / a vault), build() (GET /build)
 *   [S5]   520-737   SWAP ROUTERS: Aftermath router, then Cetus, then Bluefin 7k (routed / routeQuote / routeTrade), Kivy's swap + lend fees, allowlist
 *   [S6]   738-1064  MULTI-STEP BUILDS: swap-then-lend (/swaplend), withdraw-then-swap or move (/withdrawswap), any chain of steps (POST /compose)
 *   [S7]  1065-1163  DUST SWEEP: POST /dust: every coin worth under $0.01 swapped into one coin, one transaction
 *   [S8]  1164-1212  MULTI-SEND: POST /sendmany: several coins to several addresses, from the wallet or withdrawn from lending
 *   [S9]  1213-1380  COIN LIST, ALL POSITIONS, REWARDS: /known-coins, /positions-all, /rewards and the claim transaction (build … claim)
 *   [S10] 1381-1556  PERPS ACCOUNT + GUARD: Aftermath perpetuals API, the perps check against the chain (perpGuard), open / deposit / withdraw (GET /perp)
 *   [S11] 1557-1634  PERPS MARKET ORDERS: market list, order preview (/perp-preview), market order with optional SL / TP (/perp-order)
 *   [S12] 1635-1886  PERPS PRO ORDERS: limit, stop, scale, TWAP, cancel, close, take-profit / stop-loss — all GET /perp-* and joined into one transaction
 *   [S13] 1887-2012  PERPS READS: stop / TWAP order list (POST /perp-pro-orders), positions (/perp-positions), add margin (/perp-margin), history
 *   [S14] 2013-2186  RATES, COLLATERAL, COIN LOOKUP: /rates-all (every coin on every market), /collateral, slippage tiers, coinOf() (symbol -> type + decimals)
 *   [S15] 2187-2219  RETRY + CHECK-2 FACTS: staleRetry() for stale Suilend prices; txFacts() = what a signed transaction really did (GET /tx-facts)
 *   [S16] 2220-2307  COMMAND DISPATCHER: handle(cmd, args): maps every command / GET path to its function above
 *   [S17] 2308-2402  PROCESS ENTRY: `serve` = the HTTP service on localhost:8799 (POST routes, then GET -> handle); anything else = one CLI command
 */
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S1] SETUP
// imports, the token / package constants, the lending markets Kivy supports, small unit helpers
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
import { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import {
  SuilendClient, initializeSuilend, initializeObligations, initializeSuilendRewards, formatRewards,
  getFilteredRewards, getTotalAprPercent, Side, LENDING_MARKET_ID, LENDING_MARKET_TYPE,
} from '@suilend/sdk';
import * as navi from '@naviprotocol/lending';
import * as vault from '@naviprotocol/vault';
import { mergeKinds } from './merge.ts';
import { readFileSync, statSync } from 'node:fs';
import { normalizeStructTag, normalizeSuiAddress } from '@mysten/sui/utils';
import * as CET from '@cetusprotocol/aggregator-sdk';
import * as SK from '@bluefin-exchange/bluefin7k-aggregator-sdk';
// the Cetus SDK carries its own copy of the Sui library: its transactions are passed as bytes, never as objects
import { Transaction as CetusTx } from './node_modules/@cetusprotocol/aggregator-sdk/node_modules/@mysten/sui/dist/transactions/index.mjs';

const USDC = '0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC';
const DEC = 6;
const U64_MAX = 18446744073709551615n;
const SL_PKG = '0x59672975e15c58ffc9450f5236b6aa9fa2407d94b8081e634778aa01957d8c6f';
const CTOKEN_ORIGIN = '0xf95b06141ed4a174f239417323bde3f209b972f5930d8521ea38a52aff3a6ddf';

type Market = { key: string; name: string; kind: 'lending' | 'vault'; protocol: 'suilend' | 'navi'; id?: string; type?: string; vaultName?: string };
const MARKETS: Market[] = [
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
const SUI_VAULT_MARKETS: Market[] = [
  { key: 'navi-sui-yield', name: 'NAVI SUI High Yield', kind: 'vault', protocol: 'navi', vaultName: 'NAVI SUI High Yield' },
  { key: 'navi-sui-prime', name: 'NAVI SUI Prime', kind: 'vault', protocol: 'navi', vaultName: 'NAVI SUI Prime' },
];
const byKey = (k: string) => { const m = [...MARKETS, ...SUI_VAULT_MARKETS].find((x) => x.key === k); if (!m) throw new Error('unknown market ' + k); return m; };

const out = (o: any) => { process.stdout.write(JSON.stringify(o) + '\n'); };
// Mysten's fullnode first, Tatum's Sui gRPC when Mysten can't answer (chain.ts, 2026-10-24)
import { grpc, stats as chainStats, tatumOnly } from './chain.ts';
const toBase = (x: number) => BigInt(Math.round(x * 10 ** DEC));
const fromBase = (x: any) => Number(x) / 10 ** DEC;
const ctokenType = (m: Market, t = USDC) => `${CTOKEN_ORIGIN}::reserve::CToken<${m.type}, ${t}>`;

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S2] MARKET DATA
// Suilend markets and NAVI vaults loaded through their SDKs (cached 60 s); rate() = one market's USDC yearly %
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// Pyth's public Hermes now refuses keyless requests; supply rates and cToken values only need
// Suilend's on-chain cached prices, so reads never ask Hermes.
const NO_HERMES = {
  getLatestPriceFeeds: async () => undefined,
  getLatestPriceFeedsPartial: async (ids: string[]) => ({ feeds: [], missing: ids }),
};

// ── Suilend markets: heavy to load, kept for a minute each ──
const _sl = new Map<string, { at: number; v: Promise<{ sl: any; init: any; rewards: any[] }> }>();
// one Suilend market loaded through its SDK (client, reserves, reward APRs), shared for 60 s; used by rate, position,
// build, depositCoin, rewards, ratesAll, collateralAll and knownCoins
function suilend(m: Market) {
  const hit = _sl.get(m.key);
  if (hit && Date.now() - hit.at < 60_000) return hit.v;
  const v = (async () => {
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
  _sl.set(m.key, { at: Date.now(), v });
  v.catch(() => _sl.delete(m.key));
  return v;
}
// the market's reserve for coin t (default USDC); throws when the market does not list that coin
function usdcReserve(init: any, t = USDC) {
  const r = init.reserveMap[t] ?? Object.values(init.reserveMap).find((x: any) => normType(x.coinType) === normType(t));
  if (!r) throw new Error('this market does not list ' + (t === USDC ? 'USDC' : t.split('::').pop()));
  return r;
}

// ── NAVI vaults ──
let _vaults: { at: number; v: Promise<any[]> } | null = null;
function vaults() {
  if (!_vaults || Date.now() - _vaults.at > 60_000) {
    const v = vault.getVaults() as Promise<any[]>;
    _vaults = { at: Date.now(), v };
    v.catch(() => { _vaults = null; });
  }
  return _vaults.v;
}
// a NAVI vault market's vault object (from NAVI's vault SDK list, refreshed every 60 s)
async function vaultOf(m: Market) {
  const v = (await vaults()).find((x: any) => x.name === m.vaultName);
  if (!v) throw new Error(m.name + ' is not listed by NAVI right now');
  return v;
}

// ── rates (yearly %, as each app shows it) ──
async function rate(m: Market) {
  if (m.protocol === 'suilend') {
    const { init, rewards } = await suilend(m);
    const r: any = usdcReserve(init);
    return Number(getTotalAprPercent(Side.DEPOSIT, r.depositAprPercent, rewards));   // base + rewards, as Suilend's app shows
  }
  if (m.kind === 'lending') {
    const p: any = await navi.getPool(USDC, { cacheTime: 60_000 } as any);
    const i = p.supplyIncentiveApyInfo || {};
    return Number(i.apy ?? 0) || Number(i.vaultApr ?? 0);
  }
  const v: any = await vaultOf(m);
  return Number(v.apy?.instant ?? v.apy?.target ?? 0) * 100;   // the rate NAVI's app shows
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S3] POSITIONS
// what a wallet has lent: NAVI reads (15 s), Suilend accounts (one per coin), shared reads, position()
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── positions (USDC) ──
// NAVI's lending library caches results FOREVER unless told otherwise, and this service runs non-stop — so a new
// deposit never showed up (the owner's WBTC on NAVI, 2026-10-20). Positions are read fresh and kept 15 s at most.
const _naviPos = new Map<string, { at: number; v: Promise<any[]> }>();
const _vaultPos = new Map<string, { at: number; v: Promise<any[]> }>();
function naviPositions(addr: string) {
  const h = _naviPos.get(addr);
  if (h && Date.now() - h.at < 15_000) return h.v;
  const v = navi.getLendingPositions(addr, { disableCache: true } as any) as any as Promise<any[]>;
  _naviPos.set(addr, { at: Date.now(), v }); v.catch(() => _naviPos.delete(addr));
  return v;
}
function vaultPositions(addr: string) {
  const h = _vaultPos.get(addr);
  if (h && Date.now() - h.at < 15_000) return h.v;
  const v = vault.getPositions(addr, { disableCache: true } as any) as any as Promise<any[]>;
  _vaultPos.set(addr, { at: Date.now(), v }); v.catch(() => _vaultPos.delete(addr));
  return v;
}
// ── Suilend accounts: ONE PER COIN (2026-10-20) ──
// A Suilend withdrawal must re-price every coin in the account it comes from; one stale price blocks it all.
// So each coin gets its own account: new deposits go to an account holding only that coin (or an empty one, or a
// new one), and a withdrawal touches only the accounts holding that coin. Older mixed accounts keep working.
// One position read shares its chain lookups (2026-10-24, scale): inside a 'positions' / 'positions-all' request each
// Suilend market's accounts are fetched once and all coin balances come from one call, instead of once per use.
// Builds run outside this and always read fresh.
import { AsyncLocalStorage } from 'node:async_hooks';
const readCtx = new AsyncLocalStorage<Map<string, Promise<any>>>();
// inside a sharedRead() request: run each keyed lookup once and hand every caller the same promise
function shared<T>(key: string, f: () => Promise<T>): Promise<T> {
  const ctx = readCtx.getStore();
  if (!ctx) return f();
  if (!ctx.has(key)) ctx.set(key, f());
  return ctx.get(key)!;
}
const sharedRead = <T>(f: () => Promise<T>) => readCtx.run(new Map(), f);
// the wallet's coin balances in one call (cTokens included), only inside a shared read; otherwise a single fresh balance
async function coinBalance(addr: string, type: string): Promise<number> {
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

// the wallet's Suilend accounts on market m: from the shared batch inside a read, else fresh
function accounts(m: Market, addr: string) {
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
const obIdxs = (ob: any) => [...new Set([...(ob?.deposits ?? []), ...(ob?.borrows ?? [])].map((d: any) => BigInt(d.reserveArrayIndex)))];

// how much of coin T this wallet has lent on market m. For Suilend it also returns the accounts holding it
// (single-coin first) and the old cTokens, which build() withdraws from. Callers: build, positionsAll, the positions command
async function position(m: Market, addr: string, T = USDC, dec = DEC) {
  const fromB = (x: any) => Number(x) / 10 ** dec;
  if (m.protocol === 'suilend') {
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
  if (m.kind === 'lending') {
    const ps = await naviPositions(addr);
    let usdc = 0;
    for (const p of ps ?? []) {
      const s = p['navi-lending-supply'] ?? p['navi-lending-emode-supply'];   // E-mode deposits count too
      if (s && navi.normalizeCoinType(s.token?.coinType ?? s.pool?.coinType ?? s.pool?.suiCoinType ?? '') === navi.normalizeCoinType(T)) usdc += Number(s.amount ?? 0);
    }
    return { usdc };
  }
  
  const v: any = await vaultOf(m);
  const p: any = (await vaultPositions(addr)).find((x: any) => x.vaultId === v.id);
  return { usdc: Number(p?.tokenBalance ?? 0), shares: p?.shares ?? '0' };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S4] SIMULATE + PLAIN LEND / WITHDRAW
// simulate() (dry run + gas budget), depositCoin() (into Suilend / NAVI / a vault), build() (GET /build)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// dry-runs a built transaction on Sui (chain.ts gRPC) and throws if it would fail; then sets the gas budget and checks the
// wallet has the SUI for it. Every builder calls this before handing a transaction back. Returns the size in bytes.
async function simulate(tx: Transaction) {
  const bytes = await tx.build({ client: grpc as any });
  // effects must be asked for: without them Sui returns no status and no gas numbers (2026-10-20: the check and the
  // gas margin silently did nothing, and two NAVI moves ran out of gas)
  const sim: any = await (grpc as any).core.simulateTransaction({ transaction: bytes, include: { effects: true, balanceChanges: true } });
  const t = sim.Transaction ?? sim.FailedTransaction ?? sim.transaction ?? sim;
  const st = t?.effects?.status ?? t?.status;
  if (!st) throw new Error('simulation returned no result');
  if (st.success === false) throw new Error('simulation failed: ' + JSON.stringify(st.error ?? st).slice(0, 300));
  // gas budget: Sui needs the NET cost (computation + storage - refund) to fit. The library's own budget left only
  // ~0.0001 SUI spare, so when the real run cost a bit more than the dry run (route / price moved) it ran out of gas
  // (the owner's NAVI/Suilend moves, 2026-10-20). Now: 2x the net estimate + 0.02 SUI. Only the real cost is charged.
  const g = t?.effects?.gasUsed;
  if (g) {
    const comp = Number(g.computationCost ?? 0);
    const net = Math.max(comp + Number(g.storageCost ?? 0) - Number(g.storageRebate ?? 0), comp);
    (tx as any).__gas = { comp, storage: Number(g.storageCost ?? 0), rebate: Number(g.storageRebate ?? 0) };   // the real cost, for the dust sweep's sum
    (tx as any).__bal = t?.balanceChanges ?? [];                     // who gets what: the dust sweep checks it against the page's guard
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
  return bytes.length;
}

// Put a coin (already in the transaction) into a market. Shared by plain deposits and swap-then-lend.
async function depositCoin(tx: Transaction, m: Market, C: Coin, coin: any, addr: string, base?: bigint) {
  if (m.protocol === 'suilend') {
    const { sl, init } = await suilend(m);
    usdcReserve(init, C.type);                      // the market must list this coin
    const idx = BigInt(sl.findReserveArrayIndex(C.type));
    const all = await accounts(m, addr);
    const only = (a: any) => !(a.ob?.borrows ?? []).length && obIdxs(a.ob).every((i) => i === idx);
    const pick = all.find((a) => only(a) && obIdxs(a.ob).length) ?? all.find((a) => !obIdxs(a.ob).length);   // this coin's account, or an empty one
    if (pick) sl.deposit(coin, C.type, pick.cap.id, tx);
    else {                                          // no account for this coin yet: open one
      const cap = sl.createObligation(tx);
      sl.deposit(coin, C.type, cap, tx);
      tx.transferObjects([cap], addr);
    }
  } else if (m.kind === 'lending') {
    await navi.depositCoinPTB(tx, C.type, coin, base != null ? { amount: Number(base) } : {});   // no amount: the whole coin
  } else {
    const v: any = await vaultOf(m);
    const vt = v.assets?.baseCoin?.coinType;
    if (!vt || normType(vt) !== normType(C.type)) throw new Error(`${m.name} takes ${v.assets?.baseCoin?.symbol ?? 'another coin'}, not ${C.sym}`);
    const value = tx.moveCall({ target: '0x2::coin::value', typeArguments: [C.type], arguments: [coin] });
    await vault.depositPTB(tx, v, addr, value as any, { coin } as any);
  }
}

// keep: withdraw into the transaction (for withdraw-then-swap) instead of sending the coin to the wallet
// GET /build — plain lend (deposit) or withdraw of one coin on one market. Caller: server.py /api/tx/lend (lend_cli).
// Also the withdrawal step inside withdrawswap, compose and sendmany (keep / into).
async function build(addr: string, key: string, action: string, amountArg: string, sym = 'USDC', keep?: any[], into?: Transaction) {
  const m = byKey(key);
  const C = await coinOf(key, sym);
  const tb = (x: number) => BigInt(Math.round(x * 10 ** C.dec)), fb = (x: any) => Number(x) / 10 ** C.dec;
  const tx = into ?? new Transaction();             // into: add this withdrawal to a transaction being built (multi-send)
  if (!into) tx.setSender(addr);
  const give = (c: any) => { if (keep) keep.push(c); else tx.transferObjects([c], addr); };
  let amount = Number(amountArg);
  const all = amountArg === 'all';
  if (action === 'deposit' && !(amount > 0)) throw new Error('Enter an amount to lend');
  if (m.protocol === 'suilend') {
    const { sl } = await suilend(m);
    const idx = sl.findReserveArrayIndex(C.type);
    const market = tx.object(m.id!);
    const pos: any = await position(m, addr, C.type, C.dec);
    if (action === 'deposit') {
      const coin = tx.add(coinWithBalance({ type: C.type, balance: tb(amount) }));
      takeLendFee(tx, coin, tb(amount));
      await depositCoin(tx, m, C, coin, addr);
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
          typeArguments: [`${SL_PKG}::lending_market::RateLimiterExemption<${m.type}, ${C.type}>`] });
        const usdc = tx.moveCall({ target: `${SL_PKG}::lending_market::redeem_ctokens_and_withdraw_liquidity`,
          typeArguments: [m.type!, C.type], arguments: [market, tx.pure.u64(idx), tx.object.clock(), ctCoin, none] });
        give(usdc);
      }
      if (all) amount = total;
    }
  } else if (m.kind === 'lending') {
    if (action === 'deposit') {
      const coin = tx.add(coinWithBalance({ type: C.type, balance: tb(amount) }));
      const fee = takeLendFee(tx, coin, tb(amount));
      await depositCoin(tx, m, C, coin, addr, fee ? undefined : tb(amount));      // with a fee, NAVI reads the coin's value
    } else {
      const pos = await position(m, addr, C.type, C.dec);
      if (pos.usdc <= 0) throw new Error(`No ${C.sym} lent on NAVI`);
      if (all) amount = pos.usdc;
      const pool = await navi.getPool(C.type, { cacheTime: 60_000 } as any);
      await navi.updateOraclePriceBeforeUserOperationPTB(tx, addr, [pool] as any);
      const coin: any = await navi.withdrawCoinPTB(tx, C.type, Number(tb(amount)));
      give(coin);
    }
  } else {
    const v: any = await vaultOf(m);
    if (normType(v.assets?.baseCoin?.coinType ?? '') !== normType(C.type)) throw new Error(`${m.name} takes ${v.assets?.baseCoin?.symbol ?? 'another coin'}, not ${C.sym}`);
    if (action === 'deposit' && lendFee()) {
      const coin = tx.add(coinWithBalance({ type: C.type, balance: tb(amount) }));
      takeLendFee(tx, coin, tb(amount));
      await depositCoin(tx, m, C, coin, addr);
    } else if (action === 'deposit') {
      await vault.depositPTB(tx, v, addr, String(amount), { useGasCoin: C.type === SUI_T } as any);
    } else {
      const pos = await position(m, addr, C.type, C.dec);
      if (pos.usdc <= 0) throw new Error(`No ${C.sym} in ${m.name}`);
      if (all) amount = pos.usdc;
      const w: any = await vault.withdrawPTB(tx, v, addr, all ? { kind: 'all' } as any : { kind: 'amount', amount: String(amount) } as any,
                                             keep ? { disableAutoTransfer: true } as any : undefined);
      if (keep) { if (!w?.coin) throw new Error(`${m.name} can't withdraw-and-swap in one go`); keep.push(w.coin); }
    }
  }
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
// [S5] SWAP ROUTERS
// Aftermath router, then Cetus, then Bluefin 7k (routed / routeQuote / routeTrade), Kivy's swap + lend fees, allowlist
// the plain swap card's check + backup (POST /swapcheck, /swapalt)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── swap then lend, one transaction (2026-10-20) ──
// Aftermath's router appends the swap to our transaction kind (v1/transactions/add-trade, from the coin Kivy
// built) and returns the output coin as a transaction argument; that coin goes straight into the market, so
// the user approves once and nothing is left over. If any step fails, nothing happens.
// Aftermath's router API: 'trade-route' quotes a swap, 'v1/transactions/add-trade' appends it to a transaction kind
async function afRouter(path: string, body: any) {
  const r = await fetch('https://aftermath.finance/api/router/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
                                                                         signal: AbortSignal.timeout(20_000) })   // a hung router moves on to the backups (review 2026-10-26)
    .catch((e: any) => { throw new Error('Aftermath router: ' + String(e?.message ?? e).slice(0, 120)); });
  const t = await r.text();
  let j: any; try { j = JSON.parse(t); } catch { throw new Error('Aftermath router: ' + t.slice(0, 160)); }
  if (!r.ok || typeof j === 'string' || j?.error) throw new Error('Aftermath router: ' + String(j?.error ?? j).slice(0, 200));
  return j;
}
// Kivy's own swap fee (knowledge/kivy.json 'revenue', read fresh each time so turning it on needs no restart).
// Off during testing (2026-10-20). Aftermath's router takes it out of the swap and pays the address.
function revenueFee(): { recipient: string; feePercentage: number } | null {
  try {
    const r: any = JSON.parse(readFileSync(process.env.KIVY_JSON_TEST || new URL('../knowledge/kivy.json', import.meta.url), 'utf8')).revenue ?? {};
    const pct = Number(r.swap_fee_pct);
    if (r.enabled === true && /^0x[0-9a-fA-F]{64}$/.test(String(r.address)) && pct > 0 && pct <= 1)
      return { recipient: String(r.address).toLowerCase(), feePercentage: pct / 100 };
  } catch { /* no file, bad json: no fee */ }
  return null;
}
// every route: Kivy's fee, and never through a protocol on the block list (Metastable is winding down — 2026-10-25)
function routeBlock(): string[] {
  try { const v = JSON.parse(readFileSync(process.env.KIVY_JSON_TEST || new URL('../knowledge/kivy.json', import.meta.url), 'utf8')).route_blocklist; return Array.isArray(v) ? v : []; }
  catch { return []; }
}
const withFee = (body: any) => { const f = revenueFee(), bl = routeBlock(); return { ...body, ...(f ? { externalFee: f } : {}), ...(bl.length ? { protocolBlacklist: bl } : {}) }; };

// ── swap routers (2026-10-26: "A, add bluefin agg too") ──
// Aftermath first. When its route fails (no route, or the test run aborts in the route — e.g. 1.3 USDC -> SUI aborted in
// router::assert_acceptable_slippage_on_amount_out while other amounts worked), the WHOLE build runs again with the Cetus
// aggregator, then with Bluefin 7k: same coins, same amount, same slippage, one approval. Left out of the backups: pools
// priced by Pyth (a paid feed now), Cetus Tide (order-book quotes that expire before the user approves) and blocked
// protocols. A backup route may only call programs on Kivy's allowlist (/opt/kivy/kivy_pkgs.json, the list the server and
// the page enforce): anything else and that router is skipped. Every swap site goes through routeQuote + routeTrade.
const ROUTERS = ['aftermath', 'cetus', 'bluefin7k'] as const;
const ROUTER_NAME: Record<string, string> = { aftermath: 'Aftermath', cetus: 'Cetus', bluefin7k: 'Bluefin 7k' };
const routerCtx = new AsyncLocalStorage<{ router: string; swapped: boolean; n?: number }>();
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
  if (stage === 'quote' && kind === 'quote') return Promise.reject(new Error(r === 'aftermath' ? 'Aftermath router: no route (test fault)' : `${who}: no route (test fault)`));
  if (stage === 'quote' && kind === 'hang') return within(new Promise<never>(() => {}), r === 'aftermath' ? 20_000 : 10_000, r === 'aftermath' ? 'Aftermath router' : who);
  if (stage === 'trade' && kind === 'abort') return Promise.reject(new Error(`simulation failed: MoveAbort in 41st command, abort code: 5, in '0x7de5de8d75a8f4e42cdd3c018f788bc9b9ebf2d3d61dcfe9d2136f17f077afd5::router::assert_acceptable_slippage_on_amount_out' (test fault, ${who})`));
  if (stage === 'trade' && kind === 'pkgs') return Promise.reject(new Error(`${who}: route calls 0xdeadbeef…::router, not on Kivy's list (test fault)`));
  return null;
}
// a failure of the swap route (worth another router), never a balance, amount or lending-protocol error
const ROUTE_FAIL = /Aftermath router|router returned no swap|Cetus: |Bluefin 7k: |not on Kivy's list|assert_acceptable_slippage|MoveAbort[^']*'0x[0-9a-f]+::(?:router|pool|swap|trade|amm|clmm|lb_pair|settle|cetus|bluefin|aftermath|turbos|kriya|flowx|momentum|magma|ferra|bolt)::|MoveAbort[^']*'0x62412b7268c35f3808336aee57a52836501f40b8ba5d936f8ad275e672befd04::/i;
async function routed<T>(f: () => Promise<T>, only?: string[]): Promise<T> {
  let first: any = null;
  const tried: string[] = [];
  // tests only: KIVY_ROUTERS=cetus (or bluefin7k, or a comma list) forces the order on a command-line run
  // tests only: KIVY_ROUTERS=cetus (or a comma list) on a command-line run, or the file state/router-test (same text) for a live
  // test through the page — it counts only for 10 minutes after it was written, then everyone is back on Aftermath first
  let testOrder = process.env.KIVY_ROUTERS ?? '';
  try { const f = new URL('./state/router-test', import.meta.url); if (Date.now() - statSync(f).mtimeMs < 600_000) testOrder = readFileSync(f, 'utf8').trim(); } catch { /* no test */ }
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
      if (!ctx.swapped || !ROUTE_FAIL.test(m)) throw e;  // not about the route: the same error on every router
      first ??= e; tried.push(ROUTER_NAME[r]);
      console.error(`route ${ROUTER_NAME[r]} failed:`, m.slice(0, 300));
    }
  }
  if (first) first.message = String(first.message) + ` (tried ${tried.join(', ')})`;
  throw first ?? new Error('No swap route');
}
let _pkgs = { at: 0, set: new Set<string>() };
// Kivy's package allowlist (kivy_pkgs.json, re-read every 30 s): the same list server.py and the page enforce
function allowedPkgs(): Set<string> {
  if (Date.now() - _pkgs.at > 30_000) {
    try {
      const list = JSON.parse(readFileSync(process.env.KIVY_PKGS_TEST || '/opt/kivy/kivy_pkgs.json', 'utf8')).packages as string[];   // KIVY_PKGS_TEST: tests only
      _pkgs = { at: Date.now(), set: new Set(list.map((x) => normalizeSuiAddress(x))) };
    } catch { _pkgs = { at: Date.now(), set: new Set() }; }
  }
  return _pkgs.set;
}
function checkPkgs(kind: Uint8Array, who: string) {             // a backup route calls only programs the page will sign
  const allow = allowedPkgs();
  for (const c of Transaction.fromKind(kind).getData().commands as any[]) {
    const pk = c.MoveCall?.package;
    if (pk && !allow.has(normalizeSuiAddress(pk))) throw new Error(`${who}: route calls ${normalizeSuiAddress(pk).slice(0, 10)}…::${c.MoveCall.module}, not on Kivy's list`);
  }
}
const asArg = (a: any) => a && !a.$kind ? (a.NestedResult ? { $kind: 'NestedResult', ...a } : a.Result != null ? { $kind: 'Result', ...a } : a.Input != null ? { $kind: 'Input', ...a } : a) : a;
const blockedHas = (name: string) => routeBlock().some((b) => String(b).toLowerCase().replace(/[^a-z0-9]/g, '') === name.toLowerCase().replace(/[^a-z0-9_]/g, '').replace(/_/g, ''));
const within = <T>(p: Promise<T>, ms: number, who: string) => Promise.race([p, new Promise<T>((_, no) => setTimeout(() => no(new Error(who + ': no answer in ' + ms / 1000 + ' s')), ms))]);
let _cetus: any = null;
const cetus = () => (_cetus ??= new CET.AggregatorClient({ env: CET.Env.Mainnet }));
let _skInit = false;
// a route for `body` ({coinInType, coinOutType, coinInAmount: "123n"}) from this build's router; coinOut.amount = expected out
async function routeQuote(body: any): Promise<any> {
  const ctx = routerCtx.getStore(); if (ctx) { ctx.swapped = true; ctx.n = (ctx.n ?? 0) + 1; }
  const r = ctx?.router ?? 'aftermath';
  const f = fault(r, 'quote'); if (f) await f;
  const amt = String(body.coinInAmount).replace('n', '');
  if (r === 'aftermath') return afRouter('trade-route', withFee(body));
  if (r === 'cetus') {
    const skip = [...CET.PYTH_ORACLE_BASED_DEXES, CET.CETUS_TIDE, CET.OBRIC, CET.SEVENK, CET.STEAMM_OMM, CET.STEAMM_OMM_V2, CET.METASTABLE];
    const providers = CET.getProvidersExcluding(skip).filter((p: string) => !blockedHas(p));
    let q: any;
    try { q = await within(cetus().findRouters({ from: body.coinInType, target: body.coinOutType, amount: amt, byAmountIn: true, providers }), 10_000, 'Cetus'); }
    catch (e: any) { throw new Error('Cetus: ' + String(e?.message ?? e).slice(0, 160)); }
    if (!q || q.error || q.insufficientLiquidity || !(BigInt(q.amountOut?.toString() ?? '0') > 0n)) throw new Error('Cetus: no route' + (q?.error ? ' (' + JSON.stringify(q.error).slice(0, 80) + ')' : ''));
    return { coinOut: { amount: q.amountOut.toString() + 'n' }, __router: 'cetus', __q: q };
  }
  if (!_skInit) { SK.Config.setSuiClient(grpc as any); _skInit = true; }
  const sources = (SK.DEFAULT_SOURCES as string[]).filter((s) => !SK.ORACLE_BASED_SOURCES.has(s as any) && !blockedHas(s));
  let q: any;
  try { q = await within(SK.getQuote({ tokenIn: body.coinInType, tokenOut: body.coinOutType, amountIn: amt, sources: sources as any }), 10_000, 'Bluefin 7k'); }
  catch (e: any) { throw new Error('Bluefin 7k: ' + String(e?.message ?? e).slice(0, 160)); }
  const out = BigInt(String(q?.returnAmountWithDecimal ?? '0').split('.')[0] || '0');
  if (!q?.routes?.length && !q?.swaps?.length || !(out > 0n)) throw new Error('Bluefin 7k: no route');
  return { coinOut: { amount: out.toString() + 'n' }, __router: 'bluefin7k', __q: q };
}
// append the swap of `body.completeRoute` to body.txKind (base64), from body.coinInId; returns {txKind (base64), coinOutId}
async function routeTrade(body: any): Promise<{ txKind: string; coinOutId: any }> {
  const q = body.completeRoute;
  const f = fault(q?.__router ?? 'aftermath', 'trade'); if (f) await f;
  if (!q?.__router) return afRouter('v1/transactions/add-trade', body);
  const kind = Buffer.from(String(body.txKind), 'base64');
  if (q.__router === 'cetus') {
    let out: any, bytes: Uint8Array;
    try {
      const t = CetusTx.fromKind(kind); t.setSender(body.walletAddress);
      out = await within(cetus().routerSwap({ router: q.__q, txb: t as any, inputCoin: asArg(body.coinInId), slippage: Number(body.slippage) }), 15_000, 'Cetus');
      bytes = await t.build({ client: cetus().client, onlyTransactionKind: true } as any);
    } catch (e: any) { throw new Error('Cetus: ' + String(e?.message ?? e).slice(0, 200)); }
    checkPkgs(bytes, 'Cetus');
    return { txKind: Buffer.from(bytes).toString('base64'), coinOutId: out };
  }
  let out: any, bytes: Uint8Array;
  try {
    const t0 = Transaction.fromKind(kind); t0.setSender(body.walletAddress);
    const r: any = await within(SK.buildTx({ quoteResponse: q.__q, accountAddress: body.walletAddress, slippage: Number(body.slippage),
                                      commission: { partner: body.walletAddress, commissionBps: 0 }, extendTx: { tx: t0 as any, coinIn: asArg(body.coinInId) } }), 15_000, 'Bluefin 7k');
    out = r.coinOut;
    bytes = await (r.tx as Transaction).build({ client: grpc as any, onlyTransactionKind: true });
  } catch (e: any) { throw new Error('Bluefin 7k: ' + String(e?.message ?? e).slice(0, 200)); }
  checkPkgs(bytes, 'Bluefin 7k');
  return { txKind: Buffer.from(bytes).toString('base64'), coinOutId: out };
}
// the lending-deposit fee (same switch + address): a fraction of the deposit coin, paid to the address, before it goes in
function lendFee(): { recipient: string; frac: number } | null {
  const f = revenueFee(); if (!f) return null;
  try {
    const p = Number(JSON.parse(readFileSync(process.env.KIVY_JSON_TEST || new URL('../knowledge/kivy.json', import.meta.url), 'utf8')).revenue?.lend_fee_pct);
    return p > 0 && p <= 1 ? { recipient: f.recipient, frac: p / 100 } : null;
  } catch { return null; }
}
function takeLendFee(tx: Transaction, coin: any, base: bigint) {   // base = what the coin holds (or at least holds)
  const f = lendFee(); if (!f) return 0n;
  const fee = BigInt(Math.floor(Number(base) * f.frac));
  if (fee <= 0n) return 0n;
  const [cut] = tx.splitCoins(coin, [fee]);
  tx.transferObjects([cut], f.recipient);
  return fee;
}
// ── the plain swap card's backup (2026-10-26) ──
// The server builds a plain swap with Aftermath's trade endpoint and never ran it. swapCheck runs that transaction for the
// wallet first; when the route fails, swapAlt builds the same swap (same coins, amount, slippage) with Cetus, then Bluefin 7k.
async function swapCheck(addr: string, kindB64: string) {
  const tx = Transaction.fromKind(Buffer.from(kindB64, 'base64')); tx.setSender(addr);
  try { await simulate(tx); return { ok: true }; }
  catch (e: any) {
    const m = String(e?.message ?? e);
    const swapAbort = /MoveAbort[^']*'0x[0-9a-f]+::(?:router|pool|swap|trade|amm|clmm)::|assert_acceptable_slippage/i.test(m);
    if (swapAbort) console.error('swap card route failed:', m.slice(0, 300));
    return { ok: false, route_fail: swapAbort, error: m.slice(0, 300) };
  }
}
async function swapAlt(addr: string, fromType: string, toType: string, base: bigint, slip: number, minOut = 0n) {
  return routed(async () => {
    const tx = new Transaction(); tx.setSender(addr);
    tx.add(coinWithBalance({ type: fromType, balance: base }));
    const resolved = JSON.parse(await tx.toJSON({ client: grpc as any }));
    const kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
    const route = await routeQuote({ coinInType: fromType, coinOutType: toType, coinInAmount: base.toString() + 'n' });
    const exp0 = BigInt(String(route.coinOut?.amount ?? '0').replace('n', ''));
    if (minOut > 0n) {                                   // the minimum the user approved is what the CHAIN enforces (audit nv #5):
      if (exp0 * 9999n / 10000n < minOut) throw new Error(`${ROUTER_NAME[routerCtx.getStore()?.router ?? 'cetus']}: price moved — this route gives less than the minimum you approved`);
      slip = Math.min(slip, Math.max(0.0001, 1 - (Number(minOut) + 2) / Number(exp0)));   // tighter, never looser; +2 units so rounding never lands under
    }
    const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route, slippage: slip, walletAddress: addr, coinInId: coinArg(resolved) });
    const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
    t2.transferObjects([r.coinOutId as any], addr);
    await simulate(t2);
    const exp = BigInt(String(route.coinOut?.amount ?? '0').replace('n', ''));
    return { ok: true, tx_kind: Buffer.from(await t2.build({ client: grpc as any, onlyTransactionKind: true })).toString('base64'),
             expected_base: exp.toString(), min_base: (exp * BigInt(Math.floor((1 - slip) * 1_000_000)) / 1_000_000n).toString() };
  }, ['cetus', 'bluefin7k']);
}
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S6] MULTI-STEP BUILDS
// swap-then-lend (/swaplend), withdraw-then-swap or move (/withdrawswap), any chain of steps (POST /compose)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// GET /swaplend — swap coin A into B and lend B on market `key`, one transaction. Caller: server.py /api/tx/swaplend.
// Runs inside routed(), so a failing route is retried on the next router.
async function buildSwapLend(addr: string, key: string, fromSym: string, toSym: string, amountArg: string) {
  const m = byKey(key);
  const A = await coinOf('', fromSym), B = await coinOf(key, toSym);
  if (normType(A.type) === normType(B.type)) throw new Error(`${A.sym} is already ${B.sym} — lend it directly`);
  const amount = Number(amountArg);
  if (!(amount > 0)) throw new Error('Enter an amount to swap');
  const base = BigInt(Math.round(amount * 10 ** A.dec));
  const route = await routeQuote(({ coinInType: A.type, coinOutType: B.type, coinInAmount: base.toString() + 'n' }));
  const tx = new Transaction(); tx.setSender(addr);
  tx.add(coinWithBalance({ type: A.type, balance: base }));
  const resolved = JSON.parse(await tx.toJSON({ client: grpc as any }));
  const kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
  const slip = slipFor(A.sym, B.sym);
  const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route,
                                                          slippage: slip, walletAddress: addr, coinInId: coinArg(resolved) });
  if (!r.txKind || !r.coinOutId) throw new Error('Aftermath router returned no swap');
  const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
  const minOut = BigInt(Math.floor(Number(String(route.coinOut?.amount ?? '0').replace('n', '')) * (1 - slip)));
  takeLendFee(t2, r.coinOutId, minOut);                 // on what the swap is sure to deliver
  await depositCoin(t2, m, B, r.coinOutId, addr);
  let size = 0;
  try { size = await simulate(t2); }
  catch (e: any) { throw new Error('This swap-then-lend would fail right now: ' + String(e?.message ?? e).slice(0, 160)); }
  const out = Number(String(route.coinOut?.amount ?? '0').replace('n', '')) / 10 ** B.dec;
  return { ok: true, market: key, fee_pct: (revenueFee()?.feePercentage ?? 0) * 100, from: A.sym, to: B.sym, from_type: A.type, from_decimals: A.dec, to_type: B.type, amount,
           expected_out: out, min_out: out * (1 - slip), tx: await t2.toJSON({ client: grpc as any }), bytes: size };
}

// ── withdraw, then swap what came out, one transaction (2026-10-20) ──
// The withdrawn coin(s) stay in the transaction; a slightly smaller piece (99.99%, so rounding and interest can
// never make it short) goes through Aftermath's router and the swapped coin, plus the tiny remainder, go to the wallet.
// lendTo (a market key): a MOVE — withdraw, swap if the coin changes, and lend it on that market, all one transaction
// (2026-10-20: "withdraw ssui from suilend and swap all of it into USDC then lend it in NAVI best yield market")
// GET /withdrawswap — caller: server.py /api/tx/withdrawswap. Uses build(…, keep) for the withdrawal, then a route.
async function buildWithdrawSwap(addr: string, key: string, sym: string, amountArg: string, toSym: string, lendTo?: string) {
  const keep: any[] = [];
  const w: any = await build(addr, key, 'withdraw', amountArg, sym, keep);
  const { tx, C } = w;
  const M2 = lendTo ? byKey(lendTo) : null;
  const T = await coinOf(lendTo ?? '', toSym);
  const same = normType(T.type) === normType(C.type);
  if (same && !M2) throw new Error(`That is already ${C.sym}: a plain withdraw does it`);
  if (same && M2 && M2.key === key) throw new Error(`That is where your ${C.sym} already is`);
  if (!keep.length) throw new Error('Nothing to withdraw');
  const coin = keep[0];
  if (keep.length > 1) tx.mergeCoins(coin, keep.slice(1));
  if (same && M2) {                                                  // same coin, other market: no swap
    takeLendFee(tx, coin, BigInt(Math.floor(Number(w.amount) * 10 ** C.dec)));
    await depositCoin(tx, M2, C, coin, addr);
    let size0 = 0;
    try { size0 = await simulate(tx); }
    catch (e: any) { throw new Error('This move would fail right now: ' + String(e?.message ?? e).slice(0, 160)); }
    return { ok: true, market: key, lend_to: M2.key, coin: C.sym, coin_type: C.type, to: C.sym, to_type: C.type, amount: w.amount,
             expected_out: w.amount, min_out: w.amount, tx: await tx.toJSON({ client: grpc as any }), bytes: size0 };
  }
  const base = BigInt(Math.floor(Number(w.amount) * 10 ** C.dec * 0.9999)) - 1n;
  if (base <= 0n) throw new Error('Too little to swap');
  const route = await routeQuote(({ coinInType: C.type, coinOutType: T.type, coinInAmount: base.toString() + 'n' }));
  const [piece] = tx.splitCoins(coin, [base]);
  tx.transferObjects([coin], addr);                                  // the remainder (dust) back to the wallet
  const resolved = JSON.parse(await tx.toJSON({ client: grpc as any }));
  const kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
  let at = -1;                                                       // our split = the last SplitCoins after intents resolve
  resolved.commands.forEach((c: any, i: number) => { if (c.SplitCoins) at = i; });
  if (at < 0) throw new Error('could not place the swap');
  void piece;
  const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route,
                                                          slippage: slipFor(C.sym, T.sym), walletAddress: addr, coinInId: { NestedResult: [at, 0] } });
  if (!r.txKind || !r.coinOutId) throw new Error('Aftermath router returned no swap');
  const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
  if (M2) {                                                          // move: the swapped coin goes straight into the new market
    takeLendFee(t2, r.coinOutId, BigInt(Math.floor(Number(String(route.coinOut?.amount ?? '0').replace('n', '')) * (1 - slipFor(C.sym, T.sym)))));
    await depositCoin(t2, M2, T, r.coinOutId, addr);
  } else t2.transferObjects([r.coinOutId as any], addr);
  let size = 0;
  try { size = await simulate(t2); }
  catch (e: any) {
    if (/unwrap_spot_price|price/i.test(String(e?.message)) && byKey(key).protocol === 'suilend')
      throw new Error(`Your ${byKey(key).name} account also holds coins whose price isn't fresh on chain right now — try again in a minute.`);
    throw new Error('This withdraw-and-swap would fail right now: ' + String(e?.message ?? e).slice(0, 160));
  }
  const out = Number(String(route.coinOut?.amount ?? '0').replace('n', '')) / 10 ** T.dec;
  return { ok: true, market: key, lend_to: M2?.key ?? null, coin: C.sym, coin_type: C.type, to: T.sym, to_type: T.type, amount: w.amount,
           expected_out: out, min_out: out * (1 - slipFor(C.sym, T.sym)), tx: await t2.toJSON({ client: grpc as any }), bytes: size };
}

// ── Compose: any chain of steps as ONE transaction (2026-10-20: "can kivy handle several complex steps") ──
// spec = { source: {op:'wallet', coin, amount} | {op:'withdraw', market, coin, amount: number|'all'},
//          swaps: [{to}] (at most 2),
//          sinks: [{op:'lend', market} | {op:'send', to: 0x-address} | {op:'keep'}, each with amount
//                  {frac} | {units} (in the coin's own units) | {rest: true}] }
// The coin "in hand" flows from step to step. Before each swap an exact piece is split off (any remainder goes back to
// the wallet); after a swap the coin is the router's output, and amounts are planned on its guaranteed minimum.
type Hand = { tx: Transaction; coin: any; C: Coin; est: bigint };   // est = base units the coin holds at least
// one swap of a chain: split the planned piece off the coin in hand, append the route, and return the router's output
// (planned on its guaranteed minimum) as the new coin in hand
async function swapInHand(h: Hand, B: Coin, addr: string): Promise<Hand & { exp: bigint }> {
  const base = h.est;
  if (base <= 0n) throw new Error('Too little to swap');
  const route = await routeQuote(({ coinInType: h.C.type, coinOutType: B.type, coinInAmount: base.toString() + 'n' }));
  const [piece] = h.tx.splitCoins(h.coin, [base]);
  void piece;
  h.tx.transferObjects([h.coin], addr);                              // remainder (dust / slippage surplus) back to the wallet
  const resolved = JSON.parse(await h.tx.toJSON({ client: grpc as any }));
  const kind = await h.tx.build({ client: grpc as any, onlyTransactionKind: true });
  let at = -1;
  resolved.commands.forEach((c: any, i: number) => { if (c.SplitCoins) at = i; });
  if (at < 0) throw new Error('could not place the swap');
  const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route,
                                                          slippage: slipFor(h.C.sym, B.sym), walletAddress: addr, coinInId: { NestedResult: [at, 0] } });
  if (!r.txKind || !r.coinOutId) throw new Error('Aftermath router returned no swap');
  const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
  const exp = BigInt(String(route.coinOut?.amount ?? '0').replace('n', ''));
  const min = exp * BigInt(Math.floor((1 - slipFor(h.C.sym, B.sym)) * 1_000_000)) / 1_000_000n;
  return { tx: t2, coin: r.coinOutId, C: B, est: min, exp };
}

// POST /compose — caller: server.py /api/tx/compose (lend_post). Source (wallet / withdraw / perps_out / dust) -> optional
// "plus" from the wallet -> a fan-out split OR up to 2 swaps -> 1-4 sinks (lend / send / keep / perps), then simulate.
async function buildCompose(addr: string, spec: any) {
  const src = spec?.source ?? {}, swaps: any[] = spec?.swaps ?? [], sinks: any[] = spec?.sinks ?? [];
  if (swaps.length > 2) throw new Error('At most 2 swaps in one transaction');
  if (!sinks.length || sinks.length > 4) throw new Error('A chain needs 1 to 4 final steps (lend / send / keep)');
  if (sinks.filter((k) => k.amount?.rest).length > 1) throw new Error('Only one step can take "the rest"');
  let h: Hand;
  const plan: any[] = [];
  if (src.op === 'wallet') {
    const C = await coinOf('', src.coin);
    const base = BigInt(Math.floor(Number(src.amount) * 10 ** C.dec));
    if (!(base > 0n)) throw new Error('Enter an amount to start with');
    const tx = new Transaction(); tx.setSender(addr);
    const coin = tx.add(coinWithBalance({ type: C.type, balance: base }));
    h = { tx, coin, C, est: base };
    plan.push({ op: 'wallet', coin: C.sym, amount: Number(base) / 10 ** C.dec });
  } else if (src.op === 'withdraw') {
    const keep: any[] = [];
    const w: any = await build(addr, src.market, 'withdraw', String(src.amount), src.coin, keep);
    plan.push({ op: 'withdraw', market: src.market, coin: w.C.sym, amount: Number(w.amount) });
    let total = Number(w.amount);
    // the same coin from more markets, into the SAME transaction ("withdraw USDC from both suilend and navi" — 2026-10-24)
    for (const x of (src.more ?? []).slice(0, 4)) {
      const w2: any = await build(addr, x.market, 'withdraw', String(x.amount), x.coin, keep, w.tx);
      if (normType(w2.C.type) !== normType(w.C.type)) throw new Error('Several withdrawals in one go must be the same coin');
      plan.push({ op: 'withdraw', market: x.market, coin: w2.C.sym, amount: Number(w2.amount) });
      total += Number(w2.amount);
    }
    if (!keep.length) throw new Error('Nothing to withdraw');
    const coin = keep[0];
    if (keep.length > 1) w.tx.mergeCoins(coin, keep.slice(1));
    h = { tx: w.tx, coin, C: w.C, est: BigInt(Math.floor(total * 10 ** w.C.dec * 0.9999)) - 1n };
  } else if (src.op === 'perps_out') {                                  // margin out of the Aftermath perps account (USDC)
    const acct: any = await needAccount(addr);
    const idle = await idleMarketKinds(acct);                            // margin parked on markets comes back first
    const free = Math.floor((Number(acct.unparked ?? 0) + idle.total) * 1e6) / 1e6;
    if (free <= 0) throw new Error('Your perps account has no free USDC to withdraw');
    const amount = src.amount === 'all' ? free : Number(src.amount);
    if (!(amount > 0)) throw new Error('Enter an amount to withdraw from perps');
    if (amount > free + 1e-9) throw new Error(`Your perps account has ${free} USDC free, not ${amount}`);
    const base = BigInt(Math.floor(amount * 1e6));
    // no recipient: Aftermath hands the coin back as a result, so the next step can use it
    const w: any = await af('account/transactions/withdraw-collateral', { accountId: acct.accountId, withdrawAmount: base.toString() + 'n' });
    let kind = idle.kinds[0] ?? w.txKind;
    for (const next of [...idle.kinds.slice(1), ...(idle.kinds.length ? [w.txKind] : [])]) {
      const m = await mergeKinds(kind, next, addr);
      kind = Buffer.from(await m.build({ client: grpc as any, onlyTransactionKind: true })).toString('base64');
    }
    const tx = Transaction.fromKind(kind); tx.setSender(addr);
    const cmds = tx.getData().commands;
    const at = cmds.map((c: any) => c.MoveCall?.function).lastIndexOf('withdraw_collateral');
    if (at < 0) throw new Error('Aftermath did not return a withdrawal');
    const C = await coinOf('', 'USDC');
    h = { tx, coin: { $kind: 'Result', Result: at } as any, C, est: base };
    plan.push({ op: 'perps_out', coin: 'USDC', amount });
  } else if (src.op === 'dust') {                                       // the dust sweep starts the chain (2026-10-25: "swap all my dust as USDC then deposit into suilend")
    const d: any = await buildDust(addr, String(src.coin), src.items ?? [], true);
    if (!d.hand) throw new Error('None of your dust can be swapped right now');
    const C = await coinOf('', String(src.coin));
    h = { tx: d.hand.tx, coin: d.hand.coin, C, est: d.hand.min };
    plan.push({ op: 'dust', coin: C.sym, amount: d.expected, min: d.min, legs: d.legs, skipped: d.skipped });
  } else throw new Error('Start with a coin from the wallet, a withdrawal or your dust');
  // the wallet's own coins of the same kind, merged in ("…including my remaining USDC in wallet" — 2026-10-21)
  if (src.plus != null && src.op !== 'wallet') {
    const add = BigInt(Math.floor(Number(src.plus) * 10 ** h.C.dec));
    if (!(add > 0n)) throw new Error('Nothing to add from the wallet');
    const extra = h.tx.add(coinWithBalance({ type: h.C.type, balance: add }));
    h.tx.mergeCoins(h.coin, [extra]);
    h = { ...h, est: h.est + add };
    plan.push({ op: 'plus', coin: h.C.sym, amount: Number(add) / 10 ** h.C.dec });
  }
  // fan-out (2026-10-24: "withdraw hasui from navi, then 25% each into USDC, USDsui, SUI and deep then deposit into suilend"):
  // every final step names its own coin ('to'). The coin in hand is cut into one piece per step in ONE split, each piece is
  // swapped on its own route, then lent or kept. Lend / keep only; the leftover of the split (dust) goes back to the wallet.
  if (sinks.some((k) => k.to && !/^0x/i.test(String(k.to)))) {                // a send's 'to' is an address, not a coin
    if (swaps.length) throw new Error('Split into several coins OR swap first, not both');
    if (sinks.some((k) => !['lend', 'keep'].includes(k.op))) throw new Error('Each coin of a split can be lent or kept');
    const E = h.est;
    const want = (k: any) => k.amount?.rest ? null : k.amount?.units != null ? BigInt(Math.floor(Number(k.amount.units) * 10 ** h.C.dec))
                           : k.amount?.frac != null ? BigInt(Math.floor(Number(E) * Number(k.amount.frac))) : null;
    const fixed = sinks.map(want).filter((x) => x != null) as bigint[];
    const sum = fixed.reduce((t, x) => t + x, 0n);
    if (sum > E) throw new Error(`Those amounts add up to more than the ${Number(E) / 10 ** h.C.dec} ${h.C.sym} you'd have`);
    const amts = sinks.map((k) => want(k) ?? (E - sum));
    if (amts.some((a) => a <= 0n)) throw new Error('One of the amounts is zero');
    h.tx.splitCoins(h.coin, amts);
    h.tx.transferObjects([h.coin], addr);                              // what the split leaves (dust) back to the wallet
    const resolved = JSON.parse(await h.tx.toJSON({ client: grpc as any }));
    let at = -1;
    resolved.commands.forEach((c: any, i: number) => { if (c.SplitCoins) at = i; });
    if (at < 0) throw new Error('could not place the split');
    let kind = await h.tx.build({ client: grpc as any, onlyTransactionKind: true });
    const legs: { coin: any; C: Coin; est: bigint }[] = [];
    for (let i = 0; i < sinks.length; i++) {
      const piece = { $kind: 'NestedResult', NestedResult: [at, i] } as any;
      const B = await coinOf('', sinks[i].to ?? h.C.sym);
      if (normType(B.type) === normType(h.C.type)) { legs.push({ coin: piece, C: h.C, est: amts[i] }); continue; }
      const route = await routeQuote(({ coinInType: h.C.type, coinOutType: B.type, coinInAmount: amts[i].toString() + 'n' }));
      const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route,
                                                              slippage: slipFor(h.C.sym, B.sym), walletAddress: addr, coinInId: { NestedResult: [at, i] } });
      if (!r.txKind || !r.coinOutId) throw new Error('Aftermath router returned no swap');
      kind = Buffer.from(r.txKind, 'base64');
      const exp = BigInt(String(route.coinOut?.amount ?? '0').replace('n', ''));
      const min = exp * BigInt(Math.floor((1 - slipFor(h.C.sym, B.sym)) * 1_000_000)) / 1_000_000n;
      plan.push({ op: 'swap', from: h.C.sym, to: B.sym, in: Number(amts[i]) / 10 ** h.C.dec, expected: Number(exp) / 10 ** B.dec, min: Number(min) / 10 ** B.dec });
      legs.push({ coin: r.coinOutId, C: B, est: min });
    }
    const tx = Transaction.fromKind(kind); tx.setSender(addr);
    for (let i = 0; i < sinks.length; i++) {
      const k = sinks[i], L = legs[i];
      if (k.op === 'lend') {
        const M = byKey(k.market);
        const C2 = await coinOf(k.market, L.C.sym).catch(() => L.C);
        if (normType(C2.type) !== normType(L.C.type)) throw new Error(`${M.name} doesn't take ${L.C.sym}`);
        takeLendFee(tx, L.coin, L.est);
        await depositCoin(tx, M, L.C, L.coin, addr);
      } else tx.transferObjects([L.coin], addr);
      plan.push({ op: k.op, market: k.market, coin: L.C.sym, type: L.C.type, dec: L.C.dec, amount: Number(L.est) / 10 ** L.C.dec, rest: false });
    }
    let size = 0;
    try { size = await simulate(tx); }
    catch (e: any) {
      if (/unwrap_spot_price|price/i.test(String(e?.message)) && src.op === 'withdraw' && byKey(src.market).protocol === 'suilend')
        throw new Error(`Your ${byKey(src.market).name} account also holds coins whose price isn't fresh on chain right now — try again in a minute.`);
      console.error('compose simulate failed:', String(e?.message ?? e).slice(0, 1500));   // the full abort, for whoever debugs it (2026-10-26)
    throw new Error('This chain would fail right now: ' + String(e?.message ?? e).slice(0, 160));
    }
    return { ok: true, compose: true, fanout: true, plan, final_coin: legs[0].C.sym, final_type: legs[0].C.type, final_dec: legs[0].C.dec,
             tx: await tx.toJSON({ client: grpc as any }), bytes: size };
  }
  for (const sw of swaps) {
    const B = await coinOf('', sw.to);
    if (normType(B.type) === normType(h.C.type)) throw new Error(`It is already ${B.sym}`);
    const r = await swapInHand(h, B, addr);
    plan.push({ op: 'swap', from: h.C.sym, to: B.sym, expected: Number(r.exp) / 10 ** B.dec, min: Number(r.est) / 10 ** B.dec });
    h = r;
  }
  // final steps: explicit pieces first, the "rest" (or the last step) takes whatever is left
  const E = h.est;
  const want = (k: any) => k.amount?.units != null ? BigInt(Math.floor(Number(k.amount.units) * 10 ** h.C.dec))
                         : k.amount?.frac != null ? BigInt(Math.floor(Number(E) * Number(k.amount.frac))) : null;
  const fixed = sinks.filter((k) => !k.amount?.rest && want(k) != null);
  const total = fixed.reduce((t, k) => t + (want(k) as bigint), 0n);
  if (total > E) throw new Error(`Those amounts add up to more than the ${Number(E) / 10 ** h.C.dec} ${h.C.sym} you'd have`);
  const restSink = sinks.find((k) => k.amount?.rest || want(k) == null) ?? null;
  // perps margin (Aftermath): USDC only, and it must be the step that takes the rest — Aftermath appends its deposit
  // to the transaction built so far, so it has to come last
  const perp = sinks.find((k) => k.op === 'perps');
  if (perp) {
    if (sinks.filter((k) => k.op === 'perps').length > 1) throw new Error('One perps deposit per transaction');
    if (perp !== restSink) throw new Error('Put the perps deposit last — it takes the rest');
    if (normType(h.C.type) !== normType(USDC)) throw new Error('Perps margin is USDC — swap to USDC first');
  }
  const sinkTo = async (k: any, coin: any, amt: bigint | null) => {
    if (k.op === 'lend') {
      const M = byKey(k.market);
      const C2 = await coinOf(k.market, h.C.sym).catch(() => h.C);
      if (normType(C2.type) !== normType(h.C.type)) throw new Error(`${M.name} doesn't take ${h.C.sym}`);
      takeLendFee(h.tx, coin, amt ?? E);
      await depositCoin(h.tx, M, h.C, coin, addr);
    } else if (k.op === 'send') {
      if (!/^0x[0-9a-fA-F]{64}$/.test(String(k.to))) throw new Error('Send needs a full 0x address');
      h.tx.transferObjects([coin], k.to);
    } else if (k.op === 'keep') {
      h.tx.transferObjects([coin], addr);
    } else throw new Error('Unknown final step ' + k.op);
    plan.push({ op: k.op, market: k.market, to: k.to, coin: h.C.sym, amount: amt != null ? Number(amt) / 10 ** h.C.dec : null, rest: amt == null });
  };
  for (const k of fixed) {
    const a = want(k) as bigint;
    if (a <= 0n) throw new Error('One of the amounts is zero');
    const [piece] = h.tx.splitCoins(h.coin, [a]);
    await sinkTo(k, piece, a);
  }
  if (perp) {                                                         // the rest goes into the perps account as margin
    const acct: any = await perpAccount(addr);
    if (!acct.has) throw new Error('Open an Aftermath perps account first (say "open perps account")');
    const amt = E - total;
    if (amt <= 0n) throw new Error('Nothing left for the perps deposit');
    h.tx.splitCoins(h.coin, [amt]);
    h.tx.transferObjects([h.coin], addr);                              // anything above the planned amount stays yours
    const resolved = JSON.parse(await h.tx.toJSON({ client: grpc as any }));
    const kind = await h.tx.build({ client: grpc as any, onlyTransactionKind: true });
    let at = -1;
    resolved.commands.forEach((c: any, i: number) => { if (c.SplitCoins) at = i; });
    const r = await af('account/transactions/deposit-collateral', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
      collateralCoinType: USDC, txKind: Buffer.from(kind).toString('base64'), depositCoinArg: { NestedResult: [at, 0] } });
    h.tx = Transaction.fromKind(r.txKind); h.tx.setSender(addr);
    plan.push({ op: 'perps', coin: 'USDC', amount: Number(amt) / 1e6, rest: true });
  } else if (restSink && fixed.indexOf(restSink) < 0) await sinkTo(restSink, h.coin, null);   // the rest: the coin itself
  else h.tx.transferObjects([h.coin], addr);                                          // nothing takes the rest: it stays yours
  let size = 0;
  try { size = await simulate(h.tx); }
  catch (e: any) {
    if (/unwrap_spot_price|price/i.test(String(e?.message)) && src.op === 'withdraw' && byKey(src.market).protocol === 'suilend')
      throw new Error(`Your ${byKey(src.market).name} account also holds coins whose price isn't fresh on chain right now — try again in a minute.`);
    console.error('compose simulate failed:', String(e?.message ?? e).slice(0, 1500));   // the full abort, for whoever debugs it (2026-10-26)
    throw new Error('This chain would fail right now: ' + String(e?.message ?? e).slice(0, 160));
  }
  return { ok: true, compose: true, plan, final_coin: h.C.sym, final_type: h.C.type, final_dec: h.C.dec,
           tx: await h.tx.toJSON({ client: grpc as any }), bytes: size };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S7] DUST SWEEP
// POST /dust: every coin worth under $0.01 swapped into one coin, one transaction
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── dust sweep (2026-10-25): the coins worth under $0.01 that the wallet hides, swapped into ONE coin in ONE transaction.
// Each coin's objects are merged and spent whole, and the emptied object is deleted, so Sui refunds its storage deposit.
// Every route is asked for first: a coin the router can't swap is left where it is, never half-built into the transaction. ──
// POST /dust — caller: server.py dust_build() (/api/tx/dust); also the "dust" source of a compose chain
async function buildDust(addr: string, toSym: string, items: any[], hand = false) {   // hand: also return the swept coin, unsent, for a chain
  const B = await coinOf('', toSym);
  const SUI_T = normType('0x2::sui::SUI');
  const legs: any[] = [], skipped: any[] = [];
  for (const it of (items ?? []).slice(0, 10)) {
    const t = normType(String(it.type ?? ''));
    if (!t || t === SUI_T || t === normType(B.type)) continue;
    const lc: any = await (grpc as any).core.listCoins({ owner: addr, coinType: t, limit: 50 });
    const objs = lc.objects ?? [];
    const total = objs.reduce((x: bigint, o: any) => x + BigInt(o.balance), 0n);
    if (total <= 0n) continue;
    try {
      const route = await routeQuote(({ coinInType: t, coinOutType: B.type, coinInAmount: total.toString() + 'n' }));
      const exp = BigInt(String(route?.coinOut?.amount ?? '0').replace('n', ''));
      if (exp <= 0n) { skipped.push({ sym: it.sym, why: 'too small for the swap router' }); continue; }
      legs.push({ sym: it.sym, type: t, dec: Number(it.dec), objs, total, route, exp });
    } catch { skipped.push({ sym: it.sym, why: 'no swap route' }); }
  }
  // a route can still abort on chain (a pool that can't take so little): the sweep goes on without that coin (up to 3 tries)
  for (let tries = 0; ; tries++) {
    if (!legs.length) return { ok: true, dust: true, to: B.sym, to_type: B.type, to_dec: B.dec, legs: [], skipped };
    try { const r: any = await dustTx(addr, B, legs, skipped); return hand ? { ...r, hand: await dustTx(addr, B, legs, skipped, true) } : r; }
    catch (e: any) {
      if (e?.badLeg != null && tries < 6) {                         // its route calls a program Kivy hasn't checked, or pays someone: the page would refuse it
        skipped.push({ sym: legs[e.badLeg].sym, why: e.why ?? 'its swap route uses a program Kivy hasn\'t verified yet' });
        legs.splice(e.badLeg, 1); continue;
      }
      const at = Number((String(e?.message ?? '').match(/in (\d+)(?:st|nd|rd|th) command/) ?? [])[1]);
      const bad = Number.isFinite(at) ? legs.findIndex((L) => at >= L.from && at < L.to) : -1;
      if (bad < 0 || tries >= 2) throw e;
      skipped.push({ sym: legs[bad].sym, why: 'its swap route fails right now' });
      legs.splice(bad, 1);
    }
  }
}

// the package allowlist for the dust sweep (kivy_pkgs.json, package ids only)
function dustAllow(): Set<string> {
  try { return new Set(JSON.parse(readFileSync('/opt/kivy/kivy_pkgs.json', 'utf8')).packages.map((p: string) => normType(p + '::a::b').split('::')[0])); }
  catch { return new Set(); }
}

// one sweep: merge each coin's objects, split all of it off, destroy the empty object, append its route, check every
// program against the allowlist, merge the outputs. A bad leg throws {badLeg} so buildDust drops that coin and retries.
async function dustTx(addr: string, B: Coin, legs: any[], skipped: any[], hand = false): Promise<any> {
  const tx = new Transaction(); tx.setSender(addr);
  for (const L of legs) {
    const ref = (o: any) => tx.objectRef({ objectId: o.objectId, version: o.version, digest: o.digest });
    const prim = ref(L.objs[0]);
    if (L.objs.length > 1) tx.mergeCoins(prim, L.objs.slice(1).map(ref));
    L.at = tx.getData().commands.length;
    tx.splitCoins(prim, [tx.pure.u64(L.total)]);
    tx.moveCall({ target: '0x2::coin::destroy_zero', typeArguments: [L.type], arguments: [prim] });   // the emptied object goes: its storage comes back
  }
  let kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
  const outs: any[] = [];
  let expT = 0n, minT = 0n;
  for (const L of legs) {
    const slip = slipFor(L.sym, B.sym);
    L.from = Transaction.fromKind(kind).getData().commands.length;        // this coin's swap commands, to name it if one aborts
    const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: L.route,
                                                            slippage: slip, walletAddress: addr, coinInId: { NestedResult: [L.at, 0] } });
    if (!r.txKind || !r.coinOutId) throw new Error('Aftermath router returned no swap for ' + L.sym);
    kind = Buffer.from(r.txKind, 'base64');
    L.to = Transaction.fromKind(kind).getData().commands.length;
    L.min = L.exp * BigInt(Math.floor((1 - slip) * 1_000_000)) / 1_000_000n;
    expT += L.exp; minT += L.min;
    outs.push(r.coinOutId);
  }
  const tx2 = Transaction.fromKind(kind); tx2.setSender(addr);
  const allow = dustAllow();                                          // the same list the page checks before signing (kivy_pkgs.json)
  if (allow.size) tx2.getData().commands.forEach((c: any, i: number) => {
    const pkg = c.MoveCall && normType(c.MoveCall.package + '::a::b').split('::')[0];
    if (pkg && !allow.has(pkg)) { const bad = legs.findIndex((L) => i >= L.from && i < L.to); if (bad >= 0) throw Object.assign(new Error('unverified program'), { badLeg: bad }); }
  });
  if (outs.length > 1) tx2.mergeCoins(outs[0], outs.slice(1));
  if (hand) return { tx: tx2, coin: outs[0], min: minT };              // a chain's next steps take the swept coin from here
  tx2.transferObjects([outs[0]], addr);
  let size = 0;
  try { size = await simulate(tx2); }
  catch (e: any) { throw new Error('This dust sweep would fail right now: ' + String(e?.message ?? e).slice(0, 160)); }
  // simulate()'s budget (net cost x2 + 0.02 SUI) is enough: Sui checks the NET cost, so a sweep whose storage charge is far
  // above its refund-adjusted cost still fits (tested 2026-10-25: 1.03 SUI gross storage ran on a 0.021 SUI budget)
  const G = (tx2 as any).__gas;
  return { ok: true, dust: true, to: B.sym, to_type: B.type, to_dec: B.dec, skipped,
           legs: legs.map((L) => ({ sym: L.sym, type: L.type, objects: L.objs.length, units: L.total.toString(), amount: Number(L.total) / 10 ** L.dec,
                                    expected: Number(L.exp) / 10 ** B.dec, min: Number(L.min) / 10 ** B.dec })),
           expected: Number(expT) / 10 ** B.dec, min: Number(minT) / 10 ** B.dec, min_units: minT.toString(),
           gas: G ?? null, tx: await tx2.toJSON({ client: grpc as any }), bytes: size };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S8] MULTI-SEND
// POST /sendmany: several coins to several addresses, from the wallet or withdrawn from lending
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── multi-send (2026-10-23): several coins to several addresses in ONE transaction. Each line comes from the
// wallet, or from a lending market (withdrawn here, then sent). Lines from the same market + coin share one withdrawal;
// the last line of a group takes what is left of it, so a tiny rounding gap on Suilend can't fail the transaction. ──
// POST /sendmany — caller: server.py /api/tx/sendmany (the Send card)
async function buildSendMany(addr: string, lines: any[]) {
  if (!Array.isArray(lines) || !lines.length || lines.length > 12) throw new Error('Send 1 to 12 transfers at a time');
  const tx = new Transaction(); tx.setSender(addr);
  const plan: any[] = [], groups = new Map<string, any[]>();
  for (const l of lines) {
    if (!/^0x[0-9a-f]{64}$/.test(String(l.to))) throw new Error('A recipient is not a full Sui address');
    if (!(Number(l.amount) > 0) && l.amount !== 'all') throw new Error('Every transfer needs an amount');
    if (l.source && l.source !== 'wallet') { const k = l.source + '|' + l.coin; groups.set(k, [...(groups.get(k) ?? []), l]); continue; }
    const C = await coinOf('', l.coin);
    const base = BigInt(Math.floor(Number(l.amount) * 10 ** C.dec));
    tx.transferObjects([tx.add(coinWithBalance({ type: C.type, balance: base }))], tx.pure.address(l.to));
    plan.push({ op: 'send', source: 'wallet', coin: C.sym, coin_type: C.type, dec: C.dec, amount: Number(base) / 10 ** C.dec, to: l.to });
  }
  for (const [k, ls] of groups) {
    const [market, coin] = k.split('|');
    const all = ls.length === 1 && ls[0].amount === 'all';
    const total = all ? 'all' : String(ls.reduce((t: number, x: any) => t + Number(x.amount), 0));
    const keep: any[] = [];
    const w: any = await build(addr, market, 'withdraw', total, coin, keep, tx);
    if (!keep.length) throw new Error('Nothing to withdraw from ' + byKey(market).name);
    const c0 = keep[0];
    if (keep.length > 1) tx.mergeCoins(c0, keep.slice(1));
    plan.push({ op: 'withdraw', market, name: byKey(market).name, coin: w.C.sym, amount: Number(w.amount) });
    ls.forEach((l: any, i: number) => {
      const last = i === ls.length - 1;
      const amt = all ? Number(w.amount) : Number(l.amount);
      const part = last ? c0 : tx.splitCoins(c0, [BigInt(Math.floor(amt * 10 ** w.C.dec))])[0];
      tx.transferObjects([part], tx.pure.address(l.to));
      plan.push({ op: 'send', source: market, coin: w.C.sym, coin_type: w.C.type, dec: w.C.dec, amount: amt, to: l.to, rest: last });
    });
  }
  let size = 0;
  try { size = await simulate(tx); }
  catch (e: any) {
    if (/unwrap_spot_price|price/i.test(String(e?.message)) && [...groups.keys()].some((g) => byKey(g.split('|')[0]).protocol === 'suilend'))
      throw new Error("A Suilend account here holds coins whose price isn't fresh on chain right now — try again in a minute.");
    throw new Error('This send would fail right now: ' + String(e?.message ?? e).slice(0, 160));
  }
  return { ok: true, sendmany: true, plan, tx: await tx.toJSON({ client: grpc as any }), bytes: size };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S9] COIN LIST, ALL POSITIONS, REWARDS
// /known-coins, /positions-all, /rewards and the claim transaction (build … claim)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── every coin the markets list (lending versions, NAVI/Suilend reserves, reward coins): Kivy can swap / send / show
// these, and nothing else — a protocol listing is the filter against airdropped junk tokens ──
let _known: { at: number; v: Promise<any[]> } | null = null;
// GET /known-coins — caller: server.py ecoins(). Also coinOf()'s last resort. Rebuilt once an hour.
function knownCached() {
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
  await Promise.all(MARKETS.filter((m) => m.protocol === 'suilend').map((m) => settle(async () => {
    const { init } = await suilend(m);
    for (const r of Object.values(init.reserveMap) as any[]) add(r.token?.symbol ?? r.symbol, r.coinType, r.mintDecimals, r.token?.iconUrl);
    for (const [t, md] of Object.entries(init.rewardCoinMetadataMap ?? {}) as any[]) add(md?.symbol, t, md?.decimals, md?.iconUrl);
  })));
  await settle(async () => {
    const ps: any = await (navi as any).getPools({ cacheTime: 60_000 });
    for (const p of (Array.isArray(ps) ? ps : Object.values(ps)) as any[]) add(p.token?.symbol, p.suiCoinType ?? p.coinType, p.token?.decimals, p.token?.logoUri);
  });
  await settle(async () => { add('NAVX', '0xa99b8952d4f7d947ea77fe0ecdcc9e5fc0bcab2841d6e2a5aa00c3044e5544b5::navx::NAVX', 9); });   // NAVI's reward coin
  return out;
}

// ── everything this wallet has lent, any coin (for the 'You lent' column and withdrawals) ──
// GET /positions-all — caller: server.py lend_positions_all(). Runs inside sharedRead() (see shared()).
async function positionsAll(addr: string) {
  const rows = await rowsAll();
  const label = (t: string) => { const r = rows.find((x) => x.type && normType(x.type) === normType(t)); return r ? { asset: r.asset, token: r.token ?? r.asset } : null; };
  const out: any[] = [];
  await Promise.all([...MARKETS, ...SUI_VAULT_MARKETS].map((m) => settle(async () => {
    if (m.protocol === 'suilend') {
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
      const p: any = await position(m, addr);                           // USDC, incl. Kivy's first-version cTokens
      if (p.usdc > 0) out.push({ market: m.key, type: normType(USDC), asset: 'USDC', token: 'USDC', amount: p.usdc });
    } else if (m.kind === 'lending') {
      for (const p of (await naviPositions(addr)) ?? []) {
        const s = p['navi-lending-supply'] ?? p['navi-lending-emode-supply'];   // E-mode deposits count too if (!s || !(Number(s.amount) > 0)) continue;
        const t = normType(s.token?.coinType ?? s.pool?.coinType ?? s.pool?.suiCoinType ?? '');
        const lb = label(t) ?? { asset: s.token?.symbol ?? t.split('::').pop(), token: s.token?.symbol ?? t.split('::').pop() };
        out.push({ market: m.key, type: t, ...lb, amount: Number(s.amount) });
      }
    } else {
      const p: any = await position(m, addr);
      const isSui = SUI_VAULT_MARKETS.includes(m);
      if (p.usdc > 0) out.push({ market: m.key, type: normType(isSui ? SUI_T : USDC), asset: isSui ? 'SUI' : 'USDC', token: isSui ? 'SUI' : 'USDC', amount: p.usdc });
    }
  })));
  return out;
}

// ── rewards: what each market has paid out that the user can claim ──
const _meta = new Map<string, Promise<{ symbol: string; decimals: number }>>();
// a coin's symbol + decimals from Sui GraphQL (kept for the life of the process)
function coinMeta(t: string) {
  if (!_meta.has(t)) _meta.set(t, (async () => {
    const q = { query: `{ coinMetadata(coinType: "${t}") { symbol decimals } }` };
    const r: any = await (await fetch('https://graphql.mainnet.sui.io/graphql', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(q) })).json();
    const m = r?.data?.coinMetadata ?? {};
    return { symbol: m.symbol ?? t.split('::').pop(), decimals: Number(m.decimals ?? 9) };
  })());
  return _meta.get(t)!;
}
// USD prices from Aftermath's price-info API, for rewards the protocol SDK did not price
async function usdPrices(types: string[]) {
  if (!types.length) return {} as Record<string, number>;
  try {
    const r: any = await (await fetch('https://aftermath.finance/api/price-info', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ coins: types }) })).json();
    return Object.fromEntries(types.map((t) => [t, Math.max(0, Number(r?.[t]?.price ?? 0))]));
  } catch { return {} as Record<string, number>; }
}
type Reward = { market: string; coinType: string; symbol: string; amount: number; usd: number; raw: any };

// claimable rewards, one reader per protocol: Suilend SDK (per account), NAVI lending, NAVI vaults
async function suilendRewards(m: Market, addr: string): Promise<Reward[]> {
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
async function naviRewards(addr: string): Promise<Reward[]> {
  const rw: any[] = ((await navi.getUserAvailableLendingRewards(addr, { disableCache: true } as any)) as any[]).filter((r) => Number(r.userClaimableReward) > 0);
  return Promise.all(rw.map(async (r) => {
    const t = navi.normalizeCoinType(r.rewardCoinType); const m = await coinMeta(t);
    return { market: 'navi', coinType: t, symbol: m.symbol, amount: Number(r.userClaimableReward), usd: 0, raw: r };
  }));
}
async function vaultRewards(m: Market, addr: string): Promise<Reward[]> {
  const v: any = await vaultOf(m);
  const rw: any[] = ((await vault.getVaultRewards(v, addr)) as any[]).filter((r) => BigInt(r.claimable ?? 0) > 0n);
  return Promise.all(rw.map(async (r) => {
    const t = navi.normalizeCoinType(r.rewardCoinType); const md = await coinMeta(t);
    return { market: m.key, coinType: t, symbol: md.symbol, amount: Number(BigInt(r.claimable)) / 10 ** md.decimals, usd: 0, raw: r };
  }));
}
// GET /rewards — callers: server.py defi_positions() and /api/lend/rewards. only = a market key, p-suilend / p-navi, or all
async function rewards(addr: string, only?: string) {
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
async function buildClaim(addr: string, only: string) {
  const rs = await rewards(addr, only);
  if (!rs.length) throw new Error('No rewards to claim right now');
  const tx = new Transaction();
  tx.setSender(addr);
  for (const m of [...MARKETS, ...SUI_VAULT_MARKETS]) {
    const mine = rs.filter((r) => r.market === m.key);
    if (!mine.length) continue;
    if (m.protocol === 'suilend') {
      const { sl } = await suilend(m);
      for (const cap of [...new Set(mine.map((r) => r.raw.cap))])       // one claim per account
        sl.claimRewardsAndSendToUser(addr, cap, mine.filter((r) => r.raw.cap === cap).map((r) => ({ reserveArrayIndex: r.raw.reserveArrayIndex,
          rewardIndex: r.raw.rewardIndex, rewardCoinType: r.raw.rewardCoinType, side: r.raw.side })), tx);
    } else if (m.kind === 'lending') {
      await navi.claimLendingRewardsPTB(tx, mine.map((r) => r.raw), { customCoinReceive: { type: 'transfer', transfer: addr } } as any);
    } else {
      await vault.claimRewardsPTB(tx, mine.map((r) => r.raw), addr);
    }
  }
  await simulate(tx);
  const usd = rs.reduce((a, r) => a + r.usd, 0);
  return { ok: true, market: only, action: 'claim', usd, rewards: rs.map(({ raw, ...r }) => r), gas: (tx as any).__gas ?? null,   // real gas: the card weighs it against the rewards
           tx: await tx.toJSON({ client: grpc as any }) };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S10] PERPS ACCOUNT + GUARD
// Aftermath perpetuals API, the perps check against the chain (perpGuard), open / deposit / withdraw (GET /perp)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── Aftermath perpetuals: the user's account and its USDC collateral ──
// Aftermath's API returns transaction *kinds*; we add sender + gas, simulate, and hand back
// the full transaction. Deposits use a coin Kivy builds (coinWithBalance) passed in as
// depositCoinArg, so USDC in the address balance works too.
const AF = 'https://aftermath.finance/api/perpetuals/';
// POST to Aftermath's perpetuals API (AF + path); returns its JSON or throws 'Aftermath: …'
async function af(path: string, body: any) {
  const r = await fetch(AF + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const t = await r.text();
  let j: any; try { j = JSON.parse(t); } catch { throw new Error('Aftermath: ' + t.slice(0, 160)); }
  if (!r.ok || j?.error || typeof j === 'string') throw new Error('Aftermath: ' + String(j?.error ?? j?.message ?? t).slice(0, 200));
  return j;
}
// The USDC coin Kivy built: coinWithBalance resolves to a SplitCoins (from coins, or from the address
// balance via redeem_funds; send_funds may follow to return change), so the coin is that split's output.
function coinArg(resolved: any) {
  for (let i = resolved.commands.length - 1; i >= 0; i--) if (resolved.commands[i].SplitCoins) return { NestedResult: [i, 0] };
  return { Result: resolved.commands.length - 1 };
}
// GET /perp-account — the wallet's USDC perps account: id, cap, free collateral. Caller: server.py perp_account().
// Every perps builder starts here.
async function perpAccount(addr: string) {
  const own: any = await af('accounts/owned', { walletAddress: addr });
  const a = (own.accountCaps ?? []).find((x: any) => String(x.collateralCoinType).endsWith('::usdc::USDC') && !x.isAgent && String(x.walletAddress ?? addr) === addr)
         ?? (own.accountCaps ?? []).find((x: any) => String(x.collateralCoinType).endsWith('::usdc::USDC') && !x.isAgent);
  if (!a) return { has: false, collateral: 0 };
  const unparked = fromBase(String(a.collateral).replace('n', ''));
  // what the user can use: Aftermath's available collateral (unparked + margin parked on markets but not in use)
  let available = unparked;
  try { available = Number((await af('accounts/positions', { accountIds: [String(a.accountId)] })).accounts?.[0]?.availableCollateral ?? unparked); } catch {}
  return { has: true, accountId: String(a.accountId), capId: a.objectId, collateral: available, unparked };
}
// ── perps check (security audit 2026-10-27, finding 3) ──
// Aftermath's API writes every perps transaction. Before one reaches the wallet, Kivy runs it and reads what the CHAIN says it
// does — the events and the money moving — and compares that with what the user asked: the market (from the on-chain-verified
// list in perps_markets.json, kept by tools/sync_perps_markets.py), the account, long or short, the size and the leverage,
// and that nothing leaves the wallet but gas (or the deposit). Any mismatch is refused and logged ("kivy perps refused:"),
// which wakes the alert and the fixer. Reading events instead of raw arguments survives Aftermath re-ordering them.
const perpCtx = new AsyncLocalStorage<{ cmd: string; args: string[] }>();
let _ptab: { at: number; v: any } = { at: 0, v: null };
// perps_markets.json (verified market + package ids), re-read every 60 s
function perpTable() {
  if (Date.now() - _ptab.at > 60_000) {
    try { _ptab = { at: Date.now(), v: JSON.parse(readFileSync(new URL('./perps_markets.json', import.meta.url), 'utf8')) }; }
    catch { _ptab = { at: Date.now(), v: null }; }
  }
  return _ptab.v;
}
// log the refusal (the alert watches for "kivy perps refused:") and stop the build
function perpRefuse(why: string, cmd: string): never {
  console.error(`kivy perps refused: ${why} | ${cmd}`);
  throw new Error(`This perps transaction did not match your request, so nothing was prepared (${why}). Kivy has been alerted.`);
}
// the perps check described above; runs on every perps transaction through finishKind()
async function perpGuard(tx: Transaction, addr: string) {
  const ctx = perpCtx.getStore(), cmd = ctx?.cmd ?? 'perp';
  let a = ctx?.args ?? [];
  // tests only (command-line runs; the service never has it): KIVY_PERP_EXPECT='{"1":"BTC","2":"1"}' replaces request arguments
  // by position, so a correctly built order can be checked against a request it does NOT match (tests/test_perpguard.sh)
  if (process.env.KIVY_PERP_EXPECT) { a = [...a]; for (const [i, v] of Object.entries(JSON.parse(process.env.KIVY_PERP_EXPECT))) a[Number(i)] = String(v); }
  const T = perpTable();
  if (!T?.markets || !T?.packages) perpRefuse('the verified market list is missing', cmd);
  const me = normalizeSuiAddress(addr);
  const progs = new Set([...T.packages, '0x1', '0x2'].map((x: string) => normalizeSuiAddress(x)));
  for (const c of tx.getData().commands as any[]) {
    if (c.Publish || c.Upgrade || c.$kind === 'Publish' || c.$kind === 'Upgrade') perpRefuse('it publishes or upgrades a program', cmd);
    if (c.MoveCall && !progs.has(normalizeSuiAddress(c.MoveCall.package))) perpRefuse(`it calls ${normalizeSuiAddress(c.MoveCall.package).slice(0, 10)}…::${c.MoveCall.module}, not Aftermath perps`, cmd);
  }
  const bytes = await tx.build({ client: grpc as any });
  const sim: any = await (grpc as any).core.simulateTransaction({ transaction: bytes, include: { effects: true, balanceChanges: true, events: true } });
  const t = sim.Transaction ?? sim.FailedTransaction ?? sim;
  if (t?.effects?.status?.success === false) return;     // a failing transaction is refused by simulate() with its own reason
  // money: nothing reaches another address; the wallet pays only gas, except a deposit's own USDC
  const dep = cmd === 'perp' && a[1] === 'deposit' ? Number(a[2]) : 0;
  for (const b of t?.balanceChanges ?? []) {
    const who = normalizeSuiAddress(b.address), amt = BigInt(b.amount), sym = String(b.coinType).split('::').pop();
    if (who !== me && amt > 0n) perpRefuse(`it pays ${sym} to another address`, cmd);
    if (who === me && amt < 0n) {
      if (/::sui::SUI$/.test(b.coinType)) { if (-amt > 500_000_000n) perpRefuse('it spends more SUI than gas', cmd); }
      else if (!(dep > 0 && normType(b.coinType) === normType(USDC) && Number(-amt) <= Math.round(dep * 1e6) + 1)) perpRefuse(`it takes ${sym} from the wallet`, cmd);
    }
  }
  for (const o of t?.effects?.changedObjects ?? []) {   // none of the wallet's objects goes to someone else
    const ino = o.inputOwner?.AddressOwner;
    const outo = o.outputOwner?.AddressOwner;
    if (ino && normalizeSuiAddress(ino) === me && outo && normalizeSuiAddress(outo) !== me) perpRefuse('it moves one of the wallet\'s objects to another owner', cmd);
  }
  // markets and the account, as the chain's own events name them
  const known = new Set(Object.values(T.markets).map((x: any) => normalizeSuiAddress(x)));
  const sym = ['perp-order', 'perp-limit', 'perp-stop', 'perp-scale', 'perp-twap', 'perp-margin', 'perp-close', 'perp-tpsl', 'perp-tpsl-multi'].includes(cmd)
    ? String(a[1] || '').toUpperCase() : '';
  const want = sym ? T.markets[sym] : null;
  if (sym && !want) perpRefuse(`${sym} is not on the verified market list`, cmd);
  const accts = new Set<string>();
  const evs = (t?.events ?? []).map((e: any) => ({ type: String(e.eventType ?? e.type ?? ''), j: e.json ?? e.parsedJson ?? {} }));
  for (const e of evs) {
    if (!e.type.includes('::events::')) continue;
    if (e.j.ch_id) {
      const ch = normalizeSuiAddress(String(e.j.ch_id));
      if (!known.has(ch)) perpRefuse(`it touches market ${ch.slice(0, 10)}…, not on the verified list`, cmd);
      if (want && ch !== normalizeSuiAddress(want)) perpRefuse(`it is built for another market than ${sym}`, cmd);
    }
    for (const k of ['account_id', 'taker_account_id']) if (e.j[k] != null) accts.add(String(e.j[k]));
  }
  if (accts.size > 1) perpRefuse('it acts on more than one perps account', cmd);
  if (cmd === 'perp-order') {                            // a market order: long or short, size and leverage as asked
    const side = Number(a[2]), usd = Number(a[3]), lev = Number(a[4]);
    const fill = evs.find((e: any) => e.type.endsWith('::events::FilledTakerOrder'));
    if (fill) {
      const bid = BigInt(fill.j.base_asset_delta_bid ?? 0), ask = BigInt(fill.j.base_asset_delta_ask ?? 0);
      if (side === 0 && !(bid > 0n && ask === 0n)) perpRefuse('it opens a short, you asked for a long', cmd);
      if (side === 1 && !(ask > 0n && bid === 0n)) perpRefuse('it opens a long, you asked for a short', cmd);
      const quote = Number(BigInt(fill.j.quote_asset_delta_bid ?? 0) + BigInt(fill.j.quote_asset_delta_ask ?? 0)) / 1e18;
      if (usd > 0 && quote > usd * 1.1 + 0.5) perpRefuse(`its size is about $${quote.toFixed(2)}, you asked for $${usd}`, cmd);
    }
    const imr = evs.find((e: any) => e.type.endsWith('::events::SetPositionInitialMarginRatio'));
    const alloc = evs.find((e: any) => e.type.endsWith('::events::AllocatedCollateral'));
    if (imr && lev > 0) {
      const got = 1e18 / Number(imr.j.initial_margin_ratio);
      if (Math.abs(got - lev) / lev > 0.05) perpRefuse(`its leverage is ${got.toFixed(2)}x, you asked for ${lev}x`, cmd);
    } else if (fill && alloc && lev > 0) {             // leverage already stored on the account: no 'set' event, so measure it from
      const quote = Number(BigInt(fill.j.quote_asset_delta_bid ?? 0) + BigInt(fill.j.quote_asset_delta_ask ?? 0)) / 1e18;   // the margin
      const margin = Number(alloc.j.collateral ?? 0) / 1e6;                                                                    // put up (USDC)
      const got = margin > 0 ? quote / margin : 0;
      if (!(got > 0) || Math.abs(got - lev) / lev > 0.1) perpRefuse(`its leverage is about ${got.toFixed(2)}x, you asked for ${lev}x`, cmd);
    }
  }
}
// an Aftermath transaction kind -> the full transaction for the wallet: sender, perpGuard, simulate
async function finishKind(kindB64: string, addr: string) {
  const tx = Transaction.fromKind(kindB64);
  tx.setSender(addr);
  await perpGuard(tx, addr);                            // the chain's own account of it must match the request (audit finding 3)
  await simulate(tx);                                   // never hand the user a transaction that would fail
  return tx.toJSON({ client: grpc as any });
}
// GET /perp — open the perps account, deposit or withdraw USDC collateral. Caller: server.py /api/tx/perp
async function buildPerp(addr: string, action: string, amountArg: string) {
  const acct: any = await perpAccount(addr);
  if (action === 'open') {
    if (acct.has) throw new Error('You already have an Aftermath perps account');
    const r = await af('transactions/create-account', { walletAddress: addr, collateralCoinType: USDC });
    return { ok: true, action, tx: await finishKind(r.txKind, addr) };
  }
  if (!acct.has) throw new Error('Open an Aftermath perps account first');
  if (action === 'deposit') {
    const amount = Number(amountArg);
    const tx = new Transaction(); tx.setSender(addr);
    tx.add(coinWithBalance({ type: USDC, balance: toBase(amount) }));
    const resolved = JSON.parse(await tx.toJSON({ client: grpc as any }));
    const kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
    const r = await af('account/transactions/deposit-collateral', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
      collateralCoinType: USDC, txKind: Buffer.from(kind).toString('base64'), depositCoinArg: coinArg(resolved) });
    return { ok: true, action, amount, tx: await finishKind(r.txKind, addr) };
  }
  if (action === 'withdraw') {
    // idle margin parked on markets comes back first, so 'all' really means all that is free
    const idle = await idleMarketKinds(acct);
    const free = Math.floor((acct.unparked + idle.total) * 1e6) / 1e6;
    if (free <= 0) throw new Error('Your perps account has no free USDC to withdraw');
    const want = Number(amountArg);
    if (amountArg !== 'all' && !(want > 0)) throw new Error('Enter an amount to withdraw from perps');
    // more than is free: say so instead of quietly withdrawing less (audit V16); a rounding hair still counts as 'all'
    if (amountArg !== 'all' && want > free + 0.000001) throw new Error(`Your perps account has ${free} USDC free, not ${want}`);
    const amount = amountArg === 'all' ? free : Math.min(want, free);
    const r = await af('account/transactions/withdraw-collateral', { accountId: acct.accountId, withdrawAmount: toBase(amount).toString() + 'n', recipientAddress: addr });
    const j = await joinParts([...idle.kinds, r.txKind], addr);
    return { ok: true, action, amount, released: idle.total, tx: j.tx, steps: j.steps };
  }
  throw new Error('perp action must be open, deposit or withdraw');
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S11] PERPS MARKET ORDERS
// market list, order preview (/perp-preview), market order with optional SL / TP (/perp-order)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── perps orders: preview + market order (optional stop-loss / take-profit) ──
let _pmk: { at: number; v: Record<string, any> } | null = null;
// Aftermath's market for a symbol (BTC, SUI…); the market list is kept for an hour
async function perpMarket(sym: string) {
  if (!_pmk || Date.now() - _pmk.at > 3600_000) {
    const r: any = await af('all-markets', { collateralCoinType: USDC });
    _pmk = { at: Date.now(), v: Object.fromEntries((r.markets ?? []).map((m: any) => [String(m.marketParams.baseAssetSymbol).replace(/USD$/, ''), m])) };
  }
  const m = _pmk.v[sym.toUpperCase()];
  if (!m) throw new Error('No Aftermath market for ' + sym);
  return m;
}
const SIZE_DEC = 1e9;                                    // order size = base amount × 1e9
function baseSize(m: any, base: number) {
  const lot = Number(String(m.marketParams.lotSize).replace('n', ''));
  const raw = Math.floor(base * SIZE_DEC / lot) * lot;    // round down to the lot size
  return BigInt(raw);
}
// GET /perp-preview — size, price, margin, fees and liquidation price of a market order, from Aftermath's preview API.
// Callers: server.py /api/perp/preview, perp_confirm(); also buildOrder().
async function preview(addr: string, sym: string, side: number, usd: number, lev: number) {
  const m = await perpMarket(sym);
  const px: any = await af('markets/prices', { marketIds: [m.objectId] });
  const mark = Number(px.marketsPrices?.[0]?.markPrice ?? 0);
  if (!(mark > 0)) throw new Error('No price for ' + sym);
  const size = baseSize(m, usd / mark);
  if (size <= 0n) throw new Error('Order too small');
  const acct: any = addr ? await perpAccount(addr) : { has: false };
  let pv: any = null;
  if (acct.has) {
    try { pv = await af('account/previews/place-market-order', { accountId: acct.accountId, accountCapId: acct.capId, marketId: m.objectId, side, size: size.toString() + 'n', reduceOnly: false, leverage: lev }); }
    catch { pv = null; }
    if (pv?.error) pv = null;
  }
  if (!pv) pv = await af('account/previews/place-market-order', { marketId: m.objectId, side, size: size.toString() + 'n', reduceOnly: false, leverage: lev });
  if (pv.error) throw new Error(String(pv.error).replace(/^.*"message":"/, '').replace(/"}.*$/, '').slice(0, 200));
  const fee = Number(m.marketParams.takerFee ?? 0);
  const need = Number(pv.collateralChange ?? 0);
  const free = acct.has ? Number(acct.unparked ?? acct.collateral ?? 0) : 0;
  return { symbol: sym.toUpperCase(), side, lev, marketId: m.objectId, size: size.toString(), base: Number(size) / SIZE_DEC, mark,
           execution_price: pv.executionPrice, order_value: pv.filledSizeUsd, margin: need, slippage_pct: Math.abs(Number(pv.percentSlippage ?? 0)) * 100,
           liquidation_price: pv.updatedPosition?.liquidationPrice, fee_pct: fee * 100, fee_usd: Number(pv.filledSizeUsd ?? 0) * fee,
           maker_fee_pct: Number(m.marketParams.makerFee ?? 0) * 100,
           has_account: !!acct.has, account_free: free,
           // margin gets a 3% cushion (price moves while signing); the top-up also covers the taker fee
           deposit_needed: Math.max(0, need * 1.005 + Number(pv.filledSizeUsd ?? 0) * fee - free),   // shortfall the user must deposit first
           max_lev: Math.round(1 / Number(m.marketParams.marginRatioInitial)), min_usd: Number(m.marketParams.minOrderUsdValue ?? 1),
           has_position: !!pv.hasPosition || await hasPos(acct, m.objectId) };
}
// GET /perp-order — market order (+ optional stop-loss / take-profit) as one transaction. Caller: server.py /api/tx/perp
async function buildOrder(addr: string, sym: string, side: number, usd: number, lev: number, slip: number, sl?: number, tp?: number) {
  const acct: any = await perpAccount(addr);
  if (!acct.has) throw new Error('Open an Aftermath perps account first');
  const p: any = await preview(addr, sym, side, usd, lev);
  if (p.order_value < p.min_usd) throw new Error(`Minimum order is $${p.min_usd}`);
  // Aftermath's builders ignore an incoming txKind, so each part is built alone and Kivy joins them:
  // [top-up deposit] + market order + [stop-loss / take-profit], one transaction, one signature.
  const parts: string[] = [];
  if (p.deposit_needed > 0.000001) parts.push(await topUpKind(addr, acct, p.deposit_needed));
  parts.push((await af('account/transactions/place-market-order', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    marketId: p.marketId, side, size: p.size + 'n', reduceOnly: false, leverage: lev,
    collateralChange: p.deposit_needed > 0.000001 ? p.margin * 1.03 : p.margin,
    hasPosition: p.has_position, cancelSlTp: false, slippage: slip / 100 })).txKind);
  if (sl || tp) parts.push((await af('account/transactions/place-sl-tp-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    marketId: p.marketId, positionSide: side, size: p.size + 'n', stopLossPrice: sl || null, takeProfitPrice: tp || null, triggerPriceType: 2 })).txKind);
  let kind = parts[0];
  for (const next of parts.slice(1)) {
    const m = await mergeKinds(kind, next, addr);
    kind = Buffer.from(await m.build({ client: grpc as any, onlyTransactionKind: true })).toString('base64');
  }
  const steps = JSON.parse(await Transaction.fromKind(kind).toJSON()).commands.filter((c: any) => c.MoveCall).map((c: any) => c.MoveCall.function);
  const r = { txKind: kind };
  return { ok: true, action: 'order', preview: p, steps, tx: await finishKind(r.txKind, addr) };
}
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S12] PERPS PRO ORDERS
// limit, stop, scale, TWAP, cancel, close, take-profit / stop-loss — all GET /perp-* and joined into one transaction
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── pro orders: limit, stop market / stop limit, scale, TWAP; plus cancel and close ──
const PX_DEC = 1e9;                                       // order prices = price × 1e9, rounded to the tick size
function orderPrice(m: any, price: number) {
  const tick = Number(String(m.marketParams.tickSize).replace('n', ''));
  return BigInt(Math.round(price * PX_DEC / tick) * tick);
}
async function walletUsdc(addr: string) {
  const bal: any = await (grpc as any).core.getBalance({ owner: addr, coinType: USDC });
  const b = bal?.balance ?? bal;
  return fromBase(b?.balance ?? b?.totalBalance ?? 0);
}
// Orders never pull money from the wallet (2026-10-16): they use only the USDC the user deposited into
// the perps account themselves. Short -> a clear message; the user deposits the amount they choose first.
async function topUpKind(addr: string, acct: any, amount: number, what = 'order') {
  const free = Number(acct.unparked ?? acct.collateral ?? 0);
  const f6 = (x: number) => String(Number(x.toFixed(6)));                 // 0.000316, not "0.00"
  throw new Error(`Not enough USDC in your perps account: this ${what} needs ${f6(amount)} USDC more (free ${f6(free)}). ` +
                  `Deposit into perps first — say "deposit to perps" — or lower the size / raise leverage.`);
}
// join Aftermath transaction kinds into one (merge.ts), then finishKind(); steps = the Move functions it calls
async function joinParts(parts: string[], addr: string) {
  let kind = parts[0];
  for (const next of parts.slice(1)) {
    const m = await mergeKinds(kind, next, addr);
    kind = Buffer.from(await m.build({ client: grpc as any, onlyTransactionKind: true })).toString('base64');
  }
  const steps = JSON.parse(await Transaction.fromKind(kind).toJSON()).commands.filter((c: any) => c.MoveCall).map((c: any) => c.MoveCall.function);
  return { steps, tx: await finishKind(kind, addr) };
}
async function needAccount(addr: string) {
  const acct: any = await perpAccount(addr);
  if (!acct.has) throw new Error('Open an Aftermath perps account first');
  return acct;
}
// Does the account already HAVE a position object on this market? An emptied position (size 0, e.g. after a
// cancelled limit order) still exists on-chain; creating it again aborts with code 41 in add_position.
async function hasPos(acct: any, marketId: string) {
  if (!acct?.has) return false;
  const r: any = await af('accounts/positions', { accountIds: [acct.accountId] });
  return (r.accounts?.[0]?.positions ?? []).some((x: any) => x.marketId === marketId);
}
// GET /perp-limit, /perp-stop, /perp-scale, /perp-twap — caller: server.py /api/tx/perp. Each one asks Aftermath for the
// order's transaction kind, refuses when the perps account is short (topUpKind), then joinParts().
async function buildLimit(addr: string, sym: string, side: number, usd: number, lev: number, price: number, postOnly: boolean, sl?: number, tp?: number) {
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  const size = baseSize(m, usd / price), px = orderPrice(m, price);
  if (size <= 0n) throw new Error('Order too small');
  const body: any = { marketId: m.objectId, side, size: size + 'n', price: px + 'n', orderType: postOnly ? 2 : 0, reduceOnly: false, leverage: lev };
  let pv: any = await af('account/previews/place-limit-order', { ...body, accountId: acct.accountId, accountCapId: acct.capId }).catch(() => null);
  if (!pv || pv.error) pv = await af('account/previews/place-limit-order', body);
  if (pv.error) throw new Error(String(pv.error).replace(/^.*"message":"/, '').replace(/"}.*$/, '').slice(0, 200));
  const need = Number(pv.collateralChange ?? usd / lev), fee = Number(m.marketParams.takerFee) * usd;
  const top = Math.max(0, need * 1.005 + fee - Number(acct.unparked ?? acct.collateral ?? 0));   // own perps balance only: margin + fee
  const parts: string[] = [];
  if (top > 0.000001) parts.push(await topUpKind(addr, acct, top));
  parts.push((await af('account/transactions/place-limit-order', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId, ...body,
    collateralChange: top > 0.000001 ? need * 1.03 : need, hasPosition: !!pv.hasPosition || await hasPos(acct, m.objectId), cancelSlTp: false })).txKind);
  if (sl || tp) parts.push((await af('account/transactions/place-sl-tp-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    marketId: m.objectId, positionSide: side, size: size + 'n', stopLossPrice: sl || null, takeProfitPrice: tp || null, triggerPriceType: 2 })).txKind);
  return { ok: true, action: 'limit', margin: need, deposit: top, ...(await joinParts(parts, addr)) };
}
async function buildStop(addr: string, sym: string, side: number, usd: number, lev: number, trigger: number, limit: number) {
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  const px: any = await af('markets/prices', { marketIds: [m.objectId] });
  const mark = Number(px.marketsPrices?.[0]?.markPrice ?? 0);
  const size = baseSize(m, usd / (limit || trigger));
  if (size <= 0n) throw new Error('Order too small');
  // the stop fills later from free collateral, so make sure the account holds enough now
  const need = usd / lev, fee = Number(m.marketParams.takerFee) * usd;
  const top = Math.max(0, need * 1.005 + fee - Number(acct.unparked ?? acct.collateral ?? 0));   // own perps balance only: margin + fee
  const parts: string[] = [];
  if (top > 0.000001) parts.push(await topUpKind(addr, acct, top));
  parts.push((await af('account/transactions/place-stop-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    stopOrders: [{ marketId: m.objectId, side, size: size + 'n', expiryTimestamp: null,
      nonSlTp: { stopIndexPrice: trigger, triggerIfGeStopIndexPrice: trigger >= mark, reduceOnly: false, triggerPriceType: 2 },
      limitOrder: limit ? { price: orderPrice(m, limit) + 'n', orderType: 0 } : null }] })).txKind);
  return { ok: true, action: limit ? 'stop-limit' : 'stop-market', trigger_above_mark: trigger >= mark, mark, deposit: top, ...(await joinParts(parts, addr)) };
}
async function buildScale(addr: string, sym: string, side: number, usd: number, lev: number, start: number, end: number, n: number) {
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  if (!(n >= 2 && n <= 50)) throw new Error('Scale needs 2–50 orders');
  const avg = (start + end) / 2, size = baseSize(m, usd / avg);
  const need = usd / lev * 1.02, fee = Number(m.marketParams.takerFee) * usd;
  const top = Math.max(0, need * 1.005 + fee - Number(acct.unparked ?? acct.collateral ?? 0));   // own perps balance only: margin + fee
  const parts: string[] = [];
  if (top > 0.000001) parts.push(await topUpKind(addr, acct, top));
  parts.push((await af('account/transactions/place-scale-order', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    marketId: m.objectId, side, totalSize: size + 'n', startPrice: orderPrice(m, start) + 'n', endPrice: orderPrice(m, end) + 'n',
    numberOfOrders: n, orderType: 0, sizeSkew: 1, reduceOnly: false, leverage: lev, collateralChange: need,
    hasPosition: await hasPos(acct, m.objectId), cancelSlTp: false })).txKind);
  return { ok: true, action: 'scale', margin: need, deposit: top, ...(await joinParts(parts, addr)) };
}
async function buildTwap(addr: string, sym: string, side: number, usd: number, lev: number, minutes: number, chunks: number, slip: number) {
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  const px: any = await af('markets/prices', { marketIds: [m.objectId] });
  const mark = Number(px.marketsPrices?.[0]?.markPrice ?? 0);
  if (!(chunks >= 2 && chunks <= 100) || !(minutes >= 1 && minutes <= 1440)) throw new Error('TWAP needs 2–100 slices over 1 minute to 24 hours');
  const size = baseSize(m, usd / mark);
  if (Number(size) / SIZE_DEC * mark / chunks < Number(m.marketParams.minOrderUsdValue ?? 1)) throw new Error('Each slice must be at least $1 — use fewer slices or a bigger size');
  const need = usd / lev, fee = Number(m.marketParams.takerFee) * usd;
  const top = Math.max(0, need * 1.005 + fee - Number(acct.unparked ?? acct.collateral ?? 0));   // own perps balance only: margin + fee
  const parts: string[] = [];
  if (top > 0.000001) parts.push(await topUpKind(addr, acct, top));
  parts.push((await af('account/transactions/create-twap-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    twapOrders: [{ marketId: m.objectId, side, size: size + 'n', reduceOnly: false, chunksAmount: chunks,
      executionGapMs: String(Math.round(minutes * 60000 / chunks)) + 'n', executionTimeUncertaintyMs: '0n', amountUncertaintyBps: 0,
      maxOneExecutionAmountBps: 10000, maxSlippageBps: Math.round(slip * 100), smallTailMergeThresholdBps: 0, timeForRetryMs: '60000n' }] })).txKind);
  return { ok: true, action: 'twap', deposit: top, ...(await joinParts(parts, addr)) };
}
// Collateral parked on a market with no position and no resting orders is idle: Aftermath counts it as
// available, but withdraw only sees the account's unparked balance. Move it back first (deallocate).
async function idleMarketKinds(acct: any, exceptOrders: { marketId?: string; ids?: string[] } = {}) {
  const r: any = await af('accounts/positions', { accountIds: [acct.accountId] });
  const a = r.accounts?.[0] ?? {};
  const kinds: string[] = []; let total = 0;
  for (const p of a.positions ?? []) {
    const left = (p.pendingOrders ?? []).filter((o: any) => !(p.marketId === exceptOrders.marketId && (exceptOrders.ids ?? []).includes(String(o.orderId).replace(/n$/, ''))));
    const amt = Math.floor(Number(p.collateral ?? 0) * 1e6);
    if (Number(p.baseAssetAmount) !== 0 || left.length || amt <= 0) continue;
    kinds.push((await af('account/transactions/deallocate-collateral', { accountId: acct.accountId, marketId: p.marketId,
      deallocateAmount: BigInt(amt).toString() + 'n' })).txKind);
    total += amt / 1e6;
  }
  return { kinds, total, available: Number(a.availableCollateral ?? 0) };
}
// GET /perp-cancel — cancel a limit / stop / TWAP order, release its idle margin, optionally withdraw to the wallet.
// Callers: server.py /api/tx/perp and /api/perp/cancel-preview.
async function buildCancel(addr: string, kind: string, sym: string, id: string, withdraw = false) {
  const acct = await needAccount(addr);
  let r: any;
  if (kind === 'limit') {
    const m = await perpMarket(sym);
    r = await af('account/transactions/cancel-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
      marketIdsToData: { [m.objectId]: { orderIds: [id.replace(/n$/, '') + 'n'], collateralChange: 0 } } });
  } else if (kind === 'stop') {
    r = await af('account/transactions/cancel-stop-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId, stopOrderIds: [id] });
  } else if (kind === 'twap') {
    r = await af('account/transactions/cancel-twap-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId, twapOrderIds: [id] });
  } else throw new Error('cancel kind must be limit, stop or twap');
  // cancelling the last order on a market (no position): its parked margin goes back to the account in the same tx
  const idle = kind === 'limit' ? await idleMarketKinds(acct, { marketId: (await perpMarket(sym)).objectId, ids: [id.replace(/n$/, '')] }).catch(() => null) : null;
  const parts = [r.txKind, ...(idle?.kinds ?? [])];
  // optional: send the freed USDC (plus any idle balance) back to the wallet in the same transaction
  let out = 0;
  if (withdraw) {
    // freshest numbers + a hair of room: balances move between preview and signing (funding, fills)
    const fresh: any = await af('accounts/owned', { walletAddress: addr }).catch(() => null);
    const cap = (fresh?.accountCaps ?? []).find((x: any) => String(x.accountId) === String(acct.accountId));
    const unparked = cap ? fromBase(String(cap.collateral).replace('n', '')) : Number(acct.unparked ?? 0);
    out = Math.floor((Math.min(unparked, Number(acct.unparked ?? unparked)) + (idle?.total ?? 0)) * 0.9999 * 1e6) / 1e6;
    if (out > 0) parts.push((await af('account/transactions/withdraw-collateral', { accountId: acct.accountId,
      withdrawAmount: toBase(out).toString() + 'n', recipientAddress: addr })).txKind);
  }
  return { ok: true, action: 'cancel', released: idle?.total ?? 0, withdrawn: out, ...(await joinParts(parts, addr)) };
}
// the open position on sym (account, market, position), or a clear error
async function openPos(addr: string, sym: string) {
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  const r: any = await af('accounts/positions', { accountIds: [acct.accountId] });
  const pos = (r.accounts?.[0]?.positions ?? []).find((x: any) => x.marketId === m.objectId && Number(x.baseAssetAmount) !== 0);
  if (!pos) throw new Error('No open ' + sym + ' position');
  return { acct, m, pos };
}
// close all of a position (frac 1) or part of it (0 < frac < 1, rounded down to the market's lot size)
// GET /perp-close — caller: server.py /api/tx/perp (a reduce-only market order on the other side)
async function buildClose(addr: string, sym: string, slip: number, frac = 1) {
  if (!(frac > 0 && frac <= 1)) throw new Error('Close between 1% and 100% of the position');
  const { acct, m, pos } = await openPos(addr, sym);
  const side = Number(pos.baseAssetAmount) > 0 ? 1 : 0;           // close = the opposite side, reduce-only
  const full = BigInt(Math.round(Math.abs(Number(pos.baseAssetAmount)) * SIZE_DEC));
  // only 100% means the whole position; 99.9% stays a partial close, rounded DOWN to the lot size (user-harm #7)
  const part = frac < 1 ? baseSize(m, Math.abs(Number(pos.baseAssetAmount)) * frac) : full;
  if (part <= 0n) throw new Error('That part is smaller than ' + sym + "'s smallest order size — close all of it instead");
  const size = part >= full ? full : part;
  const pv: any = await af('account/previews/place-market-order', { accountId: acct.accountId, accountCapId: acct.capId, marketId: m.objectId, side, size: size + 'n', reduceOnly: true });
  if (pv.error) throw new Error(String(pv.error).slice(0, 200));
  const o = await af('account/transactions/place-market-order', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    marketId: m.objectId, side, size: size + 'n', reduceOnly: true, collateralChange: Number(pv.collateralChange ?? 0), hasPosition: true, cancelSlTp: size >= full, slippage: slip / 100 });
  return { ok: true, action: 'close', pnl: pos.unrealizedPnlUsd, closed_base: Number(size) / SIZE_DEC, position_base: Number(full) / SIZE_DEC,
           ...(await joinParts([o.txKind], addr)) };
}
// take-profit / stop-loss on a position that is already open (whole size, reduce-only, index price)
// TP1 can close part of the position (frac) and TP2 the rest; SL always covers the whole position (2026-10-21)
// GET /perp-tpsl — caller: server.py /api/tx/perp
async function buildTpSl(addr: string, sym: string, sl: number, tp: number, frac = 1, tp2 = 0) {
  if (!(sl > 0 || tp > 0)) throw new Error('Give a stop-loss or take-profit price');
  if (!(frac > 0 && frac <= 1)) throw new Error('The take-profit share must be between 1% and 100%');
  if (tp2 > 0 && !(tp > 0 && frac < 1)) throw new Error('A second take-profit needs the first one to close only part of the position');
  const { acct, m, pos } = await openPos(addr, sym);
  const long = Number(pos.baseAssetAmount) > 0;
  const px: any = await af('markets/prices', { marketIds: [m.objectId] });
  const mark = Number(px.marketsPrices?.[0]?.indexPrice ?? px.marketsPrices?.[0]?.markPrice ?? 0);
  if (mark > 0) {                                    // the wrong side of the price would trigger at once
    if (sl > 0 && (long ? sl >= mark : sl <= mark)) throw new Error('A stop-loss on a ' + (long ? 'long must be below' : 'short must be above') + ' the price now (' + mark.toPrecision(6) + ')');
    if (tp > 0 && (long ? tp <= mark : tp >= mark)) throw new Error('A take-profit on a ' + (long ? 'long must be above' : 'short must be below') + ' the price now (' + mark.toPrecision(6) + ')');
    if (tp2 > 0 && (long ? tp2 <= tp : tp2 >= tp)) throw new Error('TP2 must be ' + (long ? 'above' : 'below') + ' TP1 (' + tp + ')');
  }
  const size = BigInt(Math.round(Math.abs(Number(pos.baseAssetAmount)) * SIZE_DEC));
  const place = (sz: bigint, slp: number, tpp: number) => af('account/transactions/place-sl-tp-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    marketId: m.objectId, positionSide: long ? 0 : 1, size: sz + 'n', stopLossPrice: slp || null, takeProfitPrice: tpp || null, triggerPriceType: 2 });
  if (!(tp > 0) || frac >= 1) {                      // one order for the whole position, as before
    const o = await place(size, sl, tp);
    return { ok: true, action: 'tpsl', mark, side: long ? 'long' : 'short', base: Number(size) / SIZE_DEC, ...(await joinParts([o.txKind], addr)) };
  }
  const s1 = baseSize(m, Number(size) / SIZE_DEC * frac);
  if (s1 <= 0n) throw new Error('That share is smaller than the smallest order on ' + sym);
  const rest = size - s1;
  const parts: string[] = [(await place(s1, 0, tp)).txKind];
  if (tp2 > 0 && rest > 0n) parts.push((await place(rest, 0, tp2)).txKind);
  if (sl > 0) parts.push((await place(size, sl, 0)).txKind);
  return { ok: true, action: 'tpsl', mark, side: long ? 'long' : 'short', base: Number(size) / SIZE_DEC,
           tp1_base: Number(s1) / SIZE_DEC, tp2_base: tp2 > 0 ? Number(rest) / SIZE_DEC : 0, ...(await joinParts(parts, addr)) };
}

// up to 3 take-profits, each closing its own share of the position, plus one stop-loss for all of it — one transaction (2026-10-21)
// GET /perp-tpsl-multi — caller: server.py /api/tx/perp
async function buildTpSlMulti(addr: string, sym: string, sl: number, tps: [number, number][]) {
  tps = (tps || []).filter((t) => t && t[0] > 0).slice(0, 3);
  if (!(sl > 0 || tps.length)) throw new Error('Give a stop-loss or take-profit price');
  const total = tps.reduce((t, x) => t + x[1], 0);
  if (tps.some((t) => !(t[1] > 0)) || total > 1.0001) throw new Error('The take-profit shares must add up to 100% or less');
  const { acct, m, pos } = await openPos(addr, sym);
  const long = Number(pos.baseAssetAmount) > 0;
  const px: any = await af('markets/prices', { marketIds: [m.objectId] });
  const mark = Number(px.marketsPrices?.[0]?.indexPrice ?? px.marketsPrices?.[0]?.markPrice ?? 0);
  if (mark > 0) {
    if (sl > 0 && (long ? sl >= mark : sl <= mark)) throw new Error('A stop-loss on a ' + (long ? 'long must be below' : 'short must be above') + ' the price now (' + mark.toPrecision(6) + ')');
    tps.forEach((t, i) => {
      if (long ? t[0] <= mark : t[0] >= mark) throw new Error('TP' + (i + 1) + ' on a ' + (long ? 'long must be above' : 'short must be below') + ' the price now (' + mark.toPrecision(6) + ')');
      if (i && (long ? t[0] <= tps[i - 1][0] : t[0] >= tps[i - 1][0])) throw new Error('TP' + (i + 1) + ' must be ' + (long ? 'above' : 'below') + ' TP' + i + ' (' + tps[i - 1][0] + ')');
    });
  }
  const size = BigInt(Math.round(Math.abs(Number(pos.baseAssetAmount)) * SIZE_DEC));
  const place = (sz: bigint, slp: number, tpp: number) => af('account/transactions/place-sl-tp-orders', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
    marketId: m.objectId, positionSide: long ? 0 : 1, size: sz + 'n', stopLossPrice: slp || null, takeProfitPrice: tpp || null, triggerPriceType: 2 });
  const parts: string[] = [], sizes: number[] = [];
  let used = 0n;
  for (let i = 0; i < tps.length; i++) {
    const last = i === tps.length - 1 && total > 0.9999;
    const sz = last ? size - used : baseSize(m, Number(size) / SIZE_DEC * tps[i][1]);   // the last one takes exactly the rest
    if (sz <= 0n) throw new Error('TP' + (i + 1) + '\'s share is smaller than the smallest order on ' + sym);
    used += sz; sizes.push(Number(sz) / SIZE_DEC);
    parts.push((await place(sz, 0, tps[i][0])).txKind);
  }
  if (sl > 0) parts.push((await place(size, sl, 0)).txKind);
  return { ok: true, action: 'tpsl', mark, side: long ? 'long' : 'short', base: Number(size) / SIZE_DEC, tp_bases: sizes, ...(await joinParts(parts, addr)) };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S13] PERPS READS
// stop / TWAP order list (POST /perp-pro-orders), positions (/perp-positions), add margin (/perp-margin), history
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// Stop and TWAP orders are private on Aftermath: listing them needs the wallet's signature over the
// fixed message "Aftermath Terms and Conditions" (a free personal-message signature, not a transaction).
// POST /perp-pro-orders — caller: server.py /api/perp/pro-orders (body: address, bytes, signature)
async function perpProOrders(addr: string, bytes: string, signature: string) {
  const acct = await needAccount(addr);
  if (!_pmk) await perpMarket('BTC').catch(() => null);
  const name = (id: string) => Object.entries(_pmk?.v ?? {}).find(([, m]: any) => m.objectId === id)?.[0] ?? String(id).slice(0, 8);
  const [st, tw]: any = await Promise.all([
    af('account/stop-order-datas', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId, bytes, signature }).catch((e) => ({ error: String(e.message) })),
    af('account/twap-order-datas', { walletAddress: addr, accountId: acct.accountId, bytes, signature }).catch((e) => ({ error: String(e.message) })),
  ]);
  const stops = (st.stopOrderDatas ?? st ?? []).filter?.((x: any) => x && x.objectId) ?? [];
  const twaps = (tw.twapOrderDatas ?? tw.orders ?? tw ?? []).filter?.((x: any) => x && (x.objectId || x.id)) ?? [];
  return { ok: true,
    stops: stops.map((x: any) => ({ id: x.objectId, symbol: name(x.marketId), side: x.side === 0 ? 'long' : 'short', size: Number(String(x.size).replace('n', '')) / SIZE_DEC,
      kind: x.slTp ? 'TP/SL' : (x.limitOrder ? 'Stop Limit' : 'Stop Market'),
      trigger: x.nonSlTp?.stopIndexPrice ?? null, sl: x.slTp?.stopLossPrice ?? null, tp: x.slTp?.takeProfitPrice ?? null,
      limit: x.limitOrder ? Number(String(x.limitOrder.price).replace('n', '')) / PX_DEC : null })),
    twaps: twaps.map((x: any) => ({ id: x.objectId ?? x.id, symbol: name(x.marketId ?? x.details?.marketId), side: (x.side ?? x.details?.side) === 0 ? 'long' : 'short' })),
    errors: [st.error, tw.error].filter(Boolean) };
}

// the order id carries the price: high 64 bits = price × 1e9 for sells, bit-inverted for buys (checked against order-history)
function idPrice(id: any, side: number) {
  const hi = BigInt(String(id).replace('n', '')) >> 64n;
  return Number(side === 0 ? (2n ** 64n - 1n - hi) : hi) / 1e9;
}
// GET /perp-positions — open positions, their orders and funding since opening. Callers: server.py /api/perp/positions
// and the wallet / chat readers (wallet_snapshot, perp_manage, perp_widget, market_overview…); server.py caches it 15 s.
async function perpPositions(addr: string) {
  const acct: any = await perpAccount(addr);
  if (!acct.has) return { has: false, positions: [] };
  // positions + the trades and collateral history, together: funding paid since each position opened (2026-10-21)
  const [r, th, ch]: any = await Promise.all([af('accounts/positions', { accountIds: [acct.accountId] }),
    af('account/order-history-detailed', { accountId: acct.accountId, limit: 200 }).catch(() => ({})),
    af('account/collateral-history', { accountId: acct.accountId, limit: 200 }).catch(() => ({}))]);
  const a = r.accounts?.[0] ?? {};
  if (!_pmk) await perpMarket('BTC').catch(() => null);
  const name = (id: string) => Object.entries(_pmk?.v ?? {}).find(([, m]: any) => m.objectId === id)?.[0] ?? id.slice(0, 8);
  // when did this position open? undo trades newest-first from today's size until it was flat
  const openedAt = (mid: string, now: number) => {
    let s = now, at = 0;
    for (const t of (th.trades ?? []).filter((t: any) => t.marketId === mid)) {
      const sz = Number(t.size), side = String(t.side);
      const d = /Open Long|Close Short/.test(side) ? sz : /Open Short|Close Long/.test(side) ? -sz : 0;
      s -= d; at = Number(t.timestamp);
      if (Math.abs(s) < Math.abs(now) * 1e-6 + 1e-12) return at;
    }
    return at;                                       // history shorter than the position: from the oldest trade we have
  };
  const settled = (mid: string, since: number) => (ch.collateralChanges ?? []).filter((c: any) => c.marketId === mid && String(c.eventType).endsWith('::SettledFunding')
    && Number(c.timestamp) >= since).reduce((t: number, c: any) => t + Number(c.collateralChangeUsd ?? 0), 0);
  return { has: true, free: a.availableCollateral, equity: a.totalEquityUsd, upnl: a.totalUnrealizedPnlUsd,
           positions: (a.positions ?? []).filter((x: any) => Number(x.baseAssetAmount) !== 0).map((x: any) => {
             const since = openedAt(x.marketId, Number(x.baseAssetAmount)), uf = Number(x.unrealizedFundingsUsd ?? 0);
             return {
             symbol: name(x.marketId), market_id: x.marketId, side: Number(x.baseAssetAmount) > 0 ? 'long' : 'short', size: Math.abs(Number(x.baseAssetAmount)),
             entry: x.entryPrice, liquidation: x.liquidationPrice, leverage: x.leverage, collateral: x.collateral, pnl: x.unrealizedPnlUsd,
             funding: uf + settled(x.marketId, since), funding_unsettled: uf, opened_at: since || null,
             funding_partial: !since || Number((ch.collateralChanges ?? []).slice(-1)[0]?.timestamp ?? 0) > since,   // history doesn't reach back to the open
             orders: (x.pendingOrders ?? []).map((o: any) => ({ id: String(o.orderId), side: o.side === 0 ? 'long' : 'short', size: Number(String(o.currentSize).replace('n', '')) / SIZE_DEC, price: idPrice(o.orderId, o.side) })) }; }),
           // markets with only resting limit orders (no position yet)
           orders: (a.positions ?? []).filter((x: any) => Number(x.baseAssetAmount) === 0 && (x.pendingOrders ?? []).length).map((x: any) => ({
             symbol: name(x.marketId), orders: x.pendingOrders.map((o: any) => ({ id: String(o.orderId), side: o.side === 0 ? 'long' : 'short', size: Number(String(o.currentSize).replace('n', '')) / SIZE_DEC, price: idPrice(o.orderId, o.side) })) })) };
}

// add margin to an open position: allocate free account collateral to that market (top up from the wallet first if short)
// GET /perp-margin — caller: server.py /api/tx/perp
async function buildMargin(addr: string, sym: string, amount: number) {
  if (!(amount > 0)) throw new Error('Enter an amount of USDC to add');
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  const r: any = await af('accounts/positions', { accountIds: [acct.accountId] });
  const a = r.accounts?.[0] ?? {};
  const pos = (a.positions ?? []).find((x: any) => x.marketId === m.objectId && Number(x.baseAssetAmount) !== 0);
  if (!pos) throw new Error('No open ' + sym + ' position to add margin to');
  const free = Number(a.availableCollateral ?? 0);
  const parts: string[] = [];
  const short = amount - free;
  if (short > 0.000001) parts.push(await topUpKind(addr, acct, Math.ceil(short * 1e6) / 1e6, 'margin'));
  parts.push((await af('account/transactions/allocate-collateral', { accountId: acct.accountId, marketId: m.objectId,
    allocateAmount: BigInt(Math.floor(amount * 1e6)).toString() + 'n' })).txKind);
  const j = await joinParts(parts, addr);
  return { ok: true, action: 'margin', symbol: sym, amount, deposit: short > 0 ? short : 0, ...j };
}
// filled trades + deposits / withdrawals, newest first (public, by account id)
// GET /perp-history — caller: server.py /api/perp/history
async function perpHistory(addr: string) {
  const acct: any = await perpAccount(addr);
  if (!acct.has) return { has: false, trades: [], transfers: [] };
  if (!_pmk) await perpMarket('BTC').catch(() => null);
  const name = (id: string) => Object.entries(_pmk?.v ?? {}).find(([, m]: any) => m.objectId === id)?.[0] ?? (id ? id.slice(0, 8) : '');
  const [t, c]: any = await Promise.all([af('account/order-history-detailed', { accountId: acct.accountId, limit: 30 }),
                                         af('account/collateral-history', { accountId: acct.accountId, limit: 50 })]);
  // the activity timeline: every order event + every collateral change, grouped by transaction, newest first
  const o: any = await af('account/order-history', { accountId: acct.accountId, limit: 60 }).catch(() => ({}));
  const ORD: Record<string, string> = { PostedOrder: 'placed', FilledTakerOrder: 'filled_market', FilledMakerOrder: 'filled_limit',
    CanceledOrder: 'cancelled', ReducedOrder: 'reduced', LiquidatedPosition: 'liquidated' };
  const COL: Record<string, string> = { DepositedCollateral: 'deposit', WithdrewCollateral: 'withdraw', AllocatedCollateral: 'margin_in',
    DeallocatedCollateral: 'margin_out', SettledFunding: 'funding', LiquidatedPosition: 'liquidated' };
  const ev = (x: any) => String(x.eventType).split('<')[0].split('::').pop() as string;
  const tx: Record<string, any> = {};
  const at = (d: string, time: number) => (tx[d] ??= { digest: d, time, items: [] as any[] });
  for (const x of o.orders ?? []) {
    const k = ORD[ev(x)] ?? ev(x);
    at(x.txDigest, x.timestamp).items.push({ k, symbol: name(x.marketId), side: x.side === 0 ? 'long' : 'short', size: x.size, price: x.price,
      value: x.size && x.price ? x.size * x.price : null, pnl: x.pnl, fee: x.fees,
      sl: x.slTp?.stopLossPrice ?? null, tp: x.slTp?.takeProfitPrice ?? null, stop: x.stopOrder ?? null });
  }
  for (const x of c.collateralChanges ?? []) {
    const k = COL[ev(x)]; if (!k || /Filled/.test(ev(x))) continue;
    if (k === 'funding' && Math.abs(Number(x.collateralChange)) < 1e-6) continue;          // zero funding: noise
    at(x.txDigest, x.timestamp).items.push({ k, symbol: x.marketId ? name(x.marketId) : '', usdc: x.collateralChange });
  }
  const activity = Object.values(tx).sort((a: any, b: any) => b.time - a.time).slice(0, 40);
  return { has: true, activity,
    trades: (t.trades ?? []).map((x: any) => ({ time: x.timestamp, digest: x.txDigest, symbol: name(x.marketId), side: x.side, type: x.tradeType,
      size: x.size, price: x.price, value: x.tradeValueUsd, fee: x.feeUsd, pnl: x.realizedPnlUsd })),
    transfers: (c.collateralChanges ?? []).filter((x: any) => /Deposited|Withdrew|Allocated|Deallocated|Liquidat|SettledFunding/.test(x.eventType)).slice(0, 20)
      .map((x: any) => ({ time: x.timestamp, digest: x.txDigest, symbol: x.marketId ? name(x.marketId) : '', kind: String(x.eventType).split('<')[0].split('::').pop(),
        usd: x.collateralChangeUsd })) };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S14] RATES, COLLATERAL, COIN LOOKUP
// /rates-all (every coin on every market), /collateral, slippage tiers, coinOf() (symbol -> type + decimals)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── rates for every coin Kivy knows (2026-10-19); each row also carries the coin type + decimals Kivy lends with ──
const KIVY_JSON: any = JSON.parse(readFileSync(new URL('../knowledge/kivy.json', import.meta.url), 'utf8'));
const KIVY_COINS: Record<string, any> = KIVY_JSON.coins ?? {};
const SWAP_SLIP = Math.min(0.01, Number(KIVY_JSON.slippage_cap_pct ?? 0.5) / 100);   // Kivy's ceiling (0.5%), same as plain swaps
// per pair, as low as it safely goes (2026-10-24): the tightest tier holding BOTH coins — stablecoins 0.05%, SUI and its
// staked versions 0.1%, big coins 0.2% — else the ceiling. Same rule as slip_for() in server.py (knowledge/kivy.json slippage_tiers).
function slipFor(a: string, b: string): number {
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
const normType = (t: string) => { try { return normalizeStructTag(t); } catch { return t; } };
// Other versions of a coin, shown under it (2026-10-19): SUI's liquid-staking tokens, BTC's wrapped / staked kinds
const VARIANTS: Record<string, string[]> = { SUI: ['vSUI', 'haSUI', 'sSUI'], BTC: ['LBTC', 'wBTC', 'WBTC', 'suiWBTC'],
                                             USDC: ['USDsui', 'USDSUI'] };   // USDC row group = the 'Stable' filter
const VARIANT_OF: Record<string, string> = Object.fromEntries(Object.entries(VARIANTS).flatMap(([a, ss]) => ss.map((x) => [x, a])));
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
const SHOW_VOLO = false;
// GET /rates-all — one row per (market, coin): yearly %, coin type, decimals. Caller: server.py lend_rates_all().
// rowsAll() keeps it 5 minutes for coinOf() and positionsAll().
async function ratesAll() {
  const stake = await lstApys().catch(() => ({} as Record<string, number>));
  const assets = Object.entries(KIVY_COINS).map(([sym, c]: any) => ({ sym, type: normType(c.type) }));
  const rows: any[] = [];
  await Promise.all(MARKETS.map((m) => settle(async () => {
    if (m.protocol === 'suilend') {
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
    } else if (m.kind === 'lending') {
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
    } else {
      rows.push({ market: m.key, name: m.name, protocol: 'navi', asset: 'USDC', type: normType(USDC), dec: DEC, total_apr: await rate(m) });   // NAVI's USDC vaults
    }
  })));
  await settle(async () => {                                                   // NAVI's own SUI vaults
    for (const v of (await vaults()) as any[]) {
      const sm = SUI_VAULT_MARKETS.find((x) => x.vaultName === v.name); if (!sm) continue;
      rows.push({ market: sm.key, name: v.name.replace('NAVI SUI', 'NAVI'), protocol: 'navi',
                  asset: 'SUI', vault: true, type: SUI_T, dec: 9, total_apr: Number(v.apy?.instant ?? v.apy?.target ?? 0) * 100 });
    }
  });
  // Volo vaults — HIDDEN for now (2026-10-20: "not from volo yet"); flip SHOW_VOLO to list them again.
  // Volo vaults (NAVI's sister protocol; BTC vaults and outside curators): RATES ONLY — their deposits and withdrawals
  // are queued for an operator, not settled in the transaction (2026-10-20). Open, unlocked, ≥ $100k only.
  if (SHOW_VOLO) await settle(async () => {
    const title = (n: string) => n.toLowerCase().replace(/\b[a-z]/g, (c) => c.toUpperCase()).replace(/\bSui\b/g, 'SUI').replace(/\bLst\b/g, 'LST')
                                 .replace(/\bNavi\b/g, 'NAVI').replace(/\bXbtc\b/g, 'xBTC').replace(/\bWbtc\b/g, 'WBTC').replace(/#/g, ' #');
    for (const v of (await vaults()) as any[]) {
      if (v.source !== 'volo' || v.status !== 'open' || v.lockup || !(Number(v.totalStakedUsd) >= 100_000)) continue;
      const t = normType(v.assets?.baseCoin?.coinType ?? '');
      const own = Object.entries(KIVY_COINS).find(([, c]: any) => normType(c.type) === t)?.[0];
      const tok = own ? undefined : rows.find((r) => r.token && r.type && normType(r.type) === t)?.token;
      const asset = own ?? (tok ? VARIANT_OF[tok] : undefined);
      if (!asset) continue;
      rows.push({ market: 'volo-' + String(v.id).slice(2, 10), name: 'Volo ' + (v.name === 'wBTC' ? 'wBTC' : title(v.name)), protocol: 'volo',
                  asset, token: tok, type: t, dec: Number(v.assets.baseCoin.decimals), vault: true, rate_only: true,
                  tvl_usd: Math.round(Number(v.totalStakedUsd)), total_apr: Number(v.apy?.instant ?? v.apy?.target ?? 0) * 100 });
    }
  });
  return rows.filter((r) => Number.isFinite(r.total_apr));
}

// What borrowers post as collateral in each market (2026-10-23): lenders' risk is the collateral behind the loans —
// if it crashes faster than liquidations can sell it, the market takes bad debt. A coin counts when its LTV > 0.
// NAVI's managed vaults publish no collateral list, only NAVI's own risk level.
const webIcon = (u: any) => (typeof u === 'string' && /^https:\/\//.test(u) && u.length < 400 ? u : null);   // no inline data: icons
// GET /collateral — caller: server.py lend_collateral()
async function collateralAll() {
  const out: Record<string, any> = {};
  const num = (x: any) => { const n = Number(String(x ?? 0)); return Number.isFinite(n) ? n : 0; };
  await Promise.all(MARKETS.map((m) => settle(async () => {
    if (m.protocol === 'suilend') {
      const { init } = await suilend(m);
      out[m.key] = { coins: (Object.values(init.reserveMap) as any[]).filter((r) => num(r.config?.openLtvPct) > 0).map((r) => ({
        sym: r.token?.symbol ?? r.symbol, type: normType(r.coinType), icon: webIcon(r.token?.iconUrl ?? r.iconUrl),
        ltv: num(r.config.openLtvPct), liq: num(r.config.closeLtvPct), usd: Math.round(num(r.depositedAmountUsd)) })) };
    } else if (m.kind === 'lending') {
      const ps: any = await (navi as any).getPools({ cacheTime: 60_000 });
      out[m.key] = { coins: ((Array.isArray(ps) ? ps : Object.values(ps)) as any[]).filter((p) => !p.isDeprecated && num(p.ltv) / 1e27 > 0).map((p) => ({
        sym: p.token?.symbol ?? p.symbol, type: normType(p.suiCoinType ?? p.coinType), icon: webIcon(p.token?.logoUri),
        ltv: Math.round(num(p.ltv) / 1e25), liq: Math.round(num(p.liquidationFactor?.threshold) * 100), usd: Math.round(num(p.poolSupplyValue)) })) };
    } else {
      const v: any = await vaultOf(m); out[m.key] = { vault: true, risk: v.riskLevel ?? null };
    }
  })));
  await settle(async () => {
    for (const sm of SUI_VAULT_MARKETS) { const v: any = (await vaults()).find((x: any) => x.name === sm.vaultName); if (v) out[sm.key] = { vault: true, risk: v.riskLevel ?? null }; }
  });
  for (const k of Object.keys(out)) if (out[k].coins) out[k].coins.sort((a: any, b: any) => b.usd - a.usd);
  return out;
}

// A market + coin symbol -> its coin type and decimals. Kivy's own coins come from knowledge/kivy.json; the
// other versions (vSUI, LBTC, USDsui…) from the market's own listing, so the type is exactly what that market takes.
type Coin = { type: string; dec: number; sym: string };
const SUI_T = normType('0x2::sui::SUI');
let _rowsAll: { at: number; v: Promise<any[]> } | null = null;
function rowsAll() {
  if (!_rowsAll || Date.now() - _rowsAll.at > 300_000) {
    const v = ratesAll();
    _rowsAll = { at: Date.now(), v };
    v.catch(() => { _rowsAll = null; });
  }
  return _rowsAll.v;
}
async function coinOf(key: string, sym: string): Promise<Coin> {
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

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S15] RETRY + CHECK-2 FACTS
// staleRetry() for stale Suilend prices; txFacts() = what a signed transaction really did (GET /tx-facts)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// A Suilend withdrawal whose on-chain price is stale: wait and build again (2 more tries, 20 s apart)
async function staleRetry<T>(f: () => Promise<T>): Promise<T> {
  for (let i = 0; ; i++) {
    try { return await f(); }
    catch (e: any) {
      if (i >= 2 || !/price isn't fresh/.test(String(e?.message))) throw e;
      await new Promise((r) => setTimeout(r, 20_000));
    }
  }
}

// ── what a signed transaction really did (for Kivy's after-the-fact check against the plan) ──
// GET /tx-facts — caller: server.py check_tx() (check 2: the landed transaction against the plan). Read-only.
async function txFacts(digest: string) {
  const r: any = await (grpc as any).core.getTransaction({ digest, include: { effects: true, balanceChanges: true, transaction: true } });
  const t = r.Transaction ?? r.FailedTransaction ?? r.transaction ?? r;
  const st = t.effects?.status ?? t.status ?? {};
  const own = (o: any) => o?.AddressOwner ?? o?.ConsensusAddressOwner?.owner ?? null;
  const sender = t.transaction?.sender ?? null;
  const moved = (t.effects?.changedObjects ?? []).filter((o: any) => own(o.inputOwner) && own(o.outputOwner) && normType(own(o.inputOwner) + '::a::b') !== normType(own(o.outputOwner) + '::a::b'))
    .map((o: any) => ({ object: o.objectId, from: own(o.inputOwner), to: own(o.outputOwner) }));
  const modules = [...new Set((t.transaction?.commands ?? []).filter((c: any) => c.MoveCall).map((c: any) => c.MoveCall.module + '::' + c.MoveCall.function))];
  const packages = [...new Set((t.transaction?.commands ?? []).filter((c: any) => c.MoveCall).map((c: any) => normType(c.MoveCall.package + '::a::b').split('::')[0]))];
  return { ok: true, success: st.success !== false, error: st.success === false ? JSON.stringify(st.error ?? st).slice(0, 200) : null, sender,
           balance_changes: (t.balanceChanges ?? []).map((b: any) => ({ address: b.address, coin_type: normType(b.coinType), amount: String(b.amount) })),
           moved, modules, packages,
           calls: [...new Set((t.transaction?.commands ?? []).filter((c: any) => c.MoveCall).map((c: any) => normType(c.MoveCall.package + '::a::b').split('::')[0] + '::' + c.MoveCall.module + '::' + c.MoveCall.function))] };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S16] COMMAND DISPATCHER
// handle(cmd, args): maps every command / GET path to its function above
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// run f; an error becomes {error} so one market failing never fails a whole list
const settle = async (f: () => Promise<any>) => { try { return await f(); } catch (e: any) { if (process.env.KIVY_DEBUG) console.error("settle:", e?.message ?? e); return { error: String(e?.message ?? e).slice(0, 200) }; } };

// One name per command: the CLI (`tsx builder.ts <cmd> <args…>`) and every GET endpoint (/<cmd>?a=…&a=…) land here.
// Builders that swap are wrapped in routed() (router failover); Suilend withdrawals in staleRetry().
async function handle(cmd: string, args: string[]) {
  if (cmd === 'rates') {
    const rs = await Promise.all(MARKETS.map((m) => settle(async () => ({ total_apr: await rate(m) }))));
    return { ok: true, rates: MARKETS.map((m, i) => ({ market: m.key, name: m.name, kind: m.kind, ...rs[i] })) };
  }
  // the backup path for kivy-site's wallet reads when Sui GraphQL is down (Mysten retired public JSON-RPC, 2026-10-23)
  if (cmd === 'grpc-balances') {
    const rows: any[] = []; let cursor: string | null = null;
    for (let i = 0; i < 10; i++) {
      const r: any = await (grpc as any).core.listBalances({ owner: args[0], limit: 50, cursor });
      rows.push(...r.balances.map((b: any) => ({ coinType: b.coinType, totalBalance: b.balance })));
      if (!r.hasNextPage) break; cursor = r.cursor;
    }
    return { ok: true, balances: rows };
  }
  if (cmd === 'tatum-balances') {                // Tatum alone (health checks): is the backup answering?
    const t0 = Date.now();
    const r: any = await (await tatumOnly()).core.listBalances({ owner: args[0], limit: 50 });
    return { ok: true, ms: Date.now() - t0, balances: r.balances.map((b: any) => ({ coinType: b.coinType, totalBalance: b.balance })) };
  }
  if (cmd === 'chain-stats') return { ok: true, ...chainStats };
  if (cmd === 'grpc-name') {
    const r: any = await (grpc as any).core.resolveNameServiceAddress({ name: String(args[1] ?? '') });
    return { ok: true, address: r?.address ?? null };
  }
  if (cmd === 'rates-all') return { ok: true, rates: await ratesAll() };
  if (cmd === 'collateral') return { ok: true, markets: await collateralAll() };
  if (cmd === 'positions') {
    _naviPos.delete(args[0]); _vaultPos.delete(args[0]);
    const rs = await sharedRead(() => Promise.all(MARKETS.map((m) => settle(() => position(m, args[0])))));
    return { ok: true, positions: MARKETS.map((m, i) => ({ market: m.key, name: m.name,
      ...(rs[i].error ? { error: rs[i].error } : { usdc: rs[i].usdc }) })) };
  }
  if (cmd === 'rewards') {
    const rs = await rewards(args[0], args[1]);
    return { ok: true, usd: rs.reduce((a, r) => a + r.usd, 0), rewards: rs.map(({ raw, ...r }) => r) };
  }
  if (cmd.startsWith('perp') && !perpCtx.getStore() && !['perp-account', 'perp-params', 'perp-preview', 'perp-positions', 'perp-history'].includes(cmd))
    return perpCtx.run({ cmd, args }, () => handle(cmd, args));
  if (cmd === 'perp-account') return { ok: true, ...(await perpAccount(args[0])) };
  if (cmd === 'perp') return await buildPerp(args[0], args[1], args[2] ?? '0');
  if (cmd === 'perp-params') {                         // one market's numbers for Kivy's order math (2026-10-23): price, margins, fees
    const sym = args[0] === '0x0' ? args[1] : args[0];   // over HTTP the first argument is always an address slot ('0x0')
    const m: any = await perpMarket(sym); const p = m.marketParams;
    const px: any = await af('markets/prices', { marketIds: [m.objectId] });
    return { ok: true, symbol: String(sym).toUpperCase(), mark: Number(px.marketsPrices?.[0]?.markPrice ?? 0), imr: Number(p.marginRatioInitial),
             mmr: Number(p.marginRatioMaintenance), taker: Number(p.takerFee), maker: Number(p.makerFee), min_usd: Number(p.minOrderUsdValue ?? 1),
             max_lev: Math.round(1 / Number(p.marginRatioInitial)) };
  }
  if (cmd === 'perp-preview') return { ok: true, ...(await preview(args[0] === '0x0' ? '' : args[0], args[1], Number(args[2]), Number(args[3]), Number(args[4]))) };
  if (cmd === 'perp-order') return await buildOrder(args[0], args[1], Number(args[2]), Number(args[3]), Number(args[4]), Number(args[5]),
                                                     Number(args[6]) || undefined, Number(args[7]) || undefined);
  if (cmd === 'perp-positions') return { ok: true, ...(await perpPositions(args[0])) };
  const n = (i: number) => Number(args[i]);
  if (cmd === 'perp-limit') return await buildLimit(args[0], args[1], n(2), n(3), n(4), n(5), args[6] === '1', n(7) || undefined, n(8) || undefined);
  if (cmd === 'perp-stop') return await buildStop(args[0], args[1], n(2), n(3), n(4), n(5), n(6) || 0);
  if (cmd === 'perp-scale') return await buildScale(args[0], args[1], n(2), n(3), n(4), n(5), n(6), n(7));
  if (cmd === 'perp-twap') return await buildTwap(args[0], args[1], n(2), n(3), n(4), n(5), n(6), n(7) || 1);
  if (cmd === 'perp-cancel') return await buildCancel(args[0], args[1], args[2], args[3], args[4] === '1');
  if (cmd === 'perp-margin') return await buildMargin(args[0], args[1], n(2));
  if (cmd === 'perp-history') return { ok: true, ...(await perpHistory(args[0])) };
  if (cmd === 'perp-close') return await buildClose(args[0], args[1], n(2) || 1, n(3) || 1);
  if (cmd === 'perp-tpsl') return await buildTpSl(args[0], args[1], n(2) || 0, n(3) || 0, n(4) || 1, n(5) || 0);
  if (cmd === 'perp-tpsl-multi')                     // take-profits as "price x share" pieces joined by "-": 900x0.3-910x0.3-930x0.4
    return await buildTpSlMulti(args[0], args[1], n(2) || 0, String(args[3] || '').split('-').filter(Boolean).map((p) => p.split('x').map(Number) as [number, number]));
  if (cmd === 'build' && args[2] === 'claim') return await buildClaim(args[0], args[1]);
  if (cmd === 'build' && args[2] === 'withdraw') return await staleRetry(() => build(args[0], args[1], args[2], args[3], args[4] || 'USDC'));
  if (cmd === 'build') return await build(args[0], args[1], args[2], args[3], args[4] || 'USDC');
  if (cmd === 'withdrawswap') return await staleRetry(() => routed(() => buildWithdrawSwap(args[0], args[1], args[2], args[3], args[4], args[5] || undefined)));
  if (cmd === 'tx-facts') return await txFacts(args[0]);
  if (cmd === 'compose') return await routed(() => buildCompose(args[0], JSON.parse(args[1])));
  if (cmd === 'dust') return await routed(() => buildDust(args[0], args[1], JSON.parse(args[2])));   // CLI testing: dust <addr> <TO> '<items json>'
  if (cmd === 'sendmany') return await staleRetry(() => buildSendMany(args[0], JSON.parse(args[1])));      // CLI testing: compose <addr> '<json>'
  if (cmd === 'known-coins') return { ok: true, coins: await knownCached() };
  if (cmd === 'swaplend') return await routed(() => buildSwapLend(args[0], args[1], args[2], args[3], args[4]));
  if (cmd === 'positions-all') { _naviPos.delete(args[0]); _vaultPos.delete(args[0]); return { ok: true, positions: await sharedRead(() => positionsAll(args[0])) }; }
  return { ok: false, error: 'usage: rates | positions <addr> | rewards <addr> [market] | build <addr> <market|all> <deposit|withdraw|claim> <amount|all> | serve' };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S17] PROCESS ENTRY
// `serve` = the HTTP service on localhost:8799 (POST routes, then GET -> handle); anything else = one CLI command
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// One request's stray error must never stop the builder for everyone (2026-10-24: it exited ~70 times since 10-20 on a
// coin-metadata 503, a failed fetch, a wallet without enough USDC — each a promise nothing awaited). Log it, keep serving;
// the request itself still gets its own error. watchdog counts these lines.
process.on('unhandledRejection', (e: any) => console.error('kivy-lend: stray error (kept running):', String(e?.stack ?? e?.message ?? e).split('\n').slice(0, 3).join(' | ')));
process.on('uncaughtException', (e: any) => console.error('kivy-lend: stray exception (kept running):', String(e?.stack ?? e?.message ?? e).split('\n').slice(0, 3).join(' | ')));
const [cmd, ...args] = process.argv.slice(2);
if (cmd === 'serve') {
  // Long-lived: keeps the markets loaded. Listens on localhost only — Kivy's server is the sole caller.
  const http = await import('node:http');
  const ADDR = /^0x[0-9a-fA-F]{64}$/;
  let inflight = 0;                                    // at most 16 builds at once; beyond that a quick 'busy' (audit V11)
  let lastUse = Date.now();
  http.createServer(async (req, res) => {
    if (!/^\/(grpc-balances|tatum-balances|perp-params|health)\b/.test(req.url ?? '')) lastUse = Date.now();
    if (inflight >= 16) { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: false, error: 'The transaction builder is busy — try again in a moment.' })); return; }
    inflight++; res.on('close', () => { inflight--; });
    const u = new URL(req.url ?? '/', 'http://x');
    // POST routes (JSON body; server.py lend_post()): /compose <- /api/tx/compose · /swapcheck + /swapalt <- swap_tx()
    // (/api/tx/swap) · /kind-calls <- the allowlist check of a plain swap's kind · /dust <- dust_build() · /sendmany <-
    // /api/tx/sendmany · /perp-pro-orders <- /api/perp/pro-orders. Bodies are size-capped and the address checked first.
    if (req.method === 'POST' && u.pathname === '/compose') {               // a chain of steps -> one transaction
      let raw = ''; for await (const ch of req) { raw += ch; if (raw.length > 10000) break; }
      let o: any;
      try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await staleRetry(() => routed(() => buildCompose(b.address, b.spec))); }
      catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 300) }; }
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); return;
    }
    if (req.method === 'POST' && (u.pathname === '/swapcheck' || u.pathname === '/swapalt')) {   // the plain swap card (above)
      let raw = ''; for await (const ch of req) { raw += ch; if (raw.length > 400000) break; }
      let o: any;
      try {
        const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address');
        if (u.pathname === '/swapcheck') o = await swapCheck(b.address, String(b.kind));
        else {
          const slip = Number(b.slip), base = BigInt(String(b.amount).replace('n', ''));
          if (!(slip > 0 && slip <= 0.05) || !(base > 0n) || !/^0x[0-9a-fA-F]+::\w+::\w+$/.test(String(b.from)) || !/^0x[0-9a-fA-F]+::\w+::\w+$/.test(String(b.to))) throw new Error('bad swap');
          o = await swapAlt(b.address, String(b.from), String(b.to), base, slip, BigInt(String(b.min ?? '0').replace(/\D/g, '') || '0'));
        }
      } catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 300) }; }
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); return;
    }
    if (req.method === 'POST' && u.pathname === '/kind-calls') {            // the programs a built transaction kind calls (the server's allowlist check, S-02)
      let raw = ''; for await (const ch of req) { raw += ch; if (raw.length > 400000) break; }
      let o: any;
      try {
        const cmds = Transaction.fromKind(Buffer.from(String(JSON.parse(raw).kind), 'base64')).getData().commands;
        o = { ok: true, others: cmds.filter((c: any) => !c.MoveCall).map((c: any) => c.$kind ?? Object.keys(c)[0]),   // audit nv #2
              calls: cmds.filter((c: any) => c.MoveCall).map((c: any) => ({ package: c.MoveCall.package, module: c.MoveCall.module, function: c.MoveCall.function })) };
      } catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 200) }; }
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); return;
    }
    if (req.method === 'POST' && u.pathname === '/dust') {                  // the dust sweep: many tiny coins -> one coin, one transaction
      let raw = ''; for await (const ch of req) { raw += ch; if (raw.length > 10000) break; }
      let o: any;
      try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await staleRetry(() => routed(() => buildDust(b.address, String(b.to), b.items))); }
      catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 300) }; }
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); return;
    }
    if (req.method === 'POST' && u.pathname === '/sendmany') {              // the Send card: several transfers -> one transaction
      let raw = ''; for await (const ch of req) { raw += ch; if (raw.length > 10000) break; }
      let o: any;
      try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await staleRetry(() => buildSendMany(b.address, b.lines)); }
      catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 300) }; }
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); return;
    }
    if (req.method === 'POST' && u.pathname === '/perp-pro-orders') {
      let raw = ''; for await (const ch of req) { raw += ch; if (raw.length > 10000) break; }
      let o: any;
      try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await perpProOrders(b.address, String(b.bytes), String(b.signature)); }
      catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 300) }; }
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); return;
    }
    // every other path is a GET command: /<cmd>?a=<address or 0x0>&a=… -> handle(cmd, args) (server.py lend_cli / grpc_backup).
    // Args must match a short character set and the first one must be an address (or 0x0, or a digest for /tx-facts).
    const a = u.searchParams.getAll('a');
    let o: any;
    try {
      const dig = u.pathname === '/tx-facts' && /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(a[0] ?? '');   // read-only: a transaction digest
      if (a.some((x) => !/^[0-9a-zA-Z.x-]{1,80}$/.test(x)) || (a[0] && a[0] !== '0x0' && !ADDR.test(a[0]) && !dig)) o = { ok: false, error: 'bad args' };
      else o = await handle(u.pathname.slice(1), a);
    } catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 400) }; }
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o));
  }).listen(8799, '127.0.0.1', () => console.error('kivy-lend listening on 127.0.0.1:8799'));
  // keep the lending markets warm only while someone uses Kivy: idle, it re-loaded 3 Suilend markets ~1,600 times a day (W-02).
  // The watchdog's health checks don't count as use.
  const warm = () => { if (Date.now() - lastUse > 600_000) return; for (const m of MARKETS) if (m.protocol === 'suilend') suilend(m).catch(() => {}); vaults().catch(() => {}); };
  warm(); setInterval(warm, 55_000);
} else {
  try { out(await handle(cmd, args)); } catch (e: any) { out({ ok: false, error: String(e?.message ?? e).slice(0, 400) }); }
  process.exit(0);
}
