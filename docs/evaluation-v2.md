# DB·룰 V2 평가 실행

이전 12개 사례는 기능 검증용이었다. V2는 시간 구간과 유형을 넓히고 검증 전용 조정과 새 최종평가를 분리한다. 실제 결과는 [2차 평가 기록](db-rule-v2.md)에 있다.

## 실행 순서

저장소 루트에서 실행한다. 출력 폴더·파일이 이미 있으면 덮어쓰지 않으므로 새 실험에는 새 버전 이름을 사용한다.

```powershell
python data-pipeline/evaluation_index.py --source data/processed/HI-Small/transactions.csv --output data/evaluation/HI-Small-source.sqlite3
python data-pipeline/prepare_evaluation.py --index data/evaluation/HI-Small-source.sqlite3 --output data/evaluation/HI-Small-v2
python rule-service/experiment.py tune --data data/evaluation/HI-Small-v2 --baseline artifacts/neo4j-rule-pilot.json --target-recall 0.9 --output artifacts/rule-v2-frozen.json
python rule-service/experiment.py holdout --data data/evaluation/HI-Small-v2 --frozen artifacts/rule-v2-frozen.json --output artifacts/rule-v2-holdout.json
python scripts/report-rule-v2.py
```

0.9는 이번 연구 실험의 목표다. 검증에서 미달하면 최대 Recall의 후보를 연구용으로 기록하고 미달 사실을 명시한다. 최종평가에서 미달하거나 오탐이 많으면 배포 기준으로 승격하지 않는다. API 기본값은 자동으로 변경되지 않는다.

## 데이터 규칙

- 원천 파일의 전역 txId를 유지한다. 정상·세탁을 구분해 표본을 뽑고 세탁은 8개 유형과 미지정 유형으로 층화한다.
- 각 층에서 고정 시드의 reservoir sampling을 사용한다. 전체 조건 적격 수/표본 수를 가중치로 저장한다.
- 이미 본 MVP 거래와 관련 패턴 가족을 제외한다. 최종평가에는 최종 구간에 시작된 새 패턴 가족만 허용한다.
- 모델 입력에는 정답 필드가 들어가지 않는다. 오프라인 조회는 전처리 전체에서 대상 시점 이전 거래만 가져온다.
- SQLite 파일은 대규모 원천을 반복해서 읽지 않기 위한 로컬 디스크 인덱스다. Neo4j 운영 스키마와 Spring의 접근 책임을 바꾸지 않는다.
- `validation.jsonl`과 `holdout.jsonl`은 물리적으로 분리한다. tune은 validation만 읽는다. holdout 실행 시 데이터와 규칙 코드, 고정 설정의 해시를 검증한다.
- 표본 Precision과 원본 분포 보정 Precision을 구분한다. 소규모 층화 표본의 비율 추정치이며 운영 보장을 뜻하지 않는다.

## API의 추가 설정

`fan_in_min_counterparties`, `fan_out_min_counterparties`를 각각 지정할 수 있다. 생략하면 기존 `min_counterparties` 값을 사용한다. 기존 클라이언트와 JSON 계약은 유지한다. 캐시 분석은 시간 창·홉 수가 같고 원래 증거 기준이 더 넓을 때만 재사용한다.

백엔드 연동에서 `rule-service/configs/rule-v2-research.json`의 `config`를 기존 요청의 `config`에 넣을 수 있다. 해당 설정은 오탐률이 높아 연구용이다.

## 테스트

```powershell
python -m unittest discover -s data-pipeline/tests -v
python -m unittest discover -s rule-service/tests -v
```

로컬 HTTP 테스트에는 루프백 네트워크 접근이 가능한 실행 환경이 필요하다.
