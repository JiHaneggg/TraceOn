export function savedTime(value){return new Date(value).toLocaleString('ko-KR',{hour12:false,timeZone:'Asia/Seoul'});}
export function assertArchive(result){
  const h=result?.history,p=result?.payload;
  if(!h?.historyId||h.integrity?.valid!==true||h.integrity.currentSourcesChecked!==false||h.readyForSubmission!==false||!p?.graph||p.graph.targetTxId!==h.targetTxId||p.graph.edges.length!==h.transactions||p.graph.nodes.length!==h.accounts||p.reviewSnapshotId)throw new Error('저장 당시 분석과 이력 정보가 일치하지 않습니다.');
  if(Boolean(result.review)!==h.hasReview)throw new Error('저장된 초안 유무가 이력 정보와 다릅니다.');
  if(result.review&&(result.review.context.graphSha256!==h.graphSha256||result.review.report.reportId!==h.reportId||result.review.context.corpusSha256!==h.corpusSha256))throw new Error('저장된 초안의 거래·문서 버전이 일치하지 않습니다.');
  return result;
}
export function archiveNotice(h){return `저장 당시 결과 · ${savedTime(h.savedAt)} · 현재 DB·규정은 재조회하지 않았습니다. 사람 검토가 필요합니다.`;}
export function historySaveState(current){
  return {enabled:!!current?.payload?.reviewSnapshotId&&!current.payload.history&&!current.busy,
    label:current?.payload?.history?'저장본 읽기 전용':current?.reviewId?'그래프·초안 함께 저장':'그래프·룰 저장'};
}
