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
// Audit V01: the Move packages a Kivy transaction may call — Suilend, NAVI, Aftermath perps, the framework and the swap
// routes seen so far (same list as /opt/kivy/kivy_pkgs.json). A lending/perps/other call to any other package is refused;
// an unlisted DEX adapter inside a swap route is allowed but reported (the money rules below still bound it).
// Aftermath's swap-route fee wallet: its share can come in a middle coin of the route (afSUI…), which the plan doesn't list
var KIVY_PKGS = (function () {                      // the allowlist: the server puts kivy_pkgs.json in #kivy-pkgs (one list); this copy is the fallback
  var el = document.getElementById('kivy-pkgs'); try { if (el) return JSON.parse(el.textContent); } catch (e) {}
  return ["0x1", "0x2", "0x59672975e15c58ffc9450f5236b6aa9fa2407d94b8081e634778aa01957d8c6f", "0x13e1e0ddcf3a76cde006d530e98a0f985c446013cfedeae6dd067a2f1ea88ff5", "0x4837ae94425107554c8847721cf9954c1ad8e10520433b9e37dc11c507148bea", "0x512f28261c1a293f49416d8885b8d5d32bde6dd68a99a0be36fe42b248e6833a", "0x3ec740df8428aa9c93aaef7f8cc1542ac3194fd014826b51bfe245346d64efc7", "0x145d86a4d4c6d2ea04fe858b60099faf3ea00ca4fd441263229780f0099fc552", "0xdd7a36e04666c3d816f4d3b97bc666e7188fb468fb0dfdfbbec3898d07707e7f", "0xe42ac8aae0abacc1cad43439927c3563f7d315386203a876d7f7006632d77f14", "0x011354dcf03830d91354a7895949188e60c88b2bb96ef66143880c3cace66fa3", "0x05bb8426f7a0d92382533ce392d87975770394403990f83c5a43157e282627e3", "0x7ca2b6a3241764817175cfa99c2cd3e973a2025071e7c83ec7338ce52a893413", "0x7de5de8d75a8f4e42cdd3c018f788bc9b9ebf2d3d61dcfe9d2136f17f077afd5", "0x8ad41a6d9374b18a2fd9cdedb2a0c4cd1da540848e17577fa0328ea8f1c96d24", "0x8cd0b2cbaf9d39f457f2d6a6fca0c96500a4d5654b99e1ba86d74a518c68017a", "0xb5e52ac983d8be934d5d1ca7cd51695c4e6a549e55943b166dd02d0b522e1d2d", "0xbee9cb333752646694dcf1fbdfcf232a7e110e4cce02047cf52a8c04de34c133", "0xeef5a96c8797c5bfbf1dbee1ce33782111d51aeb91a92fea4233513ab7ca7498", "0xf92e4e916418d5ae50437cd9fddc17f338429df64062feab1a512c506072f742", "0xfa1fbcb69a0018c620171e191beffa01c38bea9eefd8e55f5cf8708903f7cb29", "0x1443655ce6bb848c59a4114eec6efb5f46126462c92dbe4e610f9682a7d369ee", "0xfa9379a0d22b59732f2f7fac6e6ddc87afa54f8335277c33de9f04fdd4be7520"];
})().map(normAddr);
// swap routers' own fee wallets (Aftermath; Cetus and Bluefin 7k pay none for Kivy's routes): the ONLY addresses a coin the plan
// never mentions may go to during a swap (audit nv #1 — any address used to pass, so a route could drain lent positions)
var ROUTER_FEE_WALLETS = ['0x1fb0c0a6790860fc83c8f26f6fa089868ad94de9468348f98dfb4cdf89a53777'];
// the swap aggregators' entry programs (Aftermath router, Cetus aggregator v3, Bluefin 7k). 2026-10-28: a hop inside their
// route is trusted wherever it goes — a coin only the route touches may go to any address (a pool's fee wallet); the user's own
// coins keep the 1% limit and every "you must receive" check below
var AGG_PKGS = ['0x7de5de8d75a8f4e42cdd3c018f788bc9b9ebf2d3d61dcfe9d2136f17f077afd5', '0xffd4058af7d6f6d66c335930cced8c91e38ec943e2392232aee27e8127e684e9', '0x62412b7268c35f3808336aee57a52836501f40b8ba5d936f8ad275e672befd04'].map(normAddr);
function checkIntent(json, g, addr) {
  if (!g) return Promise.reject(new Error('Kivy could not check this transaction, so it was not sent.'));
  var calls = [], reserve = {}, odd = false;
  return Promise.all([import('/vendor/sui-2.36.0.js'), import('/vendor/sui-2.36.0.js')]).then(function (m) {
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
    out[SUI] = (out[SUI] || 0n) + BigInt(g.gas || 0);
    // every approved payment, added up per recipient and coin: two sends to one address need both amounts (audit V01)
    pays.forEach(function (p) { var k = normAddr(p.address) + '|' + normType(p.coin_type); need[k] = (need[k] || 0n) + BigInt(p.amount); });
    Object.keys(reserve).forEach(function (ty) {
      if (reserve[ty] > (out[ty] || 0n)) bad.push('it could take up to ' + reserve[ty].toString() + ' units of ' + ty.split('::').pop() + ' from your account balance' + (out[ty] ? ' (approved ' + out[ty].toString() + ')' : ', which you did not approve'));
    });
    if (odd) pipe(g, 'pkg_unlisted', {pkgs: ['FundsWithdrawal: unreadable reservation']});   // a new format: reported, the simulation still bounds it
    var swapping = calls.some(function (c) { return c.mod === 'router' && KIVY_PKGS.indexOf(c.pkg) >= 0; });   // Aftermath's own (allowlisted) router, not any module named 'router' (S-01)
    calls.forEach(function (c) {
      if (KIVY_PKGS.indexOf(c.pkg) >= 0) return;
      if (swapping && c.mod === 'router') warn.push(c.pkg + '::' + c.mod);   // a new DEX in the swap route: reported, still bounded below
      else bad.push('it calls a program Kivy has not verified (' + c.pkg.slice(0, 10) + '…::' + c.mod + ')');
    });
    // pure aggregator swap: an aggregator is called and nothing outside a router pulls value out of a protocol (no lending
    // withdraw / redeem / borrow / claim, no perps collateral withdraw) — else a route-only coin keeps the strict rule
    var FW = [normAddr('0x1'), normAddr('0x2')];
    var pureAggSwap = calls.some(function (c) { return AGG_PKGS.indexOf(c.pkg) >= 0; }) &&
      !calls.some(function (c) { return c.mod !== 'router' && FW.indexOf(c.pkg) < 0 && /withdraw|redeem|borrow|claim|collateral|unstake|liquidat/i.test(c.fn); });
    (t.balanceChanges || []).forEach(function (c) {
      var who = normAddr(c.address), ty = normType(c.coinType), v = BigInt(c.amount);
      if (who === me) {
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
    Object.keys(inn).forEach(function (ty) { if ((got[ty] || 0n) < inn[ty]) bad.push('you would not receive the ' + ty.split('::').pop() + ' this should pay you'); });
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
    });
    if (warn.length) pipe(g, 'pkg_unlisted', {pkgs: warn.slice(0, 5)});
    if (bad.length) throw new Error('Kivy stopped this transaction: ' + bad[0] + '. Nothing was signed.');
    return (t.balanceChanges || []).slice(0, 12).map(function (c) { return {a: normAddr(c.address), t: c.coinType, v: String(c.amount)}; });   // for the stats pipeline
  });
}
