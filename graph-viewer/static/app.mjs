import {RelationshipGraph} from './graph.mjs';
import {accountParts,accountShort,accountActivity,amountText,decimalText,timeText,durationText,ruleName,visibleTransactionIds} from './format.mjs';
import {buildFlow,primaryFinding} from './flow.mjs';
import {ReviewPanel} from './review.mjs';

const $=id=>document.getElementById(id);
function element(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node;}
const state={cases:[],payload:null,selectedId:null,selectedKind:'transaction',selectedKey:null,mode:'evidence',finding:null,flow:null,page:0,request:0,controller:null};
let graph,review;

function connection(ok){$('connection').className='connection '+(ok?'connected':'failed');$('connection').querySelector('span').textContent=ok?'실제 DB 연결됨':'DB 조회 확인 필요';}
function overlay(title,message,error=false){$('graph-overlay').hidden=false;$('graph-overlay').classList.toggle('error',error);$('overlay-title').textContent=title;$('overlay-message').textContent=message;}
async function api(path,signal){const response=await fetch(path,{signal,cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.message||'조회를 완료하지 못했습니다.');return data;}

function renderCases(){
  const query=$('case-search').value.trim().toLowerCase();
  const items=state.cases.filter(row=>[row.txId,row.fromAccountKey,row.toAccountKey].some(value=>value.toLowerCase().includes(query)));
  const list=$('case-list');list.replaceChildren();$('case-count').textContent=state.cases.length;
  if(!items.length){list.append(element('div','empty-note','일치하는 시연 거래가 없습니다.'));return;}
  for(const row of items){
    const button=element('button','case-item'+(row.txId===state.selectedId?' selected':''));button.dataset.txId=row.txId;button.setAttribute('aria-pressed',String(row.txId===state.selectedId));
    const top=element('span','case-top');top.append(element('span','case-number',`CASE ${String(state.cases.indexOf(row)+1).padStart(2,'0')}`),element('span','case-state',row.txId===state.selectedId?'분석 대상':'↗'));
    button.append(top,element('strong','case-id',row.txId),element('span','case-date',timeText(row.timestamp)),
      element('span','case-money',amountText(row.amountPaid,row.paymentCurrency)),element('span','case-route',`${accountShort(row.fromAccountKey)} → ${accountShort(row.toAccountKey)}`));
    button.addEventListener('click',()=>loadTransaction(row.txId));list.append(button);
  }
}

function updateModes(){document.querySelectorAll('[data-mode]').forEach(button=>{const active=button.dataset.mode===state.mode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});}
function setMode(mode,finding=state.finding){state.mode=mode;state.finding=finding;state.flow=buildFlow(state.payload,finding);updateModes();graph.setMode(mode,finding);renderFlow();updateNotice();graph.select(state.selectedKind,state.selectedKey);review.setContext(state.payload,finding);}
function updateNotice(){
  if(!state.payload)return;
  const p=state.payload;let message;
  if(state.finding&&state.mode==='evidence')message=`${ruleName(state.finding.ruleId)}의 실제 근거 거래 ${state.finding.evidenceTxIds.length}건을 표시합니다. 선의 번호와 시간순 근거의 번호가 연결됩니다.`;
  else if(state.mode==='evidence')message=p.highlightTxIds.length?'탐지 근거와 대상 거래를 중심으로 표시합니다. 주변 거래는 ‘주변 연결’에서 확인하세요.':'현재 룰 조건에 해당하는 경로가 없어 대상 거래만 표시합니다. ‘주변 연결’에서 주변 거래를 확인하세요.';
  else message=`대상 거래 시각 이전 ${p.graph.meta.windowDays||$('days').value}일 · ${p.graph.meta.hops}홉 범위의 연결을 표시합니다.`;
  if(p.graph.meta.truncated)message+=' 조회 상한에 도달해 일부 경로가 생략됐습니다.';
  if(!graph.labels)message+=' 금액은 거래를 선택하거나 ‘전체 금액’으로 확인하세요.';
  $('graph-notice').textContent=message;
}

function resetDetails(){
  state.payload=null;state.finding=null;state.flow=null;state.page=0;graph.clear();review.setContext(null,null);
  for(const id of ['rule-score','account-count','transaction-count','pattern-count','table-count','findings-count'])$(id).textContent='—';
  $('score-fill').style.width='0%';$('score-state').textContent='조회 중';$('hop-caption').textContent='선택한 범위를 DB에서 조회합니다';
  $('evidence-caption').textContent='룰 근거 확인 중';$('graph-scope').textContent='조회 중';$('visible-count').textContent='—';$('table-range').textContent='—';
  $('inspector').replaceChildren(element('div','empty-note','원본 거래 정보를 확인하고 있습니다…'));
  $('transactions-body').replaceChildren();$('findings-list').replaceChildren();$('export').disabled=true;
  $('pattern-picker').replaceChildren();$('flow-summary').replaceChildren();$('timeline-list').replaceChildren();$('timeline-count').textContent='—';$('timeline-note').textContent='근거 거래를 조회하고 있습니다.';$('timeline-progress').textContent='—';
  $('flow-previous').disabled=true;$('flow-next').disabled=true;
  $('next-page').disabled=true;$('previous-page').disabled=true;
}

async function loadTransaction(id){
  if(!id)return;
  state.selectedId=id;state.controller?.abort();state.controller=new AbortController();const request=++state.request;
  const row=state.cases.find(row=>row.txId===id);
  resetDetails();renderCases();$('mobile-cases').value=id;$('target-id').textContent=id;
  $('target-flow').textContent=row?`${accountShort(row.fromAccountKey)} → ${accountShort(row.toAccountKey)}`:'DB에서 조회 중';
  $('target-amount').textContent=row?amountText(row.amountPaid,row.paymentCurrency):'—';
  overlay('거래 관계를 확인하고 있습니다','실제 DB에서 계좌·송금 경로를 조회하고 룰 근거를 계산합니다.');
  try{
    const params=new URLSearchParams({txId:id,hops:$('hops').value,windowDays:$('days').value});
    const payload=await api('/api/graph?'+params,state.controller.signal);
    if(request!==state.request)return;
    if(payload.graph.targetTxId!==id)throw new Error('선택한 거래와 응답이 일치하지 않습니다.');
    state.payload=payload;state.finding=primaryFinding(payload);state.flow=buildFlow(payload,state.finding);state.mode='evidence';connection(true);renderMetrics();updateModes();graph.setData(payload,state.mode,state.finding);$('labels').setAttribute('aria-pressed',String(graph.labels));renderFlow();updateNotice();
    selectItem('transaction',id,false);renderFindings();renderTable();review.setContext(payload,state.finding);$('graph-overlay').hidden=true;$('export').disabled=false;
    const time=new Date(payload.source.queriedAt).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
    $('query-time').textContent=`${time} DB 조회 · 시연용 부분 데이터이며 전체 원장이 아닙니다.`;
  }catch(error){
    if(error.name==='AbortError'||request!==state.request)return;
    connection(false);overlay('거래를 불러오지 못했습니다',error.message,true);$('score-state').textContent='조회 실패';
    $('inspector').replaceChildren(element('div','empty-note','상단의 새로 조회 버튼으로 다시 시도할 수 있습니다.'));
  }
}

function renderMetrics(){
  const p=state.payload;$('rule-score').textContent=p.ruleResult.ruleScore;$('score-fill').style.width=`${Math.max(0,Math.min(100,p.ruleResult.ruleScore))}%`;
  $('score-state').textContent=p.ruleResult.findings.length?'검토 필요':'탐지 없음';$('account-count').textContent=p.summary.accounts;
  $('transaction-count').textContent=p.summary.transactions;$('pattern-count').textContent=p.summary.patterns;
  $('hop-caption').textContent=`이전 ${$('days').value}일 · 최대 ${p.graph.meta.hops}홉`;
  $('evidence-caption').textContent=p.highlightTxIds.length?`근거 거래 ${p.highlightTxIds.length}건 · 추가 확인 필요`:'현재 조건의 탐지 없음 · 정상 확정 아님';
  $('graph-scope').textContent=`${p.graph.meta.hops}홉 · 부분 조회`;$('table-count').textContent=p.summary.transactions;$('findings-count').textContent=p.summary.patterns;
}

function tagsFor(txId){
  const tags=element('div','tags');if(txId===state.payload.graph.targetTxId)tags.append(element('span','tag target','분석 대상'));
  if(state.payload.highlightTxIds.includes(txId))tags.append(element('span','tag evidence','탐지 근거'));
  if(!tags.children.length)tags.append(element('span','tag','주변 거래'));return tags;
}
function inspectRow(title,value){const row=element('div','inspect-row');row.append(element('span','',title),element('strong','',value));return row;}
function accountButton(key){const button=element('button','account-button',key);button.addEventListener('click',()=>selectItem('account',key));return button;}
function copyButton(value){const button=element('button','copy-button','복사');button.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(value);button.textContent='복사됨';}catch{button.textContent='직접 선택해 복사';}});return button;}

