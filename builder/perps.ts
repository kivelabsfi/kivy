/*
 * perps.ts — Aftermath perpetuals: account, orders, reads, and perpGuard() (every perps transaction checked against the chain).
 * In detail: the wallet's perps account, open / deposit / withdraw, market and pro orders (limit, stop, scale, TWAP, cancel,
 * close, take-profit / stop-loss, margin), the reads (positions, stop / TWAP orders, history), and perpGuard() — the chain's own
 * events of every perps transaction compared with the request before it reaches the wallet.
 * Called by: builder.ts handle() (/perp, /perp-*, POST /perp-pro-orders), compose.ts (perps margin into / out of a chain).
 * Imports: core.ts, guard.ts (the perps event table, the object rule), routers.ts (checkAppended), merge.ts. State: the market list (1 h, _pmk), perps_markets.json (60 s, _ptab),
 * perpCtx (the request perpGuard compares with; handle() sets it).
 */
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import { readFileSync } from 'node:fs';
import { AsyncLocalStorage } from 'node:async_hooks';
import { grpc } from './chain.ts';
import { normType } from './txfacts.ts';
import { mergeKinds } from './merge.ts';
import { callWhy, coinArg, fromBase, perpsPkgs, perpsRefused, RELEASE_ID, setPerps, simulate, toBase, USDC } from './core.ts';
import { objectProblem, perpsEventProblem, perpsTable } from './guard.ts';
import { checkAppended } from './routers.ts';

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S14] PERPS ACCOUNT + GUARD
// Aftermath perpetuals API, the perps check against the chain (perpGuard), open / deposit / withdraw (GET /perp)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── Aftermath perpetuals: the user's account and its USDC collateral ──
// Aftermath's API returns transaction *kinds*; we add sender + gas, simulate, and hand back
// the full transaction. Deposits use a coin Kivy builds (coinWithBalance) passed in as
// depositCoinArg, so USDC in the address balance works too.
const AF = 'https://aftermath.finance/api/perpetuals/';
// POST to Aftermath's perpetuals API (AF + path); returns its JSON or throws 'Aftermath: …'
export async function af(path: string, body: any) {
  const r = await fetch(AF + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(20_000) });   // a hung Aftermath must not hold a build slot (review 2026-11-04 S5)
  const t = await r.text();
  if (r.status >= 500) {                               // their service failing (2026-11-15: HTTP 500 'Error with ID' on every order for hours)
    const id = (t.match(/#[0-9a-f]{6,}/i) || [''])[0];
    throw new Error(`Aftermath: their perps service is not working right now${id ? ' (' + id + ')' : ''} — nothing was prepared. Try again in a few minutes.`);
  }
  let j: any; try { j = JSON.parse(t); } catch { throw new Error('Aftermath: ' + t.slice(0, 160)); }
  if (!r.ok || j?.error || typeof j === 'string') throw new Error('Aftermath: ' + String(j?.error ?? j?.message ?? t).slice(0, 200));
  return j;
}

export function markPrices(m: any) { return af('markets/prices', { marketIds: [m.objectId] }); }

// ── Whose perps account (fix package 2 "Your perps account is yours", QA 2026-11-17 BLD-V1; the owner's design: "no need to remember
// for other wallet. Just what you found on that wallet") ──
// An Aftermath account key (AuthorityCap<ACCOUNT, ADMIN>) has `store`, so anyone can send one to any address, and its giver may
// keep an ASSISTANT key: deposit, allocate, orders and stop / TWAP tickets accept any role; only ADMIN withdraws or revokes. A key
// in the wallet is therefore no proof the account is the user's own. Each request reads every USDC account whose ADMIN key the
// wallet holds — from Aftermath and from the chain itself — and decides from what it finds; nothing is stored anywhere:
//   another wallet holds an assistant key on it            -> blocked: never used, never offered
//   one usable account, no open orders, no stop / TWAP ticket -> used as before (and named on every card)
//   several usable, or one with orders / tickets            -> the request names one: the chat's choice card puts the pick in
//                                                              the follow-up ask, cards carry it (GET ?acct=, spec.perps_account)
// A read (positions, history) and a build that moves no wallet money in may use the one usable account even with orders on it;
// a deposit may not. Every build re-reads the chain (needAccount), the last trusted point: the named account's ADMIN key is in
// this wallet, it holds USDC, and no other wallet holds an assistant key on it — else it is refused with the reason.
export const acctPick = new AsyncLocalStorage<{ id: string }>();
// run f with the account the request names ('' = none); anything but a number is refused
export function withPick<T>(id: any, f: () => Promise<T>): Promise<T> {
  const s = String(id ?? '').trim().replace(/n$/, '');
  if (s && !/^\d{1,20}$/.test(s)) throw new Error('bad perps account');
  return acctPick.run({ id: s }, f);
}
const AUTHORITY = normalizeSuiAddress('0x4e2df80a5e2fd0392878298c51ce15164222111ccea05504b9291b158f552677');   // Aftermath's authority package (keys)
const short = (a: string) => (a ? a.slice(0, 6) + '…' + a.slice(-4) : '');
// the owner of an object as the chain says: a plain address owner or a party (consensus) owner; 0x0 for anything else
const ownerOf = (o: any) => normalizeSuiAddress(String(o?.owner?.AddressOwner ?? o?.owner?.ConsensusAddressOwner?.owner ?? '0x0'));
const typeArgs = (t: string) => { const m = /^([^<]+)<(.+)>$/.exec(t.replace(/\s+/g, '')); return m ? { head: m[1], args: m[2].split(',') } : { head: t, args: [] as string[] }; };
const pkgOf = (t: string) => normalizeSuiAddress(String(t).split('::')[0]);
// what blocks an account, in the user's words
export function blockedWhy(a: any) {
  const who = (a.others ?? []).filter(Boolean)[0];
  return `Account #${a.id} — another wallet${who ? ' (' + short(who) + ')' : ''} can trade it, so Kivy won't use it`;
}
// Every USDC perps account whose ADMIN key is in the wallet, with margin, positions, open orders, stop / TWAP tickets and the
// wallet(s) holding an assistant key on it. strict (builds): the chain must answer, or nothing is built.
export async function perpAccounts(addr: string, strict = false) {
  const me = normalizeSuiAddress(addr);
  const own: any = await af('accounts/owned', { walletAddress: addr });
  const caps = (own.accountCaps ?? []).filter((x: any) => String(x.collateralCoinType).endsWith('::usdc::USDC') && !x.isAgent
    && normalizeSuiAddress(String(x.walletAddress ?? addr)) === me && /^\d+n?$/.test(String(x.accountId)));
  if (!caps.length) return [];
  const ids = caps.map((x: any) => String(x.accountId).replace(/n$/, ''));
  const T = perpTable(), ACCT_PKG = T?.original ? normalizeSuiAddress(T.original) : '';
  const [pos, objs]: any = await Promise.all([
    af('accounts/positions', { accountIds: ids.map((i: string) => i + 'n') }).catch(() => null),
    (async () => {                                     // the chain's own word on each key and account (one read for all)
      if (!ACCT_PKG) throw new Error('the verified perps list is missing');
      const r: any = await (grpc as any).core.getObjects({ objectIds: [...caps.map((x: any) => x.objectId), ...caps.map((x: any) => x.accountObjectId)], include: { json: true } });
      const tk = await Promise.all(caps.map(async (x: any) => {   // stop / TWAP tickets sit on the account object as fields
        let n = 0, cursor: string | null = null;
        for (let p = 0; p < 4; p++) {
          const d: any = await (grpc as any).core.listDynamicFields({ parentId: x.accountObjectId, limit: 50, cursor });
          n += (d.dynamicFields ?? []).filter((f: any) => /::keys::OrderTicketKey$/.test(String(f?.name?.type ?? ''))).length;
          if (!d.hasNextPage) return n;
          cursor = d.cursor;
        }
        return n;                                       // 200+ fields: counted as far as read (any number above 0 already counts)
      }));
      return { list: r.objects ?? [], tickets: tk };
    })().catch((e: any) => { if (strict) throw new Error('Kivy could not check your perps account on chain right now (' + String(e?.message ?? e).slice(0, 80) + ') — nothing was prepared. Try again in a moment.'); return null; }),
  ]);
  const byId: Record<string, any> = Object.fromEntries((pos?.accounts ?? []).map((a: any) => [String(a.accountId).replace(/n$/, ''), a]));
  if (!_pmk) await perpMarket('BTC').catch(() => null);
  const name = (id: string) => Object.entries(_pmk?.v ?? {}).find(([, m]: any) => m.objectId === id)?.[0] ?? String(id).slice(0, 8);
  const out: any[] = [];
  for (let i = 0; i < caps.length; i++) {
    const c = caps[i], id = ids[i], p = byId[id] ?? {};
    const unparked = fromBase(String(c.collateral ?? '0').replace('n', ''));
    const a: any = { id, raw: String(c.accountId), capId: c.objectId, objectId: c.accountObjectId, unparked, margin: Number(p.availableCollateral ?? unparked),
      positions: (p.positions ?? []).filter((x: any) => Number(x.baseAssetAmount) !== 0)
        .map((x: any) => ({ symbol: name(x.marketId), side: Number(x.baseAssetAmount) > 0 ? 'long' : 'short', size: Math.abs(Number(x.baseAssetAmount)) })),
      orders: byId[id] ? (p.positions ?? []).reduce((t: number, x: any) => t + (x.pendingOrders ?? []).length, 0) : null,   // null: Aftermath didn't answer
      tickets: null as number | null, assistants: (c.whitelistedAgentCapIds ?? []).map((x: any) => normalizeSuiAddress(String(x))),
      holders: {} as Record<string, string>, others: [] as string[], checked: false };
    for (const w of c.whitelistedAgentCaps ?? []) {        // Aftermath's word on who holds each assistant key: {objectId, owner}
      const o = w?.owner ?? w?.walletAddress, k = w?.objectId;
      if (typeof o === 'string' && typeof k === 'string' && /^0x[0-9a-fA-F]{1,64}$/.test(o)) a.holders[normalizeSuiAddress(k)] = normalizeSuiAddress(o);
    }
    if (objs) {
      const cap = objs.list[i], acct = objs.list[caps.length + i];
      const ct = typeArgs(String(cap?.type ?? '')), at = typeArgs(String(acct?.type ?? ''));
      const capOk = cap?.objectId && ownerOf(cap) === me && ct.head.endsWith('::authority::AuthorityCap') && pkgOf(ct.head) === AUTHORITY
        && ct.args.length === 2 && ct.args[0] === `${ACCT_PKG}::authority::ACCOUNT` && ct.args[1] === `${AUTHORITY}::authority::ADMIN`
        && normalizeSuiAddress(String(cap?.json?.for ?? '0x0')) === normalizeSuiAddress(String(c.accountObjectId));
      const acctOk = acct?.objectId && at.head === `${ACCT_PKG}::account::Account` && at.args.length === 1 && normType(at.args[0]) === normType(USDC)
        && String(acct?.json?.account_id ?? '') === id;
      if (!capOk || !acctOk) continue;                  // not an ADMIN key of this wallet on a USDC account: not the user's account
      // the chain's own list of live assistant keys replaces Aftermath's (which can lag a revoke — the very fix Kivy suggests)
      a.assistants = (acct.json?.active_assistants?.contents ?? acct.json?.active_assistants ?? []).map((x: any) => normalizeSuiAddress(String(x)));
      a.tickets = objs.tickets[i];
      a.checked = true;
    }
    out.push(a);
  }
  // who holds each assistant key: the chain first, Aftermath's word when the chain can't say. A key held by this same wallet leaves
  // nobody else able to trade; any other holder — or one nobody can name — blocks the account (shown as "another wallet (0xab12…)")
  const ask = [...new Set(out.flatMap((a) => a.assistants))].slice(0, 50);
  if (ask.length) {
    try {
      const r: any = await (grpc as any).core.getObjects({ objectIds: ask, include: {} });
      for (const o of r.objects ?? []) if (o?.objectId) for (const a of out) if (a.assistants.includes(normalizeSuiAddress(o.objectId))) a.holders[normalizeSuiAddress(o.objectId)] = ownerOf(o);
    } catch (e: any) { if (strict) throw new Error('Kivy could not check your perps account on chain right now — nothing was prepared. Try again in a moment.'); }
  }
  for (const a of out) {
    a.others = [...new Set(a.assistants.map((k: string) => a.holders[k] ?? '').filter((o: string) => o !== me))] as string[];
    a.blocked = a.others.length > 0;                    // '' = a holder nobody could name: blocked too (fail closed)
    a.others = a.others.filter((o: string) => o && o !== normalizeSuiAddress('0x0'));
    delete a.holders;
  }
  return out;
}
// One request's answer: the account to use, or why there is none. moneyIn = a deposit (wallet money goes in): a single usable
// account with open orders or tickets then needs the request to name it too. Callers: perpAccount (reads), needAccount (builds).
function decide(list: any[], pick: string, moneyIn: boolean) {
  const usable = list.filter((a) => !a.blocked);
  if (pick) {
    const a = list.find((x) => x.id === pick);
    if (!a) return { error: `Perps account #${pick} is not one whose key is in this wallet, so Kivy won't use it.` };
    if (a.blocked) return { error: '⛔ ' + blockedWhy(a) + '.' };
    return { use: a };
  }
  if (usable.length === 1 && (!moneyIn || (usable[0].orders === 0 && usable[0].tickets === 0))) return { use: usable[0] };
  return usable.length ? { choose: true } : {};
}
const pub = (a: any) => ({ id: a.id, margin: a.margin, positions: a.positions, orders: a.orders, tickets: a.tickets, blocked: !!a.blocked,
                           others: a.blocked ? a.others : [], checked: a.checked });
// GET /perp-account — which perps account this request uses: id, cap, free collateral, plus every account found and whether the
// user must pick one (`choose`). Caller: server.py perp_account() (the chat, before a deposit or an order: the deposit rule).
export async function perpAccount(addr: string, moneyIn = true, strict = false) {
  const list = await perpAccounts(addr, strict);
  const pick = acctPick.getStore()?.id ?? '';
  const d: any = decide(list, pick, moneyIn);
  const all = list.map(pub), blocked = all.filter((a) => a.blocked).length;
  if (!d.use) return { has: false, collateral: 0, accounts: all, blocked, choose: !!d.choose || !!d.error, ...(d.error ? { pick_error: d.error } : {}) };
  const a = d.use;
  return { has: true, id: a.id, accountId: a.raw, capId: a.capId, collateral: a.margin, unparked: a.unparked, accounts: all, blocked, choose: false,
           account: pub(a), others: all.length - 1 };   // accountId: Aftermath's own wire form ("1042n"), id: the number users see
}
// what a read without an account in use says about the wallet's accounts (several to pick from, blocked ones)
const acctNote = (acct: any) => ({ accounts: acct.accounts ?? [], choose: !!acct.choose, ...(acct.pick_error ? { pick_error: acct.pick_error } : {}) });
// ── perps check (security audit 2026-10-27, finding 3) ──
// Aftermath's API writes every perps transaction. Before one reaches the wallet, Kivy runs it and reads what the CHAIN says it
// does — the events and the money moving — and compares that with what the user asked: the market (from the on-chain-verified
// list in perps_markets.json, kept by tools/sync_perps_markets.py), the account, long or short, the size and the leverage,
// and that nothing leaves the wallet but gas (or the deposit). Any mismatch is refused and logged ("kivy perps refused:"),
// which wakes the alert and the fixer. Reading events instead of raw arguments survives Aftermath re-ordering them.
export const perpCtx = new AsyncLocalStorage<{ cmd: string; args: string[]; accountId?: string }>();   // accountId: needAccount()'s, for the check below
let _ptab: { at: number; v: any } = { at: 0, v: null };
// perps_markets.json (verified market + package ids), re-read every 60 s
function perpTable() {
  if (Date.now() - _ptab.at > 60_000) {
    try { _ptab = { at: Date.now(), v: JSON.parse(readFileSync(process.env.KIVY_PERPS_MARKETS || (RELEASE_ID !== 'none' ? '/srv/kivy/builder/perps_markets.json' : new URL('./perps_markets.json', import.meta.url)), 'utf8')) }; }   // a release reads the cron's file (env, 2026-11-14)
    catch { _ptab = { at: Date.now(), v: null }; }
  }
  return _ptab.v;
}
// log the refusal (the alert watches for "kivy perps refused:") and stop the build — one copy, in core.ts (simulate() uses it too)
const perpRefuse = perpsRefused;
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
  // the event table for this request (guard.ts, 2026-11-15): every event the chain may show for it, and how many. No table =
  // a command nobody wrote a rule for: refused. simulate() checks the same table again on the final transaction (setPerps).
  const table = perpsTable(cmd, a);
  if (!table) perpRefuse('Kivy has no rule for this perps command', cmd);
  setPerps(tx, table);
  const progs = new Set([...T.packages, '0x1', '0x2'].map((x: string) => normalizeSuiAddress(x)));
  const fw2 = normalizeSuiAddress('0x2'), opening = cmd === 'perp' && a[1] === 'open';
  for (const c of tx.getData().commands as any[]) {
    if (c.Publish || c.Upgrade || c.$kind === 'Publish' || c.$kind === 'Upgrade') perpRefuse('it publishes or upgrades a program', cmd);
    if (c.MoveCall && !progs.has(normalizeSuiAddress(c.MoveCall.package))) perpRefuse(`it calls ${normalizeSuiAddress(c.MoveCall.package).slice(0, 10)}…::${c.MoveCall.module}, not Aftermath perps`, cmd);
    const why = c.MoveCall ? callWhy(c.MoveCall.package, c.MoveCall.module, c.MoveCall.function, ['perps']) : '';   // call rules (2026-11-13)
    if (why) perpRefuse(why, cmd);
    // 0x2::party / 0x2::transfer hand an object to an owner: the call rules allow them for every perps plan because opening
    // an account needs them — here they are allowed for that one command only (review scan B2, 2026-11-15)
    if (c.MoveCall && normalizeSuiAddress(c.MoveCall.package) === fw2 && ['party', 'transfer'].includes(String(c.MoveCall.module)) && !opening)
      perpRefuse(`it calls 0x2::${c.MoveCall.module}::${c.MoveCall.function}, which only opening an account needs`, cmd);
  }
  const bytes = await tx.build({ client: grpc as any });
  const sim: any = await (grpc as any).core.simulateTransaction({ transaction: bytes, include: { effects: true, balanceChanges: true, events: true, objectTypes: true } });
  const t = sim.Transaction ?? sim.FailedTransaction ?? sim;
  if ((t?.effects?.status ?? t?.status)?.success === false) return;     // a failing transaction is refused by simulate() with its own reason
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
  // none of the wallet's objects that is not a coin leaves it, in either owner kind — the perps account key is a party object,
  // which the old rule (plain address owners only) could not see; and nothing but a coin is made for someone else (B2)
  const obj = objectProblem(t, addr, true);
  if (obj) perpRefuse(obj, cmd);
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
  if (ctx?.accountId && [...accts].some((x) => x !== ctx.accountId)) perpRefuse(`it acts on another perps account than #${ctx.accountId}`, cmd);   // BLD-V1
  // default refuse (B1): every event must be on this command's list, in its count — an order in a "cancel", a second fill, a
  // ticket nobody asked for, an event of another program. The checks below then judge side, size, price and leverage.
  const off = perpsEventProblem(evs, table!, [...T.packages, ...(T.original ? [T.original] : []), ...perpsPkgs()]);
  if (off) perpRefuse(off, cmd);
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
  // pro orders (review M2, 2026-10-29): what the chain places must be what was asked. A limit / scale order's side and price sit
  // in its order id (top 64 bits: an ask = price × 1e9, a bid = 2^64-1 minus that) and its size in PostedOrder. Stop, TP and SL
  // tickets keep their trigger prices hashed with a salt only Aftermath holds (encrypted_details), so for those the chain
  // shows only how many tickets, of which kind, and that nothing is placed or filled now — that much is checked.
  const posted = evs.filter((e: any) => e.type.endsWith('::events::PostedOrder'));
  const takes = evs.filter((e: any) => e.type.endsWith('::events::FilledTakerOrder'));
  const tickets = evs.filter((e: any) => /::events::CreatedStopOrderTicket(<|$)/.test(e.type));
  const num = (i: number) => Number(a[i]) || 0;
  const mk: any = sym && ['perp-limit', 'perp-scale', 'perp-close'].includes(cmd) ? await perpMarket(sym).catch(() => null) : null;
  const tick = Number(String(mk?.marketParams?.tickSize ?? '0').replace('n', '')) / PX_DEC;
  const lot = Number(String(mk?.marketParams?.lotSize ?? '0').replace('n', '')) / SIZE_DEC;
  const fillOf = (f: any) => {
    const bid = BigInt(f.j.base_asset_delta_bid ?? 0), ask = BigInt(f.j.base_asset_delta_ask ?? 0);
    return { bid, ask, base: Number(bid + ask) / 1e18, quote: Number(BigInt(f.j.quote_asset_delta_bid ?? 0) + BigInt(f.j.quote_asset_delta_ask ?? 0)) / 1e18 };
  };
  const ticketsAre = (lo: number, hi: number, kind: string) => {   // kind: '0' = TP / SL on a position, '1' = a stop order
    if (tickets.length < lo || tickets.length > hi) perpRefuse(`it sets ${tickets.length} stop / take-profit orders, you asked for ${lo === hi ? lo : lo + '–' + hi}`, cmd);
    if (tickets.some((e: any) => String(e.j.stop_order_type) !== kind)) perpRefuse('it sets another kind of stop order than you asked for', cmd);
  };
  const nothingNow = () => { if (posted.length || takes.length) perpRefuse('it places or fills an order now', cmd); };
  if (cmd === 'perp-limit' || cmd === 'perp-scale') {
    const side = num(2), usd = num(3), lev = num(4), n = cmd === 'perp-scale' ? num(7) : 1;
    const lo = cmd === 'perp-limit' ? num(5) : Math.min(num(5), num(6)), hi = cmd === 'perp-limit' ? num(5) : Math.max(num(5), num(6));
    const room = (p: number) => Math.max(p * 0.001, tick);              // 0.1 %, or one price tick when that is coarser
    let base = 0;
    for (const e of posted) {
      const hi64 = BigInt(String(e.j.order_id ?? 0)) >> 64n, bid = hi64 >= (1n << 63n);
      const px = Number(bid ? (1n << 64n) - 1n - hi64 : hi64) / PX_DEC;
      if (bid !== (side === 0)) perpRefuse(`it places a ${bid ? 'long' : 'short'} order, you asked for a ${side === 0 ? 'long' : 'short'}`, cmd);
      if (px < lo - room(lo) || px > hi + room(hi)) perpRefuse(`its order price is ${px}, you asked for ${lo === hi ? lo : lo + '–' + hi}`, cmd);
      if (e.j.reduce_only === true) perpRefuse('it places a reduce-only order, you asked to open one', cmd);
      base += Number(BigInt(String(e.j.order_size ?? 0))) / SIZE_DEC;
    }
    for (const f of takes) {                            // a limit that crosses the book fills now: same side, at its price or better
      const x = fillOf(f);
      if (side === 0 ? !(x.bid > 0n && x.ask === 0n) : !(x.ask > 0n && x.bid === 0n)) perpRefuse(`it fills a ${side === 0 ? 'short' : 'long'}, you asked for a ${side === 0 ? 'long' : 'short'}`, cmd);
      if (x.base > 0 && (side === 0 ? x.quote / x.base > hi + room(hi) : x.quote / x.base < lo - room(lo))) perpRefuse(`it fills at about ${(x.quote / x.base).toPrecision(6)}, past your price`, cmd);
      base += x.base;
    }
    if (!posted.length && !takes.length) perpRefuse('it places no order', cmd);
    if (cmd === 'perp-scale' && posted.length > n) perpRefuse(`it places ${posted.length} orders, you asked for ${n}`, cmd);
    const asked = usd > 0 ? usd / (cmd === 'perp-limit' ? lo : (lo + hi) / 2) : 0;   // the builder sizes it usd / price, lot-rounded down
    if (asked > 0 && base > asked * 1.001 + 1e-12) perpRefuse(`its size is ${base} ${sym}, you asked for about ${Number(asked.toPrecision(6))}`, cmd);
    if (asked > 0 && base < asked * 0.999 - lot * n - 1e-12) perpRefuse(`its size is ${base} ${sym}, you asked for about ${Number(asked.toPrecision(6))}`, cmd);
    const imr = evs.find((e: any) => e.type.endsWith('::events::SetPositionInitialMarginRatio'));
    if (imr && lev > 0 && Math.abs(1e18 / Number(imr.j.initial_margin_ratio) - lev) / lev > 0.05)
      perpRefuse(`its leverage is ${(1e18 / Number(imr.j.initial_margin_ratio)).toFixed(2)}x, you asked for ${lev}x`, cmd);
    if (cmd === 'perp-limit') { const k = num(7) > 0 || num(8) > 0 ? 1 : 0; ticketsAre(k, k, '0'); } else ticketsAre(0, 0, '0');
  }
  if (cmd === 'perp-stop') { nothingNow(); ticketsAre(1, 1, '1'); }
  if (cmd === 'perp-tpsl') {                            // one order for all, or TP1 + (TP2) + (SL) as buildTpSl places them
    const sl = num(2), tp = num(3), frac = num(4) || 1, tp2 = num(5);
    const k = !(tp > 0) || frac >= 1 ? 1 : 1 + (sl > 0 ? 1 : 0) + (tp2 > 0 ? 1 : 0);
    nothingNow(); ticketsAre(tp2 > 0 && k > 1 ? k - 1 : k, k, '0');     // TP2 is left out when TP1 already takes the whole size
  }
  if (cmd === 'perp-tpsl-multi') {
    const tps = String(a[3] || '').split('-').filter(Boolean).map((p) => Number(p.split('x')[0])).filter((p) => p > 0).slice(0, 3);
    const k = tps.length + (num(2) > 0 ? 1 : 0);
    nothingNow(); ticketsAre(k, k, '0');
  }
  if (cmd === 'perp-close') {                           // the other side of the position, and frac of its size (lot-rounded down)
    const frac = num(3) || 1;
    const { pos } = await openPos(addr, sym);
    const size = Math.abs(Number(pos.baseAssetAmount)), long = Number(pos.baseAssetAmount) > 0;
    if (takes.length !== 1 || posted.length) perpRefuse('it does not close with one market order', cmd);
    const x = fillOf(takes[0]);
    if (long ? !(x.ask > 0n && x.bid === 0n) : !(x.bid > 0n && x.ask === 0n)) perpRefuse(`it adds to your ${long ? 'long' : 'short'} instead of closing it`, cmd);
    const want = frac >= 1 ? size : size * frac;
    if (x.base > want * 1.001 + 1e-12 || x.base < want * 0.999 - (frac >= 1 ? 0 : lot) - 1e-12)
      perpRefuse(`it closes ${x.base} ${sym}, you asked for ${frac >= 1 ? 'all' : Math.round(frac * 100) + '%'} of ${size}`, cmd);
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
export async function buildPerp(addr: string, action: string, amountArg: string) {
  if (action === 'open') {                              // a new account: also when the wallet's others are blocked or need a pick (BLD-V1)
    const acct: any = await acctPick.run({ id: '' }, () => perpAccount(addr, true, true));   // judged on the wallet alone, never a pick
    if (acct.has) throw new Error(`You already have an Aftermath perps account (#${acct.id})`);
    const r = await af('transactions/create-account', { walletAddress: addr, collateralCoinType: USDC });
    return { ok: true, action, tx: await finishKind(r.txKind, addr) };
  }
  const acct: any = await needAccount(addr, action === 'deposit');   // a deposit is wallet money going in: the strict rule
  if (action === 'deposit') {
    const amount = Number(amountArg);
    const tx = new Transaction(); tx.setSender(addr);
    tx.add(coinWithBalance({ type: USDC, balance: toBase(amount) }));
    const resolved = JSON.parse(await tx.toJSON({ client: grpc as any }));
    const kind = await tx.build({ client: grpc as any, onlyTransactionKind: true });
    const r = await af('account/transactions/deposit-collateral', { walletAddress: addr, accountId: acct.accountId, accountCapId: acct.capId,
      collateralCoinType: USDC, txKind: Buffer.from(kind).toString('base64'), depositCoinArg: coinArg(resolved) });
    checkAppended(kind, Buffer.from(String(r.txKind), 'base64'), 'Aftermath perps', addr, ['perps'], [coinArg(resolved)]);   // 2026-11-13
    return { ok: true, action, amount, account: acct.id, tx: await finishKind(r.txKind, addr) };
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
    return { ok: true, action, amount, released: idle.total, account: acct.id, tx: j.tx, steps: j.steps };
  }
  throw new Error('perp action must be open, deposit or withdraw');
}

// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// [S15] PERPS MARKET ORDERS
// market list, order preview (/perp-preview), market order with optional SL / TP (/perp-order)
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── perps orders: preview + market order (optional stop-loss / take-profit) ──
let _pmk: { at: number; v: Record<string, any> } | null = null;
// Aftermath's market for a symbol (BTC, SUI…); the market list is kept for an hour
export async function perpMarket(sym: string) {
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
export async function preview(addr: string, sym: string, side: number, usd: number, lev: number) {
  const m = await perpMarket(sym);
  const px: any = await markPrices(m);
  const mark = Number(px.marketsPrices?.[0]?.markPrice ?? 0);
  if (!(mark > 0)) throw new Error('No price for ' + sym);
  const size = baseSize(m, usd / mark);
  if (size <= 0n) throw new Error('Order too small');
  const acct: any = addr ? await perpAccount(addr, false) : { has: false };   // an order moves no wallet money in: the read rule
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
           has_account: !!acct.has, account_free: free, account: acct.has ? acct.id : null, choose: !!acct.choose,
           // margin gets a 3% cushion (price moves while signing); the top-up also covers the taker fee
           deposit_needed: Math.max(0, need * 1.005 + Number(pv.filledSizeUsd ?? 0) * fee - free),   // shortfall the user must deposit first
           max_lev: Math.round(1 / Number(m.marketParams.marginRatioInitial)), min_usd: Number(m.marketParams.minOrderUsdValue ?? 1),
           has_position: !!pv.hasPosition || await hasPos(acct, m.objectId) };
}
// GET /perp-order — market order (+ optional stop-loss / take-profit) as one transaction. Caller: server.py /api/tx/perp
export async function buildOrder(addr: string, sym: string, side: number, usd: number, lev: number, slip: number, sl?: number, tp?: number) {
  const acct: any = await needAccount(addr);
  const p: any = await preview(addr, sym, side, usd, lev);
  if (p.account !== acct.id) throw new Error('Your perps account changed while the order was prepared — nothing was prepared. Try again.');
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
// [S16] PERPS PRO ORDERS
// limit, stop, scale, TWAP, cancel, close, take-profit / stop-loss — all GET /perp-* and joined into one transaction
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// ── pro orders: limit, stop market / stop limit, scale, TWAP; plus cancel and close ──
const PX_DEC = 1e9;                                       // order prices = price × 1e9, rounded to the tick size
function orderPrice(m: any, price: number) {
  const tick = Number(String(m.marketParams.tickSize).replace('n', ''));
  return BigInt(Math.round(price * PX_DEC / tick) * tick);
}
// Orders never pull money from the wallet (2026-10-16): they use only the USDC the user deposited into
// the perps account themselves. Short -> a clear message; the user deposits the amount they choose first.
async function topUpKind(addr: string, acct: any, amount: number, what = 'order'): Promise<string> {   // always throws (typed for its callers)
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
// Every build's account (design point 6): read again from Aftermath AND the chain, never from a cache; refused with a plain reason
// when the request names an account that is not this wallet's own, when another wallet can trade it, or when the user must pick.
// moneyIn: wallet money goes in (a deposit). Inside a perps build the account is also handed to perpGuard, which refuses a
// transaction whose events name any other account.
export async function needAccount(addr: string, moneyIn = false) {
  const acct: any = await perpAccount(addr, moneyIn, true);
  if (!acct.has) {
    const bl = (acct.accounts ?? []).filter((a: any) => a.blocked);
    throw new Error(acct.pick_error ?? (acct.choose
      ? 'You have more than one Aftermath perps account' + ((acct.accounts ?? []).some((a: any) => !a.blocked && (a.orders || a.tickets)) ? ', or one with open orders' : '') +
        ' — say which one in the chat ("my perps accounts"), so Kivy never picks one for you. Nothing was prepared.'
      : bl.length ? bl.map((a: any) => '⛔ ' + blockedWhy(a)).join('; ') + '. Open a new account ("open a new perps account"), or remove the other key in Aftermath\'s app.'
      : 'Open an Aftermath perps account first'));
  }
  const ctx: any = perpCtx.getStore();
  if (ctx) ctx.accountId = acct.id;
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
export async function buildLimit(addr: string, sym: string, side: number, usd: number, lev: number, price: number, postOnly: boolean, sl?: number, tp?: number) {
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
export async function buildStop(addr: string, sym: string, side: number, usd: number, lev: number, trigger: number, limit: number) {
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  const px: any = await markPrices(m);
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
export async function buildScale(addr: string, sym: string, side: number, usd: number, lev: number, start: number, end: number, n: number) {
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
export async function buildTwap(addr: string, sym: string, side: number, usd: number, lev: number, minutes: number, chunks: number, slip: number) {
  const acct = await needAccount(addr); const m = await perpMarket(sym);
  const px: any = await markPrices(m);
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
export async function idleMarketKinds(acct: any, exceptOrders: { marketId?: string; ids?: string[] } = {}) {
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
export async function buildCancel(addr: string, kind: string, sym: string, id: string, withdraw = false) {
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
export async function buildClose(addr: string, sym: string, slip: number, frac = 1) {
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
export async function buildTpSl(addr: string, sym: string, sl: number, tp: number, frac = 1, tp2 = 0) {
  if (!(sl > 0 || tp > 0)) throw new Error('Give a stop-loss or take-profit price');
  if (!(frac > 0 && frac <= 1)) throw new Error('The take-profit share must be between 1% and 100%');
  if (tp2 > 0 && !(tp > 0 && frac < 1)) throw new Error('A second take-profit needs the first one to close only part of the position');
  const { acct, m, pos } = await openPos(addr, sym);
  const long = Number(pos.baseAssetAmount) > 0;
  const px: any = await markPrices(m);
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
export async function buildTpSlMulti(addr: string, sym: string, sl: number, tps: [number, number][]) {
  tps = (tps || []).filter((t) => t && t[0] > 0).slice(0, 3);
  if (!(sl > 0 || tps.length)) throw new Error('Give a stop-loss or take-profit price');
  const total = tps.reduce((t, x) => t + x[1], 0);
  if (tps.some((t) => !(t[1] > 0)) || total > 1.0001) throw new Error('The take-profit shares must add up to 100% or less');
  const { acct, m, pos } = await openPos(addr, sym);
  const long = Number(pos.baseAssetAmount) > 0;
  const px: any = await markPrices(m);
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
// [S17] PERPS READS
// stop / TWAP order list (POST /perp-pro-orders), positions (/perp-positions), add margin (/perp-margin), history
// ══════════════════════════════════════════════════════════════════════════════════════════════════════════════
// Stop and TWAP orders are private on Aftermath: listing them needs the wallet's signature over the
// fixed message "Aftermath Terms and Conditions" (a free personal-message signature, not a transaction).
// POST /perp-pro-orders — caller: server.py /api/perp/pro-orders (body: address, bytes, signature)
export async function perpProOrders(addr: string, bytes: string, signature: string) {
  const acct: any = await perpAccount(addr, false);
  if (!acct.has) return { ok: true, stops: [], twaps: [], errors: [], ...acctNote(acct) };
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
    errors: [st.error, tw.error].filter(Boolean), account: acct.id, accounts: acct.accounts };
}

// the order id carries the price: high 64 bits = price × 1e9 for sells, bit-inverted for buys (checked against order-history)
function idPrice(id: any, side: number) {
  const hi = BigInt(String(id).replace('n', '')) >> 64n;
  return Number(side === 0 ? (2n ** 64n - 1n - hi) : hi) / 1e9;
}
// GET /perp-positions — open positions, their orders and funding since opening. Callers: server.py /api/perp/positions
// and the wallet / chat readers (wallet_snapshot, perp_manage, perp_widget, market_overview…); server.py caches it 15 s.
export async function perpPositions(addr: string) {
  const acct: any = await perpAccount(addr, false);
  if (!acct.has) return { has: false, positions: [], ...acctNote(acct) };
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
  return { has: true, account: acct.id, accounts: acct.accounts, free: a.availableCollateral, equity: a.totalEquityUsd, upnl: a.totalUnrealizedPnlUsd,
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
export async function buildMargin(addr: string, sym: string, amount: number) {
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
export async function perpHistory(addr: string) {
  const acct: any = await perpAccount(addr, false);
  if (!acct.has) return { has: false, trades: [], transfers: [], ...acctNote(acct) };
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
  return { has: true, account: acct.id, activity,
    trades: (t.trades ?? []).map((x: any) => ({ time: x.timestamp, digest: x.txDigest, symbol: name(x.marketId), side: x.side, type: x.tradeType,
      size: x.size, price: x.price, value: x.tradeValueUsd, fee: x.feeUsd, pnl: x.realizedPnlUsd })),
    transfers: (c.collateralChanges ?? []).filter((x: any) => /Deposited|Withdrew|Allocated|Deallocated|Liquidat|SettledFunding/.test(x.eventType)).slice(0, 20)
      .map((x: any) => ({ time: x.timestamp, digest: x.txDigest, symbol: x.marketId ? name(x.marketId) : '', kind: String(x.eventType).split('<')[0].split('::').pop(),
        usd: x.collateralChangeUsd })) };
}
