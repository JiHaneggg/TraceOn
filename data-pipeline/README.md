# data-pipeline

IBM AML 원천 데이터를 **팀 공통 데이터**로 변환합니다. Rule / ML / LLM / Hybrid 모두 이 결과를 사용해야 거래 단위(`txId`) 비교가 가능합니다.

- Python 3.10 이상, 외부 패키지 없음
- 규칙과 결정 배경: [`docs/data-model.md`](../docs/data-model.md), ADR-006·007

## 실행

```powershell
cd C:\Users\82103\Desktop\git\TraceOn
python data-pipeline\preprocess.py --input C:\Users\82103\Downloads\archive --dataset HI-Small
```

결과는 `data/processed/HI-Small/`에 생성됩니다 (Git 제외). HI-Small 기준 약 1분, 약 650MB.

| 파일 | 내용 |
|---|---|
| `transactions.csv` | 거래 1건 = 1행 (`txId`, 시각, 송금/수취 계좌키, 원 금액·통화, `amountUsd`, 결제 방식, `isSelfLoop`, `isLaundering`, `patternId`, `patternType`) |
| `accounts.csv` | 거래에 등장한 계좌 (`accountKey`, 은행 ID, 계좌번호, 은행명, 개체 ID·이름) |
| `patterns.csv` | 세탁 패턴 묶음 (`patternId`, 유형, 설명, 거래 수) |
| `fx_rates.csv` | 통화별 환율 (1 USD당 통화량) |
| `manifest.json` | 입력 파일 SHA-256, 행 수, 환율, 규칙 버전 |

**팀원 간 데이터 일치 확인:** 각자 `manifest.json`의 `inputs.*.sha256`과 `counts`가 같으면 같은 데이터입니다.

**주의:** `isLaundering`, `patternId`, `patternType`은 정답 정보입니다. 학습·평가 외의 탐지 입력에 사용하면 안 됩니다.

## Neo4j 적재

`neo4j-admin database import`로 빈 데이터베이스에 한 번에 적재합니다 (ADR-008). **기존 그래프 데이터는 지워집니다.**

```powershell
cd C:\Users\82103\Desktop\git\TraceOn

# 1) import용 CSV 생성 -> data\import\HI-Small\ (약 30초, 약 740MB)
python data-pipeline\neo4j_export.py --dataset HI-Small

# 2) DB를 멈추고 import (컨테이너 안에서 실행)
docker compose stop backend neo4j
docker compose run --rm --no-deps neo4j neo4j-admin database import full neo4j `
  --overwrite-destination=true `
  --nodes=/import/HI-Small/accounts.csv `
  --relationships=/import/HI-Small/transfers.csv

# 3) 다시 시작 후 제약조건·인덱스 생성, 결과 확인
docker compose up -d
docker compose exec neo4j sh -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" -f /scripts/schema.cypher'
docker compose exec neo4j sh -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}" -f /scripts/verify.cypher'
```

`verify.cypher` 결과가 `manifest.json`의 counts와 같으면 정상입니다.

## 테스트

```powershell
cd data-pipeline
python -m unittest discover -s tests -v
```

## HI-Small 실행 결과 (2026-10-31)

거래 5,078,345 / 세탁 5,177 / 자기 거래 591,212 / 계좌 515,088 (모두 accounts.csv 정보 있음) / 패턴 370개, 패턴 거래 3,209건 전부 매칭
