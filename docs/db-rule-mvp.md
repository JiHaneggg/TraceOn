# DB·룰 MVP 실행 가이드

## 현재 구현 범위

계좌→거래 그래프, 제한된 과거 서브그래프, 집금·분산·시간 순서를 확인하는 순환 룰, 점수·근거 반환, validation 임계값 탐색과 고정 test 평가를 연결한다. 위험 점수는 휴리스틱이며 확률이 아니다. ML 점수 결합과 LLM 설명은 후속 통합 범위다.

## 재현 순서 (저장소 루트에서 실행)

1. 원천 파일을 `data/raw/HI-Small/`에 둔다. `HI-Small_Trans.csv`, `HI-Small_accounts.csv`, `HI-Small_Patterns.txt` 세 파일이 필요하다.
2. `python data-pipeline/preprocess.py --input data/raw/HI-Small --dataset HI-Small`
3. `python data-pipeline/prepare_mvp.py`
4. `.env`에 `NEO4J_PASSWORD`를 설정하고 `docker compose up -d neo4j`로 새 개발 DB를 실행한다. 기존 데이터가 있는 DB를 덮어쓰지 않는다.
5. `python data-pipeline/neo4j_mvp.py load --transactions data/processed/HI-Small-MVP/transactions.csv --dataset HI-Small-MVP`
6. `data/processed/HI-Small-MVP/manifest.json`의 targets 중 하나를 선택한다.
7. `python data-pipeline/neo4j_mvp.py subgraph --tx-id <거래ID> --output artifacts/subgraph.json`
8. `python rule-service/evaluate.py --cases data/processed/HI-Small-MVP/cases.json --target-recall 0.9 --output artifacts/hi-small-pilot.json`

0.9는 실행 예시다. 프로젝트의 목표 Recall은 팀과 멘토가 실측 기준으로 합의한다. 목표를 충족하는 validation 설정이 없으면 `target_not_met`를 출력하고 test를 실행하지 않는다. test 결과를 본 뒤 설정을 바꾸면 새 검증 계획이 필요하다.

Docker가 동작하지 않는 PC에는 이번 작업의 검증용으로 `.tools/`에 독립 Neo4j 5.26.32와 JDK 21을 준비했다. `scripts/start-local-neo4j.ps1`로 시작한다. 시스템 Java·서비스 설정은 변경하지 않는다. `.tools/`와 `.env`는 Git에서 제외된다.

## MVP 표본의 의미

`prepare_mvp.py`는 짧은 CYCLE(2~4거래), FAN-IN(3~10거래)의 이른/늦은 패턴 사례와 시드가 고정된 정상 대조군을 선택한다. 원천 전처리의 전역 txId를 유지한다. 소규모 사례의 기능 검증용 표본이며 전체 데이터의 Recall·Precision을 추정하는 벤치마크가 아니다. 선택 기준과 해시는 manifest에 기록한다.

DB에는 전체 500만 거래 대신 선택한 사례 주변의 제한된 거래만 넣는다. `meta.sourceGraphComplete=false`로 범위를 명시한다. 이후 전체 그래프와 더 큰 평가 표본에서 성능·크기 제한을 다시 검증해야 한다. 목표 시점 이전 7일만 사용하며, 각 계좌에서 최근 50거래, 기본 2홉, 최대 1,000거래를 적용한다. 제한 발생 여부는 `meta.truncated`에 기록한다. 동시각 거래는 분 단위 데이터이므로 같은 분 안에서 선후관계를 입증하지 않는다.

## 백엔드·룰 계약

`POST http://127.0.0.1:8091/v1/rules/score`

```json
{"graph": {"schemaVersion":"1.0", "targetTxId":"TX-...", "asOf":"2022-09-01T12:00:00", "nodes":[], "edges":[], "meta":{}}, "config":{"min_counterparties":3,"window_minutes":1440,"max_cycle_hops":4,"threshold":65}}
```

실제 edges에는 최소 대상 거래가 필요하다. 각 거래의 필수 속성은 `data-pipeline/graph_contract.py`의 `EDGE_FIELDS`를 따른다. 금액은 원본 십진수 문자열로 전달하고 통화를 분리한다. Neo4j의 `amountPaidExact`, `amountReceivedExact`가 원본 문자열을 보존한다. 합산은 Decimal로 수행한다.

