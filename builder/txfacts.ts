/*
 * txfacts.ts — what a signed transaction really did, read from the chain (check 2's evidence), plus the coin-type normaliser.
 * In operation: builder.ts serves it as GET /tx-facts on :8799 (caller: server.py check_tx(), the landed transaction against
 * the plan) and gateway.ts serves the same function on :8798. One copy, so the two can never disagree. Read-only.
 * Input: a gRPC client (chain.ts `grpc`) and a transaction digest. Output: {ok, success, error, sender, balance_changes,
 * moved, taken, other_cmds, gas_net, modules, packages, calls}; throws when the chain has no effects for it yet.
 * Links: chain.ts (the client), @mysten/sui/utils (normalizeStructTag).
 */
import { normalizeStructTag } from '@mysten/sui/utils';

// one spelling per coin type / address: 0x2::sui::SUI == 0x000…02::sui::SUI
export const normType = (t: string) => { try { return normalizeStructTag(t); } catch { return t; } };

// ── what a signed transaction really did (for Kivy's after-the-fact check against the plan) ──
export async function txFacts(grpc: any, digest: string) {
  const r: any = await (grpc as any).core.getTransaction({ digest, include: { effects: true, balanceChanges: true, transaction: true, objectTypes: true, events: true } });
  const t = r.Transaction ?? r.FailedTransaction ?? r.transaction ?? r;
  const st = t.effects?.status ?? t.status;
  // no status = no effects to compare: an error, so check 2 retries and ends 'unknown' — never green on zero evidence (review K-6)
  if (typeof st?.success !== 'boolean') throw new Error('the chain reply has no effects for this transaction yet');
  const own = (o: any) => o?.AddressOwner ?? o?.ConsensusAddressOwner?.owner ?? null;
  const sender = t.transaction?.sender ?? null;
  const moved = (t.effects?.changedObjects ?? []).filter((o: any) => own(o.inputOwner) && own(o.outputOwner) && normType(own(o.inputOwner) + '::a::b') !== normType(own(o.outputOwner) + '::a::b'))
    .map((o: any) => ({ object: o.objectId, from: own(o.inputOwner), to: own(o.outputOwner) }));
  // one of the user's objects that is not a coin leaving the wallet any other way: wrapped, deleted, shared or put under another
  // object — check 1 refuses these; check 2 now sees them too (review K-9, 2026-10-30)
  const types: any = r.objectTypes ?? t.objectTypes ?? {};
  const isMe = (a: any) => !!a && !!sender && normType(a + '::a::b') === normType(sender + '::a::b');
  const taken = (t.effects?.changedObjects ?? []).filter((o: any) => isMe(own(o.inputOwner)) && !isMe(own(o.outputOwner)) && !own(o.outputOwner)
      && !/^0x0*2::coin::Coin</.test(String(types[o.objectId] ?? '')))
    .map((o: any) => ({ object: o.objectId, type: String(types[o.objectId] ?? '') }));
  const other_cmds = [...new Set((t.transaction?.commands ?? []).map((c: any) => c.$kind ?? Object.keys(c)[0]).filter((k: any) => !['MoveCall', 'SplitCoins', 'MergeCoins', 'TransferObjects', 'MakeMoveVec'].includes(k)))];
  // SUI an Aftermath stop-loss / take-profit ticket holds for whoever triggers it ('gas' in CreatedStopOrderTicket; the wallet is one
  // of its executors). Check 2 allows exactly this on a perps transaction, never more (2026-11-15: an order with its stop-loss read
  // as 0.001 SUI over the plan). A ticket claiming over 0.01 SUI is not counted, so it still shows as SUI taken.
  const evs = (t.events ?? r.events ?? []).map((e: any) => ({ type: String(e.eventType ?? e.type ?? ''), j: e.json ?? e.parsedJson ?? {} }));
  const tix = evs.filter((e: any) => /::events::CreatedStopOrderTicket(<|$)/.test(e.type) && (e.j.executors ?? []).some((x: any) => isMe(x))
    && /^\d+$/.test(String(e.j.gas ?? '')) && BigInt(String(e.j.gas)) <= 10_000_000n);
  const perp_ticket_gas = String(tix.reduce((a: bigint, e: any) => a + BigInt(String(e.j.gas)), 0n));
  const gu = t.effects?.gasUsed;                    // the real network fee: check 2 compares SUI before gas, not with a flat 0.1 SUI (K-8)
  const gas_net = gu ? String(BigInt(gu.computationCost ?? 0) + BigInt(gu.storageCost ?? 0) - BigInt(gu.storageRebate ?? 0)) : null;
  const modules = [...new Set((t.transaction?.commands ?? []).filter((c: any) => c.MoveCall).map((c: any) => c.MoveCall.module + '::' + c.MoveCall.function))];
  const packages = [...new Set((t.transaction?.commands ?? []).filter((c: any) => c.MoveCall).map((c: any) => normType(c.MoveCall.package + '::a::b').split('::')[0]))];
  return { ok: true, success: st.success !== false, error: st.success === false ? JSON.stringify(st.error ?? st).slice(0, 200) : null, sender,
           balance_changes: (t.balanceChanges ?? []).map((b: any) => ({ address: b.address, coin_type: normType(b.coinType), amount: String(b.amount) })),
           moved, taken, other_cmds, gas_net, perp_ticket_gas, perp_tickets: tix.length, modules, packages,
           calls: [...new Set((t.transaction?.commands ?? []).filter((c: any) => c.MoveCall).map((c: any) => normType(c.MoveCall.package + '::a::b').split('::')[0] + '::' + c.MoveCall.module + '::' + c.MoveCall.function))] };
}
