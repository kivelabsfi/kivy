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
 *   files: this one is the entry (HTTP service, CLI, handle()); the work is in core.ts (shared: constants, settings, allowlist +
 *          call rules, simulate) · suilend.ts / navi.ts (one adapter per lending protocol SDK) · lending.ts (lend / withdraw,
 *          rates, positions, rewards, coin lookup) · routers.ts (Aftermath -> Cetus swaps, checkAppended) · perps.ts (Aftermath
 *          perpetuals + perpGuard) · compose.ts (multi-step builds, dust, multi-send). Imports go one way only (index below).
 *   links: chain.ts (Sui gRPC with failover) · merge.ts (joins two transaction kinds) · ../knowledge/kivy.json (coins, fees,
 *          slippage tiers, route blocklist) · kivy_pkgs.json (package allowlist) · perps_markets.json (verified perps
 *          markets, kept by tools/sync_perps_markets.py) · SDKs: Suilend, NAVI lending + vault, Cetus aggregator
 *          · HTTP: Aftermath router / perpetuals / price-info, Suilend SpringSui APY, Sui GraphQL (coin metadata)
 *
 * ── ONE REQUEST (example: swap then lend) ──────────────────────────────────────────────────────────────────────
 *
 *   page ──POST /api/tx/swaplend──> server.py (kivy-site)
 *                                      │ lend_cli('swaplend', address, market, from, to, amount)
 *                                      v
 *                      GET localhost:8799/swaplend?a=…        (chains, dust, multi-send: POST via lend_post)
 *                                      │ HTTP server + handle() (builder.ts) -> routed() (routers.ts)
 *                                      v
 *                      buildSwapLend() (compose.ts): routeQuote -> coinWithBalance -> routeTrade -> depositCoin
 *                                      │ Aftermath router, else Cetus; Suilend / NAVI SDKs
 *                                      v
 *                      simulate() (core.ts): dry run on Sui through chain.ts, set the gas budget
 *                                      │ fails -> {ok:false, error} (a route failure tries the next router first)
 *                                      v
 *                      {ok:true, tx: <unsigned tx JSON>, expected_out, min_out, …}
 *                                      │
 *                                      v
 *   server.py adds plan + guard ──> page: check 1 (simulate + compare) ──> wallet signs ──> Sui
 *                               ──> later GET /tx-facts (txfacts.ts) = check 2 (what the signed transaction really did)
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
 *   Every /perp* command may name the perps account (&acct=<n>; spec.perps_account in /compose, perps_account in
 *       /perp-pro-orders; CLI --acct=<n>) — kivy/perpacct.py adds the chat's pick (perps.ts: whose perps account)
 *
 * ── FILES AND SECTIONS (generated by tools/docgen.py from each file's header line and its "// [Sn]" banners — do not edit) ──
 *   chain.ts    Kivy's Sui chain connection: ONE provider list with failover, used by two processes
 *   txfacts.ts  what a signed transaction really did, read from the chain (check 2's evidence), plus the coin-type normaliser.
 *   guard.ts    the builder's own fail-closed rules on what a built transaction does: typed failures, the perps event table, the object rule.
 *     [S23]    16-86    TYPED FAILURES: what failed, from the error object (step, program, module) — route failure or not, without reading error text
 *     [S24]    87-196   PERPS EVENT TABLE: per perps command: the events the chain may show, and how many; anything else is refused (default refuse)
 *     [S25]   197-223   OBJECT RULE: nothing of the wallet's that is not a coin leaves it; nothing that is not a coin is made for someone else
 *   core.ts     shared by every builder module: constants, markets, units, kivy.json settings, allowlist + call rules, simulate().
 *     [S1]    26-82    SETUP: the release id, the allowlist path, token constants, the lending markets Kivy supports, unit helpers, the Coin type
 *     [S2]    83-136   KIVY.JSON SETTINGS: coins, slippage tiers, coin versions, Kivy's swap + lend fees (knowledge/kivy.json)
 *     [S3]   137-188   ALLOWLIST + CALL RULES: kivy_pkgs.json packages, call rules and shared values (re-read every 30 s), the treasury address
 *     [S4]   189-255   SHARED READS + SMALL HELPERS: one chain lookup per request (shared), coin balance / metadata, settle(), the coin Kivy built (coinArg)
 *     [S5]   256-381   SIMULATE: the current epoch, simulate() (dry run + gas budget + expiry) and the route-only check it runs on every build
 *   warm.ts     the keep-warm cache rule: reloaded in the background just before it expires, the old copy served meanwhile.
 *   suilend.ts  the Suilend adapter: every call into the Suilend SDK (markets, reserves, accounts, rewards).
 *     [S6]    18-287   SUILEND: the Suilend SDK: markets loaded (cached 60 s), reserves, accounts (one per coin), rewards; then the adapter lending.ts calls
 *   navi.ts     the NAVI adapter: every call into NAVI's lending and vault SDKs (vaults, positions, rewards).
 *     [S7]    24-393   NAVI: the NAVI lending + vault SDKs: vault list (60 s), positions (15 s, capped), rewards; then the adapter lending.ts calls
 *   lending.ts  lending across protocols: rate, position, lend / withdraw, rewards + claim, coin list, rates-all, coinOf().
 *     [S8]    29-82    RATES, POSITIONS, PLAIN LEND / WITHDRAW: rate(), position(), depositCoin() (into Suilend / NAVI / a vault), build() (GET /build): each picks the protocol adapter
 *     [S9]    83-166   COIN LIST, ALL POSITIONS, REWARDS: /known-coins, /positions-all, /rewards and the claim transaction (build … claim)
 *     [S10]   167-245   RATES-ALL, COLLATERAL, COIN LOOKUP: /rates-all (every coin on every market), /collateral, coinOf() (symbol -> type + decimals)
 *   routers.ts  swaps: Aftermath router, then Cetus; route checks; checkAppended(); the plain swap card's check + backup.
 *     [S11]    23-214   SWAP ROUTERS: Aftermath router, then Cetus (routed / routeQuote / routeTrade), the route block list, the allowlist check of a route
 *     [S12]   215-309   CALL RULES FOR APPENDED STEPS: checkAppended(): what a router or the perps API added must fit the plan; tamper() (tests only)
 *     [S13]   310-348   PLAIN SWAP CARD: the plain swap card's check + backup (POST /swapcheck, /swapalt)
 *   merge.ts    joins two Sui transaction kinds into one, so the user signs once.
 *   perps.ts    Aftermath perpetuals: account, orders, reads, and perpGuard() (every perps transaction checked against the chain).
 *     [S14]    22-408   PERPS ACCOUNT + GUARD: Aftermath perpetuals API, the perps check against the chain (perpGuard), open / deposit / withdraw (GET /perp)
 *     [S15]   409-487   PERPS MARKET ORDERS: market list, order preview (/perp-preview), market order with optional SL / TP (/perp-order)
 *     [S16]   488-747   PERPS PRO ORDERS: limit, stop, scale, TWAP, cancel, close, take-profit / stop-loss — all GET /perp-* and joined into one transaction
 *     [S17]   748-873   PERPS READS: stop / TWAP order list (POST /perp-pro-orders), positions (/perp-positions), add margin (/perp-margin), history
 *   compose.ts  multi-step transactions approved once: swap-then-lend, withdraw-then-swap, /compose chains, dust, multi-send.
 *     [S18]    21-390   MULTI-STEP BUILDS: swap-then-lend (/swaplend), withdraw-then-swap or move (/withdrawswap), any chain of steps (POST /compose)
 *     [S19]   391-504   DUST SWEEP: POST /dust: every coin worth under $0.01 swapped into one coin, one transaction
 *     [S20]   505-552   MULTI-SEND: POST /sendmany: several coins to several addresses, from the wallet or withdrawn from lending
 *   builder.ts  the entry: HTTP service, CLI, handle()
 *     [S21]   153-270   RETRY + COMMAND DISPATCHER: staleRetry() for stale Suilend prices; handle(cmd, args): maps every command / GET path to its function
 *     [S22]   271-488   PROCESS ENTRY: `serve` = the HTTP service on localhost:8799 (POST routes, then GET -> handle); anything else = one CLI command
 */
import { Transaction } from '@mysten/sui/transactions';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { stats as chainStats, grpc, startStatsLog, tatumOnly } from './chain.ts';
import { txFacts as txFactsOf } from './txfacts.ts';
import { refusal, refusalLine } from './guard.ts';
import { allowedPkgs, callWhy, MARKETS, PKGS_LIVE, RELEASE_ID, settle, sharedRead } from './core.ts';
import { suilendAhead } from './suilend.ts';
import { _naviPos, _vaultPos, libraryCount, libraryFull, vaultsAhead } from './navi.ts';
import { WARM } from './warm.ts';
import { build, buildClaim, collateralAll, knownCached, position, positionsAll, rate, ratesAll, rewards } from './lending.ts';
import { routed, swapAlt, swapCheck } from './routers.ts';
import { buildCancel, buildClose, buildLimit, buildMargin, buildOrder, buildPerp, buildScale, buildStop, buildTpSl, buildTpSlMulti, buildTwap, markPrices, perpAccount, perpCtx, perpHistory, perpMarket, perpPositions, perpProOrders, preview, withPick } from './perps.ts';
import { buildCompose, buildDust, buildSendMany, buildSwapLend, buildWithdrawSwap } from './compose.ts';

const STARTED = Date.now();

const out = (o: any) => { process.stdout.write(JSON.stringify(o) + '\n'); };

const txFacts = (digest: string) => txFactsOf(grpc, digest);

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S21] RETRY + COMMAND DISPATCHER
// staleRetry() for stale Suilend prices; handle(cmd, args): maps every command / GET path to its function
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// The slot of the request being answered (serve mode only; fix package 2, a reviewer scan 2026-11-15 B8): staleRetry() gives it back
// for its 20 s wait and takes it again after, so a wait holds no build slot. On the command line there is none.
type Slot = { release: () => void; retake: () => boolean };
const slotCtx = new AsyncLocalStorage<Slot>();
const BUSY = 'The transaction builder is busy — try again in a moment.';

// A Suilend withdrawal whose on-chain price is stale: wait and build again (2 more tries, 20 s apart). The wait holds no slot;
// when the slots are full again after it (or the caller left, or the request's deadline passed) the answer is "busy".
async function staleRetry<T>(f: () => Promise<T>): Promise<T> {
  for (let i = 0; ; i++) {
    try { return await f(); }
    catch (e: any) {
      if (i >= 2 || !/price isn't fresh/.test(String(e?.message))) throw e;
      const slot = slotCtx.getStore();
      slot?.release();
      await new Promise((r) => setTimeout(r, 20_000));
      if (slot && !slot.retake()) throw new Error(BUSY);
    }
  }
}

// txFacts() — what a signed transaction really did (GET /tx-facts; caller: server.py check_tx(), check 2) — is in txfacts.ts.

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
  if (cmd === 'health') return { ok: true, service: 'kivy-lend', release: RELEASE_ID, stage: process.env.KIVY_STAGE || 'live', up_s: Math.round((Date.now() - STARTED) / 1000),
                                 policy: (() => { try { return createHash('sha256').update(readFileSync(process.env.KIVY_PKGS_TEST || PKGS_LIVE)).digest('hex').slice(0, 12); } catch { return ''; } })() };
  // tests only, command line only (tests/test_policy_vectors.py): this builder's call-rules verdict on every vector of a file —
  // the same vectors check 1 and the server must agree on (architecture programme change 5, 2026-11-14)
  if (cmd === 'policy-check' && process.argv[2] === 'policy-check') {
    const V: any[] = JSON.parse(readFileSync(args[0], 'utf8')).vectors ?? [];
    const allow = allowedPkgs();
    return { ok: true, verdicts: V.map((v) => (v.calls ?? []).map((c: string) => { const [p, m, f] = String(c).split('::');
      return allow.has(normalizeSuiAddress(p)) ? callWhy(p, m, f, v.steps ?? []) : ''; }).filter(Boolean)) };
  }
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
    const px: any = await markPrices(m);
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
// [S22] PROCESS ENTRY
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
  // Two slot counters (review scaling study 2026-11-10, 2026-11-11 "go"): builds and reads have 16 each, so slow wallet
  // reads can never lock out a build (it was one counter of 16 for both). Builds also arrive from chat cards and cancel
  // previews, not only the server's build pool, so a burst can still meet a full counter: a quick 'busy' (audit V11), which
  // the server logs as "kivy busy: builder …". /kind-calls takes no slot: it only decodes bytes (no network) and the server's
  // after-build check needs it (a busy answer there refused valid swaps). BUILD_PATHS = every route that builds a
  // transaction; tests/test_slots_20261111.py checks it against handle() and server.py's BUILDER_BUILDS.
  const BUILD_PATHS = /^\/(?:compose|swapcheck|swapalt|dust|sendmany|build|withdrawswap|swaplend|perp|perp-(?:order|limit|stop|scale|twap|cancel|margin|close|tpsl|tpsl-multi))$/;
  const SLOTS = { build: 16, read: 16 }, inflight = { build: 0, read: 0 };
  // the exit lane (architecture programme change 10, 2026-11-14; = server kivy/exits.py): a build that takes money OUT of a
  // position — perps close / cancel / margin / stop, withdraw-and-swap, a lending withdraw, a chain that starts with a
  // withdrawal — may use EXIT_EXTRA slots beyond the shared 16, so in a burst entries are refused first and exits last
  const EXIT_EXTRA = 4;
  const EXIT_PATHS = /^\/(?:perp-(?:close|cancel|margin|tpsl|tpsl-multi)|withdrawswap)$/;
  const isExit = (u: URL, body: string): boolean => {
    if (EXIT_PATHS.test(u.pathname)) return true;
    if (u.pathname === '/build') return u.searchParams.getAll('a')[2] === 'withdraw';
    if (u.pathname === '/compose') { try { return ['withdraw', 'perps_out'].includes(JSON.parse(body)?.spec?.source?.op); } catch { return false; } }
    return false;
  };
  let lastUse = Date.now(); const lastBusyLog = { build: 0, read: 0 };
  // each POST route's body cap (as before); the body is read in full BEFORE a slot is taken, so a slow sender can't hold one
  // (zero-day review 2026-11-13; same idea as server.py's BODY_DEADLINE) — server.requestTimeout below bounds the read itself
  const POST_CAP: Record<string, number> = { '/swapcheck': 400_000, '/swapalt': 400_000, '/kind-calls': 400_000 };
  // request ids (server kivy/reqid.py, 2026-11-14): the server sends its request's id in X-Kivy-Request; every line printed while
  // that request is answered here ends with "[req <id>]" (at the END: the watchdog matches line starts), and each finished
  // build prints one "kivy-lend build:" line — so one id finds the request in kivy-site's and kivy-lend's journals.
  const reqIds = new AsyncLocalStorage<string>();
  for (const k of ['log', 'error', 'warn'] as const) {
    const f = console[k].bind(console);
    console[k] = (...a: any[]) => { const r = reqIds.getStore(); return r ? f(...a, `[req ${r}]`) : f(...a); };
  }
  const short = (s: string) => s.replace(/\s+/g, ' ').replace(/0x[0-9a-fA-F]{40,}/g, (x) => x.slice(0, 6) + '…' + x.slice(-4));   // one line, no wallet ids
  const serveOne = async (req: IncomingMessage, res: ServerResponse) => {
    if (!/^\/(grpc-balances|tatum-balances|perp-params|health|chain-stats)\b/.test(req.url ?? '')) lastUse = Date.now();   // + chain-stats: the watchdog reads it every 5 min (2026-11-12)
    let u: URL;                                                               // a malformed or huge request target: 400 (it threw, unanswered)
    try { if ((req.url ?? '').length > 4000) throw new Error('too long'); u = new URL(req.url ?? '/', 'http://x'); }   // never cut short: a cut amount would read as another number
    catch { req.resume(); res.writeHead(400, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: false, error: 'bad request' })); return; }
    let body = '';
    if (req.method === 'POST') {
      const cap = POST_CAP[u.pathname] ?? 10_000;
      try { for await (const ch of req) { body += ch; if (body.length > cap) break; } }
      catch { return; }                                                       // the caller went away mid-body
    } else req.resume();
    const kind = u.pathname === '/kind-calls' ? null : BUILD_PATHS.test(u.pathname) ? 'build' : 'read';
    // The slot is held until the work really ends — no longer given back when the caller hangs up while the build goes on
    // (fix package 2, a reviewer scan 2026-11-15 B8: in a burst, abandoned builds kept spending CPU and chain calls beyond the 16).
    // staleRetry() gives it back for its 20 s wait and takes it again after (slotCtx). `over`: the caller left or the deadline
    // answered — a retry after the wait stops there instead of building for nobody.
    let slot: Slot | undefined, over = false;
    if (kind) {
      const limit = SLOTS[kind] + (kind === 'build' && isExit(u, body) ? EXIT_EXTRA : 0);
      if (inflight[kind] >= limit) {
        if (Date.now() - lastBusyLog[kind] > 10_000) { lastBusyLog[kind] = Date.now(); console.error(`kivy-lend busy: ${kind} refused, all ${SLOTS[kind]} ${kind} slots taken`); }
        res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: false, error: BUSY })); return;
      }
      inflight[kind]++;
      let held = true;
      slot = {
        release: () => { if (held) { held = false; inflight[kind]--; } },
        retake: () => {
          if (held) return true;
          if (over || inflight[kind] >= limit) return false;
          held = true; inflight[kind]++; return true;
        },
      };
      res.on('close', () => { if (!res.writableEnded) over = true; });   // the caller hung up before its answer
    }
    const t0 = Date.now();
    const send = (o: any) => {                                                // every route's answer (+ the build line for builds)
      if (kind === 'build') {
        const ruled = refusalLine(u.pathname, o);                            // refused by Kivy's own rules: the watchdog's alert line (OPS-A1)
        if (ruled) console.error(short(ruled));
        console.error(`kivy-lend build: ${u.pathname} ${o?.ok === false ? 'refused (' + short(String(o?.error ?? '')).slice(0, 120) + ')' : 'ok'} ${Date.now() - t0} ms`);
      }
      if (o && typeof o === 'object' && 'rule' in o) { const { rule: _r, ...rest } = o; o = rest; }   // the answer stays as before
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o));
    };
    // The route's work: its answer object. The code of each route is as before; only `send(o); return;` became `return o;`.
    const work = async (): Promise<any> => {
      // POST routes (JSON body; server.py lend_post()): /compose <- /api/tx/compose · /swapcheck + /swapalt <- swap_tx()
      // (/api/tx/swap) · /kind-calls <- the allowlist check of a plain swap's kind · /dust <- dust_build() · /sendmany <-
      // /api/tx/sendmany · /perp-pro-orders <- /api/perp/pro-orders. Bodies are size-capped and the address checked first.
      if (req.method === 'POST' && u.pathname === '/compose') {               // a chain of steps -> one transaction
        const raw = body;
        let o: any;
        try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await staleRetry(() => routed(() => buildCompose(b.address, b.spec))); }
        catch (e: any) { o = refusal(e, 300); }
        return o;
      }
      if (req.method === 'POST' && (u.pathname === '/swapcheck' || u.pathname === '/swapalt')) {   // the plain swap card (above)
        const raw = body;
        let o: any;
        try {
          const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address');
          if (u.pathname === '/swapcheck') o = await swapCheck(b.address, String(b.kind));
          else {
            const slip = Number(b.slip), base = BigInt(String(b.amount).replace('n', ''));
            if (!(slip > 0 && slip <= 0.05) || !(base > 0n) || !/^0x[0-9a-fA-F]+::\w+::\w+$/.test(String(b.from)) || !/^0x[0-9a-fA-F]+::\w+::\w+$/.test(String(b.to))) throw new Error('bad swap');
            o = await swapAlt(b.address, String(b.from), String(b.to), base, slip, BigInt(String(b.min ?? '0').replace(/\D/g, '') || '0'));
          }
        } catch (e: any) { o = refusal(e, 300); }
        return o;
      }
      if (req.method === 'POST' && u.pathname === '/kind-calls') {            // the programs a built transaction kind calls (the server's allowlist check, S-02)
        const raw = body;
        let o: any;
        try {
          const cmds = Transaction.fromKind(Buffer.from(String(JSON.parse(raw).kind), 'base64')).getData().commands;
          o = { ok: true, others: cmds.filter((c: any) => !c.MoveCall).map((c: any) => c.$kind ?? Object.keys(c)[0]),   // audit nv #2
                calls: cmds.filter((c: any) => c.MoveCall).map((c: any) => ({ package: c.MoveCall.package, module: c.MoveCall.module, function: c.MoveCall.function })) };
        } catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 200) }; }
        return o;
      }
      if (req.method === 'POST' && u.pathname === '/dust') {                  // the dust sweep: many tiny coins -> one coin, one transaction
        const raw = body;
        let o: any;
        try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await staleRetry(() => routed(() => buildDust(b.address, String(b.to), b.items))); }
        catch (e: any) { o = refusal(e, 300); }
        return o;
      }
      if (req.method === 'POST' && u.pathname === '/sendmany') {              // the Send card: several transfers -> one transaction
        const raw = body;
        let o: any;
        try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await staleRetry(() => buildSendMany(b.address, b.lines)); }
        catch (e: any) { o = refusal(e, 300); }
        return o;
      }
      if (req.method === 'POST' && u.pathname === '/perp-pro-orders') {
        const raw = body;
        let o: any;
        try { const b = JSON.parse(raw); if (!ADDR.test(b.address)) throw new Error('bad address'); o = await withPick(b.perps_account, () => perpProOrders(b.address, String(b.bytes), String(b.signature))); }
        catch (e: any) { o = { ok: false, error: String(e?.message ?? e).slice(0, 300) }; }
        return o;
      }
      // every other path is a GET command: /<cmd>?a=<address or 0x0>&a=… -> handle(cmd, args) (server.py lend_cli / grpc_backup).
      // Args must match a short character set and the first one must be an address (or 0x0, or a digest for /tx-facts).
      // A perps command may name the perps account to use: &acct=<number> (perps.ts acctPick; BLD-V1, 2026-11-17).
      const a = u.searchParams.getAll('a');
      let o: any;
      try {
        const dig = u.pathname === '/tx-facts' && /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(a[0] ?? '');   // read-only: a transaction digest
        if (a.some((x) => !/^[0-9a-zA-Z.x-]{1,80}$/.test(x)) || (a[0] && a[0] !== '0x0' && !ADDR.test(a[0]) && !dig)) o = { ok: false, error: 'bad args' };
        else o = await withPick(u.pathname.startsWith('/perp') ? (u.searchParams.get('acct') ?? '') : '', () => handle(u.pathname.slice(1), a));
      } catch (e: any) { o = refusal(e, 400); }
      return o;
    };
    // One deadline per request, a little past the server's own wait for it (upstream.py BUILD_WAIT 100 s / READ_WAIT 60 s,
    // known-coins 120 s), so the server never sees it fire — it only answers callers that never hang up, and stops a stale-price
    // retry. The slot stays taken until the work really ends; past the deadline + 60 s it is freed anyway (a promise that never
    // settles must not keep a slot for good) and one "kivy-lend slot:" line says so.
    const wait = u.pathname === '/known-coins' ? 125_000 : kind === 'build' ? 105_000 : 65_000;
    const LATE = Symbol('late');
    let timer: ReturnType<typeof setTimeout> | undefined, ceiling: ReturnType<typeof setTimeout> | undefined;
    const job = (slot ? slotCtx.run(slot, work) : work()).catch((e: any) => refusal(e, 400));
    const late = new Promise<typeof LATE>((r) => { timer = setTimeout(() => r(LATE), wait); });
    if (slot) ceiling = setTimeout(() => { slot?.release(); console.error(`kivy-lend slot: ${u.pathname} still running ${Math.round((wait + 60_000) / 1000)} s after it began; its slot is freed`); }, wait + 60_000);
    job.finally(() => { clearTimeout(timer); clearTimeout(ceiling); slot?.release(); });
    const o = await Promise.race([job, late]);
    if (o !== LATE) { send(o); return; }
    over = true;
    console.error(`kivy-lend deadline: ${u.pathname} answered after ${wait / 1000} s; the work goes on until it ends`);
    send({ ok: false, error: 'The transaction builder took too long — try again in a moment.' });
    job.then(() => console.error(`kivy-lend late: ${u.pathname} finished ${Date.now() - t0} ms after it began (answered at the deadline)`));
  };
  const server = http.createServer({ connectionsCheckingInterval: 5_000 }, (req, res) => {   // timeouts below checked every 5 s (Node's default: 30 s)
    const rid = String(req.headers['x-kivy-request'] ?? '');
    return /^[0-9a-f]{12}$/.test(rid) ? reqIds.run(rid, () => serveOne(req, res)) : serveOne(req, res);   // only the server's own id shape
  });
  server.headersTimeout = 5_000; server.requestTimeout = 10_000; server.keepAliveTimeout = 5_000; server.maxHeadersCount = 40; server.maxConnections = 256;   // as gateway.ts (2026-11-13)
  const PORT = Number(process.env.KIVY_BUILDER_PORT || 8799);                // staging twin: 8789 (2026-11-14)
  // systemd's socket when given (kivy-lend.socket stays open across restarts: a build request waits instead of failing),
  // else the port itself (command line, tests). Architecture programme change 4, 2026-11-14.
  if (process.env.LISTEN_PID === String(process.pid) && Number(process.env.LISTEN_FDS || 0) >= 1)
    server.listen({ fd: 3 }, () => console.error(`kivy-lend listening on the systemd socket (release ${RELEASE_ID})`));
  else server.listen(PORT, '127.0.0.1', () => console.error(`kivy-lend listening on 127.0.0.1:${PORT} (release ${RELEASE_ID})`));
  // stop: take no new requests, let the running ones finish (up to 25 s, inside systemd's 30), then exit
  process.on('SIGTERM', () => {
    console.error(`kivy-lend stop: finishing ${inflight.build + inflight.read} request(s)`);
    server.close(() => { console.error('kivy-lend stop: all requests finished'); process.exit(0); });
    server.closeIdleConnections();
    setTimeout(() => { console.error('kivy-lend stop: requests still running after 25 s'); process.exit(0); }, 25_000).unref();
  });
  // keep the lending markets warm only while someone uses Kivy: idle, it re-loaded 3 Suilend markets ~1,600 times a day (W-02).
  // The watchdog's health checks don't count as use. Each tick reloads, in the background, only a copy that would expire before
  // the next one (warm.ts; fix package 4, 2026-11-15: the 55 s tick met a 60 s cache, so markets were cold 50 s of every 110 s).
  const warm = () => { if (Date.now() - lastUse > 600_000) return; for (const m of MARKETS) if (m.protocol === 'suilend') suilendAhead(m); vaultsAhead(); };
  warm(); setInterval(warm, WARM.tick);
  // NAVI's libraries keep every wallet's positions for good (navi.ts libraryFull): past 20,000 wallets or a day, the builder
  // restarts itself through the stop path above once no request is running — systemd's socket holds new requests meanwhile
  // and Restart=always starts it again (fix package 4, 2026-11-15). Only under systemd's socket: a test or hand-run builder
  // never stops on its own.
  const recycle = setInterval(() => {
    if (!libraryFull() || inflight.build + inflight.read > 0 || process.env.LISTEN_PID !== String(process.pid)) return;
    clearInterval(recycle);
    console.error(`kivy-lend recycle: NAVI's library stores hold ${libraryCount()} wallet(s), restarting cleanly to empty them`);
    process.kill(process.pid, 'SIGTERM');
  }, 30_000);
  recycle.unref();
  startStatsLog('kivy-lend');
} else {
  const pick = (args.find((x) => x.startsWith('--acct=')) ?? '').slice(7);   // the perps account, as GET &acct= (BLD-V1)
  try { out(await withPick(pick, () => handle(cmd, args.filter((x) => !x.startsWith('--acct='))))); } catch (e: any) { out({ ok: false, error: String(e?.message ?? e).slice(0, 400) }); }
  process.exit(0);
}
