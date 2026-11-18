/*
 * compose.ts — multi-step transactions approved once: swap-then-lend, withdraw-then-swap, /compose chains, dust, multi-send.
 * In detail: swap-then-lend (/swaplend), withdraw-then-swap or move (/withdrawswap), any chain of steps (POST /compose: a source,
 * then a split or swaps, then lend / send / keep / perps), the dust sweep (POST /dust) and multi-send (POST /sendmany). Each one
 * ends with simulate().
 * Called by: builder.ts (handle() and the POST routes, inside routed() / staleRetry()).
 * Imports: core.ts, guard.ts, lending.ts (build, depositCoin, coinOf), routers.ts, perps.ts, merge.ts.
 */
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import { readFileSync } from 'node:fs';
import { grpc } from './chain.ts';
import { normType } from './txfacts.ts';
import { mergeKinds } from './merge.ts';
import { byKey, type Coin, coinArg, lastSplit, PKGS_LIVE, revenueFee, routerCtx, setPerps, simulate, slipFor, takeLendFee, toUnits, unitsText, USDC } from './core.ts';
import { chainPerpsTable, execErr, failWrap, routeError } from './guard.ts';
import { build, coinOf, depositCoin } from './lending.ts';
import { checkAppended, floorOut, minAfter, routeOut, routeQuote, routeTrade } from './routers.ts';
import { af, idleMarketKinds, needAccount, withPick } from './perps.ts';

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S18] MULTI-STEP BUILDS
// swap-then-lend (/swaplend), withdraw-then-swap or move (/withdrawswap), any chain of steps (POST /compose)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// GET /swaplend — swap coin A into B and lend B on market `key`, one transaction. Caller: server.py /api/tx/swaplend.
// Runs inside routed(), so a failing route is retried on the next router.
export async function buildSwapLend(addr: string, key: string, fromSym: string, toSym: string, amountArg: string) {
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
  if (!r.txKind || !r.coinOutId) throw routeError('Aftermath router returned no swap');
  const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
  const minOut = minAfter(routeOut(route), slip);
  floorOut(t2, r.coinOutId, minOut);                    // Kivy's own floor: the chain enforces the card's minimum (2026-11-15)
  takeLendFee(t2, r.coinOutId, minOut);                 // on what the swap is sure to deliver
  await depositCoin(t2, m, B, r.coinOutId, addr);
  let size = 0;
  try { size = await simulate(t2); }
  catch (e: any) { throw failWrap('This swap-then-lend would fail right now: ', e); }
  const out = Number(routeOut(route)) / 10 ** B.dec;
  return { ok: true, market: key, fee_pct: (revenueFee()?.feePercentage ?? 0) * 100, from: A.sym, to: B.sym, from_type: A.type, from_decimals: A.dec, to_type: B.type, amount,
           expected_out: out, min_out: out * (1 - slip), tx: await t2.toJSON({ client: grpc as any }), bytes: size };
}

