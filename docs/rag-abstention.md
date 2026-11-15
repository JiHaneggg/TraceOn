# 요청 범위 확인·검색 보류 보완 — 2026-11-15

V4에서 미지원 질문 6건에 관련 문서를 반환하던 문제를 보완했다. 새 `queryScopePolicyVersion=1.0`은 검색 전에 요청한 기관/관할/내규/사례 정보/자동 결정의 범위를 확인한다. 조건이 부족하면 검색 결과를 비우고 이유를 반환한다. 일반 제도·절차 질문과 특정 정보의 실제 값을 요구하는 질문을 구분한다.

## 회귀 검사 결과

동일한 V4 질문·정답을 사용했다. 질문/정답·본문 50개·인용 ID·근거 적합성 정책은 유지했으며 원래 첫 평가 결과도 보존했다. 변경한 문항의 성능이므로 새로운 독립 평가가 아니다.

| 항목 | V4 첫 결과 | 보완 후 회귀 |
|---|---:|---:|
| 지원 질문의 필요한 근거 확보 | 32/40 | **32/40** |
| 미지원 질문의 검색 보류 | 14/20 | **20/20** |
| 보류 부족 6건 | 0/6 보류 | **6/6 보류** |
| 대표 검색 결과의 같은 절 중복 | 0% | **0%** |

개선된 문항은 N02·N09·N10·N11·N16·N20이다. 지원 질문별 통과 여부는 모두 유지됐다. 필요한 근거를 못 찾은 P02·P04·P05·P06·P08·P34·P36·P39, 총 8건은 후속 검색 개선 대상이다. 검색어 확장·순위·점수/커버리지 기준은 이번에 변경하지 않았다.

## 보류하는 요청과 허용하는 질문

| 요청 | 처리 |
|---|---|
| KB 내부 지침의 처리 기한·승인 담당자 이름 | 해당 은행 내부 자료가 없으면 보류 |
| FATF 특정 권고 원문 | FATF 발행 원문/요청 구간이 없으면 보류. 다른 문서 본문의 FATF 언급으로 대체하지 않음 |
| EU 규정 적용 조항 | EU 법령 원문이 요청 범위에 없으면 보류. 국내 안내로 대체하지 않음 |
| 특정 계좌/사례/고객의 실제 소유자·성명·직업 | 고객 원장 연결이 없으므로 보류 |
| 룰/ML 점수에 따른 자동 보고 기준 | 승인된 점수→보고 정책이 없으면 보류 |
| 사람 검토 없는 범죄/보고 의무 확정 문장 | 자동 확정 권한·근거를 제공하지 않으므로 보류 |
| 공개 STR 내부 보고 절차 | 일반 절차로 검색 허용 |
| 금융회사가 내부 지침을 마련해야 한다는 법률 근거 | 법률상의 일반 의무로 검색 허용 |
| 특정 계좌의 실제 소유자 확인에 필요한 서류/절차 | 실제 고객 값을 답하는 요청과 구분해 일반 절차 검색 허용 |
| 개인/법인 고객의 일반 신원확인 항목 | 검색 허용 |
| 자동 범죄 확정의 한계·주의 이유 | 확정 문장 생성 요청과 구분해 검색 허용 |

`AC-7788의`처럼 한국어 조사가 붙은 식별자와 ‘알아봐 주세요’ 같은 사실 요청 표현을 인식한다. 실제 값과 일반 절차를 함께 묻는 혼합 요청은 미지원 항목이 있으면 전체 답변을 보류한다. 기관/관할 문서 여부는 등록 메타데이터로 확인하며 다른 문서의 본문 언급을 원문 보유로 해석하지 않는다.

은행 내규는 해당 은행의 발행기관/제목과 자료 종류를 확인한다. 다른 은행 내규로 대신하지 않는다. FATF/EU 원문을 새로 수집한 작업은 아니다. 추가 기관/국가 자료 수집에는 허용 호스트·형식·등록 스키마 지원이 별도로 필요할 수 있다. 현재 고객 원장/신원 제공 연결도 없으며 이번 구현이 그 연결을 추가하지 않는다.

## 검색 응답과 연동

기존 요청 규격과 도구 이름을 유지한다. OpenAPI는 **0.7.0**이다. 새 색인의 검색 응답에 `scopeAssessment`를 추가했다.

주요 필드 예시:

```json
{
  "status": "insufficient_scope",
  "answerability": "insufficient_scope",
  "hits": [],
  "abstentionReason": "요청한 은행의 내부 지침·승인자·처리 기한 자료를 확보하지 못했습니다.",
  "scopeAssessment": {
    "policyVersion": "1.0",
    "decision": "abstain",
    "reasonCodes": ["internal_policy_unavailable"],
    "wholeRequestAbstained": true,
    "generalAnswerabilityVerified": false
  }
}
```

실제 응답은 요청한 자료 종류·기관/관할과 항목별 확보 여부인 `requirements`도 포함한다. 보류 코드:

- `internal_policy_unavailable`: 요청한 은행 내부 자료 없음.
- `requested_publisher_unavailable`: 요청한 발행기관 원문 없음.
- `requested_section_unavailable`: 요청한 원문의 지정 구간 없음.
- `requested_jurisdiction_unavailable`: 요청한 관할의 법령 원문 없음.
- `customer_facts_unavailable`: 사례별 실제 고객 정보 없음.
- `score_decision_policy_unavailable`: 점수에 따른 자동 결정 기준 없음.
- `automatic_decision_not_supported`: 자동 범죄/보고 의무 확정 요청.
- `requested_source_kind_unavailable`, `requested_scope_unavailable`: 자료 종류·기존 조문/시점 필터의 범위 부족.

