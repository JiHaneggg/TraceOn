# agent-service · 에이전트 조사 계층

사전발표 PPT(Slide 6·8·9)의 조사 흐름을 LangGraph로 구현한다. 알림 1건마다 그래프를 한 번 실행한다. 노드 사이의 이동은 코드가 상태를 보고 정하고, LLM은 정해진 노드 안에서만 판단한다. **계산은 툴, 판단은 LLM** 원칙을 따른다.

배경, 결정 사항, 기존 에이전트 점검 결과와 팀 논의 항목은 [에이전트 조사 계층 기록](../docs/agent-investigation.md)에 있다.

## 노드 진행 현황

| 순서 | 노드 | 주체 | 상태 |
|---|---|---|---|
| 1 | `alert_context` | 코드 | 완료: 룰 엔진 재검산, 라벨 차단, 근거 사실 ID 부여 |
| 2 | `basic_profile` | 코드 | 완료: 유입·유출, 시간 집중도, 입금 후 출금(통과), 금액 패턴, 패턴 힌트, 라우팅 신호(팀 룰+ML 위험 점수, 임베딩 유사도) |
| 3 | `evidence_check` | 코드 게이트 | 완료: 위험 점수 × 세탁 신호 사실 2×2 분기, 오탐 종료(근거 사실 ID 기록), 안전 규칙 |
| 4 | 공통 LLM 호출부 | 코드 | 예정 |
| 5 | `investigator` | LLM + 그래프 도구 | 예정 |
| 6 | `typology` | LLM | 예정 |
| 7 | `str_writer` | LLM | 예정 |
| 8 | `verifier` | 코드 | 예정 |
| 9 | `human_review` | 사람 (interrupt) | 예정 |

## 설치와 테스트

