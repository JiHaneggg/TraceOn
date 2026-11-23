# 발표용 평가표와 코드 확인 근거

2026-11-23 확인. **조회 일치, 탐지 성능, 초안 무결성, 근거 검색, 저장 복원은 서로 다른 평가 대상**이다. 기존 산출물의 수치를 읽고 코드와 대조했으며, 이번에 500건 평가·탐지 평가·동결 RAG 평가를 다시 실행하거나 조건을 조정하지 않았다. 이번 새 실행은 시연 거래의 API 흐름과 기존 화면 계약 검사다.

## 500건 Neo4j 조회 대조

질문: “기존 오프라인 평가와 같은 조건으로 Neo4j에서 읽으면 동일한 그래프와 룰 결과가 나오는가?” 기준은 정답 라벨이 아니라 **기존 그래프·고정 룰 결과**다.

| 대조 항목 | 검증 250건 | 최종평가 250건 |
|---|---:|---:|
| 그래프 일치 | 250/250 | 250/250 |
| 거래 ID·방향·계좌·금액 문자열·통화·시각 일치 | 250/250 | 250/250 |
| 조회 생략 상태 일치 | 250/250 | 250/250 |
| 기존 룰의 점수·분류·근거 일치 | 250/250 | 250/250 |
| 고정 연구 후보 룰의 점수·분류·근거 일치 | 250/250 | 250/250 |
| 상한으로 생략된 그래프 수 | 기존·Neo4j 각각 188 | 기존·Neo4j 각각 172 |

전체 원천 **5,078,345거래**의 SQLite 인덱스에서 이 고정 조회에 필요한 행을 모아 **131,089거래·21,757계좌**를 평가 전용 Neo4j 5.26.32에 적재했다. 대상 조회 500개와 계좌/시간창 조회 **6,491개**를 실행했고 계좌 조회의 거래 ID·순서는 **6,491/6,491 일치**했다. 조건은 대상 시점까지 7일·2홉·계좌당 최근 50건·최대 1,000거래다. 51번째 행을 포함해 생략 판정도 재현했다.

코드는 입력·룰·동결 설정 해시를 먼저 검사한다. 그래프 비교에서는 `meta.sourceScope`, `meta.sourceGraphComplete`, 룰 비교에서는 `limitations.sourceGraphComplete`만 출처 범위 차이로 제외한다. 생략 상태는 제외하지 않는다. 현재 `rule-service/rules.py` 해시는 대조 산출물의 룰 엔진 해시와 일치했다.

**발표 표현:** “기존 고정 500건에서 조회와 룰 결과의 재현을 확인했습니다.” 탐지 정확도 100%, 새로운 Recall 향상, 전체 507만건 Neo4j 운영 적재·성능 검증으로 표현하지 않는다.

근거: [대조 설명](neo4j-evaluation-replay.md) · [실행 코드](../scripts/verify-neo4j-evaluation.py) `frozen_inputs`, `comparable_graph`, `compare` · 로컬 산출물 [comparison.json](../artifacts/neo4j-evaluation/v2-20261123-final/comparison.json) (`passed=true`, `failures=[]`, 2026-11-23 05:16 KST).

## 탐지 평가 · 동결 V2 최종평가

질문: “정답 라벨과 비교해 의심 거래를 얼마나 찾고, 정상 거래를 얼마나 잘못 알리는가?” 검증 250건에서 조건을 고른 뒤 **별도 최종평가 250건(세탁100·정상150)**에 고정 적용했다.

| 지표 | 기존 설정 | V2 연구 후보 |
|---|---:|---:|
| 표본 정탐 TP / 미탐 FN | 1 / 99 | 88 / 12 |
| 표본 오탐 FP / 정상 TN | 0 / 150 | 99 / 51 |
| 표본 Recall | 1.00% | 88.00% |
| 원본 분포 보정 Recall | 0.98% | **87.42%** |
| 원본 분포 보정 Precision | 100.00% | **0.16%** |
| 정상 거래 오탐률 | 0.00% | **66.00%** |

세탁을 많이 포함한 층화 표본이므로 보정 Recall·Precision은 추출 확률의 역수로 가중한 비율 추정치다. **88/100과 보정 87.42%를 같은 값으로 쓰지 않는다.** 기존 Precision 100%는 정탐 단 1건의 결과다. 보정 수치의 모집단은 제외·시간·패턴 가족 조건을 통과한 합성 거래이며 은행 실거래 전체의 성능이 아니다.

연구 후보는 보정 Recall **90% 목표 미달**, 정상 오탐률 **66%**로 운영 기준으로 채택하지 않았다. 화면은 `server.render_graph_payload`에서 기본 `rules.score(graph)`를 호출하며 이 후보 설정이나 ML 모델로 자동 교체하지 않는다. 500건 Neo4j 대조는 이 기존 결과를 보존한 재현 검사다.

근거: [V2 평가 기록·표본 정책](db-rule-v2.md) · [실행 방법](evaluation-v2.md) · [룰 실행](../rule-service/experiment.py) · [고정 설정](../rule-service/configs/rule-v2-research.json) · 로컬 산출물 [rule-v2-holdout.json](../artifacts/rule-v2-holdout.json) `results.baseline/selected.sample`, `weighted`와 [rule-v2-frozen.json](../artifacts/rule-v2-frozen.json).

