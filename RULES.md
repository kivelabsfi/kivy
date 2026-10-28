# Kivy — hard rules

Set by the owner (card A-0005, 2026-09-21). Code must enforce these, not just state them.

1. **Never hold users' keys.** Users sign in their own wallet; Kivy only builds and explains transactions.
2. **No $ limit** on swaps, sends, lending or perps (2026-10-23; perps since 10-18): the user approves every transaction in their own wallet. `cap_usd` in `knowledge/kivy.json` is 0; a number brings a per-order limit back.
3. **Allowed coins:** SUI, USDC, DEEP, WAL, BTC (xBTC), ETH, SOL — plus any coin Suilend or NAVI lists (USDsui, vSUI, haSUI, sSUI, LBTC, wBTC, BLUE, NAVX…; 2026-10-20). A protocol listing is the filter; anything unlisted is refused.
4. **Allowed venues:** Aftermath router for swaps, with Cetus and then Bluefin 7k as backups when Aftermath's route fails (same coins, amount and minimum; 2026-10-26); Suilend (Main, Ember, Matrixdock) and NAVI (main + its curated vaults) for lending — **supply only, never borrow**; Aftermath perps.
5. **Say it before doing it.** Every plan lists each step, the venue, the cost (gas, slippage cap), and the risks.
6. **Confirm EVERY on-chain / money step.** Each transaction — every swap, deposit, withdrawal, order, transfer — is shown on its own and needs the user's explicit yes for THAT step before it is built and signed. A multi-step plan is confirmed step by step, never once for the whole plan. (the owner, 2026-09-30) **Exception (the owner, 2026-10-20):** steps built into ONE atomic transaction — swap-then-lend, withdraw-then-swap, deposit+order — are one approval: the card names every step, and if any step fails nothing happens.
7. **Money out needs permission, every time.** A send to any other address needs the user's explicit OK for that transfer.
8. **Slippage cap 0.5%** (`slippage_cap_pct`), tighter per pair (`slippage_tiers`: stablecoins 0.05%, SUI ↔ staked SUI 0.1%, big coins 0.2%). A swap that would exceed it is refused, and the minimum the user approved is what the chain enforces.
9. **Website safety.** Public since 2026-10-15; the chat never passes text to an agent with shell access or tools (helper agents that summarise visitor text run with no tools), and the site runs as its own sandboxed user.
10. **Log every action** to the operator's change log.
11. **Knows only its own folder.** Replies come from `knowledge/kivy.json` + public prices; nothing else on the server is read or mentioned.
12. **The AI only asks; Kivy's code acts.** With an AI model, actions go through tool calling: the model returns a structured request, Kivy's server checks the rules and runs only its own allow-listed scripts. The model never executes anything itself.
13. **Perps (live since 2026-10-16):** Aftermath perps, no cap (2026-10-18); orders only use USDC the user deposited into the perps account, never pulled from the wallet.
14. **Checked twice, every transaction.** Before the wallet opens, the transaction is simulated and must match the approved plan: every program on Kivy's allowlist (by package id), no publish or upgrade, no coin or object of the user going anywhere unapproved (coins the plan never mentions may go only to a swap router's fee wallet, or anywhere inside a trusted aggregator's route — Aftermath, Cetus, Bluefin 7k — since 2026-10-28), account-balance spends within the approved amount. Perps transactions are also checked against the chain in the builder (verified market list, side, size, leverage). After it lands, the real result is compared with the plan again (check 2); a ⚠️ alerts.
15. **What the card shows is what happens.** The recipient shown is the one paid (a .sui name that moved is refused); amounts, fees and minimums come from Kivy's own plan, never from the browser.
16. **Privacy switch is honoured everywhere.** With "Let the AI see my wallet" off, the AI receives no balances or amounts; cards are built by Kivy's code instead.
17. **Records belong to their wallet.** Labels, Activity and the AI's memory show only transactions the wallet itself signed with a plan its own visitor built.
