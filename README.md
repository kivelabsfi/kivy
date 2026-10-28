# Kivy

**DeFAI: all your finance, in one chat.**

A chat assistant for money on Sui. Say what you want in plain English, like "swap 5 SUI to USDC", "lend my USDC where
it earns most" or "long BTC $10 at 5x". Kivy builds the transaction, shows every step and its cost, checks it twice,
and your own wallet signs it. Kivy never holds a key.

Live: **https://kivy.kivelabs.xyz** (Sui mainnet, testing phase) · Built by [Kive Labs](https://kivelabs.xyz)

---

## What it does

| Job | Example | One approval |
|---|---|---|
| Swap | `swap 5 SUI to USDC` | ✓ |
| Send | `send 1 USDC to name.sui`, several coins at once | ✓ |
| Lend / withdraw | `lend 10 USDC`, `withdraw all from suilend` | ✓ |
| Multi-step plans | `withdraw sSUI from Suilend, swap to USDC, lend half on NAVI and send the rest to bob.sui` | ✓ one PTB |
| One coin into several | `withdraw haSUI from NAVI, then 25% each into USDC, USDsui, SUI and DEEP, deposit into Suilend` | ✓ one PTB (2–4 coins, each lent or kept) |
| Perps | `long US500 $20 5x`, close, TP/SL (up to 3 take-profits), margin, limit / stop / TWAP orders | ✓ |
| Across your accounts | `move my USDC from my Slush account to my Vera account and lend it` | one approval per account, in order |
| Read | balances, lending rates for every coin, positions, history, charts | — |

- **67 coins**: Kivy's core coins plus every coin Suilend or NAVI lists. **61 perps markets** on Aftermath, each verified on chain every hour.
- **Venues**: Aftermath (swap router, perps) with Cetus and Bluefin 7k as backup swap routers, Suilend and NAVI (supply only, never borrowing).
- **Many steps, one signature.** Sui transactions (PTBs) run several steps atomically: all succeed or none do.

## How it stays safe

```
message ─► reader (exact rules) ─unclear─► AI (tool calls only)
              │                                  │
              └──────────────► card ◄────────────┘
                                │
   build + simulate ─► check 1 (browser re-simulates) ─► user signs in own wallet ─► chain ─► check 2 (compare with plan) ─► ✅ / ⚠️
```

1. **The AI never builds or sends anything.** It can only ask Kivy's code for an action. The code checks the rules,
   builds the transaction and simulates it.
2. **Check 1, before the wallet opens:** the browser simulates the final transaction. It blocks the transaction if it
   would take more than the user approved, pay anyone else, move or wrap the user's objects, call a program outside
   Kivy's allowlist (by package id), or publish/upgrade a program. Spending from an account balance must fit the
   approved amount. The code: [`app/guard.js`](app/guard.js), copied unchanged from the live page.
3. **Check 2, after it lands:** the server reads the real transaction and compares it with the plan it saved. It checks:
   nothing extra went out, at least the promised amount came in, sends were exact, the right protocol and the right
   signer. A ⚠️ raises an alert.
4. **Perps and swaps checked at the builder too:** every perps transaction is simulated and compared with the request
   (market on the on-chain-verified list, side, size, leverage) before the user sees it, and every swap's on-chain
   minimum is the one the user approved, backup routers included. A .sui name that changed after the card showed it
   is refused.
5. **Limits:** no $ limit — every transaction, any size, needs the user's approval in their own wallet. Swap slippage
   is capped at 0.5%.
6. **Page lock-down:**
   - Content-Security-Policy: scripts load only from Kivy, and its libraries are self-hosted with checksums. Connections
     stay open to https/wss so wallets can reach their own servers.
   - Each card is bound to the account it was built for.
   - Amounts are read by an exact number reader.
7. **Isolation:** both services run as their own sandboxed system user and can read only Kivy's files; the builder can
   read only the one key it needs.
8. **Privacy and abuse:** with "Let the AI see my wallet" off the AI gets no balances or amounts; limits key on the
   real visitor address (IPv6 per /64); helper agents never get tools when they read visitor text.

