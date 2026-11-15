# RAG 검색 개선 — 중복 제거와 새 질문 평가

## 변경 내용

검색 목록의 한 자리는 같은 문서·출처 버전·관할·자료 종류·절에서 가장 점수가 높은 원본 청크 하나로 구성한다. 같은 문서라도 정의·보고 절차 등 서로 다른 절은 유지한다. 다른 출처나 버전의 본문을 하나로 합치지 않는다.

대표 본문에 없는 세부 사실을 보존하기 위해 같은 절의 적격 보조 청크를 최대 2개 `supportingChunks`에 제공한다. 본문을 이어 붙이거나 새 인용 ID를 만들지 않고 각 청크의 원래 ID·해시·본문 위치를 유지한다. Context는 대표·보조 청크를 각각 인용할 수 있게 전달한다.

검색어에서는 일부 질문 어미·불용어를 정리하고 영어 단복수를 정규화한다. 기존 자료에 있는 집금·분산·단일 징후·맥락 등의 한국어 표현에 대응하는 영어 검색어를 추가한다. 한국어/영어가 섞인 질의가 영어 자료와의 문자 겹침이 적다는 이유만으로 탈락하지 않도록, 해외 자료에 한해 영어 내용어가 2개 이상 일치할 때 그 언어의 일치 비율도 확인한다. 번역된 법령이나 새 사실을 생성하는 기능은 아니다.

은행 내부 규정·점수 기준뿐 아니라, 특정 고객/거래 ID의 직업·주소·실소유자와 상품 금리/수수료 등 확보하지 않은 자료에 대한 일부 요청은 `insufficient_scope`로 보류한다. 이 규칙은 제한된 표현에 대한 범위 검사이며 일반적인 의미 이해를 보장하지 않는다.

## API 응답의 달라진 부분

`POST /v1/rag/search`의 입력은 그대로다. `topK`는 이제 서로 다른 출처·버전·절의 대표 결과 수다. 점수 기준을 통과한 절이 적으면 결과 수도 적으며 무관한 문서로 채우지 않는다.

추가 응답 필드:

- `selection.policy=sections`: 현재 선택 방식.
- `selection.eligibleChunks/eligibleSections`: 점수 조건을 통과한 청크/절 수.
- `selection.suppressedSiblingChunks`: 대표 결과 자리에서 제외한 같은 절의 청크 수.
- `selection.supportingChunks`: 실제로 전달한 보조 본문 수.
- `hits[].selectionGroupId`: 검색 결과 묶음 식별자. 법 조항 번호나 인용 ID가 아니다.
- `hits[].supportingChunks`: 최대 2개의 개별 원본 청크.
- `hits[].alternateChunkIds/omittedSiblingCount`: 보조 본문 한도 밖의 후보 추적 정보.
- `citations[].citationRole`: Context의 대표/보조 인용 구분. 보조 인용도 같은 방식으로 검증된다.

실제 API 검사에서 과거 같은 FINTRAC 절 3개가 차지하던 질문은 FINTRAC·FinCEN의 서로 다른 절 3개를 반환했다. API 상태의 `retrievalPolicy=sections`로 새 프로세스 적용 여부를 확인한다. `matched`는 검색 성공이며 답변 가능 여부는 여전히 `not_assessed`다.

## 새 평가 질문과 결과

새 질문 48개를 작성했다. 근거 있는 질문 32개와 자료 없는 질문 16개로 구성하며, 한국어 바꿔쓰기·STR/CTR 혼동·고객확인·한 절 안의 복수 사실·해외 자료·여러 출처·내규·개인정보·금리 등을 포함한다. 정답은 출처·절과 그 절에 실제 존재하는 인용 문자열로 기록하고 실행 전에 검증했다.

| 지표 | 보존한 V2 | 최종 검색 |
|---|---:|---:|
| 필요한 절 Recall@3 | 78.1% | 95.3% |
| 근거 하나 이상 발견 | 26/32 | 31/32 |
| 대표 본문만의 근거 포함률 | 78.1% | 89.1% |
| 보조 본문 포함 근거 확보율 | 78.1% | 95.3% |
| 필요한 모든 문맥 근거를 확보한 질문 | 24/32 | 30/32 |
| 자료 없는 질문 보류 | 13/16 | 16/16 |
| 검색 결과 중 같은 절의 중복 자리 비중 | 11.8% | 0% |

