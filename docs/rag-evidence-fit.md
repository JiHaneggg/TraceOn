# 룰별 근거 적합성 점검 — 2026-11-15

검색된 문서가 있다는 사실과 그 문서로 해당 판단을 뒷받침할 수 있다는 판단을 구분한다. 현재는 **검토자가 정한 원문 연결 + 관찰 가능한 조건 + 미확인 조건**을 점검하는 제한된 기능이다. 일반 자유 문장의 의미를 판단하는 모델이나 법률 엔진은 아니다.

점검 결과를 Context·보고서·전체 보고서 검사에 연결했다. 룰 점수·탐지 조건·기존 성능 평가 결과는 바꾸지 않았다. 실제 LLM 호출도 하지 않는다.

## 점검 기준

각 룰의 근거 역할을 다음 세 가지로 나눈다.

- `reporting_context`: 국내 STR 보고 절차의 일반 근거. 특정금융정보법 제4조의 선별 원문과 연결한다. 특정 룰 탐지를 보고 의무로 바꾸는 근거가 아니다.
- `due_diligence_context`: 고객확인의 일반 근거. 같은 법 제5조의2의 선별 원문과 연결한다. 실제 고객 정보가 확보되었다는 뜻은 아니다.
- `pattern_reference`: 그 패턴을 설명하는 참고 유형. 위의 일반 조문이나 관련 없는 검색 결과로 이 역할을 대신하지 않는다.

