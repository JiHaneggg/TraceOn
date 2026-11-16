import {amountText,decimalText,timeText} from './format.mjs';

export const CHECK_LABELS={amounts:'금액',times:'시각·경과 시간',counts:'건수·계좌 수',citations:'원문 인용',sourceSnapshots:'저장 원문',renderedContent:'보고서 본문',coverage:'필수 사실 포함',context:'거래·문서 버전',reportDigest:'초안 내용 일치',evidenceFitness:'근거 점검 결과 일치',otherFacts:'계좌·거래 속성'};
export function verificationState(result){
  const {context,report,verification:v}=result||{};
  const required=['context','amounts','times','counts','citations','coverage','sourceSnapshots','renderedContent','reportDigest'];
  const identities=!!context&&!!report&&!!v&&context.contextId===report.contextId&&report.contextId===v.contextId&&report.reportId===v.reportId&&context.graphSha256===report.graphSha256&&context.corpusSha256===report.corpusSha256;
  const valid=identities&&v.valid===true&&required.every(key=>v.checks?.[key]?.valid===true)&&Object.values(v.checks||{}).every(check=>check.valid===true)&&v.humanReviewRequired===true&&v.readyForSubmission===false&&v.semanticEntailmentVerified===false&&report.humanReviewRequired===true;
  return {valid,downloadAllowed:valid,label:valid?'자동 검사 통과':'자동 검사 미통과',abstained:report?.answerDisposition?.status==='abstained',humanReviewRequired:true};
}
export function factLabel(id){
  const fields={amountPaid:'송금액',amountReceived:'수취 금액',paymentCurrency:'송금 통화',receivingCurrency:'수취 통화',timestamp:'거래 시각',fromAccountKey:'보내는 계좌',toAccountKey:'받는 계좌',paymentFormat:'거래 방식',transactionCount:'근거 거래 수',accountCount:'연결 계좌 수',counterpartyCount:'상대 계좌 수',hops:'경로 단계',firstTimestamp:'첫 근거 시각',lastTimestamp:'마지막 근거 시각',elapsedSeconds:'경과 시간(초)'};
  if(id==='rule.score')return '룰 점수';
  const currency=id.match(/\.(grossPaidTotalsByCurrency|grossReceivedTotalsByCurrency)\.(.+)$/);
  if(currency)return (currency[1]==='grossPaidTotalsByCurrency'?'누적 송금액':'누적 수취액')+' · '+currency[2];
  return fields[id.split('.').at(-1)]||id;
}
export function factValue(context,fact){
  const id=fact.factId,value=fact.value,target=context.targetTransaction;
  if(id==='target.amountPaid')return amountText(value,target.paymentCurrency);
  if(id==='target.amountReceived')return amountText(value,target.receivingCurrency);
  const currency=id.match(/\.(?:grossPaidTotalsByCurrency|grossReceivedTotalsByCurrency)\.(.+)$/);
  if(currency)return amountText(value,currency[1]);
  if(/\.(timestamp|firstTimestamp|lastTimestamp)$/.test(id))return timeText(value);
  return decimalText(value);
}
export function focusedFacts(result,all=false){
  const prefix=result.focus.findingIndex===null?null:`finding.${result.focus.findingIndex}.`;
  return result.context.reportFacts.filter(fact=>all||fact.factId.startsWith('target.')||fact.factId==='rule.score'||prefix&&fact.factId.startsWith(prefix));
}
export function groupedCitations(result){
  const selected=result.context.evidenceFitness?.findings?.find(row=>row.findingId===`finding.${result.focus.findingIndex}`);
  const preferred=new Set((selected?.referenceChecks||[]).flatMap(role=>role.references.filter(ref=>ref.matched).map(ref=>ref.chunkId)));
  const quotes=new Map(result.report.draft.citations.map(row=>[row.chunkId,row.quote]));
  const groups=new Map();
  for(const row of result.report.citations){
    const display=row.sourceDisplay||{category:'other_reference',label:'참고 자료',notice:'자료의 관할과 적용 범위를 확인하세요.'};
    if(!groups.has(display.category))groups.set(display.category,{...display,rows:[]});
    groups.get(display.category).rows.push({...row,quote:quotes.get(row.chunkId)||'',preferred:preferred.has(row.chunkId)});
  }
  const order=['domestic_law','domestic_guidance','domestic_internal','foreign_reference','other_reference'];
  return [...groups.values()].sort((a,b)=>order.indexOf(a.category)-order.indexOf(b.category)).map(group=>({...group,rows:group.rows.sort((a,b)=>Number(b.preferred)-Number(a.preferred))}));
}
export function sourceSegments(text,highlight){
  const characters=Array.from(text);
  if(!highlight||!Number.isInteger(highlight.start)||!Number.isInteger(highlight.end)||highlight.start<0||highlight.end<highlight.start||highlight.end>characters.length)throw new Error('원문 인용 위치를 확인하지 못했습니다.');
  return {before:characters.slice(0,highlight.start).join(''),highlight:characters.slice(highlight.start,highlight.end).join(''),after:characters.slice(highlight.end).join('')};
}
export function safeSourceUrl(value){
  try{const url=new URL(value);return url.protocol==='https:'&&!url.username&&!url.password&&['kofiu.go.kr','www.kofiu.go.kr','law.go.kr','www.law.go.kr','www.fincen.gov','fintrac-canafe.canada.ca'].includes(url.hostname)?url.href:null;}catch{return null;}
}
export class RequestEpoch{
  constructor(){this.value=0;}
  reset(){return ++this.value;}
  current(value){return value===this.value;}
}