## 탐지 평가 · 최신 룰·ML 결합은 별도 오프라인 비교

아래 표는 V2 최종평가와 **다른 새 250건(세탁100·정상150)**이다. V2 검증을 그룹 5분할해 학습·경계를 선택한 뒤 고정했다. 룰 점수와 ML 점수의 결합 비중은 50:50이다. 같은 HI-Small 원본이며 개발·평가 간 수취 계좌·과거 이력은 겹칠 수 있다.

| 새 표본의 모델 · 검증에서 Recall 90% 목표로 고른 경계 | TP / 100 | FP / 150 | 보정 Recall | 정상 오탐률 |
|---|---:|---:|---:|---:|
| V2 룰 단독 | 88 | 110 | 87.78% | 73.33% |
| 룰 + 그래프 ML | 85 | 96 | 84.80% | 64.00% |
| 룰 + 그래프 ML + 이력 여부 | 84 | 96 | 83.80% | 64.00% |
| 룰 + 그래프 ML + 이력 여부·송금 계좌 증가량 | 84 | 91 | **83.80%** | **60.67%** |

증가량 추가 효과는 같은 새 표본에서 **TP84 유지, FP96→91**이다. “90% Recall을 달성했다”거나 V2 표의 FP99와 이 표의 FP91을 직접 비교한 개선율로 말하지 않는다. 이력 부족은 `senderDelta=null`과 별도 이력 여부 특성으로 구별한다. 최신 결합도 목표 미달이며 현재 화면/API 점수에는 연결되지 않은 연구 비교다.

근거: [결합 평가 설명](recipient-hybrid-evaluation.md) · [오프라인 모델](../ml-service/recipient_hybrid.py) · [평가기](../scripts/evaluate-recipient-hybrid.py) · 로컬 산출물 [동결 모델](../artifacts/recipient-hybrid-frozen-20261123.json), [새 표본 결과](../artifacts/recipient-hybrid-fresh-20261123.json) `metrics.ruleOnly`, `graph:recallTarget`, `graphWithAvailability:recallTarget`, `graphWithAvailabilityAndDelta:recallTarget`.

## 초안 검증과 근거 검색 평가

| 평가 | 고정 평가 대상·결과 | 발표에서의 의미·한계 | 근거 |
|---|---|---|---|
| 제어형 초안 반려 | 시연 그래프12개로 정상30·결함30 초안. REJECT 양성 TP30/FP0/TN30/FN0, F1=1.0 | 정의한 구조적 결함 세트에서의 반려 결과. 같은 그래프 반복·개발자 정답·템플릿이며 실제 모델이나 현업 의미 판단 평가가 아님 | [검증 평가](verifier-evaluation.md), [원본 결과](../artifacts/verifier-benchmark-v1/results.json) `summary.confusionMatrix` |
| 통과 초안 무결성 | 수치 **520/520**, 청크·발췌 **392/392**, 조문 태그 존재 **370/370** | 사실표·허용 본문·원문 위치의 대응. 법률 적용·의미적 뒷받침 승인과 구분 | 같은 결과의 `summary.acceptedIntegrity` |
| 검증 함수 지연 | 평균0.7524초 / p95 0.9274초 / 최대1.2049초 | Context 재구성·로컬 원문 대조 포함. DB 최초 조회·LLM·UI 제외. 재시도 평균 미측정 | 같은 결과의 `summary.latencySeconds`, `meanRetryCount=null` |
| RAG V4 첫 고정 검색 | 지원 질문 필요 근거 **32/40(80%)**, 미지원 검색 보류 **14/20(70%)** | 60개 개발자 작성 질문, 당시8문서·50청크. 검색 지표이며 탐지 Recall·생성 답변 정확도와 다름. 미지원6건에 관련 본문 노출 | [V4 기록](rag-evaluation-v4.md), [첫 결과](../artifacts/rag-evaluation-v4/results.json) `summary` |

개선에 사용한 V4의 후속 회귀 40/40·20/20은 독립 첫 평가와 분리한다. 멘토 검수 대기 24질문도 완료된 평가 수치로 합치지 않는다. 현재 RAG 서비스의 10문서·61청크와 V4 첫 동결 자료의 규모를 섞지 않는다. 실제 LLM 실호출·평균 재시도·의미 판정의 현업 정답 비교는 별도 확인이 필요하다. 추가된 [Agent 도구](agent-review-workflow.md)의 합성 사례·재시도 상한 검사 기록은 이 동결60건 초안 평가와 다른 결과이며 이번 문서 작업에서 다시 실행하지 않았다.

## 로컬 시연과 저장·복원 재확인

2026-11-23 **05:47 KST** 실제 구현으로 임시 HTTP 서버를 띄워 확인했다. 사용자의 기존 이력 대신 별도 검증 SQLite를 사용했다.

