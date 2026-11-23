// Presentation derived only from returned transaction IDs; never add a synthetic link.
import {amountText} from './format.mjs';

export function primaryFinding(payload) {
  const findings=payload.ruleResult?.findings || [];
  return findings.map((finding,index)=>({finding,index})).sort((a,b)=>
    Number(b.finding.evidenceTxIds.includes(payload.graph.targetTxId))-Number(a.finding.evidenceTxIds.includes(payload.graph.targetTxId)) ||
    b.finding.score-a.finding.score || a.index-b.index)[0]?.finding || null;
}

export function sourceSeconds(timestamp) {
  // IBM local timestamps have no timezone. UTC is used only as neutral calendar arithmetic.
  if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/.test(timestamp))throw new Error('원본 시각 형식을 확인할 수 없습니다.');
  const millis=Date.parse(timestamp+'Z');
  if(!Number.isFinite(millis)||new Date(millis).toISOString().slice(0,19)!==timestamp)throw new Error('원본 시각이 올바르지 않습니다.');
  return millis/1000;
}

export function exactTotals(edges,received=false) {
  const totals=new Map();
  for(const edge of edges){
    const value=String(received?edge.amountReceived:edge.amountPaid),currency=received?edge.receivingCurrency:edge.paymentCurrency;
    if(!/^\d+(?:\.\d+)?$/.test(value))throw new Error('원본 금액 형식을 확인할 수 없습니다.');
    const [whole,fraction='']=value.split('.'),scale=fraction.length,units=BigInt(whole+fraction);
    const previous=totals.get(currency)||{units:0n,scale};
    const nextScale=Math.max(scale,previous.scale);
    totals.set(currency,{scale:nextScale,units:previous.units*10n**BigInt(nextScale-previous.scale)+units*10n**BigInt(nextScale-scale)});
  }
  return [...totals].sort(([a],[b])=>a.localeCompare(b)).map(([currency,{units,scale}])=>{
    const digits=units.toString().padStart(scale+1,'0');
    return {currency,amount:scale?digits.slice(0,-scale)+'.'+digits.slice(-scale):digits};
  });
}

export function buildFlow(payload,finding=null) {
  const byId=new Map(payload.graph.edges.map(edge=>[edge.txId,edge]));
  const ids=finding?finding.evidenceTxIds:[payload.graph.targetTxId];
  if(!ids.length||new Set(ids).size!==ids.length)throw new Error('비어 있거나 중복된 근거 거래입니다.');
  let edges=ids.map(id=>{const edge=byId.get(id);if(!edge)throw new Error('조회 밖의 근거 거래입니다.');sourceSeconds(edge.timestamp);return edge;});
  const kind=finding?.ruleId || 'TARGET';
  if(kind==='CYCLE'){
    if(edges.length<2 || edges.some((edge,i)=>edge.toAccountKey!==edges[(i+1)%edges.length].fromAccountKey ||
      (i>0 && sourceSeconds(edge.timestamp)<sourceSeconds(edges[i-1].timestamp))))throw new Error('순환 근거의 연결 또는 시간 순서가 일치하지 않습니다.');
  }else edges=[...edges].sort((a,b)=>a.timestamp.localeCompare(b.timestamp)||a.txId.localeCompare(b.txId));
  const center=finding?.accountKey || null;
  if(kind==='FAN_IN' && edges.some(edge=>edge.toAccountKey!==center))throw new Error('집금 근거의 수취 계좌가 일치하지 않습니다.');
  if(kind==='FAN_OUT' && edges.some(edge=>edge.fromAccountKey!==center))throw new Error('분산 근거의 송금 계좌가 일치하지 않습니다.');
  const accounts=[...new Set(edges.flatMap(edge=>[edge.fromAccountKey,edge.toAccountKey]))];
  const roles=new Map();
  if(kind==='FAN_IN'||kind==='FAN_OUT'){
    roles.set(center,kind==='FAN_IN'?'집금 계좌':'분산 송금 계좌');
    accounts.filter(key=>key!==center).forEach((key,i)=>roles.set(key,(kind==='FAN_IN'?'송금':'수취')+' 계좌 '+String(i+1).padStart(2,'0')));
  }else if(kind==='CYCLE')accounts.forEach((key,i)=>roles.set(key,i?'경유 계좌 '+i:'시작·복귀 계좌'));
  else{roles.set(payload.target.fromAccountKey,'보내는 계좌');roles.set(payload.target.toAccountKey,'받는 계좌');}
  const sequence=new Map(edges.map((edge,i)=>[edge.txId,i+1]));
  const rows=edges.map((edge,i)=>({...edge,sequence:i+1,gapSeconds:i?sourceSeconds(edge.timestamp)-sourceSeconds(edges[i-1].timestamp):null}));
  const start=edges[0]?.timestamp,end=edges.at(-1)?.timestamp;
  const totals=exactTotals(edges,kind==='FAN_IN');
  return {kind,finding,center,accounts,roles,sequence,rows,start,end,
    elapsedSeconds:edges.length?sourceSeconds(end)-sourceSeconds(start):0,
    hasTies:rows.some(row=>row.gapSeconds===0),totals,
    totalLabel:kind==='TARGET'?'대상 거래 송금액':kind==='FAN_IN'?'누적 수취액':kind==='CYCLE'?'경로 거래 합계 · 중복 이동 포함':'누적 송금액',
    totalsText:totals.map(row=>amountText(row.amount,row.currency)).join(' / ')};
}

export function flowPositions(flow,visibleKeys,{narrow=false}={}) {
  const positions=new Map(),keys=flow.accounts.filter(key=>visibleKeys.includes(key));
  if(flow.kind==='FAN_IN'||flow.kind==='FAN_OUT'){
    const others=keys.filter(key=>key!==flow.center);
    if(narrow){
      const direction=flow.kind==='FAN_IN'?1:-1;
      positions.set(flow.center,{x:direction*90,y:0});
      others.forEach((key,i)=>positions.set(key,{x:-direction*90,y:(i-(others.length-1)/2)*60}));
    }else{
      const radius=Math.max(230,others.length*30);
      positions.set(flow.center,{x:0,y:0});
      others.forEach((key,i)=>{const angle=-Math.PI/2+i*2*Math.PI/others.length;positions.set(key,{x:radius*Math.cos(angle),y:radius*Math.sin(angle)});});
    }
  }else if(flow.kind==='CYCLE'){
    const radius=keys.length===2?165:200;
    keys.forEach((key,i)=>{const angle=keys.length===2?(narrow?-Math.PI/2+i*Math.PI:Math.PI-i*Math.PI):-Math.PI/2+i*2*Math.PI/keys.length;positions.set(key,{x:radius*Math.cos(angle),y:radius*Math.sin(angle)});});
  }else keys.forEach((key,i)=>positions.set(key,narrow?{x:0,y:i*280}:{x:i*320,y:0}));
  const extras=visibleKeys.filter(key=>!positions.has(key));
  extras.forEach((key,i)=>positions.set(key,{x:450,y:220+i*110}));
  return positions;
}