// ── withdraw, then swap what came out, one transaction (2026-10-20) ──
// The withdrawn coin(s) stay in the transaction; a slightly smaller piece (99.99%, so rounding and interest can
// never make it short) goes through Aftermath's router and the swapped coin, plus the tiny remainder, go to the wallet.
// lendTo (a market key): a MOVE — withdraw, swap if the coin changes, and lend it on that market, all one transaction
// (2026-10-20: "withdraw ssui from suilend and swap all of it into USDC then lend it in NAVI best yield market")
// GET /withdrawswap — caller: server.py /api/tx/withdrawswap. Uses build(…, keep) for the withdrawal, then a route.
export async function buildWithdrawSwap(addr: string, key: string, sym: string, amountArg: string, toSym: string, lendTo?: string) {
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
    catch (e: any) { throw failWrap('This move would fail right now: ', e); }
    return { ok: true, market: key, lend_to: M2.key, coin: C.sym, coin_type: C.type, to: C.sym, to_type: C.type, amount: w.amount,
             expected_out: w.amount, min_out: w.amount, tx: await tx.toJSON({ client: grpc as any }), bytes: size0 };
  }
  const base = BigInt(Math.floor(Number(w.amount) * 10 ** C.dec * 0.9999)) - 1n;
  if (base <= 0n) throw new Error('Too little to swap');
  const route = await routeQuote(({ coinInType: C.type, coinOutType: T.type, coinInAmount: base.toString() + 'n' }));
  tx.splitCoins(coin, [base]);
  tx.transferObjects([coin], addr);                                  // the remainder (dust) back to the wallet
  const resolved = JSON.parse(await tx.toJSON({ client: grpc as any }));
  const kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
  const at = lastSplit(resolved);
  if (at < 0) throw new Error('could not place the swap');
  const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route,
                                                          slippage: slipFor(C.sym, T.sym), walletAddress: addr, coinInId: { NestedResult: [at, 0] } });
  if (!r.txKind || !r.coinOutId) throw routeError('Aftermath router returned no swap');
  const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
  floorOut(t2, r.coinOutId, minAfter(routeOut(route), slipFor(C.sym, T.sym)));   // Kivy's own floor under the swap (2026-11-15)
  if (M2) {                                                          // move: the swapped coin goes straight into the new market
    takeLendFee(t2, r.coinOutId, minAfter(routeOut(route), slipFor(C.sym, T.sym)));
    await depositCoin(t2, M2, T, r.coinOutId, addr);
  } else t2.transferObjects([r.coinOutId as any], addr);
  let size = 0;
  try { size = await simulate(t2); }
  catch (e: any) {
    if (/unwrap_spot_price|price/i.test(String(e?.message)) && byKey(key).protocol === 'suilend')
      throw new Error(`Your ${byKey(key).name} account also holds coins whose price isn't fresh on chain right now — try again in a minute.`);
    throw failWrap('This withdraw-and-swap would fail right now: ', e);
  }
  const out = Number(routeOut(route)) / 10 ** T.dec;
  return { ok: true, market: key, lend_to: M2?.key ?? null, coin: C.sym, coin_type: C.type, to: T.sym, to_type: T.type, amount: w.amount,
           expected_out: out, min_out: out * (1 - slipFor(C.sym, T.sym)), tx: await t2.toJSON({ client: grpc as any }), bytes: size };
}

// "withdraw 0.2 usdc, lend 0.1 here and 0.1 there": a withdrawal hands back a hair less (0.199979), so exact pieces that
// add up to the whole would fail. A shortfall of 0.1% or less is rounding dust: the last exact piece gives it up.
// Anything bigger is a real over-ask and still refused. (Claude's 3-4 step scenarios, 2026-11-07)
function trimDust(sinks: any[], want: (k: any) => bigint | null, E: bigint) {
  const fixed = sinks.filter((k) => want(k) != null);
  const total = fixed.reduce((t, k) => t + (want(k) as bigint), 0n);
  const short = total - E;
  if (short <= 0n || !fixed.length || short * 1000n > total) return;
  const last = fixed[fixed.length - 1], cut = (want(last) as bigint) - short;
  if (cut > 0n) last.amount = { ...last.amount, raw: cut };
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
  h.tx.splitCoins(h.coin, [base]);
  h.tx.transferObjects([h.coin], addr);                              // remainder (dust / slippage surplus) back to the wallet
  const resolved = JSON.parse(await h.tx.toJSON({ client: grpc as any }));
  const kind = await h.tx.build({ client: grpc as any, onlyTransactionKind: true });
  const at = lastSplit(resolved);
  if (at < 0) throw new Error('could not place the swap');
  const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route,
                                                          slippage: slipFor(h.C.sym, B.sym), walletAddress: addr, coinInId: { NestedResult: [at, 0] } });
  if (!r.txKind || !r.coinOutId) throw routeError('Aftermath router returned no swap');
  const t2 = Transaction.fromKind(r.txKind); t2.setSender(addr);
  const exp = routeOut(route);
  const min = minAfter(exp, slipFor(h.C.sym, B.sym));
  floorOut(t2, r.coinOutId, min);                                    // Kivy's own floor: the next steps are planned on `min` (2026-11-15)
  return { tx: t2, coin: r.coinOutId, C: B, est: min, exp };
}