| 검사 | 이번 결과 | 확인 수준 |
|---|---|---|
| 대상 조회·추가 | 69→72거래, 읽은4 / 신규3 / 중복1 | 기존 거래 값·대상/시점/기간/홉 보존, 신규 ID 대조 |
| 이전 초안·스냅샷 | 이전 초안 재검사와 이전 스냅샷 초안 준비 모두409 | 갱신 그래프와 이전 초안 혼합 차단 |
| 현재 초안 | 템플릿 검증 통과, 사실3개·인용1개 직접 조회 | `humanReviewRequired=true`, `readyForSubmission=false`, `llmCalled=false` |
| 저장 | 69거래 그래프만 / 72거래 초안 포함, 두 저장본 생성 | 동일 초안 재저장 멱등성 확인, 저장 시 인용 수집·재검사 |
| 새 서비스의 오프라인 복원 | 72거래·초안 동일 복원, 69거래 초안 없는 저장본도 보존. 사실1·인용1 조회 | Neo4j·RAG 호출을 실패시키는 대체 객체로 외부 조회 없음 확인. 저장 묶음 검사 통과·현재 자료 미조회 |
| 기존 자동 검사 | Python **65개** + JavaScript/Cytoscape **40개** 통과 | 실제 Python 코드 / 같은 소스 바이트의 복사본 JS 검사. API·ID·방향·금액·초기화·저장 계약 |

새 실행의 코드·시연 자료·코퍼스 해시는 로컬 [demo-current.json](../artifacts/presentation-evidence-20261123/demo-current.json)에 기록했다. 이 결과는 브라우저 조작이나 새로운 탐지 성능 측정이 아니다. 임시 서버는 종료했고 화면 8093의 상시 실행 상태와도 구별한다. Python 검사 첫 시도는 샌드박스의 로컬 소켓 접근 제한으로 오류가 났으며, 로컬 통신이 가능한 환경에서 다시 실행한 65개가 모두 통과했다.

작업 중 공통 기준 `9663073`에 추가된 [팀 연동 검사](../scripts/verify-team-integration.py)도 실행했다. [원본 출력](../artifacts/presentation-evidence-20261123/team-integration-current.json)은 `passed=true`, `stage=archive`, **`mode=local_reference`**다. 69→72건·중복1, 추가 요청 재전송, 이전 초안 관련4요청 거부, 사실2·인용14, 저장 재전송·오프라인 재시작 복원이 통과했다. 이는 로컬 기준 구현의 검사이며 팀 Spring API 연동 통과와 구별한다. 이 도구의 자료 해시는 공개 그래프 필드 정렬 SHA-256(`d88ae988…653f34`)으로, 관리 마커의 `datasetSha256`와 해시 대상이 다르다.

기존 저장·복원 평가도 원본을 확인했다: [history-verification-20261118-v3.json](../artifacts/graph-viewer/history-verification-20261118-v3.json)의 저장 6·새 서비스 복원 6, 오프라인 사실 43·인용 42·다운로드 9, 손상 읽기 거부 3이다. 해당 결과의 서버·추가 조회·저장 구현 4파일 해시는 현재 코드와 일치한다. [저장 계약](analysis-history.md)의 과거 브라우저 확인 기록은 이번 새 API 검사와 별개다.

## 근거 파일 보관과 재현

`artifacts/`와 데이터·SQLite·인용 본문은 Git 제외 자료다. 위 원본 JSON 링크는 해당 로컬 산출물이 있는 PC에서 열리며, 팀 공유 코드에 자동 포함되지 않는다. 원본 산출물은 덮어쓰지 않았다. 발표 자료에는 집계와 코드 경로를 두고, 팀 대조에 필요한 원본은 정해진 공유 절차로 따로 전달한다.

```powershell
# 기존 계약 검사: 저장소 루트에서 각각 실행
python -B -m unittest discover -s graph-viewer/tests -q
node --test graph-viewer/tests/*.test.mjs

# 공통 기준의 로컬 연동 검사: 임시 SQLite 사용 후 정리
python -B scripts/verify-team-integration.py

# Spring 구현 이후: 시연용 팀 API의 구현된 단계까지 실행
python -B scripts/verify-team-integration.py --base-url http://127.0.0.1:8080 --stage graph
# archive 단계는 지정한 팀 API에 테스트용 분석 이력 한 개를 저장한다.

# 이번 시연 API 재확인: 새 출력 파일명을 사용
# 현재 Neo4j·RAG와 시연 자료·문서 스냅샷이 준비된 PC 기준
python -B artifacts/presentation-evidence-20261123/verify-presentation-evidence.py --root . --output artifacts/presentation-evidence-20261123/demo-next.json
```

시연 재확인 도구는 Neo4j 조회 전용이며 출력 파일 옆의 새 SQLite에만 저장한다. 두 개의 임시 서비스 인스턴스로 복원하므로 8093에 상시 화면을 띄우는 실행기는 아니다. 500건 재현은 [전용 실행 방법](neo4j-evaluation-replay.md)을 사용하고 기존 실행 폴더를 보존한다. 다음 탐지 개선의 판정에는 이미 본 500건 대신 새 평가를 준비한다.
