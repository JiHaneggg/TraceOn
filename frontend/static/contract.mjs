// Refuse mismatched responses before anything is drawn or saved by the UI.
function stable(value){return JSON.stringify(value,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a<b?-1:a>b?1:0)):v);}
export function assertGraphPayload(p){
  const bad=()=>{throw new Error('표·그래프·룰 근거의 원본 거래 연결이 일치하지 않습니다. 다시 조회해 주세요.');};
  if(p?.schemaVersion!=='1.0'||!p.graph||!Array.isArray(p.graph.edges)||!Array.isArray(p.graph.nodes)||!Array.isArray(p.ruleResult?.findings)||!Array.isArray(p.highlightTxIds))bad();
  const edges=p.graph.edges,nodes=p.graph.nodes,byId=new Map(edges.map(e=>[e.txId,e])),accounts=new Set(nodes.map(n=>n.accountKey));
  if(byId.size!==edges.length||accounts.size!==nodes.length||!byId.has(p.graph.targetTxId)||p.target?.txId!==p.graph.targetTxId)bad();
  const fields=['txId','timestamp','fromAccountKey','toAccountKey','amountPaid','paymentCurrency','amountReceived','receivingCurrency','paymentFormat'];
  if(fields.some(k=>p.target[k]!==byId.get(p.graph.targetTxId)[k]))bad();
  if(edges.some(e=>fields.some(k=>typeof e[k]!=='string')||!accounts.has(e.fromAccountKey)||!accounts.has(e.toAccountKey)||e.timestamp<p.graph.windowStart||e.timestamp>p.graph.asOf))bad();
  const evidence=new Set(p.ruleResult.findings.flatMap(f=>f.evidenceTxIds));
  if([...evidence].some(id=>!byId.has(id))||new Set(p.highlightTxIds).size!==p.highlightTxIds.length||evidence.size!==p.highlightTxIds.length||p.highlightTxIds.some(id=>!evidence.has(id)))bad();
  if(p.summary?.transactions!==edges.length||p.summary.accounts!==nodes.length||p.summary.patterns!==p.ruleResult.findings.length||p.summary.evidenceTransactions!==evidence.size)bad();
  return p;
}

export function assertReviewResult(payload,result){
  const c=result?.context,r=result?.report,v=result?.verification,byId=new Map(payload.graph.edges.map(e=>[e.txId,e]));
  const bad=()=>{throw new Error('현재 그래프와 초안의 원본 거래·근거가 일치하지 않습니다. 초안을 다시 준비해 주세요.');};
  if(c?.targetTxId!==payload.graph.targetTxId||stable(c.targetTransaction)!==stable(payload.target)||stable(c.ruleResult)!==stable(payload.ruleResult)||c.graphSha256!==r?.graphSha256||c.contextId!==r?.contextId||c.corpusSha256!==r?.corpusSha256||v?.reportId!==r?.reportId)bad();
  if(!Array.isArray(c.evidenceTransactions)||!Array.isArray(c.reportFacts)||c.evidenceTransactions.some(e=>stable(e)!==stable(byId.get(e.txId)))||c.reportFacts.some(f=>f.evidenceTxIds.some(id=>!byId.has(id))))bad();
  const focus=payload.ruleResult.findings[result.focus?.findingIndex];
  if(!focus||JSON.stringify(focus.evidenceTxIds)!==JSON.stringify(result.focus.evidenceTxIds))bad();
  return result;
}