저장소 루트에서 실행한다. 노드 테스트는 표준 라이브러리만 쓰므로 LangGraph가 없어도 돈다. 그래프 연결 테스트는 LangGraph가 없으면 건너뛴다.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r agent-service/requirements.txt
.venv\Scripts\python -m unittest discover -s agent-service/tests -v
```

## 설계 규칙

- **입력**: 백엔드의 공통 Graph JSON과 룰 결과(`ruleResult`), 선택 질문(`query`)만 받는다. 입력은 처음에 복사해 고정한다.
- **라벨 차단**: `isLaundering`, `patternType` 같은 정답 필드가 들어오면 즉시 반려한다. 정답은 평가 스크립트에서만 사용한다.
- **룰 재검산**: `alert_context`가 같은 그래프로 룰 엔진을 다시 실행한다. 점수나 근거 거래가 다르면 반려한다.
- **근거 ID**: 각 노드는 `evidence`에 `factId`, 값, 근거 거래 ID를 남긴다. 이후 노드는 이 ID로만 사실을 인용한다.
- **예산**: 알림당 LLM 8회, 조사 반복 3회, 4-hop이 상한이다. 호출자는 상한을 낮출 수만 있다. 최후 안전망으로 LangGraph recursion limit을 둔다.
- **실행 기록**: 모든 노드는 `trace`에 결과와 LLM 호출 수를 남긴다.

## basic_profile 지표

알림 거래의 송금 계좌(`sender`)와 수취 계좌(`receiver`)를 서브그래프 전체 기간에서 측정한다. 통화는 섞지 않는다.

| 범주 | 지표 | 근거 사실 ID 예시 |
|---|---|---|
| 유입·유출 | 입출금 건수, 상대 계좌 수, 통화별 합계, 입금 대비 출금 비율 | `profile.receiver.flow.inCounterparties` |
| 시간 집중도 | 60분 안의 최대 거래 수, 활동 기간 | `profile.sender.timing.maxTxInBurstWindow` |
| 입금 후 출금 | 가장 짧은 입금→출금 시간, 180분 안에 들어온 돈의 80~120%가 나간 통과 구간 | `profile.receiver.forwarding.passThroughRatio` |
| 금액 패턴 | ±5% 안의 비슷한 금액 묶음, 보고 기준(1만 달러) 바로 아래 금액 | `profile.receiver.amounts.justBelowThresholdCount` |
| 유사도 | 과거 세탁 계좌 프로파일과의 코사인 유사도 | 근거 사실이 아니다. 게이트의 경로 결정에만 쓴다 |

- 기준값은 `basic_profile.PROFILE_CONFIG`에 있고 아직 조정하지 않은 기본값이다. 검증 구간 데이터로 조정한다.
- 패턴 힌트(`FAN_IN_SHAPE`, `PASS_THROUGH`, `JUST_BELOW_THRESHOLD` 등)는 조사할 곳을 알려줄 뿐 판정이 아니다. 각 힌트는 근거 사실 ID를 가진다.
- 유사도 계산은 바꿔 끼울 수 있다. 실제 데이터에는 FastRP 임베딩(`embedding_similarity.py`), 자동 테스트에는 프로파일 특성 벡터(`similarity.FeatureSimilarity`)를 쓴다. 참조 계좌는 학습 구간의 세탁 계좌로만 만든다.

## 노드 임베딩 (FastRP)

Neo4j GDS `fastRP`와 같은 알고리즘을 numpy·scipy로 구현했다(`fastrp_embeddings.py`). 64차원 중 16차원은 계좌의 입출금 특성(상대 계좌 수, 거래 수, USD 합계)을 투영하고(GDS `propertyRatio` 0.25), 반복 가중치는 GDS 기본값 `[0, 1, 1]`이다.

```powershell
.venv\Scripts\python agent-service/fastrp_embeddings.py --train-end 2022-09-06T13:36:00
.venv\Scripts\python agent-service/check_embeddings.py
```

- **정보 누수 방지**: 학습 구간(팀 V2 평가의 검증 시작 2022-09-06T13:36 이전) 거래만 읽는다. 결과는 `data/embeddings/`에 저장하고 덮어쓰지 않으며, 파일 해시를 manifest로 확인한다.
- **사용 제한**: 학습 구간 안의 알림, 학습 그래프에 없던 새 계좌는 유사도를 계산하지 않는다. 자기 자신은 가장 가까운 참조 계좌에서 제외하고, 과거 참조 계좌인지는 별도 표시한다.
- **HI-Small 결과**: 학습 거래 2,498,986건, 계좌 407,001개, 참조 세탁 계좌 3,170개, 생성 21초.

### 검증 구간 점검 결과 (2026-11-25, 벤치마크 아님)

검증 구간의 세탁 거래 계좌 1,533개와 정상 계좌 표본 20,000개를 비교했다. 최종평가 구간은 읽지 않았다.

| 방식 | AUC 전체 | 처음 보는 계좌 | 과거 참조 계좌 |
|---|---|---|---|
| 가장 가까운 참조 계좌와의 코사인 (현재 구현) | 0.506 | 0.463 | 0.602 |
| 가까운 계좌 50개 중 세탁 계좌 비율 (사전에 정한 대안 1개) | 0.527 | 0.484 | 0.624 |

두 방식 모두 무작위 수준이다. 참조 계좌가 많아 거의 모든 계좌의 최대 유사도가 0.96 이상으로 포화된다. **비지도 FastRP 유사도는 이 데이터에서 세탁 계좌를 가르는 라우팅 신호가 되지 못했다.** 게이트(3단계)는 그래프 사실을 주축으로 설계하고, 유사도는 기록만 하거나 다른 신호로 대체한다. 검증 구간에 맞춰지는 것을 막기 위해 추가 변형은 시도하지 않았다.

## 위험 점수 (팀 룰+ML, 게이트의 라우팅 신호)

PPT 게이트의 유사도 축 자리에 팀의 룰+ML 결합 점수를 쓴다(`risk_score.py`). 팀 코드는 수정하지 않고 호출한다: 수취 계좌 특성 로지스틱 모델(`ml-service/recipient_hybrid.py`, `graphWithAvailabilityAndDelta`)과 V2 룰 점수를 50:50으로 결합한다. 계수와 경계는 팀 스크립트가 고정한 파일에서 읽고 해시로 확인한다. 수취 계좌의 지난 5일 이력은 로컬 원본 색인에서 알림 시각 이전만 읽는다.

준비(저장소 루트, 모두 Git 제외 폴더에 생성):

```powershell
python data-pipeline/evaluation_index.py --source data/processed/HI-Small/transactions.csv --output data/evaluation/HI-Small-source.sqlite3
python data-pipeline/prepare_evaluation.py --index data/evaluation/HI-Small-source.sqlite3 --output data/evaluation/HI-Small-v2
python rule-service/evaluate.py --cases data/processed/HI-Small-MVP/cases.json --target-recall 0.9 --output artifacts/hi-small-pilot.json
python rule-service/experiment.py tune --data data/evaluation/HI-Small-v2 --baseline artifacts/hi-small-pilot.json --target-recall 0.9 --output artifacts/rule-v2-frozen.json
python scripts/evaluate-recipient-activity.py develop --output artifacts/recipient-activity-frozen-20261123-final.json
python scripts/evaluate-recipient-activity.py evaluate --freeze artifacts/recipient-activity-frozen-20261123-final.json --output artifacts/recipient-activity-fresh-20261123.json
python scripts/evaluate-recipient-hybrid.py develop --output artifacts/recipient-hybrid-frozen-next.json
python scripts/evaluate-recipient-hybrid.py evaluate --freeze artifacts/recipient-hybrid-frozen-next.json --output artifacts/recipient-hybrid-fresh-next.json
.venv\Scripts\python agent-service/check_risk_score.py
```

- 기준선은 Neo4j 시범 결과 대신 같은 시범 12건의 오프라인 평가를 쓴다. 기준선은 비교 기록에만 쓰이고 선택 설정과 모델에는 영향이 없다.
- `recipient-activity-fresh-20261123.json`은 팀 스크립트가 고정해 읽는 파일명이다.
- **재현 확인 (2026-11-25)**: V2 룰 설정, 입금 활동 4개 행, 룰+ML 결합 5개 행이 팀 기록과 모두 같았다. `check_risk_score.py`로 팀의 새 평가 250건을 에이전트 연결 코드로 다시 채점해 탐지 84·오탐 91(오탐 50% 경계 77·65)이 팀 기록과 일치함을 확인했다.
- 알림 거래가 모델 학습 사례(V2 검증 250건)이면 `inModelTrainingSet=true`다. 평가에서는 이 사례를 제외한다.
- 위험 점수는 원본 색인에 대상 거래가 있을 때만 계산한다. 합성 사례에는 `target_not_in_source_index`로 계산하지 않는다.

## 알림 기준 (V2 룰)

알림은 넓게 올리고 오탐은 게이트와 조사 단계에서 줄인다. 알림 기준은 팀 V2 룰 설정(`alert_rules.py`, 7일 창, 집금 2개·분산 12개 이상, 65점)이다. V2 검증 250건에서 비교한 결과(`validation_scores.py`, 룰+ML은 교차검증 점수):

| 알림 기준 | 세탁 탐지/100 | 정상 오탐/150 | 보정 Recall | 오탐률 |
|---|---|---|---|---|
| 기본 룰 (24시간) | 56 | 66 | 55.2% | 44.0% |
| **V2 룰 (7일)** | **94** | 123 | **93.7%** | 82.0% |
| 룰+ML ≥ 경계 | 91 | 101 | 90.8% | 67.3% |
| 기본 또는 V2 또는 룰+ML | 94 | 130 | 93.7% | 86.7% |

- V2 룰이 가장 넓고, 다른 기준을 합쳐도 세탁 탐지가 늘지 않았다. V2 룰은 이 검증 표본으로 고른 설정이라 수치가 낙관적이다(팀의 새 평가 250건에서 87.78%).
- 세 기준 모두 놓친 세탁 6건(BIPARTITE, CYCLE, 유형 미지정)은 에이전트까지 오지 않는다. 룰 담당과 공유할 항목이다.
- 팀 백엔드의 기본값은 아직 24시간 룰이다. `alert_context`는 알림의 룰 설정을 `ruleConfigId`(`v2`/`other`)로 기록한다.

## 게이트 (evidence_check)

| | 세탁 신호 사실 있음 | 없음 |
|---|---|---|
| **룰+ML 위험 ≥ 0.4869** | `fast_track` → 유형 판별 | `investigate` (증거 충돌) |
| **위험 < 0.4869** | `investigate` (새 유형 의심) | `close` (오탐 종료, LLM 0회) |

- 세탁 신호 사실은 `CYCLE_PATH`, `PASS_THROUGH`, `RAPID_BURST`, `REPEATED_AMOUNTS`, `JUST_BELOW_THRESHOLD` 힌트다. 집금·분산 모양 힌트는 정상에도 흔해 제외한다. 조정 전에 정했다.
- 안전 규칙: 알림 룰에 걸리지 않은 거래는 `rule_not_flagged`로 종료한다. 위험 점수가 없으면 종료하지 않는다(사실 있으면 조사, 없으면 사람 검토). LLM 예산이 없으면 LLM이 필요한 경로 대신 사람 검토로 보낸다.
- 종료할 때 확인한 프로파일 사실 ID(`closureBasisFactIds`)를 남긴다(PPT: 오탐 근거 기록).
- **경계 조정** (`tune_gate.py`, `gate_config.json`): V2 검증 구간에서 V2 룰이 올린 알림 217건(세탁 94·정상 123)만 사용하고, 위험 점수는 교차검증 점수를 쓴다. PPT의 Recall 손실 한도 5%는 시스템 전체 몫이므로 게이트에는 2%를 배정했다. 결과는 Recall 손실 1.1%(세탁 1건 종료), 오탐 감소 15.5%(정상 19건 종료)다. 5%를 모두 쓰면(경계 0.5618) 오탐 감소는 17.9%로 2.4%p 늘지만 세탁 3건을 더 놓친다.
- **한계**: 세탁 신호 힌트가 정상 알림의 72%, 세탁 알림의 62%에서 나왔다. 기본 기준값으로는 사실 축의 구분력이 약하다. 게이트만으로는 PPT 목표(오탐 40% 감소)에 못 미치므로 나머지는 조사·유형 판별 단계에서 줄인다.

## 데이터

- **자동 테스트**: `synthetic_graphs.py`의 합성 사례 5개(집금·통과, 순환, 분산, 정상 쇼핑몰, 정상 급여)와 `rag-service/examples/agent-workflow/`의 소형 사례를 쓴다. 룰 결과는 실제 룰 엔진으로 만든다. 정상 사례 두 개는 룰에 걸리지만 세탁 신호가 없는 오탐 후보다. 합성 사례는 기본 룰로 알림을 만든다(V2 룰은 분산에 상대 계좌 12개 이상이 필요하다).
- **실제 데이터**: IBM HI-Small을 로컬에 받아 전처리한다. `data/`와 `artifacts/`는 Git에 올리지 않는다.

### 실제 데이터 준비와 실행

Kaggle API 토큰이 필요하다. HI-Small 파일 3개(약 510MB)만 받는다.

```powershell
foreach ($f in "HI-Small_Trans.csv", "HI-Small_accounts.csv", "HI-Small_Patterns.txt") {
  kaggle datasets download -d ealtman2019/ibm-transactions-for-anti-money-laundering-aml -f $f -p data/raw/HI-Small
}
python data-pipeline/preprocess.py --input data/raw/HI-Small --dataset HI-Small   # 약 2.5분
python data-pipeline/prepare_mvp.py                                               # 시범 12건, Neo4j 불필요
.venv\Scripts\python agent-service/real_cases.py                                  # 1·2단계 노드 확인
```

전처리 결과가 팀 기록(`data-pipeline/README.md`)과 같은지 확인한다: 거래 5,078,345, 세탁 5,177, 계좌 515,088. 룰+ML 위험 점수와 게이트 조정에 필요한 V2 평가 데이터는 위 "위험 점수" 절의 명령으로 만든다.

### 시범 12건 전체 흐름 (2026-11-25)

V2 룰 알림 → 프로파일 → 룰+ML 위험 점수 → 게이트까지 실행했다. 기능 확인용이며 성능 평가가 아니다.

| 경로 | 세탁 | 정상 |
|---|---|---|
| `fast_track` (위험 높음 + 세탁 신호) | 4 | 0 |
| `investigate` (위험 높음, 신호 없음) | 0 | 6 |
| `close` (알림 룰에 걸리지 않음) | 0 | 2 |

기본 24시간 룰이 놓친 세탁 2건(TX-005056038, TX-005003727)이 V2 룰로 알림에 올라왔다. 학습 구간 안의 알림은 임베딩 유사도를 계산하지 않고, 학습 구간 전 이력이 부족한 9월 1~5일 알림은 팀 코드 규칙대로 이력 없이 위험 점수를 계산했다.
