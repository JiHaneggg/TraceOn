import {graphElements,visibleTransactionIds} from './format.mjs';
import {buildFlow,flowPositions} from './flow.mjs';

export class RelationshipGraph {
  constructor(container,onSelect,onVisible) {
    if(!globalThis.cytoscape)throw new Error('그래프 표시 파일을 불러오지 못했습니다.');
    this.cy=globalThis.cytoscape({container,elements:[],minZoom:.12,maxZoom:3,
      boxSelectionEnabled:false,selectionType:'single',style:[
        {selector:'node',style:{width:120,height:72,shape:'round-rectangle','background-color':'#eef2e8','border-width':1.5,'border-color':'#9bab9c',
          label:'data(label)','text-wrap':'wrap','text-valign':'center',color:'#354c43',
          'font-size':12,'font-family':'Segoe UI, Malgun Gothic, sans-serif'}},
        {selector:'.center-account',style:{'background-color':'#e0ece3','border-color':'#38674f','border-width':3,'font-weight':'bold'}},
        {selector:'.compact-account',style:{width:100,height:54,'font-size':11}},
        {selector:'.source-account',style:{'background-color':'#f6e2d0','border-color':'#bf7954','border-width':3}},
        {selector:'.destination-account',style:{'background-color':'#dfece4','border-color':'#5d8b72','border-width':3}},
        {selector:'edge',style:{width:1.5,'line-color':'#b5c2bc','target-arrow-color':'#a7b7af','target-arrow-shape':'triangle',
          'arrow-scale':.9,'curve-style':'bezier','control-point-step-size':38,opacity:.8,
          label:'data(displayLabel)','font-size':11,'font-family':'Consolas, monospace',color:'#829189',
          'text-rotation':'autorotate','text-background-color':'#fbfcf8','text-background-opacity':.95,'text-background-padding':3,
          'text-margin-y':-9}},
        {selector:'.new-edge',style:{width:2.8,'line-color':'#527f9d','target-arrow-color':'#527f9d','line-style':'dashed',opacity:1}},
        {selector:'.evidence-edge',style:{width:3,'line-color':'#c57655','target-arrow-color':'#c57655',color:'#95543c',opacity:1,'font-size':11,'z-index':5}},
        {selector:'.target-edge',style:{width:4,'line-color':'#234d43','target-arrow-color':'#234d43',color:'#234d43',opacity:1,'font-weight':'bold','z-index':7}},
        {selector:':selected',style:{'border-color':'#427c78','border-width':4,'overlay-color':'#427c78','overlay-opacity':.09,'overlay-padding':6}},
        {selector:'edge:selected',style:{width:5,'line-color':'#21766d','target-arrow-color':'#21766d',color:'#185a53','z-index':9}},
        {selector:'.route-account',style:{'border-color':'#518b80','border-width':3}},
        {selector:'.dimmed',style:{opacity:.18}},
        {selector:'.hidden',style:{display:'none'}}]});
    this.cy.on('tap','node',event=>onSelect('account',event.target.data('accountKey')));
    this.cy.on('tap','edge',event=>onSelect('transaction',event.target.data('txId')));
    this.onVisible=onVisible;this.labels=true;
    this.observer=new ResizeObserver(()=>{this.cy.resize();const narrow=this.cy.width()<560;if(this.narrow!==narrow){this.narrow=narrow;this.layout();}else this.fit();});this.observer.observe(container);
  }
  clear(){this.payload=null;this.cy.elements().remove();}
  setData(payload,mode,finding=null){this.payload=payload;this.labels=payload.highlightTxIds.length<=4;this.cy.elements().remove();this.cy.add(graphElements(payload));const added=new Set(payload.accountExpansion?.lastPage?.addedTxIds||[]);this.cy.edges().forEach(edge=>edge.toggleClass('new-edge',added.has(edge.data('txId'))));this.setMode(mode,finding);}
  setMode(mode,finding=null){
    if(!this.payload)return;
    this.mode=mode;this.finding=finding;this.flow=buildFlow(this.payload,finding);
    const ids=visibleTransactionIds(this.payload,mode,finding);const accounts=new Set();
    for(const edge of this.payload.graph.edges)if(ids.has(edge.txId)){accounts.add(edge.fromAccountKey);accounts.add(edge.toAccountKey);}
    this.cy.batch(()=>{
      this.cy.nodes().forEach(node=>node.toggleClass('hidden',!accounts.has(node.data('accountKey'))));
      this.cy.edges().forEach(edge=>edge.toggleClass('hidden',!ids.has(edge.data('txId'))));
      this.cy.elements().removeClass('dimmed');
      this.cy.nodes().forEach(node=>{
        const key=node.data('accountKey'),role=this.flow.roles.get(key)||'연결 계좌',split=key.indexOf(':'),bank=key.slice(0,split),account=key.slice(split+1);
        node.data('label',`${role}\n은행 ${bank}\n${account.length>7?'…'+account.slice(-6):account}`);
        node.toggleClass('center-account',key===this.flow.center);
      });
      this.cy.edges().forEach(edge=>{const order=this.flow.sequence.get(edge.data('txId'));edge.data('sequence',order||'');});
      if(finding&&mode==='evidence'){const focus=new Set(finding.evidenceTxIds);this.cy.edges().forEach(edge=>edge.toggleClass('dimmed',!focus.has(edge.data('txId'))&&edge.data('txId')!==this.payload.graph.targetTxId));}
    });
    this.updateLabels();this.onVisible({accounts:accounts.size,transactions:ids.size});this.layout();
  }
  layout(){
    const visible=this.cy.elements().not('.hidden');
    if(!visible.length)return;
    const narrow=this.cy.width()<560;this.narrow=narrow;
    const fan=this.mode==='evidence'&&['FAN_IN','FAN_OUT'].includes(this.flow.kind);
    this.cy.nodes().toggleClass('compact-account',narrow);
    const height=fan&&narrow?Math.max(460,(this.flow.accounts.length-2)*60+120):500;
    this.cy.container().parentElement.style.height=height+'px';this.cy.resize();
    if(this.mode==='evidence'){
      const positions=flowPositions(this.flow,visible.nodes().map(node=>node.data('accountKey')),{narrow});
      visible.nodes().forEach(node=>node.position(positions.get(node.data('accountKey'))));
    }else{
    const count=visible.nodes().length,radius=40*Math.sqrt(count);visible.nodes().forEach((node,i)=>node.position({x:radius*Math.cos(i*2*Math.PI/count),y:radius*Math.sin(i*2*Math.PI/count)}));
    visible.layout({name:count<=8?'circle':'cose',animate:false,fit:true,padding:35,
      randomize:false,nodeRepulsion:()=>12000,idealEdgeLength:()=>130,edgeElasticity:()=>80,
      nodeDimensionsIncludeLabels:true,componentSpacing:100,numIter:700,coolingFactor:.95,
      avoidOverlap:true,spacingFactor:1.3,startAngle:-Math.PI/2}).run();}
    this.fit();
  }
  fit(){const visible=this.cy.elements().not('.hidden');if(visible.length)this.cy.fit(visible,32);if(this.cy.zoom()>1.15)this.cy.zoom({level:1.15,renderedPosition:{x:this.cy.width()/2,y:this.cy.height()/2}});}
  zoom(factor){this.cy.zoom({level:Math.max(this.cy.minZoom(),Math.min(this.cy.maxZoom(),this.cy.zoom()*factor)),renderedPosition:{x:this.cy.width()/2,y:this.cy.height()/2}});}
  select(kind,key){
    this.cy.elements().unselect();this.cy.nodes().removeClass('route-account');
    const element=this.cy.getElementById((kind==='account'?'account:':'edge:')+key);
    if(element.length&&!element.hasClass('hidden')){element.select();if(kind==='transaction')element.connectedNodes().addClass('route-account');}
    this.updateLabels();
  }
  updateLabels(){this.cy.edges().forEach(edge=>{const order=edge.data('sequence'),prefix=order?String(order).padStart(2,'0')+' · ':'';edge.data('displayLabel',this.labels||edge.selected()?prefix+edge.data('amount'):(order?'#'+String(order).padStart(2,'0'):''));});}
  toggleLabels(){this.labels=!this.labels;this.updateLabels();return this.labels;}
}
