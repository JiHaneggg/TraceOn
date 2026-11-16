export const currencyCodes = {'US Dollar':'USD','Euro':'EUR','UK Pound':'GBP','Yuan':'CNY','Yen':'JPY','Ruble':'RUB','Rupee':'INR','Won':'KRW','Swiss Franc':'CHF','Brazil Real':'BRL','Saudi Riyal':'SAR','Canadian Dollar':'CAD','Australian Dollar':'AUD','Mexican Peso':'MXN','Bitcoin':'BTC','Shekel':'ILS'};
export const currencyCode = value => currencyCodes[value] || value;
export function decimalText(value) {
  const raw=String(value ?? '');
  if (!/^-?\d+(?:\.\d+)?$/.test(raw)) return raw || '—';
  const [whole,fraction]=raw.split('.');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g,',')+(fraction===undefined?'':'.'+fraction);
}
export const amountText = (value,currency) => `${decimalText(value)} ${currencyCode(currency)}`;
export const timeText = value => String(value || '—').replace('T',' ');
export function accountParts(key) {
  const value=String(key);const split=value.indexOf(':');
  return split<0?{bank:'—',account:value}:{bank:value.slice(0,split),account:value.slice(split+1)};
}
export function accountShort(key) {
  const {bank,account}=accountParts(key);
  return `${bank} · ${account.length>7?'…'+account.slice(-6):account}`;
}
export function durationText(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  if (seconds<60) return `${seconds}초`;
  const minutes=Math.floor(seconds/60),remaining=seconds%60;
  if(minutes<60)return `${minutes}분${remaining?' '+remaining+'초':''}`;
  return `${Math.floor(minutes/60)}시간${minutes%60?' '+minutes%60+'분':''}${remaining?' '+remaining+'초':''}`;
}
export const ruleName = rule => ({CYCLE:'순환 송금',FAN_IN:'분산 집금',FAN_OUT:'다수 계좌 송금'}[rule] || rule);
export function accountActivity(edge,key) {
  if(edge.toAccountKey===key)return {direction:edge.fromAccountKey===key?'내부 이체':'입금',amount:edge.amountReceived,currency:edge.receivingCurrency};
  if(edge.fromAccountKey===key)return {direction:'출금',amount:edge.amountPaid,currency:edge.paymentCurrency};
  throw new Error('이 계좌와 연결되지 않은 거래입니다.');
}

export function graphElements(payload) {
  const {graph,target,highlightTxIds}=payload;
  const highlight=new Set(highlightTxIds),keys=new Set(graph.nodes.map(n=>n.accountKey));
  if(new Set(graph.edges.map(e=>e.txId)).size!==graph.edges.length)throw new Error('중복된 거래 ID입니다.');
  const edges=graph.edges.map(edge=>{
    if(!keys.has(edge.fromAccountKey)||!keys.has(edge.toAccountKey))throw new Error('거래에 연결된 계좌가 없습니다.');
    return {data:{id:'edge:'+edge.txId,source:'account:'+edge.fromAccountKey,target:'account:'+edge.toAccountKey,
      txId:edge.txId,amount:amountText(edge.amountPaid,edge.paymentCurrency),displayLabel:amountText(edge.amountPaid,edge.paymentCurrency)},
      classes:[edge.txId===graph.targetTxId?'target-edge':'',highlight.has(edge.txId)?'evidence-edge':''].filter(Boolean).join(' ')};
  });
  for(const id of highlight)if(!graph.edges.some(e=>e.txId===id))throw new Error('조회 밖의 근거 거래입니다.');
  return [...graph.nodes.map(node=>({data:{id:'account:'+node.accountKey,accountKey:node.accountKey,
    label:`은행 ${accountParts(node.accountKey).bank}\n${accountParts(node.accountKey).account}`},
    classes:[node.accountKey===target.fromAccountKey?'source-account':'',node.accountKey===target.toAccountKey?'destination-account':''].filter(Boolean).join(' ')})),...edges];
}

export function visibleTransactionIds(payload,mode='evidence',finding=null) {
  if(mode==='all')return new Set(payload.graph.edges.map(e=>e.txId));
  return new Set([payload.graph.targetTxId,...(finding?finding.evidenceTxIds:payload.highlightTxIds)]);
}