## Fees

- **Simple transactions are free:** one swap, send, lend, withdraw or perps action. **Sends are always free**, even several coins
  to several people in one transaction.
- **Multi-step transactions** (several actions in one approval) include a flat **0.0025 SUI** to Kivy's treasury,
  inside the same transaction and shown on the card before signing.
- No percentage fees.
- After a perps account opens, Kivy offers (once, optionally) to link its Aftermath referral code with one free signature.

## Walrus and Tatum

| What | Where |
|---|---|
| **Walrus = Kivy's security memory.** Every transaction's checks (plan, check 1, chain result, check 2) and the rule set (the program allowlist) are written to Walrus as records: verdict plus a hashed wallet only, so they are tamper-proof and anyone can check them without seeing who signed. Corrections are new records, never edits. | `walrus/` (`export_records.py` → `upload.mjs`, `recall.mjs`) |
| **Tatum = backup chain and price source.** When Sui GraphQL fails, reads and transaction builds fail over to Tatum's Sui gRPC, and wallet values fall back to Tatum prices. Display only: prices never feed a safety check. | `builder/chain.ts`, `app/server.py` |

## The web page

The page itself (chat, cards, wallet panel, trading terminal) is not in this repository: use it live at
**https://kivy.kivelabs.xyz**. It runs **check 1** in the user's browser before the wallet opens: it re-simulates the
final transaction on Sui and refuses it if any coin or object would move beyond the approved plan, if it calls a
program outside the allowlist (`app/kivy_pkgs.json`), or if it would publish or upgrade a program — its code is in `app/guard.js`. The server
(`app/server.py`) builds every plan and runs check 2 after the transaction lands.

## Architecture

| Part | Path in this repo | Runs as |
|---|---|---|
| Rules, cards, transaction API, sign-in | `app/server.py`, `app/accounts.py` | `kivy-site` (Python stdlib HTTP server, :8772) |
| Transaction builder | `builder/builder.ts` (TypeScript, `@mysten/sui`, Suilend / NAVI / Aftermath SDKs) | `kivy-lend` (local only, :8799) |
| AI brain | `brain.py` (trimmed): the 10 tools the AI may request (read data or make a PLAN — none can sign or send), the tool-calling loop where Kivy's code runs every tool, and the leak filter. The prompt, model choices and keys are private. | called by the site |
| What Kivy knows and says | `knowledge/kivy.json` (rules, coins, copy, FAQ, fee switch); the AI's own prompt and tool notes are private | read fresh on every call |
| Metrics | `app/metrics.py`: SQLite, wallets stored as salted hashes, no addresses | inside the site |
| Tools | `tools/sync_perps_markets.py` (perps markets verified on chain, hourly) | cron |

Chain reads go through **Sui GraphQL** first, with JSON-RPC only as a fallback.

## Run it

```bash
# builder
cd builder && npm ci && npx tsx builder.ts serve      # serves 127.0.0.1:8799

# site
python3 app/server.py                          # serves 127.0.0.1:8772
```

- **Configuration:** `knowledge/kivy.json` (network, RPC/GraphQL endpoints, caps, coins, `revenue` switch). Set
  `KIVY_PUBLIC=0` to lock the chat behind a login.
- **AI keys:** come from the service environment, never from the repo.
- **Runtime data:** `app/data/` is created at runtime and is **not** in this repo. It holds the internal token, the
  metrics salt and database, chats and check logs.

## Security

- No keys, tokens or user data are in this repository: runtime data lives in `app/data/` and keys come from the service
  environment, both outside git (see `.gitignore`).
- Found a security problem? Email **info@kivelabs.xyz** rather than opening a public issue.

## Docs

- `RULES.md`: the hard limits

## License

**PolyForm Noncommercial 1.0.0** (see `LICENSE`). You may read, run and modify this code for any **non-commercial**
purpose. Commercial use needs a separate licence from Kive Labs: info@kivelabs.xyz.

Copyright (c) 2026 Kive Labs.
