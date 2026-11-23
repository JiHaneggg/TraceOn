import {amountText,ruleName,timeText} from './format.mjs';
import {CHECK_LABELS,RequestEpoch,factLabel,factValue,focusedFacts,groupedCitations,safeSourceUrl,sourceSegments,verificationState} from './review-format.mjs';
import {isFixture} from './runtime.mjs';
import {assertReviewResult} from './contract.mjs';

const $=id=>document.getElementById(id);
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=String(text);return node;}
function button(text,action,cls='review-link'){const node=el('button',cls,text);node.type='button';node.addEventListener('click',action);return node;}
async function request(path,body,signal){
  const response=await fetch(path,{signal,cache:'no-store',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  const data=await response.json();if(!response.ok){const error=new Error(data.message||'근거 조회를 완료하지 못했습니다.');error.code=data.error;throw error;}return data;
}

export class ReviewPanel{
  constructor(onTransaction,onReload,onChange=()=>{}){
    this.onTransaction=onTransaction;this.onChange=onChange;this.epoch=new RequestEpoch();this.dialogEpoch=new RequestEpoch();this.key=null;this.result=null;
    $('review-refresh-source').addEventListener('click',async()=>{if(this.pending)return;await onReload();$('review-card').scrollIntoView({behavior:'smooth',block:'start'});});
    $('review-prepare').addEventListener('click',()=>this.prepare());
    $('review-verify').addEventListener('click',()=>this.verify());
    $('review-download-md').addEventListener('click',()=>this.download('markdown'));
    $('review-download-json').addEventListener('click',()=>this.download('json'));
    $('review-all-facts').addEventListener('change',()=>this.renderFacts());
    $('review-evidence-close').addEventListener('click',()=>$('review-evidence-dialog').close());
    $('review-evidence-dialog').addEventListener('close',()=>{this.dialogEpoch.reset();this.dialogController?.abort();});
    const tabs=[...document.querySelectorAll('[data-review-tab]')];
    tabs.forEach((tab,i)=>{tab.addEventListener('click',()=>this.tab(tab.dataset.reviewTab));tab.addEventListener('keydown',event=>{let next;if(event.key==='ArrowRight')next=(i+1)%tabs.length;else if(event.key==='ArrowLeft')next=(i+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;event.preventDefault();this.tab(tabs[next].dataset.reviewTab);tabs[next].focus();});});
    this.setContext(null,null);
  }
  setContext(payload,finding){
    const index=finding?payload?.ruleResult.findings.indexOf(finding):null,key=payload?`${payload.reviewSnapshotId}:${index}`:null;
    if(key===this.key&&payload)return;
    this.key=key;this.payload=payload;this.findingIndex=index;this.result=null;this.archive=null;this.archiveInvalid=false;this.pending=false;this.epoch.reset();this.controller?.abort();this.dialogEpoch.reset();this.dialogController?.abort();
    if($('review-evidence-dialog').open)$('review-evidence-dialog').close();
    $('review-content').hidden=true;$('review-error').hidden=true;$('review-refresh-source').hidden=true;$('review-empty').hidden=false;$('review-state').textContent='근거 확인 대기';$('review-state').className='review-state';
    $('review-focus').textContent=payload?`${payload.graph.targetTxId} · ${finding?ruleName(finding.ruleId):'대상 거래 · 현재 룰 탐지 없음'}`:'거래를 선택해 주세요.';
    $('review-empty').textContent=payload?'선택한 경로의 관련 근거와 현재 조회 결과 전체의 검토 초안을 준비합니다.':'거래를 조회한 뒤 관련 근거를 확인할 수 있습니다.';
    $('review-prepare').disabled=!payload?.reviewSnapshotId;$('review-prepare').textContent='근거·초안 준비';
    $('review-all-facts').checked=false;this.downloadState(false);this.onChange();
  }
  setArchived(payload,result,history){
    this.setContext(null,null);this.archive=history;this.payload=payload;this.key='archive:'+history.historyId;this.result=result;this.findingIndex=result?.focus.findingIndex??null;
    if(result){this.elapsed=0;this.render();this.tab('draft');}else{$('review-focus').textContent='저장 당시 결과 · 그래프·룰만 저장됨';$('review-empty').textContent='이 저장본에는 RAG 초안이 없습니다. 현재 자료로 새 분석을 시작하면 준비할 수 있습니다.';}
    $('review-prepare').disabled=true;$('review-prepare').textContent='저장본 읽기 전용';this.onChange();
  }
  downloadState(enabled){for(const id of ['review-download-md','review-download-json'])$(id).disabled=!enabled||this.archiveInvalid;$('review-verify').disabled=!this.result||this.pending;$('review-verify').textContent=this.archive?'저장본 내용 검사':'초안 재검사';}
  async prepare(){
    if(!this.payload?.reviewSnapshotId||this.pending)return;
    const epoch=this.epoch.value;this.pending=true;this.controller?.abort();this.controller=new AbortController();
    $('review-prepare').disabled=true;$('review-prepare').textContent='근거·초안 확인 중…';$('review-state').textContent='거래 사실·인용 대조 중';$('review-error').hidden=true;this.downloadState(false);this.onChange();
    const started=performance.now();
    try{
      const result=await request('/api/review/prepare',{snapshotId:this.payload.reviewSnapshotId,findingIndex:this.findingIndex},this.controller.signal);
      if(!this.epoch.current(epoch))return;
      if(result.snapshotId!==this.payload.reviewSnapshotId||result.focus.findingIndex!==this.findingIndex)throw new Error('선택한 경로와 초안이 일치하지 않습니다.');
      assertReviewResult(this.payload,result);
      this.result=result;this.elapsed=performance.now()-started;this.render();this.tab('draft');
    }catch(error){if(!this.epoch.current(epoch)||error.name==='AbortError')return;this.error(error.message,error.code);$('review-state').textContent='근거 확인 필요';}
    finally{if(this.epoch.current(epoch)){this.pending=false;$('review-prepare').disabled=false;$('review-prepare').textContent=this.result?'근거·초안 다시 준비':'다시 시도';this.downloadState(!!this.result&&verificationState(this.result).downloadAllowed);this.onChange();}}
  }
  error(message,code){$('review-error').textContent=message;$('review-error').hidden=false;$('review-refresh-source').hidden=code!=='review_expired';}
  render(){
    const result=this.result,status=verificationState(result);$('review-content').hidden=false;$('review-empty').hidden=true;
    $('review-state').textContent=status.label;$('review-state').className='review-state '+(status.valid?'checked':'unchecked');
    $('review-boundary').textContent=status.abstained?'원질문 답변 보류 · 보조 자료는 참고용입니다.':result.report.reviewReadiness?.label||'사람 검토 대기 · 제출용 최종 STR이 아닙니다.';
    $('review-generation').textContent=`검증된 사실 기반 템플릿 · LLM 호출 없음 · 준비 ${(this.elapsed/1000).toFixed(2)}초`;
    if(isFixture()){$('review-state').textContent=status.valid?'예시 초안 · 기록된 자동 검사 통과':'예시 초안 · 검사 확인 필요';$('review-generation').textContent='기존 템플릿 엔진의 고정 응답 · 실제 모델 호출 없음';}
    if(this.archive){$('review-state').textContent='저장 당시 자동 검사 통과 · 현재 자료 미조회';$('review-generation').textContent='저장 시각 '+new Date(this.archive.savedAt).toLocaleString('ko-KR')+' · 당시 초안·원문을 표시합니다.';$('review-focus').textContent='저장 당시 선택 경로 · '+this.result.context.targetTxId;}
    $('review-identities').textContent=`보고서 ${result.report.reportId}\n문서 버전 ${result.context.corpusSha256}\n거래 그래프 ${result.context.graphSha256}`;
    this.renderDraft();this.renderSources();this.renderFacts();this.renderChecks();this.downloadState(status.downloadAllowed);
  }
  tab(name){document.querySelectorAll('[data-review-tab]').forEach(tab=>{const active=tab.dataset.reviewTab===name;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;});document.querySelectorAll('[data-review-panel]').forEach(panel=>panel.hidden=panel.dataset.reviewPanel!==name);}
  factButton(id){return button(factLabel(id),()=>this.openFact(id),'review-fact-link');}
  renderDraft(){
    const {report,context}=this.result,box=$('review-draft');box.replaceChildren();
    box.append(el('p','review-important','사람 검토 대기 · 자동 검사는 거래 사실·인용·저장 원문·허용된 본문을 대조합니다. 범죄 여부·법률 적용·근거의 의미적 충분성을 승인하지 않습니다.'));
    if(report.answerDisposition?.status==='abstained')box.append(el('p','review-warning','답변 보류: '+report.answerDisposition.abstentionReason));
    for(const observation of report.observations){const item=el('article','review-observation');item.append(el('p','',observation.text));const links=el('div','review-fact-links');for(const id of observation.factIds)links.append(this.factButton(id));item.append(links);box.append(item);}
    const fitness=context.evidenceFitness;
    if(fitness){
      const missing=el('section','review-missing');missing.append(el('h4','',fitness.label),el('p','',fitness.notice));
      for(const finding of fitness.findings){const details=el('details');details.open=finding.findingId===`finding.${this.findingIndex}`;details.append(el('summary','',ruleName(finding.ruleId)+' · 추가 확인'));
        const list=el('ul');for(const item of finding.missingEvidence)list.append(el('li','',item.label+' — '+item.reason));if(!list.children.length)list.append(el('li','','현재 정의된 조건 외에도 법률·거래 맥락은 사람이 검토해야 합니다.'));details.append(list);missing.append(details);}
      if(!fitness.findings.length)missing.append(el('p','','현재 룰 탐지가 없다는 결과만으로 정상 거래를 확정하지 않습니다.'));
      box.append(missing);
    }
    const citations=el('section','review-draft-citations');citations.append(el('h4','','인용한 원문 확인'));
    for(const group of groupedCitations(this.result)){const details=el('details');details.append(el('summary','',`${group.label} · 인용 ${group.rows.length}개`));for(const row of group.rows)details.append(button(row.publisher+' · '+row.section,()=>this.openCitation(row.chunkId)));citations.append(details);}box.append(citations);
    const raw=el('details','review-raw');raw.append(el('summary','','전체 보고서 본문 펼치기'),el('pre','',report.markdown));box.append(raw);
  }
  renderSources(){
    const box=$('review-sources');box.replaceChildren();
    for(const group of groupedCitations(this.result)){
      const section=el('details','review-source-group');section.open=group.rows.some(row=>row.preferred);section.append(el('summary','',`${group.label} · 인용 ${group.rows.length}개`),el('p','review-source-notice',group.notice));
      for(const row of group.rows){const item=el('article','review-source');item.append(el('span','review-source-publisher',row.publisher+' · '+row.jurisdiction),el('h4','',row.title),el('p','review-source-section',row.section));
        if(row.preferred)item.append(el('span','review-role-tag','선택 경로의 연결 근거'));
        item.append(el('blockquote','',row.quote),button('원문·인용 위치 확인',()=>this.openCitation(row.chunkId)));section.append(item);}
      box.append(section);
    }
    if(!box.children.length)box.append(el('p','review-warning','인용 가능한 문서 근거가 없습니다.'));
  }
  renderFacts(){
    if(!this.result)return;const body=$('review-facts-body');body.replaceChildren();
    for(const fact of focusedFacts(this.result,$('review-all-facts').checked)){
      const row=el('tr'),label=el('td'),value=el('td','review-fact-value'),action=el('td');label.append(el('strong','',factLabel(fact.factId)),el('small','',fact.factId));value.textContent=factValue(this.result.context,fact);action.append(button(`거래 근거 ${fact.evidenceTxIds.length}건`,()=>this.openFact(fact.factId)));row.append(label,value,action);body.append(row);
    }
  }
  renderChecks(){
    const box=$('review-checks');box.replaceChildren();
    for(const [key,check] of Object.entries(this.result.verification.checks)){
      const row=el('div','review-check '+(check.valid===true?'pass':'fail'));row.append(el('span','',CHECK_LABELS[key]||key),el('strong','',check.valid===true?'일치':'확인 필요'));box.append(row);
    }
    if(this.result.verification.errors.length)box.append(el('p','review-warning','미통과 검사: '+this.result.verification.errors.join(', ')));
    box.append(el('p','review-source-notice','검사 통과는 근거가 충분하거나 제출할 수 있다는 뜻이 아닙니다. 자유 문장 의미·법률 적용·원장 진위는 검사 범위 밖입니다.'));
  }
  async verify(){
    if(!this.result||this.pending)return null;
    if(this.archive)return this.verifyArchive();
    const epoch=this.epoch.value,reviewId=this.result.reviewId;this.pending=true;this.downloadState(false);$('review-prepare').disabled=true;$('review-verify').textContent='재검사 중…';this.onChange();
    try{
      const result=await request('/api/review/verify',{reviewId},this.controller?.signal);
      if(!this.epoch.current(epoch))return null;
      if(result.reviewId!==reviewId)throw new Error('초안과 재검사 결과가 일치하지 않습니다.');
      this.result={...this.result,report:result.report,verification:result.verification};this.render();return this.result;
    }catch(error){if(!this.epoch.current(epoch)||error.name==='AbortError')return null;this.error(error.message,error.code);if(this.result)this.result={...this.result,verification:{...this.result.verification,valid:false}};$('review-state').textContent='재검사 확인 필요';$('review-state').className='review-state unchecked';return null;}
    finally{if(this.epoch.current(epoch)){this.pending=false;$('review-prepare').disabled=false;$('review-verify').textContent='초안 재검사';this.downloadState(!!this.result&&verificationState(this.result).downloadAllowed);this.onChange();}}
  }
  async verifyArchive(){
    const epoch=this.epoch.value,h=this.archive;this.pending=true;this.downloadState(false);
    try{const checked=await request('/api/history/verify?'+new URLSearchParams({historyId:h.historyId}));if(!this.epoch.current(epoch))return null;
      if(checked.history.historyId!==h.historyId||checked.history.snapshotSha256!==h.snapshotSha256||checked.history.integrity.valid!==true||checked.history.integrity.currentSourcesChecked!==false)throw new Error('열어둔 저장본과 검사 결과가 다릅니다.');
      this.archiveInvalid=false;this.render();$('review-error').hidden=true;return this.result;
    }catch(error){if(!this.epoch.current(epoch))return null;this.archiveInvalid=true;this.error(error.message);$('review-state').textContent='저장본 내용 확인 필요';return null;}
    finally{if(this.epoch.current(epoch)){this.pending=false;this.downloadState(!this.archiveInvalid&&verificationState(this.result).downloadAllowed);}}
  }
  async download(format){
    const result=await this.verify();if(!result||!verificationState(result).downloadAllowed)return;
    const link=el('a');link.href=this.archive?'/api/history/download?'+new URLSearchParams({historyId:this.archive.historyId,format}):'/api/review/download?'+new URLSearchParams({reviewId:result.reviewId,format});link.download=`traceon-${result.context.targetTxId}-${this.archive?'archive':'review'}.${format==='markdown'?'md':'json'}`;link.hidden=true;document.body.append(link);link.click();link.remove();
  }
  async evidence(kind,id){
    if(!this.result)return;const epoch=this.epoch.value,dialogToken=this.dialogEpoch.reset(),reviewId=this.result.reviewId;
    this.dialogController?.abort();this.dialogController=new AbortController();const body=$('review-evidence-body');body.replaceChildren(el('p','review-empty','원본 근거를 확인하고 있습니다…'));$('review-evidence-title').textContent=kind==='citation'?'인용 원문 확인':'거래 사실의 근거';
    if(!$('review-evidence-dialog').open)$('review-evidence-dialog').showModal();
    try{
      const result=this.archive?await request('/api/history/'+kind+'?'+new URLSearchParams({historyId:this.archive.historyId,[kind==='citation'?'chunkId':'factId']:id}),undefined,this.dialogController.signal):kind==='citation'?await request('/api/review/citation?'+new URLSearchParams({reviewId,chunkId:id}),undefined,this.dialogController.signal):await request('/api/review/fact',{reviewId,factId:id},this.dialogController.signal);
      if(!this.epoch.current(epoch)||!this.dialogEpoch.current(dialogToken))return;body.replaceChildren();
      if(this.archive&&(result.history.historyId!==this.archive.historyId||result.history.snapshotSha256!==this.archive.snapshotSha256))throw new Error('저장 당시 근거와 조회 결과가 일치하지 않습니다.');
      if(kind==='citation')this.renderCitation(result,body);else this.renderFact(result,body);
    }catch(error){if(!this.epoch.current(epoch)||!this.dialogEpoch.current(dialogToken)||error.name==='AbortError')return;body.replaceChildren(el('p','review-warning',error.message));}
  }
  openCitation(id){return this.evidence('citation',id);}
  openFact(id){return this.evidence('fact',id);}
  renderCitation(result,box){
    const row=result.citation,verified=result.sourceVerification.status==='snapshot_checked';box.append(el('span','review-state '+(verified?'checked':'unchecked'),verified?'저장 원문 대조 완료':'색인 본문만 확인됨'),el('h4','',row.title),el('p','',row.publisher+' · '+(row.sourceDisplay?.label||row.sourceKind)),el('p','',row.section),el('p','review-source-notice',row.sourceDisplay?.notice||''));
    box.append(el('p','review-source-meta',`출처 버전 ${row.version||'미표시'} · 시행일 ${row.effectiveDate||'미확인'} · 수집 ${row.capturedAt||'미표시'}`));
    const passage=el('div','review-source-passage');
    if(result.section.text!==null){const parts=sourceSegments(result.section.text,result.section.highlight);passage.append(document.createTextNode(parts.before),el('mark','',parts.highlight),document.createTextNode(parts.after));}else passage.textContent=row.text;
    box.append(passage,el('p','review-source-notice','저장된 원문 스냅샷을 표시합니다. 현재 공식 사이트의 최신 내용과 다를 수 있습니다.'));
    const source=safeSourceUrl(row.url);if(source){const link=el('a','review-link','공식 사이트 원문 열기 ↗');link.href=source;link.target='_blank';link.rel='noopener noreferrer';box.append(link);}
    const identity=el('details','review-raw');identity.append(el('summary','','인용 ID·문서 버전'),el('pre','',`${row.chunkId}\n문서 코퍼스 ${result.corpusSha256}\n원본 해시 ${row.sourceSha256}`));box.append(identity);
  }
  renderFact(result,box){
    box.append(el('h4','',factLabel(result.fact.factId)),el('p','review-resolved-value',factValue(this.result.context,result.fact)),el('p','review-source-notice',`${this.archive?'저장 당시 검증한 사실과 거래를 표시합니다. 현재 자료는 재조회하지 않았습니다.':isFixture()?'기존 템플릿 엔진이 검증한 예시 사실과 거래를 표시합니다.':'동일한 조회 그래프에서 다시 계산했습니다.'} 거래 근거 ${result.transactions.length}건이며 은행 전체 원장의 진위를 인증한 것은 아닙니다.`));
    for(const edge of result.transactions){const item=el('article','review-source');item.append(el('strong','',edge.txId),el('p','',timeText(edge.timestamp)),el('p','review-route',`${edge.fromAccountKey} → ${edge.toAccountKey}`),el('p','review-resolved-amount',amountText(edge.amountPaid,edge.paymentCurrency)),button('그래프에서 이 거래 보기',()=>{$('review-evidence-dialog').close();this.onTransaction(edge.txId);}));box.append(item);}
    if(!result.transactions.length)box.append(el('p','review-source-notice','이 사실에는 별도의 탐지 근거 거래가 없습니다. 탐지 없음이 정상 거래의 확정 판정은 아닙니다.'));
  }
}