절 Recall@3는 두 방식 모두 최대 3개의 대표 검색 결과에서 계산했다. 보조 본문 포함 지표는 더 큰 문맥 예산을 사용한다. 관련 질문의 평균 본문 수는 2.19→3.72개, 평균 본문 문자는 1,179→2,098자로 증가했다. 문맥 확장 효과를 동일한 본문 예산의 향상으로 표현하지 않는다.

기존 26개 개발 질문도 같은 V2와 비교했다. 절 Recall@3는 97.2%→100%, MRR@3는 0.963→1.0이고 자료 없는 질문 8개 보류를 유지했다. 이 질문들은 이미 노출된 회귀 검사다.

새 질문의 첫 실행 때 개발 24개·고정 확인 24개로 나눴다. 그 최초 결과와 중간 개선 결과를 보존했고, 문제를 반복 개발한 이후에는 **48개 전체를 개발·회귀 세트로 취급**한다. 독립 최종평가나 실제 금융 업무 정확도로 주장하지 않는다. 모든 질문·결과와 남은 두 누락은 [평가 상세](rag-search-evaluation.md)에 있다.

## 실행·재현

```powershell
python -m unittest discover -s rag-service/tests -q

# 고정 색인·정답·코드 해시를 확인하는 새 실행. 이미 존재하는 파일은 보존한다.
python rag-service/evaluate_v3_1.py --baseline rag-service/baselines/retrieval_v2.py --manifest artifacts/my-search-run.json --freeze
python rag-service/evaluate_v3_1.py --baseline rag-service/baselines/retrieval_v2.py --manifest artifacts/my-search-run.json --output artifacts/my-search-comparison.json

# 기존 최종 결과를 읽어 질문별 검토 문서를 만든다.
python scripts/report-rag-v3.py

# 실제 Neo4j → 룰 → 검색·Context → 보고서. 새 출력 경로를 지정한다.
python scripts/verify-rag-v2.py --output artifacts/my-search-runtime-verification.json
```

고정 질문은 `rag-service/evaluation_queries_v3.json`, 최종 결과는 `artifacts/rag-v3-final-comparison.json`, 고정 해시는 `artifacts/rag-v3-final-frozen.json`이다. V2 원본 코드는 `rag-service/baselines/retrieval_v2.py`로 보존했고 운영 서비스에서는 사용하지 않는다. 같은 평가를 재현하려면 고정된 `data/rag/index.json`과 원문 스냅샷도 필요하다. 새로 수집한 색인의 수집 시각/본문이 달라지면 새 평가 버전으로 분리해야 한다.

첫 중복 제거 결과·중간 결과·최종 결과는 서로 다른 파일로 보존했다. 질문·코퍼스·코드가 고정 기록과 달라지면 기존 기록에 이어 실행하지 않는다.

## 통합 검사와 다음 한계

자동 테스트 116개(데이터 16·룰 24·RAG 76)를 통과했다. 실제 Neo4j 기존 12사례에서 새 검색·보조 인용을 포함한 보고서 생성과 수치 변조 거부를 확인했다. 결과는 `artifacts/rag-v3-runtime-verification.json`, 보고서는 `artifacts/rag-v3-runtime-reports/`에 있다. LLM 실제 호출은 0회다.

남은 두 질문은 기대한 STR 정의 절 대신 보고 대상·절차 절 등이 검색됐다. 정답을 사후에 바꾸지 않고 누락으로 기록했다. 정답 후보의 완전성·출처에 대한 의미적 해석은 사람이 검토해야 한다. 다음 개선에는 별도 전문가 정답과 더 넓은 질문을 사용하고, 문맥 크기·오탐·의미적 인용 오류를 함께 측정해야 한다.

법령·은행 내규의 수집, 실제 LLM 호출, Spring/ML/화면 통합은 이번 작업 범위에서 변경하지 않았다. 기존 룰 성능 평가와 DB 내용도 유지했다.