function selectItem(kind,key,reveal=true){
  const p=state.payload;if(!p)return;
  if(reveal){
    const ids=visibleTransactionIds(p,state.mode,state.finding);
    const visible=kind==='transaction'?ids.has(key):p.graph.edges.some(e=>ids.has(e.txId)&&(e.fromAccountKey===key||e.toAccountKey===key));
    if(!visible)setMode('all');
  }
  state.selectedKind=kind;state.selectedKey=key;graph.select(kind,key);
  if(kind==='transaction')renderTransactionInspector(p.graph.edges.find(edge=>edge.txId===key));else renderAccountInspector(key);
  renderTable();updateTimelineSelection();
}

function renderFlow(){
  const p=state.payload,flow=state.flow,picker=$('pattern-picker');picker.replaceChildren();
  for(const [i,finding] of p.ruleResult.findings.entries()){
    const button=element('button','pattern-choice',`${String(i+1).padStart(2,'0')} ${ruleName(finding.ruleId)} · ${finding.score}점`);
    button.setAttribute('aria-pressed',String(finding===state.finding));button.dataset.finding=String(i);
    button.addEventListener('click',()=>{setMode('evidence',finding);selectItem('transaction',state.flow.rows[0].txId,false);});picker.append(button);
  }
  const box=$('flow-summary');box.replaceChildren();
  const title=element('h4','',flow.kind==='TARGET'?'대상 거래 · 탐지 근거 없음':ruleName(flow.kind));
  const shape=flow.kind==='FAN_IN'?`${flow.accounts.length-1}개 송금 계좌 → 1개 집금 계좌`:flow.kind==='FAN_OUT'?`1개 송금 계좌 → ${flow.accounts.length-1}개 수취 계좌`:flow.kind==='CYCLE'?`${flow.accounts.length}개 계좌 · ${flow.rows.length}단계 · 시작 계좌로 복귀`:'현재 룰 조건에 해당하는 패턴이 없습니다.';
  const heading=element('div','flow-heading');heading.append(title,element('span','flow-shape',shape));box.append(heading);
  const config=p.ruleResult.config,minimum=flow.kind==='FAN_IN'?(config.fan_in_min_counterparties??config.min_counterparties):(config.fan_out_min_counterparties??config.min_counterparties);
  const reason=flow.kind==='FAN_IN'?`대상 거래 이전 ${durationText(config.window_minutes*60)} 범위에서 ${minimum}개 이상의 서로 다른 계좌로부터 입금되어 집금 룰에 해당합니다.`:
    flow.kind==='FAN_OUT'?`대상 거래 이전 ${durationText(config.window_minutes*60)} 범위에서 ${minimum}개 이상의 서로 다른 계좌로 송금되어 분산 송금 룰에 해당합니다.`:
    flow.kind==='CYCLE'?`거래 시각이 역전되지 않는 ${flow.rows.length}단계 연결이 시작 계좌로 돌아옵니다. 동일 자금의 순환 여부는 추가 확인이 필요합니다.`:
    '핵심 경로에서는 대상 거래만 표시합니다. 탐지 없음은 정상 거래의 확정 판정이 아닙니다.';
  box.append(element('p','flow-reason',reason));
  const stats=element('div','flow-stats');
  for(const text of [`${flow.rows.length}건 ${flow.finding?'근거':'대상 거래'}`,`${flow.accounts.length}개 계좌`,`경과 ${durationText(flow.elapsedSeconds)}`])stats.append(element('span','',text));
  box.append(stats,element('p','flow-total',`${flow.totalLabel} ${flow.totalsText}`),element('p','flow-period',`${timeText(flow.start)} → ${timeText(flow.end)} · 원본 시각`));
  const evidence=element('button','flow-review-button','관련 근거·검토 초안 확인 ↓');evidence.addEventListener('click',()=>{review.prepare();$('review-card').scrollIntoView({behavior:'smooth',block:'start'});});box.append(evidence);
  $('timeline-count').textContent=flow.rows.length;
  $('timeline-title').textContent=flow.finding?'시간순 근거':'대상 거래 시각';
  $('timeline-note').textContent=flow.kind==='TARGET'?'선택한 대상 거래의 원본 시각과 금액입니다. 현재 조회 범위에서 룰 근거가 발견되지 않았습니다.':flow.kind==='CYCLE'?'번호는 룰이 반환한 순환 경로 순서입니다. 같은 시각의 거래는 실제 선후를 확정할 수 없습니다.':'번호는 이 패턴의 거래 시각순입니다. 같은 시각은 ID순으로 나열하며, 서로 이어진 동일 자금이라는 의미는 아닙니다.';
  if(flow.hasTies)$('timeline-note').textContent+=' 동일 시각 거래가 포함되어 있습니다.';
  const list=$('timeline-list');list.replaceChildren();
  for(const row of flow.rows){
    const li=element('li','timeline-entry'),button=element('button','timeline-item');button.dataset.timelineTx=row.txId;button.setAttribute('aria-label',`${flow.finding?'근거':'대상 거래'} ${row.sequence}, ${row.txId}, ${amountText(row.amountPaid,row.paymentCurrency)}`);
    const top=element('span','timeline-top');top.append(element('span','timeline-number',String(row.sequence).padStart(2,'0')),element('time','',timeText(row.timestamp)));button.append(top);
    const route=element('span','timeline-route',`${flow.roles.get(row.fromAccountKey)||accountShort(row.fromAccountKey)} → ${flow.roles.get(row.toAccountKey)||accountShort(row.toAccountKey)}`);
    button.append(route,element('strong','timeline-money',amountText(row.amountPaid,row.paymentCurrency)),element('span','timeline-id',row.txId));
    if(row.receivingCurrency!==row.paymentCurrency||row.amountReceived!==row.amountPaid)button.append(element('span','timeline-received','수취 '+amountText(row.amountReceived,row.receivingCurrency)));
    if(row.gapSeconds!==null)button.append(element('span','timeline-gap',row.gapSeconds===0?'이전 근거와 동일 시각':'이전 근거 후 '+durationText(row.gapSeconds)));
    button.addEventListener('click',()=>selectItem('transaction',row.txId));li.append(button);list.append(li);
  }
  updateTimelineSelection();
}

