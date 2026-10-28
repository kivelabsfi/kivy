/*
 * merge.ts — joins two Sui transaction kinds into one, so the user signs once.
 * Why: Aftermath's perpetuals API builds each part (top-up, order, stop-loss / take-profit, deallocate, withdraw) as its own
 * transaction kind and ignores a kind passed in, so builder.ts asks for each part alone and chains them here (A then B).
 * Input: two base64 transaction kinds + the sender. Output: one @mysten/sui Transaction. Imported by builder.ts only.
 */
import { Transaction } from '@mysten/sui/transactions';
// Join two transaction kinds (A then B) into one: B's inputs are appended (object inputs shared by id)
// and B's references to its own inputs/results are renumbered.
// Called by builder.ts: buildOrder() and joinParts() (an Aftermath order + its SL / TP or top-up parts) and buildCompose()
// (perps_out: idle-margin releases + the withdrawal). Returns a Transaction with the sender set, not yet simulated.
export async function mergeKinds(aB64: string, bB64: string, sender: string) {
  const A = JSON.parse(await Transaction.fromKind(aB64).toJSON());
  const B = JSON.parse(await Transaction.fromKind(bB64).toJSON());
  const objId = (inp: any) => JSON.stringify(inp).match(/"objectId":"(0x[0-9a-f]+)"/)?.[1];
  const inputs = A.inputs.slice();
  // B's input k -> its index in the merged input list (an object already in A is reused, not added twice)
  const map = B.inputs.map((inp: any) => {
    const id = objId(inp);
    if (id) {
      const k = inputs.findIndex((x: any) => objId(x) === id);
      if (k >= 0) {                                   // same object in both: keep one input, mutable if either needs it
        if (JSON.stringify(inp).includes('"mutable":true')) inputs[k] = JSON.parse(JSON.stringify(inputs[k]).replace('"mutable":false', '"mutable":true'));
        return k;
      }
    }
    inputs.push(inp); return inputs.length - 1;
  });
  const off = A.commands.length;
  // rewrite B's argument references: Input -> the merged index, Result / NestedResult -> shifted past A's commands
  const remap = (x: any): any => {
    if (Array.isArray(x)) return x.map(remap);
    if (x && typeof x === 'object') {
      if ('Input' in x && Object.keys(x).length <= 2 && typeof x.Input === 'number') return { ...x, Input: map[x.Input] };
      if ('Result' in x && typeof x.Result === 'number' && Object.keys(x).length <= 2) return { ...x, Result: x.Result + off };
      if ('NestedResult' in x && Array.isArray(x.NestedResult)) return { ...x, NestedResult: [x.NestedResult[0] + off, x.NestedResult[1]] };
      return Object.fromEntries(Object.entries(x).map(([k, v]) => [k, remap(v)]));
    }
    return x;
  };
  const tx = Transaction.from(JSON.stringify({ ...A, inputs, commands: A.commands.concat(B.commands.map(remap)) }));
  tx.setSender(sender);
  return tx;
}
