import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {buildFlow,exactTotals,flowPositions,primaryFinding,sourceSeconds} from '../static/flow.mjs';
import {durationText,graphElements,visibleTransactionIds} from '../static/format.mjs';
const cytoscape=createRequire(import.meta.url)('../static/vendor/cytoscape.min.js');
const tx=(txId,from,to,timestamp,amount='1.00')=>({txId,fromAccountKey:from,toAccountKey:to,timestamp,amountPaid:amount,paymentCurrency:'US Dollar',amountReceived:amount,receivingCurrency:'US Dollar'});
function fixture(edges,finding){return {graph:{targetTxId:edges.at(-1).txId,nodes:[...new Set(edges.flatMap(e=>[e.fromAccountKey,e.toAccountKey]))].map(accountKey=>({accountKey})),edges},target:edges.at(-1),highlightTxIds:finding?.evidenceTxIds||[],ruleResult:{findings:finding?[finding]:[]}};}
const time='2022-09-01T11:00:00';

test('the primary pattern is deterministic and prioritizes a pattern containing the target',()=>{
  const p=fixture([tx('target','A','B',time)],null);
  const low={ruleId:'FAN_IN',score:65,evidenceTxIds:['target']},high={ruleId:'CYCLE',score:90,evidenceTxIds:['target']};
  p.ruleResult.findings=[low,high];assert.equal(primaryFinding(p),high);
  p.ruleResult.findings=[{score:100,evidenceTxIds:['other']},low];assert.equal(primaryFinding(p),low);
  assert.equal(primaryFinding(fixture(p.graph.edges,null)),null);
});
test('cycle display preserves the returned connected path even at identical timestamps',()=>{
  const edges=[tx('Z','1:A','2:B',time),tx('A','2:B','3:C',time),tx('M','3:C','1:A',time)];
  const finding={ruleId:'CYCLE',evidenceTxIds:['Z','A','M']},p=fixture(edges,finding),flow=buildFlow(p,finding);
  assert.deepEqual(flow.rows.map(e=>e.txId),['Z','A','M']);assert.equal(flow.accounts.length,3);assert.equal(flow.hasTies,true);assert.equal(flow.roles.get('1:A'),'시작·복귀 계좌');
  const cy=cytoscape({headless:true,elements:graphElements(p)});
  for(const row of flow.rows)assert.equal(cy.getElementById('edge:'+row.txId).source().data('accountKey'),row.fromAccountKey);
  cy.destroy();
});
test('a disconnected or time-reversed cycle is rejected instead of drawn as a loop',()=>{
  const finding={ruleId:'CYCLE',evidenceTxIds:['1','2']};
  assert.throws(()=>buildFlow(fixture([tx('1','A','B',time),tx('2','C','A',time)],finding),finding));
  assert.throws(()=>buildFlow(fixture([tx('1','A','B','2022-09-01T12:00:00'),tx('2','B','A',time)],finding),finding));
});
test('fan-in sums received amounts by currency without inferring an exchange rate',()=>{
  const edges=[{...tx('2','2:B','3:C',time,'10.00'),amountReceived:'9.42',receivingCurrency:'Euro'},tx('1','1:A','3:C',time,'0.10')];
  const finding={ruleId:'FAN_IN',accountKey:'3:C',evidenceTxIds:['2','1']},flow=buildFlow(fixture(edges,finding),finding);
  assert.deepEqual(flow.rows.map(e=>e.txId),['1','2']);assert.deepEqual(flow.totals,[{currency:'Euro',amount:'9.42'},{currency:'US Dollar',amount:'0.10'}]);assert.equal(flow.center,'3:C');assert.equal(flow.hasTies,true);
});
test('fan-out sums paid amounts and keeps every parallel transfer as a separate step',()=>{
  const edges=[tx('1','1:A','2:B',time,'0.10'),tx('2','1:A','2:B',time,'0.20'),tx('3','1:A','3:C','2022-09-01T11:01:00','0.30')];
  const finding={ruleId:'FAN_OUT',accountKey:'1:A',evidenceTxIds:['3','2','1']},flow=buildFlow(fixture(edges,finding),finding);
  assert.equal(flow.rows.length,3);assert.equal(flow.rows[2].gapSeconds,60);assert.equal(flow.totals[0].amount,'0.60');assert.equal(flow.roles.get('1:A'),'분산 송금 계좌');
});
test('currency totals preserve values beyond floating point precision and fractional scale',()=>{
  const edges=[tx('1','A','B',time,'9007199254740993.00000001'),tx('2','A','B',time,'0.00000009')];
  assert.equal(exactTotals(edges)[0].amount,'9007199254740993.00000010');
});
test('missing, duplicated, or wrongly oriented rule evidence cannot create displayed connections',()=>{
  const p=fixture([tx('1','A','B',time)],null);
  assert.throws(()=>buildFlow(p,{ruleId:'FAN_IN',accountKey:'B',evidenceTxIds:['missing']}));
  assert.throws(()=>buildFlow(p,{ruleId:'FAN_IN',accountKey:'B',evidenceTxIds:['1','1']}));
  assert.throws(()=>buildFlow(p,{ruleId:'FAN_IN',accountKey:'A',evidenceTxIds:['1']}));
  assert.throws(()=>buildFlow(p,{ruleId:'FAN_OUT',accountKey:'B',evidenceTxIds:['1']}));
});
test('no finding displays just the target, without declaring the source account normal',()=>{
  const p=fixture([tx('1','A','B',time),tx('2','B','C',time)],null),flow=buildFlow(p);
  assert.equal(flow.kind,'TARGET');assert.deepEqual(flow.rows.map(e=>e.txId),['2']);assert.equal(flow.finding,null);assert.deepEqual([...visibleTransactionIds(p)],['2']);
});
test('calendar arithmetic crosses dates without adding the browser timezone',()=>{
  assert.equal(sourceSeconds('2022-10-01T00:00:01')-sourceSeconds('2022-09-30T23:59:59'),2);
  assert.throws(()=>sourceSeconds('2022-09-30T23:59:59+09:00'));
  assert.throws(()=>sourceSeconds('2022-02-31T00:00:00'));
  assert.equal(durationText(3661),'1시간 1분 1초');
});
test('pattern layout preserves node identity, separates counterparties, and centers the actual hub',()=>{
  const edges=[tx('1','A','H',time),tx('2','B','H',time),tx('3','C','H',time)];
  const finding={ruleId:'FAN_IN',accountKey:'H',evidenceTxIds:['1','2','3']},flow=buildFlow(fixture(edges,finding),finding);
  for(const narrow of [false,true]){
    const positions=flowPositions(flow,flow.accounts,{narrow});assert.deepEqual([...positions.keys()].sort(),['A','B','C','H']);assert.equal(new Set([...positions.values()].map(p=>JSON.stringify(p))).size,4);
    if(!narrow)assert.deepEqual(positions.get('H'),{x:0,y:0});else assert.ok(positions.get('A').x<positions.get('H').x);
  }
});
test('switching between focused and surrounding views never rewrites evidence or the original payload',()=>{
  const edges=[tx('1','A','H',time),tx('2','B','H',time),tx('3','H','C',time)],finding={ruleId:'FAN_IN',accountKey:'H',evidenceTxIds:['1','2']},p=fixture(edges,finding),before=JSON.stringify(p);
  buildFlow(p,finding);assert.equal(visibleTransactionIds(p,'evidence',finding).size,3);assert.equal(visibleTransactionIds(p,'all',finding).size,3);assert.equal(JSON.stringify(p),before);
});