```json
{"schemaVersion":"1.0","txId":"TX-...","ruleScore":65,"flagged":true,"findings":[{"ruleId":"FAN_IN","score":65,"accountKey":"1:A","evidenceTxIds":["TX-..."],"facts":{"counterpartyCount":3,"transactionCount":3,"totalsByCurrency":{"US Dollar":"3000.30"}}}]}
```

서비스 시작: `python rule-service/server.py`. 헬스체크: `GET /health`. 이 서버는 로컬 연동용이며 인증·배포 기능은 포함하지 않는다. Neo4j 연결 정보 없이 공통 JSON만 받는다.

`neo4j_mvp.py`는 DB 담당자의 적재·검증 도구다. 운영 요청의 DB 접근과 서브그래프 추출은 ADR-001에 따라 Spring 백엔드가 담당한다. 이 파일의 `TARGET_QUERY`, `NEIGHBOR_QUERY`와 공통 계약을 백엔드 구현에 활용할 수 있다. 시간·홉·전체 반환 수와 별개로 허브에서 후보를 정렬하는 비용은 남으므로 실제 대용량 그래프에서 EXPLAIN/PROFILE 검증이 필요하다.

## 룰과 평가 정책

- FAN-IN / FAN-OUT: 시간 창 내 서로 다른 상대 계좌 수가 기준 이상이고 대상 거래가 그 집합에 포함되는지 확인한다. 정상 급여·상거래도 탐지할 수 있으므로 의심 근거일 뿐 확정 판정이 아니다.
- CYCLE: 대상 거래가 닫는 방향성 순환을 찾는다. 선행 경로의 시각은 비감소 순서이며 기본 최대 4홉이다. 탐색 작업은 제한한다. 초기 거래는 순환이 완성되기 전에는 탐지되지 않을 수 있다.
- 점수: 집금·분산 65, 순환 90 중 최댓값. 초기 시연 점수이며 학습·보정된 확률이 아니다.
- 평가: validation에서 상대 계좌 수·시간 창·점수 기준을 탐색한다. 목표 Recall을 충족하는 설정 중 Precision과 후보 수를 기준으로 선택하고 test에 고정한다. TP·FP·FN·TN, Recall, Precision, F1을 함께 저장한다.
- `isLaundering`, `patternId`, `patternType`은 평가 파일에만 둔다. 룰 입력에 정답 필드가 들어오면 거부한다. 조회는 속성 화이트리스트로 반환한다.
- 새 MVP 적재 도구는 정답을 DB에 저장하지 않는다. 기존 전체 적재 경로의 정답 속성도 서비스 응답에 노출하지 않는다.

## 검증

```powershell
python -m unittest discover -s data-pipeline/tests -v
python -m unittest discover -s rule-service/tests -v
python data-pipeline/make_demo.py
python rule-service/evaluate.py --cases data/demo/cases.json --target-recall 0.9 --output artifacts/demo-rule-evaluation.json
```

`make_demo.py`의 데이터는 직접 만든 synthetic smoke fixture다. IBM 성능 지표와 섞어서 보고하지 않는다. 실제 적재 후에는 같은 txId의 DB JSON과 메모리 추출 JSON을 비교해 전처리·조회 연결을 확인한다.

실제 DB 대조: `python scripts/verify-mvp.py`. 이번 실행 결과와 실패 분석은 [1차 실행 기록](db-rule-progress.md)에 남긴다. 관리자 도구와 검증용 HTTP 호출은 로컬 네트워크 접근이 가능한 터미널에서 실행한다.

## 참고 범위

Neo4j P2P Fraud Detection의 그래프 분석 흐름, APOC의 제한된 탐색 개념, IBM Multi-GNN의 거래 그래프 입력·평가 구성을 참고했다. 외부 소스 코드를 복사하지 않았으며 초기 MVP에는 GDS/APOC 설치가 필요하지 않다.

- https://github.com/neo4j-product-examples/demo-fraud-detection-with-p2p
- https://github.com/neo4j/apoc
- https://github.com/IBM/Multi-GNN

## 다음 통합 작업

- Spring Graph API에서 같은 입력 계약과 과거 조회 정책 구현.
- Rule API에 ML 점수 API를 연결하고 백엔드에서 점수 정규화·결합.
- 거래 근거 ID와 계산된 사실을 LLM 설명·보고서에 연결.
- 더 큰 시간순 평가 집합과 전체 그래프에서 Recall, 오탐, 지연시간, 비용 측정.