// POST /compose — caller: server.py /api/tx/compose (lend_post). Source (wallet / withdraw / perps_out / dust) -> optional
// "plus" from the wallet -> a fan-out split OR up to 2 swaps -> 1-4 sinks (lend / send / keep / perps), then simulate.
// spec.perps_account: the perps account the card named (BLD-V1); its perps steps use that one or nothing (perps.ts needAccount).
export async function buildCompose(addr: string, spec: any) {
  return withPick(spec?.perps_account, () => composeSteps(addr, spec));
}
async function composeSteps(addr: string, spec: any) {
  const src = spec?.source ?? {}, swaps: any[] = spec?.swaps ?? [], sinks: any[] = spec?.sinks ?? [];
  if (swaps.length > 2) throw new Error('At most 2 swaps in one transaction');
  if (!sinks.length || sinks.length > 4) throw new Error('A chain needs 1 to 4 final steps (lend / send / keep)');
  if (sinks.filter((k) => k.amount?.rest).length > 1) throw new Error('Only one step can take "the rest"');
  let h: Hand;
  const plan: any[] = [];
  if (src.op === 'wallet') {
    const C = await coinOf('', src.coin);
    const base = toUnits(src.amount, C.dec);
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
    for (const k of [...idle.kinds, w.txKind]) checkAppended(null, Buffer.from(String(k), 'base64'), 'Aftermath perps', addr, ['perps']);   // 2026-11-13: Aftermath wrote all of it
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
    const add = toUnits(src.plus, h.C.dec);
    if (!(add > 0n)) throw new Error('Nothing to add from the wallet');
    const extra = h.tx.add(coinWithBalance({ type: h.C.type, balance: add }));
    h.tx.mergeCoins(h.coin, [extra]);
    h = { ...h, est: h.est + add };
    plan.push({ op: 'plus', coin: h.C.sym, amount: Number(add) / 10 ** h.C.dec });
  }
  // fan-out (2026-10-24: "withdraw hasui from navi, then 25% each into USDC, USDsui, SUI and deep then deposit into suilend"):
  // every final step names its own coin ('to'). The coin in hand is cut into one piece per step in ONE split, each piece is
  // swapped on its own route, then lent, sent or kept; the leftover of the split (dust) goes back to the wallet.
  // A send names its coin in 'coin' (its 'to' is the address) — "swap a third to SUI, send the SUI to 0x…" (2026-11-08).
  const fanCoin = (k: any) => k.op === 'send' ? k.coin : k.to;
  if (sinks.some((k) => (k.to && !/^0x/i.test(String(k.to))) || k.coin)) {
    if (swaps.length) throw new Error('Split into several coins OR swap first, not both');
    if (sinks.some((k) => !['lend', 'keep', 'send', 'perps'].includes(k.op))) throw new Error('Each coin of a split can be lent, sent, kept or put into perps');
    if (sinks.filter((k) => k.op === 'perps').length > 1) throw new Error('One perps deposit per transaction');
    if (sinks.some((k) => k.op === 'send' && !/^0x[0-9a-fA-F]{64}$/.test(String(k.to)))) throw new Error('Send needs a full 0x address');
    const E = h.est;
    const want = (k: any) => k.amount?.rest ? null : k.amount?.raw != null ? k.amount.raw : k.amount?.units != null ? toUnits(k.amount.units, h.C.dec)
                           : k.amount?.frac != null ? BigInt(Math.floor(Number(E) * Number(k.amount.frac))) : null;
    trimDust(sinks, want, E);
    const fixed = sinks.map(want).filter((x) => x != null) as bigint[];
    const sum = fixed.reduce((t, x) => t + x, 0n);
    if (sum > E) throw new Error(`Those amounts add up to more than the ${Number(E) / 10 ** h.C.dec} ${h.C.sym} you'd have`);
    const amts = sinks.map((k) => want(k) ?? (E - sum));
    if (amts.some((a) => a <= 0n)) throw new Error('One of the amounts is zero');
    h.tx.splitCoins(h.coin, amts);
    h.tx.transferObjects([h.coin], addr);                              // what the split leaves (dust) back to the wallet
    const resolved = JSON.parse(await h.tx.toJSON({ client: grpc as any }));
    const at = lastSplit(resolved);
    if (at < 0) throw new Error('could not place the split');
    let kind = await h.tx.build({ client: grpc as any, onlyTransactionKind: true });
    const legs: { coin: any; C: Coin; est: bigint }[] = [];
    for (let i = 0; i < sinks.length; i++) {
      const piece = { $kind: 'NestedResult', NestedResult: [at, i] } as any;
      const B = await coinOf('', fanCoin(sinks[i]) ?? h.C.sym);
      if (normType(B.type) === normType(h.C.type)) { legs.push({ coin: piece, C: h.C, est: amts[i] }); continue; }
      const route = await routeQuote(({ coinInType: h.C.type, coinOutType: B.type, coinInAmount: amts[i].toString() + 'n' }));
      const r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: route,
                                                              slippage: slipFor(h.C.sym, B.sym), walletAddress: addr, coinInId: { NestedResult: [at, i] } });
      if (!r.txKind || !r.coinOutId) throw routeError('Aftermath router returned no swap');
      const exp = routeOut(route);
      const min = minAfter(exp, slipFor(h.C.sym, B.sym));
      const tf = Transaction.fromKind(r.txKind); tf.setSender(addr);
      floorOut(tf, r.coinOutId, min);                                    // Kivy's own floor under this leg (2026-11-15)
      kind = await tf.build({ client: grpc as any, onlyTransactionKind: true });
      plan.push({ op: 'swap', from: h.C.sym, to: B.sym, in: Number(amts[i]) / 10 ** h.C.dec, expected: Number(exp) / 10 ** B.dec, min: Number(min) / 10 ** B.dec });
      legs.push({ coin: r.coinOutId, C: B, est: min });
    }
    let tx = Transaction.fromKind(kind); tx.setSender(addr);
    let perpLeg: { coin: any; est: bigint } | null = null;
    for (let i = 0; i < sinks.length; i++) {
      const k = sinks[i], L = legs[i];
      if (k.op === 'perps') {                                              // margin: Aftermath appends its deposit, so it goes in last (below)
        if (normType(L.C.type) !== normType(USDC)) throw new Error('Perps margin is USDC — swap that part to USDC first');
        perpLeg = { coin: L.coin, est: L.est };
        plan.push({ op: 'perps', coin: 'USDC', type: L.C.type, dec: L.C.dec, amount: Number(L.est) / 1e6, rest: false });
        continue;
      }
      if (k.op === 'lend') {
        const M = byKey(k.market);
        const C2 = await coinOf(k.market, L.C.sym).catch(() => L.C);
        if (normType(C2.type) !== normType(L.C.type)) throw new Error(`${M.name} doesn't take ${L.C.sym}`);
        takeLendFee(tx, L.coin, L.est);
        await depositCoin(tx, M, L.C, L.coin, addr);
      } else if (k.op === 'send') tx.transferObjects([L.coin], k.to);
      else tx.transferObjects([L.coin], addr);
      plan.push({ op: k.op, market: k.market, ...(k.op === 'send' ? { to: k.to } : {}), coin: L.C.sym, type: L.C.type, dec: L.C.dec,
                  amount: Number(L.est) / 10 ** L.C.dec, rest: false });
    }
    if (perpLeg) {                                                         // "swap 60% to USDC, deposit that into perps, keep the rest" (2026-11-08)
      const acct: any = await needAccount(addr, true);                       // wallet money goes in: the deposit rule (BLD-V1)
      const k2 = await tx.build({ client: grpc as any, onlyTransactionKind: true });
      const arg = perpLeg.coin?.NestedResult ? { NestedResult: perpLeg.coin.NestedResult } : perpLeg.coin?.Result != null ? { Result: perpLeg.coin.Result } : perpLeg.coin;
      const r = await af('account/transactions/deposit-collateral', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
        collateralCoinType: USDC, txKind: Buffer.from(k2).toString('base64'), depositCoinArg: arg });
      checkAppended(k2, Buffer.from(String(r.txKind), 'base64'), 'Aftermath perps', addr, ['perps'], [arg]);   // 2026-11-13
      tx = Transaction.fromKind(r.txKind); tx.setSender(addr);
    }
    let size = 0;
    // a chain that touches the perps account carries its event table: simulate() refuses any other perps action (2026-11-15)
    setPerps(tx, chainPerpsTable(src.op === 'perps_out', !!perpLeg));
    try { size = await simulate(tx); }
    catch (e: any) {
      if (/unwrap_spot_price|price/i.test(String(e?.message)) && src.op === 'withdraw' && byKey(src.market).protocol === 'suilend')
        throw new Error(`Your ${byKey(src.market).name} account also holds coins whose price isn't fresh on chain right now — try again in a minute.`);
      console.error('compose simulate failed:', String(e?.message ?? e).slice(0, 1500));   // the full abort, for whoever debugs it (2026-10-26)
    throw failWrap('This chain would fail right now: ', e);
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
  const want = (k: any) => k.amount?.raw != null ? k.amount.raw : k.amount?.units != null ? toUnits(k.amount.units, h.C.dec)
                         : k.amount?.frac != null ? BigInt(Math.floor(Number(E) * Number(k.amount.frac))) : null;
  trimDust(sinks.filter((k) => !k.amount?.rest), want, E);
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
    const acct: any = await needAccount(addr, true);                         // wallet money goes in: the deposit rule (BLD-V1)
    const amt = E - total;
    if (amt <= 0n) throw new Error('Nothing left for the perps deposit');
    h.tx.splitCoins(h.coin, [amt]);
    h.tx.transferObjects([h.coin], addr);                              // anything above the planned amount stays yours
    const resolved = JSON.parse(await h.tx.toJSON({ client: grpc as any }));
    const kind = await h.tx.build({ client: grpc as any, onlyTransactionKind: true });
    const at = lastSplit(resolved);
    const r = await af('account/transactions/deposit-collateral', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
      collateralCoinType: USDC, txKind: Buffer.from(kind).toString('base64'), depositCoinArg: { NestedResult: [at, 0] } });
    checkAppended(kind, Buffer.from(String(r.txKind), 'base64'), 'Aftermath perps', addr, ['perps'], [{ NestedResult: [at, 0] }]);   // 2026-11-13
    h.tx = Transaction.fromKind(r.txKind); h.tx.setSender(addr);
    plan.push({ op: 'perps', coin: 'USDC', amount: Number(amt) / 1e6, rest: true });
  } else if (restSink && fixed.indexOf(restSink) < 0) await sinkTo(restSink, h.coin, null);   // the rest: the coin itself
  else h.tx.transferObjects([h.coin], addr);                                          // nothing takes the rest: it stays yours
  let size = 0;
  setPerps(h.tx, chainPerpsTable(src.op === 'perps_out', !!perp));   // as above: the chain's perps steps, and no other (2026-11-15)
  try { size = await simulate(h.tx); }
  catch (e: any) {
    if (/unwrap_spot_price|price/i.test(String(e?.message)) && src.op === 'withdraw' && byKey(src.market).protocol === 'suilend')
      throw new Error(`Your ${byKey(src.market).name} account also holds coins whose price isn't fresh on chain right now — try again in a minute.`);
    console.error('compose simulate failed:', String(e?.message ?? e).slice(0, 1500));   // the full abort, for whoever debugs it (2026-10-26)
    throw failWrap('This chain would fail right now: ', e);
  }
  return { ok: true, compose: true, plan, final_coin: h.C.sym, final_type: h.C.type, final_dec: h.C.dec,
           tx: await h.tx.toJSON({ client: grpc as any }), bytes: size };
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S19] DUST SWEEP
// POST /dust: every coin worth under $0.01 swapped into one coin, one transaction
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── dust sweep (2026-10-25): the coins worth under $0.01 that the wallet hides, swapped into ONE coin in ONE transaction.
// Each coin's objects are merged and spent whole, and the emptied object is deleted, so Sui refunds its storage deposit.
// Every route is asked for first: a coin the router can't swap is left where it is, never half-built into the transaction. ──
// POST /dust — caller: server.py dust_build() (/api/tx/dust); also the "dust" source of a compose chain
export async function buildDust(addr: string, toSym: string, items: any[], hand = false) {   // hand: also return the swept coin, unsent, for a chain
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
      // the failed step, from the error object (0-based); the text ("in 45th command", 1-based) only when it carries none
      const at = execErr(e)?.command ?? Number((String(e?.message ?? '').match(/in (\d+)(?:st|nd|rd|th) command/) ?? [])[1]) - 1;
      const bad = Number.isFinite(at) ? legs.findIndex((L) => at >= L.from && at < L.to) : -1;
      if (bad < 0 || tries >= 2) throw e;
      skipped.push({ sym: legs[bad].sym, why: 'its swap route fails right now' });
      legs.splice(bad, 1);
    }
  }
}

