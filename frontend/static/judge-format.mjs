export const JUDGE_LABELS = {
  passed:'근거 확인 통과', held:'근거 부족으로 보류', rejected:'원본과 모순되어 반려', not_judged:'모델 판정 미완료',
};

export function acceptJudge(current, response) {
  if (response.schemaVersion!=='1.0'||response.reviewId!==current.reviewId||response.snapshotId!==current.snapshotId
      ||response.judgeState!=='finished'||!Object.hasOwn(JUDGE_LABELS,response.judge?.decisionStatus)) {
    throw new Error('현재 초안과 Judge 응답이 일치하지 않습니다.');
  }
  return {...current,judgeState:response.judgeState,judge:response.judge};
}