function updateTimelineSelection(){
  const rows=state.flow?.rows||[],index=state.selectedKind==='transaction'?rows.findIndex(row=>row.txId===state.selectedKey):-1;
  $('timeline-progress').textContent=index<0?`${state.finding?'근거':'대상 거래'} ${rows.length}건`:`${index+1} / ${rows.length} 선택`;
  $('flow-previous').disabled=index<=0;$('flow-next').disabled=!rows.length||index===rows.length-1;
  document.querySelectorAll('[data-timeline-tx]').forEach(button=>{const active=state.selectedKind==='transaction'&&button.dataset.timelineTx===state.selectedKey;button.classList.toggle('selected',active);if(active)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');});
}

function moveFlow(direction){
  const rows=state.flow?.rows||[];if(!rows.length)return;
  const index=state.selectedKind==='transaction'?rows.findIndex(row=>row.txId===state.selectedKey):-1;
  const next=index<0?0:Math.max(0,Math.min(rows.length-1,index+direction));selectItem('transaction',rows[next].txId);
  document.querySelector(`[data-timeline-tx="${rows[next].txId}"]`)?.scrollIntoView({block:'nearest',behavior:'auto'});
}

function renderTransactionInspector(edge){
  if(!edge)return;const box=$('inspector');box.replaceChildren(element('span','inspector-kicker','TRANSACTION · 송금 거래'));
  const title=element('h4','',edge.txId);title.append(copyButton(edge.txId));box.append(title,tagsFor(edge.txId));
  const money=element('div','inspect-money',decimalText(edge.amountPaid));money.append(element('small','',edge.paymentCurrency));box.append(money);
  const sender=element('div','inspect-row');sender.append(element('span','','보내는 계좌'),accountButton(edge.fromAccountKey));
  const recipient=element('div','inspect-row');recipient.append(element('span','','받는 계좌'),accountButton(edge.toAccountKey));
  box.append(sender,recipient,inspectRow('원본 거래 시각',timeText(edge.timestamp)),inspectRow('거래 방식',edge.paymentFormat),
    inspectRow('수취 금액',amountText(edge.amountReceived,edge.receivingCurrency)),
    element('div','inspect-footnote','금액과 통화는 원본 값입니다. 경로와 룰 점수만으로 동일 자금의 이동이나 범죄 여부를 확정하지 않습니다.'));
}

function renderAccountInspector(key){
  const p=state.payload,{bank,account}=accountParts(key);const related=p.graph.edges.filter(edge=>edge.fromAccountKey===key||edge.toAccountKey===key);
  const incoming=related.filter(edge=>edge.toAccountKey===key),outgoing=related.filter(edge=>edge.fromAccountKey===key);
  const box=$('inspector');box.replaceChildren(element('span','inspector-kicker','ACCOUNT · 연결 계좌'));
  const title=element('h4','',key);title.append(copyButton(key));box.append(title);
  const tags=element('div','tags');tags.append(element('span','tag',`은행 ID ${bank}`));
  if(key===p.target.fromAccountKey||key===p.target.toAccountKey)tags.append(element('span','tag target','대상 거래 연결'));
  box.append(tags,inspectRow('원본 계좌 식별자',account));
  const counts=element('div','inspect-summary');for(const [label,count] of [['입금 거래',incoming.length],['출금 거래',outgoing.length]]){const tile=element('div');tile.append(element('span','',label),element('strong','',`${count}건`));counts.append(tile);}box.append(counts);
  box.append(element('span','inspector-kicker',`조회 범위 내 연결 거래 · ${related.length}건`));
  for(const edge of related.slice(0,8)){const activity=accountActivity(edge,key);const button=element('button','inspect-tx-button',edge.txId);button.append(element('span','',`${activity.direction} · ${amountText(activity.amount,activity.currency)}`));button.addEventListener('click',()=>selectItem('transaction',edge.txId));box.append(button);}
  if(related.length>8)box.append(element('div','inspect-footnote','최근 화면의 일부 거래만 표시했습니다. 전체 거래는 아래 거래 내역에서 확인하세요.'));
  box.append(element('div','inspect-footnote','입출금 건수는 현재 조회 범위의 거래 기준입니다. 계좌 잔액·실소유자·전체 거래 이력은 확인되지 않았습니다.'));
}

function renderTable(){
  const p=state.payload;if(!p)return;const all=$('only-evidence').checked?p.graph.edges.filter(edge=>p.highlightTxIds.includes(edge.txId)):p.graph.edges;
  const pageSize=25,pages=Math.max(1,Math.ceil(all.length/pageSize));state.page=Math.min(state.page,pages-1);
  const rows=all.slice(state.page*pageSize,(state.page+1)*pageSize),body=$('transactions-body');body.replaceChildren();
  if(!rows.length){const tr=element('tr');const td=element('td','empty-note','현재 조건에 해당하는 탐지 근거 거래가 없습니다.');td.colSpan=6;tr.append(td);body.append(tr);}
  for(const edge of rows){
    const tr=element('tr',state.selectedKind==='transaction'&&state.selectedKey===edge.txId?'selected':'');tr.dataset.txId=edge.txId;
    const idCell=element('td');const button=element('button','transaction-button',edge.txId);button.addEventListener('click',()=>selectItem('transaction',edge.txId));idCell.append(button);
    const amount=element('td','amount-cell',amountText(edge.amountPaid,edge.paymentCurrency));
    const badge=element('td');const tags=tagsFor(edge.txId);tags.className='';badge.append(tags);
    tr.append(idCell,element('td','',timeText(edge.timestamp)),element('td','',edge.fromAccountKey),element('td','',edge.toAccountKey),amount,badge);body.append(tr);
  }
  $('table-range').textContent=all.length?`${state.page*pageSize+1}–${Math.min((state.page+1)*pageSize,all.length)} / ${all.length}건`:'0건';
  $('previous-page').disabled=state.page===0;$('next-page').disabled=state.page>=pages-1;
}

function renderFindings(){
  const p=state.payload,list=$('findings-list');list.replaceChildren();
  if(!p.ruleResult.findings.length){list.append(element('div','empty-note','현재 조회 범위와 기본 룰 설정에서는 탐지 조건에 해당하는 패턴이 없습니다. 정상 거래의 확정 판정은 아닙니다.'));return;}
  for(const [index,finding] of p.ruleResult.findings.entries()){
    const card=element('article','finding');const top=element('div','finding-top');top.append(element('span','finding-number',String(index+1).padStart(2,'0')),element('h4','',ruleName(finding.ruleId)),element('span','tag evidence',`룰 ${finding.score}점`));
    const detail=element('p','',finding.ruleId==='CYCLE'?'시간 순서에 따라 계좌를 거쳐 시작 계좌로 돌아오는 구조가 관찰됐습니다.':finding.ruleId==='FAN_IN'?'여러 계좌에서 한 계좌로 자금이 모이는 구조가 관찰됐습니다.':'한 계좌에서 여러 계좌로 자금이 나뉘어 송금되는 구조가 관찰됐습니다.');
    const info=element('div','finding-meta');info.append(element('span','',`근거 거래 ${finding.evidenceTxIds.length}건`));
    if(finding.facts.elapsedSeconds!==undefined)info.append(element('span','',`경과 ${durationText(finding.facts.elapsedSeconds)}`));
    if(finding.facts.counterpartyCount!==undefined)info.append(element('span','',`상대 계좌 ${finding.facts.counterpartyCount}개`));
    if(finding.facts.hops!==undefined)info.append(element('span','',`${finding.facts.hops}단계 경로`));
    const ids=element('div','finding-ids');for(const id of finding.evidenceTxIds){const button=element('button','',id);button.addEventListener('click',()=>selectItem('transaction',id));ids.append(button);}
    const actions=element('div','finding-actions');const view=element('button','finding-view','이 경로 보기 ↗');view.addEventListener('click',()=>{setMode('evidence',finding);selectItem('transaction',state.flow.rows[0].txId,false);$('flow-summary').scrollIntoView({behavior:'smooth',block:'start'});});
    actions.append(element('span','section-kicker',finding.ruleId),view);card.append(top,detail,info,ids,actions);list.append(card);
  }
}

function changeTab(name){const transactions=name==='transactions';$('transactions-tab').setAttribute('aria-selected',String(transactions));$('findings-tab').setAttribute('aria-selected',String(!transactions));$('transactions-tab').classList.toggle('active',transactions);$('findings-tab').classList.toggle('active',!transactions);$('transactions-panel').hidden=!transactions;$('findings-panel').hidden=transactions;}

async function initialize(){
  try{graph=new RelationshipGraph($('graph'),selectItem,counts=>{$('visible-count').textContent=`표시 ${counts.accounts}계좌 · ${counts.transactions}거래`;});}
  catch(error){overlay('그래프를 표시할 수 없습니다',error.message,true);return;}
  review=new ReviewPanel(txId=>{selectItem('transaction',txId);$('flow-summary').scrollIntoView({behavior:'smooth',block:'start'});},()=>loadTransaction(state.selectedId));
  $('help-open').addEventListener('click',()=>$('help-dialog').showModal());$('help-close').addEventListener('click',()=>$('help-dialog').close());
  $('case-search').addEventListener('input',renderCases);$('mobile-cases').addEventListener('change',event=>loadTransaction(event.target.value));
  $('refresh').addEventListener('click',()=>state.selectedId?loadTransaction(state.selectedId):loadCases());
  for(const id of ['hops','days'])$(id).addEventListener('change',()=>loadTransaction(state.selectedId));
  document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>setMode(button.dataset.mode)));
  $('zoom-in').addEventListener('click',()=>graph.zoom(1.3));$('zoom-out').addEventListener('click',()=>graph.zoom(1/1.3));
  $('fit').addEventListener('click',()=>graph.fit());$('layout').addEventListener('click',()=>graph.layout());
  $('labels').addEventListener('click',()=>{$('labels').setAttribute('aria-pressed',String(graph.toggleLabels()));updateNotice();});
  $('flow-previous').addEventListener('click',()=>moveFlow(-1));$('flow-next').addEventListener('click',()=>moveFlow(1));
  $('transactions-tab').addEventListener('click',()=>changeTab('transactions'));$('findings-tab').addEventListener('click',()=>changeTab('findings'));
  $('only-evidence').addEventListener('change',()=>{state.page=0;renderTable();});
  $('previous-page').addEventListener('click',()=>{state.page--;renderTable();});$('next-page').addEventListener('click',()=>{state.page++;renderTable();});
  $('export').addEventListener('click',()=>{if(!state.payload)return;const blob=new Blob([JSON.stringify(state.payload,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`traceon-${state.payload.graph.targetTxId}-${state.payload.graph.meta.hops}hop.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  await loadCases();
}

async function loadCases(){
  try{
    const data=await api('/api/cases');state.cases=data.items;connection(true);renderCases();
    const select=$('mobile-cases');select.replaceChildren();for(const row of data.items){const option=element('option','',`${row.txId} · ${amountText(row.amountPaid,row.paymentCurrency)}`);option.value=row.txId;select.append(option);}
    await loadTransaction(state.selectedId||data.items[0]?.txId);
  }catch(error){connection(false);overlay('DB 연결을 확인해 주세요',error.message,true);$('case-list').replaceChildren(element('div','empty-note',error.message));}
}

if(document.readyState==='complete')initialize();else window.addEventListener('DOMContentLoaded',initialize,{once:true});
