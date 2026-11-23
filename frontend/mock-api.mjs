// Frontend-only contract fixture adapter. Never connects to Neo4j, RAG or a model.
import {readFile, mkdir, writeFile, rename} from 'node:fs/promises';
import {dirname} from 'node:path';
import {createHash, randomBytes} from 'node:crypto';
import {assertGraphPayload,assertReviewResult} from './static/contract.mjs';

const clone=value=>structuredClone(value);
export const canonical=value=>JSON.stringify(value,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a<b?-1:a>b?1:0)):v);
export const sha=value=>createHash('sha256').update(typeof value==='string'?value:canonical(value)).digest('hex');
const token=()=>randomBytes(24).toString('base64url');
export class ApiError extends Error {
  constructor(status,code,message){super(message);this.status=status;this.code=code;}
}
const fail=(status,code,message)=>{throw new ApiError(status,code,message);};
function fields(body,keys){
  if(!body||Array.isArray(body)||canonical(Object.keys(body).sort())!==canonical([...keys].sort()))fail(400,'invalid_body','현재 화면의 조회 ID로 다시 요청해 주세요.');
}

export async function createDemoApi({historyFile=null,now=()=>Date.now()}={}){
  const captures={};
  const provenance=JSON.parse(await readFile(new URL('./fixtures/provenance.json',import.meta.url),'utf8'));
  for(const name of ['initial','expanded']){
    const raw=await readFile(new URL(`./fixtures/${name}.json`,import.meta.url),'utf8');
    if(sha(raw)!==provenance.fixtureSha256[name])throw Error('Demo fixture checksum mismatch');
    captures[name]=JSON.parse(raw);
    assertGraphPayload(captures[name].payload);
    assertReviewResult(captures[name].payload,captures[name].review);
    if(captures[name].payload.accountExpansion.datasetSha256!==provenance.datasetSha256||sha(captures[name].payload.graph)!==captures[name].review.context.graphSha256)throw Error('Demo fixture dataset or graph version mismatch');
  }
  const snapshots=new Map(),reviews=new Map();let entries=[];
  if(historyFile){try{entries=JSON.parse(await readFile(historyFile,'utf8'));if(!Array.isArray(entries))throw Error('Invalid history');}catch(e){if(e.code!=='ENOENT')throw e;}}
  function put(map,value){
    for(const [key,row] of map)if(row.expires<=now())map.delete(key);
    const id=token();map.set(id,{...value,expires:now()+1_200_000});
    while(map.size>24)map.delete(map.keys().next().value);
    return id;
  }
  function snapshot(id){const row=snapshots.get(id);if(!row||row.expires<=now()||row.superseded)fail(409,'review_expired','조회 정보가 만료됐거나 추가 조회로 바뀌었습니다. 거래를 다시 조회해 주세요.');return row;}
  function review(id){const row=reviews.get(id);if(!row||row.expires<=now())fail(409,'review_expired','초안이 만료됐습니다. 거래를 다시 조회해 주세요.');snapshot(row.snapshotId);return row;}
  function payload(name){const p=clone(captures[name].payload);p.source.kind='frontend_fixture';p.notice='예시 데이터 · 기존 합성 시연 자료의 고정 응답입니다. 실시간 DB 조회가 아닙니다. '+p.notice;return p;}
  function meta(entry){const p=entry.capture.payload,r=entry.capture.review;return {
    historyId:entry.historyId,savedAt:entry.savedAt,snapshotSha256:entry.digest,
    targetTxId:p.graph.targetTxId,transactions:p.graph.edges.length,accounts:p.graph.nodes.length,
    ruleScore:p.ruleResult.ruleScore,hops:p.graph.meta.hops,asOf:p.graph.asOf,windowStart:p.graph.windowStart,
    additionalReadPages:p.accountExpansion.pagesUsed,hasReview:!!r,reportId:r?.report.reportId||null,
    graphSha256:sha(p.graph),corpusSha256:r?.context.corpusSha256||null,
    integrity:{valid:true,basis:'saved_bundle_sha256_and_identity_checks',currentSourcesChecked:false},
    humanReviewRequired:true,readyForSubmission:false,sourceGraphComplete:false};}
  function archived(id){const e=entries.find(e=>e.historyId===id);if(!e)fail(404,'history_not_found','저장본을 찾을 수 없습니다.');if(sha(e.capture)!==e.digest)fail(409,'history_integrity_failed','저장본 내용이 변경되어 열 수 없습니다.');return e;}
  function fact(capture,id,saved=false){
    const ctx=capture.review?.context,f=ctx?.reportFacts.find(f=>f.factId===id);
    if(!f)fail(404,'fact_not_in_report','이 초안에 포함된 사실만 조회할 수 있습니다.');
    return {schemaVersion:'1.0',contextId:ctx.contextId,graphSha256:ctx.graphSha256,corpusSha256:ctx.corpusSha256,
      fact:clone(f),transactions:capture.payload.graph.edges.filter(e=>f.evidenceTxIds.includes(e.txId)),
      verification:{recomputed:false,basis:saved?'archived_verified_context_and_graph':'fixed_frontend_fixture',externalLedgerChecked:false}};
  }
  function citation(capture,id){if(!Object.hasOwn(capture.citations,id))fail(404,'citation_not_in_report','이 초안에 포함된 인용만 조회할 수 있습니다.');return clone(capture.citations[id]);}
  let saving=false;
  return async function handle(method,url,body){
    const u=new URL(url,'http://localhost'),path=u.pathname,q=u.searchParams;
    const exact=keys=>{if([...q.keys()].length!==keys.length||keys.some(k=>!q.get(k)))fail(400,'invalid_request','거래와 조회 범위를 확인해 주세요.');};
    if(method==='GET'&&path==='/api/health')return {service:'traceon-frontend-fixtures',status:'ok',database:'not_connected',readOnly:true};
    if(method==='GET'&&path==='/api/cases')return {schemaVersion:'1.0',items:[clone(captures.initial.payload.target)],source:'frontend_fixture',scope:'selected_demo_subset',total:1};
    if(method==='GET'&&path==='/api/graph'){
      exact(['txId','hops','windowDays']);
      if(q.get('txId')!=='TX-000619283'||q.get('hops')!=='2'||q.get('windowDays')!=='7')fail(400,'fixture_scope_unavailable','예시 데이터는 TX-000619283 · 이전 7일 · 2홉만 제공합니다.');
      const p=payload('initial'),id=put(snapshots,{name:'initial',payload:p});return {...p,reviewSnapshotId:id};
    }
    if(method==='POST'&&path==='/api/graph/expand'){
      if(saving)fail(429,'busy','분석 저장 중입니다. 저장 후 추가 조회를 다시 시도해 주세요.');
      fields(body,['snapshotId','accountKey']);const row=snapshots.get(body.snapshotId);
      // The same parent/account retry returns the latest result without a second page.
      if(row?.next&&row.accountKey===body.accountKey){const next=snapshot(row.next);return {...clone(next.payload),reviewSnapshotId:row.next};}
      const old=snapshot(body.snapshotId);
      if(old.name!=='initial'||!old.payload.accountExpansion.candidates.some(c=>c.canExpand&&c.accountKey===body.accountKey))fail(400,'invalid_account','응답에서 제공한 추가 조회 후보 계좌를 선택해 주세요.');
      const p=payload('expanded'),id=put(snapshots,{name:'expanded',payload:p});
      old.superseded=true;old.next=id;old.accountKey=body.accountKey;
      return {...p,reviewSnapshotId:id};
    }
    if(method==='POST'&&path==='/api/review/prepare'){
      fields(body,['snapshotId','findingIndex']);const row=snapshot(body.snapshotId),c=captures[row.name];
      if(!Number.isInteger(body.findingIndex)||!c.payload.ruleResult.findings[body.findingIndex])fail(400,'invalid_finding','현재 그래프의 탐지 경로를 선택해 주세요.');
      const r=clone(c.review),f=c.payload.ruleResult.findings[body.findingIndex];
      r.focus={findingIndex:body.findingIndex,ruleId:f.ruleId,evidenceTxIds:clone(f.evidenceTxIds)};
      const id=put(reviews,{snapshotId:body.snapshotId,name:row.name,result:r});
      return {reviewId:id,snapshotId:body.snapshotId,...r};
    }
    if(path.startsWith('/api/review/')){
      const op=path.slice('/api/review/'.length);
      if(method==='POST'&&op==='verify'){fields(body,['reviewId']);const r=review(body.reviewId);return {reviewId:body.reviewId,report:clone(r.result.report),verification:clone(r.result.verification)};}
      if(method==='POST'&&op==='fact'){fields(body,['reviewId','factId']);const r=review(body.reviewId);return fact(captures[r.name],body.factId);}
      if(method==='GET'&&op==='citation'){exact(['reviewId','chunkId']);const r=review(q.get('reviewId'));return citation(captures[r.name],q.get('chunkId'));}
      if(method==='GET'&&op==='download'){exact(['reviewId','format']);const r=review(q.get('reviewId'));return download(q.get('format'),r.result);}
    }
    if(method==='POST'&&path==='/api/history/save'){
      fields(body,['snapshotId','reviewId']);
      if(saving)fail(429,'busy','저장 중입니다. 잠시 후 다시 시도해 주세요.');
      const s=snapshot(body.snapshotId),r=body.reviewId===null?null:review(body.reviewId);
      if(r&&r.snapshotId!==body.snapshotId)fail(409,'history_review_mismatch','현재 그래프와 같은 조회의 초안만 저장할 수 있습니다.');
      const capture=clone(captures[s.name]);capture.payload=clone(s.payload);capture.review=r?clone(r.result):null;if(!r)capture.citations={};
      const digest=sha(capture),existing=entries.find(e=>e.digest===digest);
      if(existing)return {schemaVersion:'1.0',created:false,history:meta(archived(existing.historyId))};
      if(entries.length>=100)fail(413,'history_full','예시 저장본 100개 한도에 도달했습니다.');
      const e={historyId:'H-'+randomBytes(12).toString('hex'),savedAt:new Date(now()).toISOString(),digest,capture};
      const next=[e,...entries];saving=true;
      try{if(historyFile){await mkdir(dirname(historyFile),{recursive:true});await writeFile(historyFile+'.tmp',JSON.stringify(next));await rename(historyFile+'.tmp',historyFile);}entries=next;}
      finally{saving=false;}
      return {schemaVersion:'1.0',created:true,history:meta(e)};
    }
    if(method==='GET'&&path==='/api/history'){
      if([...q.keys()].some(k=>k!=='cursor')||q.getAll('cursor').length>1)fail(400,'invalid_request','저장 목록 요청을 확인해 주세요.');
      const start=q.has('cursor')?entries.findIndex(e=>e.historyId===q.get('cursor'))+1:0;
      const page=entries.slice(start,start+20);page.forEach(e=>archived(e.historyId));
      return {schemaVersion:'1.0',items:page.map(meta),nextCursor:entries.length>start+20?page.at(-1).historyId:null,total:entries.length,maxEntries:100};
    }
    if(method==='GET'&&path.startsWith('/api/history/')){
      const op=path.slice('/api/history/'.length);
      exact(['historyId',...(['citation','fact','download'].includes(op)?[op==='citation'?'chunkId':op==='fact'?'factId':'format']:[])]);
      const e=archived(q.get('historyId')),h=meta(e),c=clone(e.capture);
      if(op==='open')return {schemaVersion:'1.0',history:h,payload:{...c.payload,history:h},review:c.review};
      if(op==='verify')return {schemaVersion:'1.0',history:h};
      if(op==='citation')return {...citation(c,q.get('chunkId')),history:h};
      if(op==='fact')return {...fact(c,q.get('factId'),true),history:h};
      if(op==='download')return download(q.get('format'),{...c.review,history:h,capture:c});
    }
    fail(404,'not_found','지원하지 않는 예시 API 요청입니다.');
  };
}
function download(format,result){
  if(!['json','markdown'].includes(format)||format==='markdown'&&!result.report)fail(400,'invalid_format','초안 또는 근거 JSON 형식을 선택해 주세요.');
  return {download:true,mime:format==='json'?'application/json':'text/markdown',extension:format==='json'?'json':'md',
    content:format==='json'?JSON.stringify(result,null,2):'# 예시 데이터 검토 초안\n\n사람 검토 필요 · 현재 자료 미조회\n\n'+result.report.markdown};
}
