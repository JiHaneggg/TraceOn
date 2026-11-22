import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {decimalText,amountText,accountParts,accountActivity,graphElements,visibleTransactionIds} from '../static/format.mjs';
import {RelationshipGraph} from '../static/graph.mjs';
const require=createRequire(import.meta.url);const cytoscape=require('../static/vendor/cytoscape.min.js');
const payload={graph:{targetTxId:'TX-000000001',nodes:[{accountKey:'1:ABC'},{accountKey:'2:DEF'}],edges:[
 {txId:'TX-000000001',fromAccountKey:'1:ABC',toAccountKey:'2:DEF',amountPaid:'0.10',paymentCurrency:'US Dollar'},
 {txId:'TX-000000002',fromAccountKey:'2:DEF',toAccountKey:'1:ABC',amountPaid:'0.20',paymentCurrency:'US Dollar'},
 {txId:'TX-000000003',fromAccountKey:'1:ABC',toAccountKey:'2:DEF',amountPaid:'0.30',paymentCurrency:'US Dollar'}]},
 target:{fromAccountKey:'1:ABC',toAccountKey:'2:DEF'},highlightTxIds:['TX-000000001','TX-000000002']};

test('decimal display never rounds a large or fractional source amount',()=>{assert.equal(decimalText('9007199254740993.00000001'),'9,007,199,254,740,993.00000001');assert.equal(amountText('0.10','US Dollar'),'0.10 USD');});
test('account keys preserve leading zeros and bank identity',()=>{assert.deepEqual(accountParts('001:000123'),{bank:'001',account:'000123'});});
test('incoming values use the received currency while outgoing values use paid currency',()=>{const edge={fromAccountKey:'1:A',toAccountKey:'2:B',amountPaid:'10.00',paymentCurrency:'US Dollar',amountReceived:'9.42',receivingCurrency:'Euro'};assert.deepEqual(accountActivity(edge,'2:B'),{direction:'입금',amount:'9.42',currency:'Euro'});assert.deepEqual(accountActivity(edge,'1:A'),{direction:'출금',amount:'10.00',currency:'US Dollar'});assert.throws(()=>accountActivity(edge,'3:C'));});
test('parallel and reverse transfers retain separate IDs and correct direction',()=>{const cy=cytoscape({headless:true,elements:graphElements(payload)});assert.equal(cy.edges().length,3);assert.equal(cy.getElementById('edge:TX-000000002').source().data('accountKey'),'2:DEF');assert.equal(cy.getElementById('edge:TX-000000002').target().data('accountKey'),'1:ABC');cy.destroy();});
test('only rule evidence is highlighted, regardless of amount',()=>{const cy=cytoscape({headless:true,elements:graphElements(payload)});assert.equal(cy.edges('.evidence-edge').length,2);assert.equal(cy.getElementById('edge:TX-000000003').hasClass('evidence-edge'),false);cy.destroy();});
test('evidence mode and a finding do not silently drop the target',()=>{assert.equal(visibleTransactionIds(payload).size,2);assert.deepEqual([...visibleTransactionIds(payload,'evidence',{evidenceTxIds:['TX-000000002']})],['TX-000000001','TX-000000002']);assert.equal(visibleTransactionIds(payload,'all').size,3);});
test('unconnected or duplicated source IDs are rejected',()=>{const p=structuredClone(payload);p.graph.edges[0].toAccountKey='missing';assert.throws(()=>graphElements(p));const other=structuredClone(payload);other.graph.edges.push(other.graph.edges[0]);assert.throws(()=>graphElements(other));});
test('a rule cannot highlight evidence outside the displayed source graph',()=>{const p=structuredClone(payload);p.highlightTxIds.push('unavailable');assert.throws(()=>graphElements(p));});

test('new account page transactions are marked without replacing evidence or direction',()=>{
  const p=structuredClone(payload);p.accountExpansion={lastPage:{addedTxIds:['TX-000000003']}};
  const graph=Object.create(RelationshipGraph.prototype);graph.cy=cytoscape({headless:true});graph.setMode=()=>{};
  graph.setData(p,'all');
  assert.equal(graph.cy.edges('.new-edge').length,1);assert.equal(graph.cy.edges('.evidence-edge').length,2);
  assert.equal(graph.cy.getElementById('edge:TX-000000003').source().data('accountKey'),'1:ABC');
  graph.setData({...p,accountExpansion:{lastPage:{addedTxIds:[]}}},'all');assert.equal(graph.cy.edges('.new-edge').length,0);graph.cy.destroy();
});

test('surrounding view leaves newly added transactions visible without dimming',()=>{
  const p=structuredClone(payload);p.graph.edges.forEach(row=>Object.assign(row,{timestamp:'2026-01-09T12:00:00',amountReceived:row.amountPaid,receivingCurrency:row.paymentCurrency}));
  const finding={ruleId:'CYCLE',evidenceTxIds:['TX-000000001','TX-000000002']};
  const graph=Object.create(RelationshipGraph.prototype);graph.cy=cytoscape({headless:true});graph.layout=()=>{};graph.onVisible=()=>{};
  graph.setData(p,'all',finding);
  assert.equal(graph.cy.edges('.hidden').length,0);assert.equal(graph.cy.edges('.dimmed').length,0);
  graph.setMode('evidence',finding);assert.equal(graph.cy.edges('.hidden').length,1);graph.cy.destroy();
});
