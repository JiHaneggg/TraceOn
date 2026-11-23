import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,mkdtemp,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createServer} from 'node:http';
import {createDemoApi,sha} from '../mock-api.mjs';
import {frontendServer} from '../server.mjs';
import {assertGraphPayload,assertReviewResult} from '../static/contract.mjs';
import {assertExpandedPayload} from '../static/expansion-format.mjs';
import {assertArchive} from '../static/history-format.mjs';
import {graphElements} from '../static/format.mjs';
import {verificationState,sourceSegments} from '../static/review-format.mjs';

const query='/api/graph?txId=TX-000619283&hops=2&windowDays=7';
const prepare=(api,p)=>api('POST','/api/review/prepare',{snapshotId:p.reviewSnapshotId,findingIndex:0});
const save=(api,p,r=null)=>api('POST','/api/history/save',{snapshotId:p.reviewSnapshotId,reviewId:r?.reviewId||null});
const graph=api=>api('GET',query);

test('same demo target is preserved in case, graph edge, rule evidence, facts and report',async()=>{
  const api=await createDemoApi(),list=await api('GET','/api/cases'),p=assertGraphPayload(await graph(api));
  assert.equal(p.graph.edges.length,69);assert.deepEqual(list.items[0],p.target);
  const target=p.graph.edges.find(e=>e.txId===list.items[0].txId);
  assert.ok(p.highlightTxIds.includes(target.txId));assert.ok(p.ruleResult.findings[0].evidenceTxIds.includes(target.txId));
  const element=graphElements(p).find(e=>e.data.txId===target.txId);
  assert.equal(element.data.source,'account:'+target.fromAccountKey);assert.equal(element.data.target,'account:'+target.toAccountKey);
  const r=assertReviewResult(p,await prepare(api,p));assert.equal(r.context.graphSha256,sha(p.graph));assert.equal(verificationState(r).valid,true);
  const reordered=structuredClone(r);reordered.context.targetTransaction=Object.fromEntries(Object.entries(reordered.context.targetTransaction).reverse());assertReviewResult(p,reordered);
  const fact=await api('POST','/api/review/fact',{reviewId:r.reviewId,factId:'target.amountPaid'});
  assert.equal(fact.fact.value,target.amountPaid);assert.deepEqual(fact.transactions,[target]);
  assert.equal(p.source.kind,'frontend_fixture');
});

test('69→72 merges exactly 3 new originals, preserves evidence and invalidates every old review route',async()=>{
  const api=await createDemoApi(),p=await graph(api),r=await prepare(api,p);
  const body={snapshotId:p.reviewSnapshotId,accountKey:p.accountExpansion.candidates[0].accountKey};
  const after=assertExpandedPayload(p,assertGraphPayload(await api('POST','/api/graph/expand',body)));
  assert.equal(after.graph.edges.length,72);assert.equal(after.accountExpansion.lastPage.readRows,4);assert.equal(after.accountExpansion.lastPage.duplicateTransactions,1);
  assert.deepEqual(after.highlightTxIds,p.highlightTxIds);
  assert.deepEqual(after.graph.edges.filter(e=>!p.graph.edges.some(old=>old.txId===e.txId)).map(e=>e.txId).sort(),after.accountExpansion.lastPage.addedTxIds.toSorted());
  assert.deepEqual(await api('POST','/api/graph/expand',body),after);
  for(const [method,url,request] of [
    ['POST','/api/review/prepare',{snapshotId:p.reviewSnapshotId,findingIndex:0}],
    ['POST','/api/review/verify',{reviewId:r.reviewId}],
    ['POST','/api/review/fact',{reviewId:r.reviewId,factId:'target.amountPaid'}],
    ['GET','/api/review/citation?'+new URLSearchParams({reviewId:r.reviewId,chunkId:r.report.citations[0].chunkId})],
    ['GET','/api/review/download?'+new URLSearchParams({reviewId:r.reviewId,format:'json'})],
    ['POST','/api/history/save',{snapshotId:after.reviewSnapshotId,reviewId:r.reviewId}],
  ])await assert.rejects(api(method,url,request),e=>e.status===409);
  const fresh=assertReviewResult(after,await prepare(api,after));assert.notEqual(fresh.context.graphSha256,r.context.graphSha256);
});