| 룰 | 연결한 유형 자료 | 확인하는 구조/지급 방식 | 추가로 필요한 정보 |
|---|---|---|---|
| CYCLE | 현재 확보 자료 중 직접 연결한 순환 유형 근거 없음 | 재검산한 시간 순서·경로 | 동일 자금의 이동, 거래 목적, 고객/소유자 관계, 유형 원문 |
| FAN_IN | [FinCEN FIN-2014-A005](https://www.fincen.gov/resources/advisories/fincen-advisory-fin-2014-a005)의 Funnel Account 정의 | 여러 송신 계좌, 지급 방식 Cash 기록 | 지역 간 출금 맥락, 거래 목적, 고객/소유자 관계 |
| FAN_OUT | [FINTRAC 금융기관 징후 안내](https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation/indicators-indicateurs/fin_mltf-eng)의 복수 고객 송금 참고 문구 | 여러 수취 계좌, 지급 방식 Wire 기록 | 거래 목적, 실제 고객/소유자 관계 |

FinCEN의 현금 입금·다른 지역 출금 등 맥락을 단순 집금 그래프와 같다고 보지 않는다. FINTRAC의 고객 관련 조건도 계좌 수만으로 확인하지 않는다. 지급 방식 대조는 원천 `paymentFormat` 문자열을 확인하는 것이며 거래의 법적 분류나 현금 입금의 성격을 인증하지 않는다. 문서가 언급하는 전체 조건을 자동 판독하는 기능도 아니다.

국내 일반 근거의 원문은 [특정금융정보법 제4조](https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1024562291)와 [확보한 법률 본문](https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=290725&efYd=20261002&chrClsCd=010202)에서 확인했다. 자료는 데이터이며 원문에 포함된 지시를 실행하지 않는다.

## 정책과 원문 고정

`rag-service/evidence_fit_policy.json`은 검토한 원문 4개를 특정 청크·원본/본문 해시·정확한 발췌와 연결한다. 역할별 매핑과 최소 확인 항목을 기록하며 정책 자체의 SHA256을 검증한다. 임의 실행 코드·검색식·판단식을 받지 않는다.

현재 `reviewStatus=developer_curated_pending_domain_review`다. 개발자가 출처/문구/조건을 검토한 초안으로, 멘토나 현업의 법적 적합성 검토를 통과했다는 뜻은 아니다. 명시적 조건 점검을 일반 의미 검증 성공으로 표시하지 않는다.

정책을 현재 색인 안에 함께 보관해 과거 보고서를 같은 기준으로 재현한다. `corpus_manager.py apply-evidence-policy`는 모든 연결의 원문/발췌를 대조하고 이전/새 색인을 보존한 뒤 반영한다. 현재/예전 8문서의 본문 50개와 인용 ID는 동일하다. 이후 원문 버전이 바뀌어 연결이 끊기면 다른 유사 본문으로 대체하지 않고 근거 부족으로 남긴다. 새 원문에 맞춘 정책은 검토 후 새 버전으로 반영해야 한다.

```powershell
python rag-service/corpus_manager.py apply-evidence-policy --policy rag-service/evidence_fit_policy.json --expected-corpus-sha256 현재_색인_해시
```

이 정책은 현재 확보한 원본/청크에 연결된다. 다른 PC에서 새로 수집한 자료의 해시/청크가 다르면 그대로 활성화할 수 없다. 기존 스냅샷을 공유하거나 새 원문을 대조해 정책을 다시 작성한다. 기존 정책/엔진 버전의 동작을 임의로 덮어쓰지 않는다.

## 상태와 화면·Agent 사용

`POST /v1/rag/evidence/assess`는 기존 Context 입력과 같은 `graph`, 선택적 `ruleResult`, `query`, `corpusSha256`를 받는다. 입력 룰 결과를 재검산하고 정책을 적용한다. 사용자가 점검 결과나 충족 여부를 전달할 수 없다. Agent 도구 `assess_evidence_fit`도 같은 동작이다. 전체 도구는 9개, OpenAPI는 0.6.0이다.

```json
{
  "status": "needs_more_evidence",
  "contextVersion": "evidence-fit-v3",
  "evidenceFitness": {
    "status": "needs_more_evidence",
    "label": "근거 부족·추가 검토 필요",
    "sufficientForAutomatedDecision": false,
    "semanticEntailmentVerified": false,
    "legalApplicabilityVerified": false,
    "rawSnapshotChecked": false
  },
  "humanReviewRequired": true
}
```

위 예시는 일부 필드만 표시했다. 실제 응답은 Context/색인/Graph ID·정책 해시·질문 범위 상태·룰별 `referenceChecks`, `conditionChecks`, `missingEvidence`를 포함한다.

| 결과 | 의미 |
|---|---|
| `needs_more_evidence` | 문서 역할·관찰 조건·요청 범위 중 추가 근거가 필요한 부분이 있음 |
| `no_rule_findings` | 현재 범위/설정에서 룰 탐지가 없음. 정상 거래 확정이 아님 |
| `bounded_requirements_met` | 정의한 제한된 확인 항목을 충족한 경우의 상태. 법률/의미 판정 성공이 아님 |
| `not_configured` | 지정한 과거 색인에는 이 정책이 없음. 새로운 기준을 과거 결과에 조용히 덧씌우지 않음 |

룰별 세부 상태는 원문 역할 누락 시 `insufficient_evidence`, 관련 원문은 있어도 조건이 불충분하면 `partial_reference`다. 관찰 항목은 `observed / not_observed / not_verified`로 구분한다. 고객 신원·관계·경제적 목적·지역·동일 자금은 현재 Graph만으로 확인할 수 없다. 현재 정책의 모든 조건을 Graph만으로 충족했다고 표시하지 않는다.

질문의 1차 검색 범위가 부족하면 CDD/법령 보조 검색이 성공해도 질문에 답한 것으로 취급하지 않는다. 별도 `queryCheck`와 보고서 경고로 남긴다.

## 보고서 검사 해석

- 새 Context와 보고서에 `evidenceFitness`, 보고서에는 `reviewReadiness`를 추가했다. Markdown의 `근거 적합성·추가 확인` 항목에 역할별 근거와 빠진 정보를 표시한다.
- 정책이 사용한 인용은 실제 점검한 발췌를 인용한다. 해당 발췌와 근거 부족 안내는 제어된 Agent 계획에서 뺄 수 없다. 검색 청크 첫 부분의 다른 문구로 바꿔도 전체 검사가 거부한다.
- `verification.valid=true`는 수치·원문·본문·근거 부족 표시가 계산 결과와 일치한다는 의미다. 근거 충분이나 제출 승인과 다르다. 화면은 `reviewReadiness.status`와 `evidenceFitness.findings[].missingEvidence`를 함께 표시해야 한다.
- `checks.evidenceFitness.valid`도 점검 결과의 재계산 일치 여부다. `semanticEntailmentVerified=false`, `readyForSubmission=false`, `sufficientForAutomatedDecision=false`를 유지한다.
- 적합성 점검 자체는 버전 고정 색인 기준이다. 원본 HTML 대조는 전체 보고서의 `checks.sourceSnapshots`에서 수행하며 성공 여부를 별도로 확인한다. 독립 점검 응답의 `rawSnapshotChecked=false`를 원문 검증 성공으로 표시하지 않는다.

근거 부족·제출 가능 표시·필수 인용/안내를 변경한 뒤 자체 해시를 다시 계산해도 원래 데이터·정책과 비교해 거부한다.

## 이전 자료와 검증 결과

현재 색인: `056de82c20321bf8f94330f5e5a1f50faaa3dc9e68711014591d20e122ce5c91`.
정책 해시: `fe05afb812e980cd9ce32e434174831b0680bd72e429f82b83492c698259d2a9`.
정책 반영 전 색인: `98fc99a9787c5dfb1bad2897ea72f4f47ea7075b9872521c4e34db0b008100cd`.

기존 색인의 `evidence-v1 / evidence-kr-v2` 보고서를 유지한다. 정책 반영 후 문서를 갱신한 경우에도 과거 `evidence-fit-v3` 보고서와 사실 조회를 원래 색인/정책으로 재현하는 검사를 추가했다.

- RAG 자동 검사 **150개** 통과(이전 131 + 새 19). 데이터 16·룰 24 포함 총 **190개**.
- 실제 Neo4j 12사례: 룰 점수/설정/결과 유지, 새 보고서 검증, 원문 인용 154건 조회.
- 룰 탐지 4사례는 `needs_more_evidence`, 나머지 8사례는 `no_rule_findings`. 이는 이 작은 연결 검사 사례의 상태이며 탐지 정확도나 모집단 비율이 아니다.
- 과거 보고서 24건(국내 자료 확대 전/후 각각 12건) 원래 색인으로 검증 성공.
- 근거 충분/제출 가능 조작, 필수 발췌/근거 부족 안내 제거 후 자체 해시 재계산을 모두 거부했다.
- 실제 LLM 호출 0회. 법률 적용·자유 문장 의미·최종 STR 제출 검증은 하지 않았고 Spring/ML/관제 UI를 모두 연결한 검사도 아니다.

```powershell
python -m unittest discover -s rag-service/tests -q
python scripts/verify-evidence-fit.py --output artifacts/new-evidence-fit-verification.json
```

검사 결과는 `artifacts/rag-evidence-fit-verification.json`, 정책 반영 이력은 `artifacts/rag-evidence-fit-policy-publication.json`, 보고서는 `artifacts/rag-evidence-fit-reports/`에 있다. 검사 도구는 DB를 읽기만 하며 과거 파일을 덮어쓰지 않는다. 실제 원문/색인·Neo4j 데이터·정책 반영 기록과 `rag-document-evidence-reports/`, `rag-domestic-evidence-reports/`의 과거 보고서 총 24건이 필요하다. 기존 결과가 있으면 새 출력 경로를 지정한다. 자동 단위 검사는 이 자료 없이 합성 입력으로 실행한다.

이번 코드/안내는 `codex/rag-safe-share` 스냅샷으로 공유한다. 원문/등록부/색인/산출물은 Git에서 제외한다. `main` 병합과 관제 화면 구현은 별도이며 [팀 공유 안내](team-handoff.md)를 따른다.
