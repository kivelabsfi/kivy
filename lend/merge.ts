import { Transaction } from '@mysten/sui/transactions';
// Join two transaction kinds (A then B) into one: B's inputs are appended (object inputs shared by id)
// and B's references to its own inputs/results are renumbered.
export async function mergeKinds(aB64: string, bB64: string, sender: string) {
  const A = JSON.parse(await Transaction.fromKind(aB64).toJSON());
  const B = JSON.parse(await Transaction.fromKind(bB64).toJSON());
  const objId = (inp: any) => JSON.stringify(inp).match(/"objectId":"(0x[0-9a-f]+)"/)?.[1];
  const inputs = A.inputs.slice();
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