test('saved 69 graph and 72 review survive service recreation, retain all citations, and cannot act as a live snapshot',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'traceon-frontend-')),historyFile=join(dir,'history.json');
  let api=await createDemoApi({historyFile});const p=await graph(api),before=await save(api,p);
  const after=await api('POST','/api/graph/expand',{snapshotId:p.reviewSnapshotId,accountKey:p.accountExpansion.candidates[0].accountKey});
  const r=await prepare(api,after),saved=await save(api,after,r);
  assert.equal((await save(api,after,r)).created,false);
  api=await createDemoApi({historyFile});const list=await api('GET','/api/history');assert.equal(list.total,2);
  const opened=assertArchive(await api('GET','/api/history/open?historyId='+saved.history.historyId));
  assert.equal(opened.payload.graph.edges.length,72);assert.equal(opened.payload.reviewSnapshotId,undefined);assert.equal(opened.review.reviewId,undefined);
  assert.deepEqual(opened.review.report,r.report);assert.deepEqual(opened.payload.graph,after.graph);
  assert.equal(opened.history.humanReviewRequired,true);assert.equal(opened.history.readyForSubmission,false);assert.equal(opened.history.integrity.currentSourcesChecked,false);
  for(const c of r.report.citations){
    const resolved=await api('GET','/api/history/citation?'+new URLSearchParams({historyId:saved.history.historyId,chunkId:c.chunkId}));
    assert.equal(resolved.corpusSha256,r.context.corpusSha256);
    assert.equal(sourceSegments(resolved.section.text,resolved.section.highlight).highlight,c.text);
  }
  const f=await api('GET','/api/history/fact?'+new URLSearchParams({historyId:saved.history.historyId,factId:'target.amountPaid'}));
  assert.equal(f.transactions[0].txId,p.target.txId);assert.equal(f.verification.recomputed,false);
  assert.equal((await api('GET','/api/history/open?historyId='+before.history.historyId)).payload.graph.edges.length,69);
  await assert.rejects(api('POST','/api/review/prepare',{snapshotId:saved.history.historyId,findingIndex:0}),e=>e.status===409);
  const tampered=JSON.parse(await readFile(historyFile,'utf8'));tampered[0].capture.payload.graph.edges[0].amountPaid='0.01';await writeFile(historyFile,JSON.stringify(tampered));
  const changed=await createDemoApi({historyFile});await assert.rejects(changed('GET','/api/history/open?historyId='+saved.history.historyId),e=>e.status===409);
});

test('mismatched target, missing evidence, original values and cross-graph draft are refused',async()=>{
  const api=await createDemoApi(),p=await graph(api),r=await prepare(api,p);
  for(const mutate of [p=>p.target.txId='TX-999999999',p=>p.graph.edges.pop(),p=>p.highlightTxIds.push('TX-999999999'),p=>p.target.amountPaid='0.01',p=>p.graph.nodes.pop()]){
    const bad=structuredClone(p);mutate(bad);assert.throws(()=>assertGraphPayload(bad));
  }
  const bad=structuredClone(r);bad.context.targetTransaction.amountPaid='0.01';assert.throws(()=>assertReviewResult(p,bad));
  const altered=structuredClone(r);altered.context.graphSha256='0'.repeat(64);assert.throws(()=>assertReviewResult(p,altered));
  await assert.rejects(api('GET','/api/graph?txId=TX-000619283&hops=3&windowDays=7'),e=>e.status===400);
});

test('expired source rejects drafts and saving; mismatched snapshot review cannot be saved',async()=>{
  let time=0;const api=await createDemoApi({now:()=>time}),p=await graph(api),r=await prepare(api,p),other=await graph(api);
  await assert.rejects(save(api,other,r),e=>e.code==='history_review_mismatch');
  time=1_200_001;await assert.rejects(save(api,p,r),e=>e.status===409);await assert.rejects(prepare(api,p),e=>e.status===409);
});

test('HTTP serves the frontend and contract responses, rejects hidden files and cross-origin writes',async t=>{
  const server=await frontendServer({historyFile:null});await new Promise(ok=>server.listen(0,'127.0.0.1',ok));t.after(()=>new Promise(ok=>server.close(ok)));
  const base='http://127.0.0.1:'+server.address().port;
  assert.equal((await fetch(base)).status,200);assert.equal((await fetch(base+'/static/.env')).status,404);assert.equal((await fetch(base+'/fixtures/initial.json')).status,404);
  assert.equal((await (await fetch(base+'/_frontend/config')).json()).mode,'fixture');
  const response=await fetch(base+query);assertGraphPayload(await response.json());
  assert.equal((await fetch(base+'/api/history/save',{method:'POST',headers:{Origin:'https://unrelated.invalid','Content-Type':'application/json'},body:'{}'})).status,403);
});

test('explicit backend mode proxies same routes, reports API errors without fixture fallback',async t=>{
  const requests=[];const backend=createServer(async(req,res)=>{let body='';for await(const c of req)body+=c;requests.push({url:req.url,method:req.method,body});res.writeHead(503,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'database_unavailable',message:'test backend unavailable'}));});
  await new Promise(ok=>backend.listen(0,'127.0.0.1',ok));t.after(()=>new Promise(ok=>backend.close(ok)));
  const server=await frontendServer({api:'http://127.0.0.1:'+backend.address().port});await new Promise(ok=>server.listen(0,'127.0.0.1',ok));t.after(()=>new Promise(ok=>server.close(ok)));
  const base='http://127.0.0.1:'+server.address().port;assert.equal((await (await fetch(base+'/_frontend/config')).json()).mode,'api');
  const result=await fetch(base+'/api/graph/expand',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({snapshotId:'source-id',accountKey:'70:1004286A8'})});
  assert.equal(result.status,503);assert.equal((await result.json()).error,'database_unavailable');assert.equal(requests[0].url,'/api/graph/expand');assert.equal(JSON.parse(requests[0].body).snapshotId,'source-id');
});