`decision=scope_supported`는 위의 제한된 범위 조건을 통과했다는 의미다. 관련 본문을 찾았는지, 의미적으로 답할 수 있는지는 별도다. 어휘 검색 결과가 없으면 기존 `no_evidence`를 유지한다.

## Context·보고서·Agent

새 Context 버전은 `evidence-scope-v4`다. Context·보고서와 근거 적합성 API/Agent 응답에 `answerDisposition`을 제공한다.

```json
{
  "answerDisposition": {
    "status": "abstained",
    "abstentionReason": "요청 범위에 맞는 자료가 없습니다.",
    "scopeAssessment": {"policyVersion":"1.0","decision":"abstain"},
    "notice": "보조 검색·거래 사실은 참고용이며 보류된 원질문의 답을 대신하지 않습니다."
  }
}
```

일부 필드만 표시한 예시다. 범위 부족 또는 검색 근거가 없으면 `status=abstained`다. 검색이 되더라도 `status=not_assessed`이며 의미 검증 통과로 표시하지 않는다.

보고서의 `원질문 답변 보류` 항목에 원인과 참고 범위를 표시한다. 원질문이 보류되어도 거래 사실·일반 참고 인용은 보고서에 남을 수 있다. 화면/Agent는 이를 원질문에 대한 답변으로 표시하지 말고 `answerDisposition`을 먼저 확인한다. 적합성 정책이 있으면 `queryCheck.requestedScopeFound=false`와 `needs_more_evidence`도 함께 표시한다.

전체 보고서의 `verification.valid=true`는 수치·인용·보류 표시가 재계산과 일치한다는 의미다. 질문에 답했거나 자료가 충분하다는 승인이 아니다. 보류 상태를 `answered`로 바꾼 뒤 자체 해시를 재계산해도 원래 Context/렌더링과 비교해 거부한다.

## 버전 고정과 과거 보고서

보류 정책 버전을 색인 메타데이터에 기록했다. 본문/출처/청크는 유지하고 새 색인 해시로 반영했다.

- 보완 전: `056de82c20321bf8f94330f5e5a1f50faaa3dc9e68711014591d20e122ce5c91`.
- 현재: `d964bd9f8b4378b2cbd17ee24838873d7fe573cf3cd6e8a0f496465f5284b58f`.
- 기존 V4 첫 결과/질문 해시와 복사된 실행 코드는 변경하지 않았다.

```powershell
python rag-service/corpus_manager.py set-query-scope --version 1.0 --expected-corpus-sha256 현재_색인_해시
```

이미 적용한 현재 PC에서는 동일 버전 반영이 무변경으로 끝난다. 운영자가 명시적으로 `--version legacy`를 사용하면 이전 동작으로 되돌릴 수 있으며 기존/새 색인은 모두 보존한다. 공개 API에 정책 변경·우회 옵션을 추가하지 않았다.

현재 정책보다 오래된 색인은 기존 원문 조회·사실 조회·저장 보고서 검증에 계속 사용한다. 새 Context/보고서/부분 초안 검증/적합성 요청에서 오래된 정책의 색인을 선택하면 **409 `legacy_corpus_read_only`**를 반환한다. 현재 조건을 예전 버전 선택으로 우회하지 않기 위함이다. 기존 보고서는 `/v1/rag/reports/verify`, 사실은 `/v1/rag/facts/resolve`로 조회/검증한다.

## 검증 결과

- 자동 검사 **217개**: 데이터 16·룰 24·RAG 177. 새 요청 범위 검사 17개는 실제 값/절차 구분, 혼합 요청, 원문 보유 여부, 타 은행 문서 대체 금지, 버전 복원, 과거 조회, 보류 표시 조작 거부와 HTTP/Agent 동작을 포함한다.
- 기존 60문항 회귀: 검색 보류 14/20→20/20, 지원 근거 확보 32/40 유지. 개선된 ID는 앞의 6건과 정확히 일치한다. 새 독립 평가가 아니다.
- 6건 모두 실제 검색 API와 검토 보고서에서 보류 사유를 확인했다.
- 실제 거래 12사례의 새 보고서 검증, 과거 보고서 36건과 그 사실 조회 성공.
- 과거 정책으로 새 Context를 생성하려는 요청의 409 거부 확인.
- 실제 LLM 호출 0회. 룰/ML 점수·자료 수집 범위·검색 순위·기존 평가/보고서는 유지했다.

```powershell
python -m unittest discover -s rag-service/tests -p test_query_scope.py -q
python scripts/verify-abstention.py --output artifacts/new-abstention-verification.json
```

실제 회귀 검사 스크립트에는 V4 스냅샷·결과, 현재 문서 자료·Neo4j 시연 데이터와 `rag-document-evidence-reports/`, `rag-domestic-evidence-reports/`, `rag-evidence-fit-reports/`의 과거 보고서 총 36건이 필요하다. 자동 단위 검사는 합성 입력으로 실행한다. 기존 출력은 덮어쓰지 않는다.

결과는 `artifacts/rag-abstention-verification.json`, 반영 기록은 `artifacts/rag-abstention-policy-publication.json`, 새 보고서/보류 예시는 `artifacts/rag-abstention-reports/`에 있다. 이번 보류는 제한된 문구/메타데이터 규칙이므로 모든 표현·언어·부정을 판별하는 의미 모델로 일반화하지 않는다. 구현에 사용하지 않은 새 질문과 현업 검수는 후속으로 필요하다.

코드와 안내는 `codex/rag-safe-share` 스냅샷으로 공유한다. 개인정보·비밀값·개인 접속 주소·원본/산출물은 제외하며 `main` 병합은 별도다. 실행은 [팀 공유 안내](team-handoff.md)를 따른다.
