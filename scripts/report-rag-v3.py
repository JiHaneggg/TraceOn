"""Render the paired search results and all question judgments for team review."""
import argparse
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def render(result):
    old,new=result['baseline'],result['candidate']
    lines=['# RAG 검색 개선 — 48개 질문 비교 기록','',
           '같은 5문서·26청크와 같은 질문을 사용했다. 보존한 V2 원본 코드와 최종 검색 구현을 비교했다. '
           '질문은 수작업 개발·회귀 세트이며 독립 최종평가가 아니다.','',
           f"질문 {new['queries']}개: 관련 근거가 있는 질문 {new['answerable']}개, 자료 범위 밖 질문 {new['unsupported']}개.",'',
           '| 지표 | 기존 V2 | 개선 후 |','|---|---:|---:|']
    metrics=[('필요한 절 Recall@3','macroSectionRecallAt3'),('근거 하나 이상 발견 HitRate@3','hitRateAt3'),
             ('대표 본문에 필요한 인용 포함','macroPrimaryEvidenceRecallAt3'),
             ('보조 본문 포함 필요한 인용 확보','macroContextEvidenceRecall'),
             ('필요한 모든 근거 확보 질문 비율','allRequiredContextEvidenceRate'),
             ('자료 없는 질문 보류','unsupportedAbstentionRate'),('상위 결과 중복 자리 비율','duplicateSlotRate')]
    for label,key in metrics:lines.append(f"| {label} | {old[key]*100:.1f}% | {new[key]*100:.1f}% |")
    lines += ['',f"상위 결과는 양쪽 모두 최대 3개다. 개선 후에는 같은 절의 보조 청크를 최대 2개씩 따로 제공한다. "
                  f"관련 질문의 평균 본문 수는 {old['meanExpandedChunks']:.2f} → {new['meanExpandedChunks']:.2f}, "
                  f"본문 문자 수는 {old['meanContextCharacters']:.0f} → {new['meanContextCharacters']:.0f}다. "
                  '보조 본문 포함 지표는 문맥 예산이 더 큰 조건임을 구분해야 한다.','',
              '## 남은 누락과 해석','']
    for row in result['failures']:
        lines += [f"- {row['id']}: {row['query']}"]
        for missing in row['missingContextEvidence']:
            lines.append(f"  - 미확보 정답: {missing['sectionId']} / {missing['quote']}")
        if row['unverifiedRelatedPassages']:lines.append('  - 자료 없는 질문에 관련 일반 본문이 반환됨. 답변 가능으로 처리하면 안 됨.')
    lines += ['', '남은 두 질문은 기대한 STR 정의 절 대신 보고 대상·절차 절 등이 검색됐다. '
              '정답 절·인용 기준을 그대로 적용해 누락으로 남겼다. 정답 후보의 완전성과 해석은 별도 검토가 필요하다.',
              '', '## 전체 질문과 결과','',
              '| ID | 종류 | 질문 | 기존 → 개선 상태 | 개선 절 Recall@3 | 개선 문맥 근거 확보 |',
              '|---|---|---|---|---:|---:|']
    for before,after in zip(result['baselineCases'],result['candidateCases']):
        if after['requiredEvidence']:
            recall=f"{after['sectionRecallAt3']*100:.0f}%"
            context=f"{after['contextEvidenceRecall']*100:.0f}%"
        else:
            recall='—';context='보류' if after['abstained'] else '검토 필요'
        query=after['query'].replace('|','/')
        lines.append(f"| {after['id']} | {after['category']} | {query} | {before['status']} → {after['status']} | {recall} | {context} |")
    lines += ['', '## 재현·한계','',
              f"- 코퍼스 SHA256: `{result['fingerprint']['corpusSha256']}`",
              f"- 질문 SHA256: `{result['fingerprint']['questionsSha256']}`",
              f"- 기존 V2 코드 SHA256: `{result['fingerprint']['baselineV2Sha256']}`",
              '- 첫 중복 제거 결과와 중간 개선 결과는 artifacts/rag-v3-development.json, rag-v3-verification.json, rag-v3-1-comparison.json에 보존했다.',
              '- 원래 development/verification 구분은 최초 실행 순서를 나타낸다. 반복 개발 후에는 전체를 개발·회귀 질문으로 취급한다.',
              '- 인용 문자열의 포함 여부를 검사하며 문장의 의미적 뒷받침이나 법률 적용까지 평가한 것은 아니다.',
              '- 같은 절의 대표 청크 하나만 보는 소비자는 세부 사실을 놓칠 수 있다. supportingChunks와 함께 사용한다.',
              '- 보류율 100%는 이번 16개 질문에 한정된다. 모든 고객정보·상품·내규 질문에 대한 의미 판별 정확도를 뜻하지 않는다.','']
    return '\n'.join(lines)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,default=ROOT/'artifacts/rag-v3-final-comparison.json')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/rag-search-evaluation.md')
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(render(json.loads(args.input.read_text(encoding='utf-8'))),encoding='utf-8')
    print(str(args.output))
