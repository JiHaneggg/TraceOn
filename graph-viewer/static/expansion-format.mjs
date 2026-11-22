const fields=['txId','timestamp','fromAccountKey','toAccountKey','amountPaid','paymentCurrency','amountReceived','receivingCurrency','paymentFormat'];
export function assertExpandedPayload(before,after){
  const a=before.graph,b=after.graph;
  if(!after.reviewSnapshotId||after.reviewSnapshotId===before.reviewSnapshotId||a.targetTxId!==b.targetTxId||a.asOf!==b.asOf||a.windowStart!==b.windowStart||a.meta.hops!==b.meta.hops)throw new Error('현재 거래·시점·홉과 추가 조회 결과가 일치하지 않습니다.');
  if(after.accountExpansion.pagesUsed!==before.accountExpansion.pagesUsed+1||b.edges.length>1000||b.edges.length<a.edges.length)throw new Error('추가 조회 한도를 확인하지 못했습니다.');
  const rows=new Map(b.edges.map(edge=>[edge.txId,edge]));
  if(rows.size!==b.edges.length||a.edges.some(edge=>!rows.has(edge.txId)||fields.some(key=>rows.get(edge.txId)[key]!==edge[key])))throw new Error('기존 거래와 추가 조회의 원본 값이 일치하지 않습니다.');
  if(b.edges.some(edge=>edge.timestamp<b.windowStart||edge.timestamp>b.asOf))throw new Error('추가 거래가 원래 조회 기간 밖에 있습니다.');
  return after;
}
export function expandableAccounts(payload){
  if(payload?.history)return [];
  const expansion=payload?.accountExpansion;
  return expansion?.enabled&&!expansion.budgetReached?expansion.candidates.filter(row=>row.canExpand):[];
}