// the package allowlist for the dust sweep (kivy_pkgs.json, package ids only)
function dustAllow(): Set<string> {
  try { return new Set(JSON.parse(readFileSync(PKGS_LIVE, 'utf8')).packages.map((p: string) => normType(p + '::a::b').split('::')[0])); }
  catch { return new Set(); }
}

// one sweep: merge each coin's objects, split all of it off, destroy the empty object, append its route, check every
// program against the allowlist, merge the outputs. A bad leg throws {badLeg} so buildDust drops that coin and retries.
async function dustTx(addr: string, B: Coin, legs: any[], skipped: any[], hand = false): Promise<any> {
  const rc = routerCtx.getStore(); if (rc) rc.ranges = [];           // each try is a new transaction: its own route steps only
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
    let r: any;
    try {
      r = await routeTrade({ txKind: Buffer.from(kind).toString('base64'), completeRoute: L.route,
                             slippage: slip, walletAddress: addr, coinInId: { NestedResult: [L.at, 0] } });
    } catch (e: any) {                                                   // its route uses an unlisted program: drop this coin only
      if (e?.unlisted) throw Object.assign(new Error('unverified program'), { badLeg: legs.indexOf(L) });
      throw e;
    }
    if (!r.txKind || !r.coinOutId) throw routeError('Aftermath router returned no swap for ' + L.sym);
    L.min = minAfter(L.exp, slip);
    const tf = Transaction.fromKind(r.txKind); tf.setSender(addr);
    floorOut(tf, r.coinOutId, L.min);                                    // Kivy's own floor under this coin's swap (2026-11-15)
    kind = await tf.build({ client: grpc as any, onlyTransactionKind: true });
    L.to = Transaction.fromKind(kind).getData().commands.length;        // its floor steps included: falling short drops this coin
    expT += L.exp; minT += L.min;
    outs.push(r.coinOutId);
  }
  const tx2 = Transaction.fromKind(kind); tx2.setSender(addr);
  const allow = dustAllow();                                          // the same list the page checks before signing (kivy_pkgs.json)
  if (!allow.size) throw new Error('Kivy could not read its list of verified programs, so nothing was prepared');   // fail closed (L3)
  tx2.getData().commands.forEach((c: any, i: number) => {
    const pkg = c.MoveCall && normType(c.MoveCall.package + '::a::b').split('::')[0];
    if (pkg && !allow.has(pkg)) {                                      // inside a leg: drop that coin and retry; outside every leg: refuse (review L3)
      const bad = legs.findIndex((L) => i >= L.from && i < L.to);
      if (bad >= 0) throw Object.assign(new Error('unverified program'), { badLeg: bad });
      throw new Error('This dust sweep would call a program Kivy hasn\'t verified, so nothing was prepared');
    }
  });
  if (outs.length > 1) tx2.mergeCoins(outs[0], outs.slice(1));
  if (hand) return { tx: tx2, coin: outs[0], min: minT };              // a chain's next steps take the swept coin from here
  tx2.transferObjects([outs[0]], addr);
  let size = 0;
  try { size = await simulate(tx2); }
  catch (e: any) { throw failWrap('This dust sweep would fail right now: ', e); }
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
// [S20] MULTI-SEND
// POST /sendmany: several coins to several addresses, from the wallet or withdrawn from lending
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── multi-send (2026-10-23): several coins to several addresses in ONE transaction. Each line comes from the
// wallet, or from a lending market (withdrawn here, then sent). Lines from the same market + coin share one withdrawal;
// the last line of a group takes what is left of it, so a tiny rounding gap on Suilend can't fail the transaction. ──
// POST /sendmany — caller: server.py /api/tx/sendmany (the Send card)
export async function buildSendMany(addr: string, lines: any[]) {
  if (!Array.isArray(lines) || !lines.length || lines.length > 12) throw new Error('Send 1 to 12 transfers at a time');
  const tx = new Transaction(); tx.setSender(addr);
  const plan: any[] = [], groups = new Map<string, any[]>();
  for (const l of lines) {
    if (!/^0x[0-9a-f]{64}$/.test(String(l.to))) throw new Error('A recipient is not a full Sui address');
    if (!(Number(l.amount) > 0) && l.amount !== 'all') throw new Error('Every transfer needs an amount');
    if (l.source && l.source !== 'wallet') { const k = l.source + '|' + l.coin; groups.set(k, [...(groups.get(k) ?? []), l]); continue; }
    const C = await coinOf('', l.coin);
    const base = toUnits(l.amount, C.dec);
    tx.transferObjects([tx.add(coinWithBalance({ type: C.type, balance: base }))], tx.pure.address(l.to));
    plan.push({ op: 'send', source: 'wallet', coin: C.sym, coin_type: C.type, dec: C.dec, amount: Number(base) / 10 ** C.dec, to: l.to });
  }
  for (const [k, ls] of groups) {
    const [market, coin] = k.split('|');
    const all = ls.length === 1 && ls[0].amount === 'all';
    const total = all ? 'all' : unitsText(ls.reduce((t: bigint, x: any) => t + toUnits(x.amount, 18), 0n), 18);   // exact sum, no float drift
    const keep: any[] = [];
    const w: any = await build(addr, market, 'withdraw', total, coin, keep, tx);
    if (!keep.length) throw new Error('Nothing to withdraw from ' + byKey(market).name);
    const c0 = keep[0];
    if (keep.length > 1) tx.mergeCoins(c0, keep.slice(1));
    plan.push({ op: 'withdraw', market, name: byKey(market).name, coin: w.C.sym, amount: Number(w.amount) });
    ls.forEach((l: any, i: number) => {
      const last = i === ls.length - 1;
      const amt = all ? Number(w.amount) : Number(l.amount);
      const part = last ? c0 : tx.splitCoins(c0, [toUnits(l.amount, w.C.dec)])[0];
      tx.transferObjects([part], tx.pure.address(l.to));
      plan.push({ op: 'send', source: market, coin: w.C.sym, coin_type: w.C.type, dec: w.C.dec, amount: amt, to: l.to, rest: last });
    });
  }
  let size = 0;
  try { size = await simulate(tx); }
  catch (e: any) {
    if (/unwrap_spot_price|price/i.test(String(e?.message)) && [...groups.keys()].some((g) => byKey(g.split('|')[0]).protocol === 'suilend'))
      throw new Error("A Suilend account here holds coins whose price isn't fresh on chain right now — try again in a minute.");
    throw failWrap('This send would fail right now: ', e);
  }
  return { ok: true, sendmany: true, plan, tx: await tx.toJSON({ client: grpc as any }), bytes: size };
}
