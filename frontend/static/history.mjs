import {savedTime,historySaveState} from './history-format.mjs';
const $=id=>document.getElementById(id);
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
async function request(path,body){const response=await fetch(path,{cache:'no-store',...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});const data=await response.json();if(!response.ok)throw new Error(data.message||'저장 이력을 확인하지 못했습니다.');return data;}

export class HistoryPanel{
  constructor(current,onOpen){this.current=current;this.onOpen=onOpen;this.saving=false;this.loading=false;this.cursor=null;this.generation=0;
    $('history-save').addEventListener('click',()=>this.save());$('history-refresh').addEventListener('click',()=>this.reload());$('history-more').addEventListener('click',()=>this.reload(true));this.sync();}
  sync(){const status=historySaveState(this.current());$('history-save').disabled=this.saving||!status.enabled;$('history-save').textContent=this.saving?'원본·근거 저장 중…':status.label;}
  message(text,error=false){$('history-message').textContent=text;$('history-message').classList.toggle('history-error',error);}
  async save(){
    const current=this.current();if(this.saving||!historySaveState(current).enabled)return;
    this.saving=true;this.sync();this.message(current.reviewId?'거래·초안을 재검사하고 인용 원문을 함께 보관합니다.':'현재 그래프와 룰 결과를 저장합니다. RAG 초안은 포함되지 않습니다.');
    try{const result=await request('/api/history/save',{snapshotId:current.payload.reviewSnapshotId,reviewId:current.reviewId||null});
      await this.reload();this.message(`${result.created?'저장했습니다.':'같은 분석이 이미 저장되어 있습니다.'} ${result.history.hasReview?'초안·인용 원문 포함':'그래프·룰 결과만 저장'} · ${savedTime(result.history.savedAt)}`);
    }catch(error){this.message(error.message,true);}finally{this.saving=false;this.sync();}
  }
  async reload(append=false){
    if(append&&(!this.cursor||this.loading))return;
    const generation=++this.generation;this.loading=true;$('history-refresh').disabled=true;$('history-more').disabled=true;
    try{const result=await request('/api/history'+(append?'?cursor='+encodeURIComponent(this.cursor):''));if(generation!==this.generation)return;
      const list=$('history-list');if(!append)list.replaceChildren();$('history-count').textContent=`${result.total} / ${result.maxEntries}`;
      for(const row of result.items){const button=el('button','history-item');button.type='button';button.append(el('strong','',row.targetTxId),el('span','',`${savedTime(row.savedAt)} · ${row.transactions}거래 · 룰 ${row.ruleScore}점`),el('small','',`${row.hasReview?'초안·원문 포함':'그래프·룰만 저장'} · 추가 조회 ${row.additionalReadPages}회`));
        button.addEventListener('click',async()=>{button.disabled=true;try{await this.onOpen(row.historyId);this.message(`${row.targetTxId}의 저장 당시 결과를 열었습니다.`);}catch(error){this.message(error.message,true);}finally{button.disabled=false;}});list.append(button);}
      if(!result.total)list.append(el('p','history-empty','아직 저장한 분석이 없습니다. 거래를 조회한 뒤 저장해 주세요.'));
      this.cursor=result.nextCursor;$('history-more').hidden=!this.cursor;
    }catch(error){if(generation===this.generation)this.message(error.message,true);}
    finally{if(generation===this.generation){this.loading=false;$('history-refresh').disabled=false;$('history-more').disabled=false;}}
  }
}
