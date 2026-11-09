// Kivy check 1 — runs in the user's browser right before the wallet opens (copied unchanged from the live page).
// It simulates the exact transaction on Sui and refuses it unless what would happen matches the card the user
// approved: allowlisted programs only, no publish/upgrade, no more of the user's coins out than approved, at least
// the promised amount in, exact amounts to recipients, at most 1% of the user's coins to anyone else (route-only
// coins only in a pure aggregator swap), no object of the user leaving the wallet, account-balance spends bounded.
// From the page around it: gqlUrl(NET) / NET (the Sui GraphQL endpoint), pipe() (reports to Kivy's stats, never
// blocks), /vendor/sui-2.36.0.js (Mysten's Sui TypeScript SDK) and #kivy-pkgs (app/kivy_pkgs.json put into the page).
// The server re-checks the landed transaction against the plan as check 2 (app/server.py, check_tx).

// Audit T11: before the wallet opens, simulate the FINAL transaction and compare what it does to this wallet
// with what the user approved (the server's `guard`). Simulation success alone is not enough.
function normAddr(a) { try { return '0x' + BigInt(a).toString(16).padStart(64, '0'); } catch (e) { return String(a).toLowerCase(); } }
function normType(t) { var p = String(t).split('::'); p[0] = normAddr(p[0]); return p.join('::'); }
function ownerAddr(o) { return o && (o.AddressOwner || (o.ConsensusAddressOwner && o.ConsensusAddressOwner.owner)) || null; }
function ownerShared(o) { return !!(o && (o.Shared || o.Immutable || o.$kind === 'Shared' || o.$kind === 'Immutable')); }   // shared or frozen: out of the owner's sole control
// Audit V01: the Move packages a Kivy transaction may call — Suilend, NAVI, Aftermath perps, the framework and the swap
// routes seen so far (same list as /opt/kivy/kivy_pkgs.json). A call to any other package is refused, whatever its module is
// called — a swap-route adapter too (review H1, 2026-10-29: a module NAMED 'router' could drain lent coins); it is also
// reported so it can be vetted and listed (kivy-vet-package).
// Aftermath's swap-route fee wallet: its share can come in a middle coin of the route (afSUI…), which the plan doesn't list
var KIVY_PKGS = (function () {                      // the allowlist: the server puts kivy_pkgs.json in #kivy-pkgs (one list)
  var el = document.getElementById('kivy-pkgs'); try { if (el) return JSON.parse(el.textContent); } catch (e) {}
  return [];                                      // no block: nothing is allowlisted, every call is refused (was a stale copy; a reviewer 2026-11-04 S2)
})().map(normAddr);
// swap routers' own fee wallets (Aftermath; Cetus pays none for Kivy's routes): the ONLY addresses a coin the plan
// never mentions may go to during a swap (audit nv #1 — any address used to pass, so a route could drain lent positions)
var ROUTER_FEE_WALLETS = ['0x1fb0c0a6790860fc83c8f26f6fa089868ad94de9468348f98dfb4cdf89a53777'];
// the swap aggregators' entry programs (Aftermath router, Cetus aggregator v3). 2026-10-28: a hop inside their
// route is trusted wherever it goes — a coin only the route touches may go to any address (a pool's fee wallet); the user's own
// coins keep the 1% limit and every "you must receive" check below
var AGG_PKGS = ['0x7de5de8d75a8f4e42cdd3c018f788bc9b9ebf2d3d61dcfe9d2136f17f077afd5', '0xffd4058af7d6f6d66c335930cced8c91e38ec943e2392232aee27e8127e684e9'].map(normAddr);
// Aftermath perps (allowlisted): only a perps plan may call it — the server marks its guard `perps`. An order slipped into a swap
// moves no coin and no object, so no other rule here would see it (review K-4, 2026-10-30; the server's check 2 refuses it too)
var PERPS_PKGS = ['0x3ec740df8428aa9c93aaef7f8cc1542ac3194fd014826b51bfe245346d64efc7'].map(normAddr);
// json: the transaction as the wallet will get it; g: the server's guard (out / in / vol / pay / gas / fee);
// addr: the signer. Resolves with the balance changes (for pipe), rejects with the reason the card shows.
// Called by sign() in kivyAct1.
function checkIntent(json, g, addr) {
  if (!g) return Promise.reject(new Error('Kivy could not check this transaction, so it was not sent.'));
  var calls = [], reserve = {}, odd = false, budget = null;
  return import('/vendor/sui-2.36.0.js').then(function (s) { return [s, s]; }).then(function (m) {
    var client = new m[1].SuiGraphQLClient({url: gqlUrl(NET), network: NET});
    var tx = m[0].Transaction.from(json);
    // spending straight from the account balance (FundsWithdrawal): each reserved maximum is read here, not only the simulated
    // result, and must fit what was approved (audit nv #4)
    (tx.getData().inputs || []).forEach(function (inp) {
      var fw = inp && inp.FundsWithdrawal; if (!fw) return;
      var wf = fw.withdrawFrom || {}, mine = wf.Sender || wf.$kind === 'Sender' || (wf.SenderAllowance && normAddr(wf.SenderAllowance.funder) === normAddr(addr));
      if (!mine) return;
      var rs = fw.reservation || {}, ty = fw.typeArg && fw.typeArg.Balance;
      if (rs.MaxAmountU64 == null || !ty) { odd = true; return; }
      var k = normType(String(ty)); reserve[k] = (reserve[k] || 0n) + BigInt(rs.MaxAmountU64);
    });
    var gd = tx.getData().gasData || {};
    if (gd.budget != null && gd.budget !== '') budget = BigInt(gd.budget);   // the transaction's own gas limit (L1)
    var cmds0 = tx.getData().commands || [];
    if (cmds0.some(function (c) { return c.Publish || c.Upgrade || c.$kind === 'Publish' || c.$kind === 'Upgrade'; }))   // audit nv #2
      throw new Error('Kivy stopped this transaction: it would publish or upgrade a program. Nothing was signed.');
    try { calls = (tx.getData().commands || []).filter(function (c) { return c.MoveCall; }).map(function (c) { return {pkg: normAddr(c.MoveCall.package), mod: c.MoveCall.module, fn: c.MoveCall.function}; }); }
    catch (e) { throw new Error('Kivy could not read this transaction, so it was not sent.'); }
    return tx.build({client: client}).then(function (b) {
      return client.core.simulateTransaction({transaction: b, include: {effects: true, balanceChanges: true, objectTypes: true}});
    });
  }).then(function (s) {
    var t = s.Transaction || s.FailedTransaction || s, st = t.status || {};
    var why = st.error && typeof st.error === 'object' ? (st.error.message || st.error.$kind || JSON.stringify(st.error)) : st.error;   // not "[object Object]"
    if (!st.success) throw new Error('This transaction would fail on-chain' + (why ? ' (' + String(why).slice(0, 100) + ')' : '') + ', so it was not sent.');
    var me = normAddr(addr), SUI = normType('0x2::sui::SUI'), out = {}, inn = {}, got = {}, vol = {}, credit = {}, need = {}, bad = [], warn = [];
    var pays = g.pay ? (Array.isArray(g.pay) ? g.pay : [g.pay]) : [];
    Object.keys(g.out || {}).forEach(function (k) { out[normType(k)] = BigInt(g.out[k]); });
    Object.keys(g.in || {}).forEach(function (k) { inn[normType(k)] = BigInt(g.in[k]); });
    Object.keys(g.vol || {}).forEach(function (k) { vol[normType(k)] = BigInt(g.vol[k]); });
    // gas: never more than this transaction's own budget + 10% (it can't burn more), and never more than the server's cap —
    // a flat 0.1 SUI let a route skim ~0.09 SUI unseen (review L1, 2026-10-29)
    var gasOk = BigInt(g.gas || 0); if (budget != null && budget * 11n / 10n < gasOk) gasOk = budget * 11n / 10n;
    out[SUI] = (out[SUI] || 0n) + gasOk;
    // SUI promised to the user is counted before the network fee, which comes out of the same SUI — a small SUI withdrawal
    // was refused as "short" by its own fee (review K-8 follow-up, 2026-10-30)
    var gu = (t.effects || {}).gasUsed, suiNet = 0n, gasNet = gu ? BigInt(gu.computationCost || 0) + BigInt(gu.storageCost || 0) - BigInt(gu.storageRebate || 0) : gasOk;
    if (gasNet < 0n) gasNet = 0n; if (gasNet > gasOk) gasNet = gasOk;
    // Kivy's own fee comes out of the same SUI too; it is an approved payment (in g.pay, checked below), so it is added back —
    // a dust sweep into SUI was refused for exactly its fee
    var fa = g.fee || g.fee_added;                    // sign() adds the fee, then passes it here as fee_added
    var feeBack = fa && pays.some(function (p) { return normAddr(p.address) === normAddr(fa.address) && normType(p.coin_type) === SUI; }) ? BigInt(fa.mist || 0) : 0n;
    // every approved payment, added up per recipient and coin: two sends to one address need both amounts (audit V01)
    pays.forEach(function (p) { var k = normAddr(p.address) + '|' + normType(p.coin_type); need[k] = (need[k] || 0n) + BigInt(p.amount); });
    Object.keys(reserve).forEach(function (ty) {
      if (reserve[ty] > (out[ty] || 0n)) bad.push('it could take up to ' + reserve[ty].toString() + ' units of ' + ty.split('::').pop() + ' from your account balance' + (out[ty] ? ' (approved ' + out[ty].toString() + ')' : ', which you did not approve'));
    });
    if (odd) {                                       // a new format: reported, and refused (was reported only; a reviewer 2026-11-04 S3)
      pipe(g, 'pkg_unlisted', {pkgs: ['FundsWithdrawal: unreadable reservation']});
      bad.push('it takes from your account balance in a way Kivy cannot read');
    }
    var swapping = calls.some(function (c) { return c.mod === 'router' && KIVY_PKGS.indexOf(c.pkg) >= 0; });   // Aftermath's own (allowlisted) router, not any module named 'router' (S-01)
    calls.forEach(function (c) {
      if (KIVY_PKGS.indexOf(c.pkg) >= 0) return;
      warn.push(c.pkg + '::' + c.mod);                 // reported for vetting, and refused: no exemption for a module's NAME (review H1)
      bad.push('it calls a program Kivy has not verified (' + c.pkg.slice(0, 10) + '…::' + c.mod + ')');
    });
    if (!g.perps && calls.some(function (c) { return PERPS_PKGS.indexOf(c.pkg) >= 0; })) bad.push('it calls the perps program, which this plan does not use');
    // pure aggregator swap: an aggregator is called and nothing outside a router pulls value out of a protocol (no lending
    // withdraw / redeem / borrow / claim, no perps collateral withdraw) — else a route-only coin keeps the strict rule
    var FW = [normAddr('0x1'), normAddr('0x2')];
    var pureAggSwap = calls.some(function (c) { return AGG_PKGS.indexOf(c.pkg) >= 0; }) &&
      !calls.some(function (c) { return FW.indexOf(c.pkg) < 0 && /withdraw|redeem|borrow|claim|collateral|unstake|liquidat/i.test(c.fn); });   // any module (H1)
    (t.balanceChanges || []).forEach(function (c) {
      var who = normAddr(c.address), ty = normType(c.coinType), v = BigInt(c.amount);
      if (who === me) {
        if (ty === SUI) suiNet += v;
        if (v >= 0n) { got[ty] = (got[ty] || 0n) + v; return; }
        var ct = g.ctokens && /::reserve::CToken<.+?,\s*(.+)>$/.exec(ty);
        if (ct) {                                   // a withdrawal hands back the user's own receipts — only of the planned coin, and
          var und = normType(ct[1].trim());         // never more receipt units than the planned coin amount (a receipt is worth ≥ 1 unit)
          if (!(und in vol)) bad.push('it would spend receipts of ' + und.split('::').pop() + ', which this plan does not withdraw');
          else if (vol[und] > 0n && -v > vol[und] * 101n / 100n) bad.push('it would spend more ' + und.split('::').pop() + ' receipts than this withdrawal needs');
          return;
        }
        if (-v > (out[ty] || 0n)) bad.push('it would take ' + (-v).toString() + ' units of ' + ty.split('::').pop() + (out[ty] ? ' (approved ' + out[ty].toString() + ')' : ', which you did not approve'));
      } else if (v > 0n) credit[who + '|' + ty] = (credit[who + '|' + ty] || 0n) + v;
    });
    Object.keys(inn).forEach(function (ty) { if ((ty === SUI ? suiNet + gasNet + feeBack : (got[ty] || 0n)) < inn[ty]) bad.push('you would not receive the ' + ty.split('::').pop() + ' this should pay you'); });
    Object.keys(need).forEach(function (k) { if ((credit[k] || 0n) < need[k]) bad.push('the recipient would not get the ' + k.split('::').pop() + ' you approved'); });
    var payee = {}; pays.forEach(function (p) { payee[normAddr(p.address)] = 1; });
    Object.keys(credit).forEach(function (k) {     // anyone else (a swap route's fee wallet): at most 1% of the most of that coin the plan moves
      var who = k.split('|')[0], ty = k.split('|')[1];
      if (k in need || payee[who]) return;
      var base = [vol[ty], out[ty], inn[ty], got[ty]].reduce(function (m, x) { return x && x > m ? x : m; }, 0n);
      if (swapping && !base && ROUTER_FEE_WALLETS.some(function (w) { return normAddr(w) === who; })) return;   // the route's own fee wallet only (audit nv #1)
      if (!base && pureAggSwap) return;               // a route-only coin inside a PURE trusted-aggregator swap (review pre-launch H1)
                                       // (2026-10-25); the user's own coins keep the 1% limit below, as the server's check 2 does
      if (!base) { bad.push('another address (' + who.slice(0, 10) + '…) would receive ' + ty.split('::').pop() + ' that this plan never mentions'); return; }
      if (credit[k] * 100n > base) bad.push('another address (' + who.slice(0, 10) + '…) would receive ' + credit[k].toString() + ' units of ' + ty.split('::').pop());
    });
    var types = s.objectTypes || t.objectTypes || {};
    ((t.effects || {}).changedObjects || []).forEach(function (o) {
      var was = ownerAddr(o.inputOwner), now = ownerAddr(o.outputOwner), ty = String(types[o.objectId] || '');
      if (was && normAddr(was) === me && now && normAddr(now) !== me) bad.push('it would hand one of your objects to another address');
      // one of your objects that is not a coin may not leave your wallet in any other way either: wrapped, deleted, put under
      // another object or shared (audit nv #3 — only transfers to an address were caught). Coins merge and split all the time.
      else if (was && normAddr(was) === me && !(now && normAddr(now) === me) && !/^0x0*2::coin::Coin</.test(ty)) bad.push('it would take one of your objects (' + (ty.split('::').slice(-2).join('::') || 'an object') + ') out of your wallet');
      // a NEW object for someone else must be a plain coin (a payment, checked above) — never a wrapper that could carry your funds
      else if (!was && now && normAddr(now) !== me && !/^0x0*2::coin::Coin</.test(ty)) bad.push('it would create something (' + (ty.split('::').slice(-2).join('::') || 'an object') + ') for another address');
      // a coin of yours, or a new coin, made shared or frozen is out of your hands too — anyone may take a shared coin (review M1)
      else if (ownerShared(o.outputOwner) && /^0x0*2::coin::Coin</.test(ty) && (was ? normAddr(was) === me : !o.inputOwner)) bad.push('it would put a coin where anyone can take it (shared)');
    });
    if (warn.length) pipe(g, 'pkg_unlisted', {pkgs: warn.slice(0, 5)});
    if (bad.length) throw new Error('Kivy stopped this transaction: ' + bad[0] + '. Nothing was signed.');
    return (t.balanceChanges || []).slice(0, 12).map(function (c) { return {a: normAddr(c.address), t: c.coinType, v: String(c.amount)}; });   // for the stats pipeline
  });
}
