# 데이터 모델 (초안)

## 1. 원천 데이터: IBM AML

| 파일 | 내용 |
|---|---|
| `*_Trans.csv` | 전체 거래. 탐지 대상 |
| `*_Patterns.txt` | 세탁 거래를 유형별 묶음(BEGIN/END LAUNDERING ATTEMPT)으로 나열 |
| `*_accounts.csv` | 계좌 → 은행·소유 개체(Entity) 매핑 |

거래 컬럼: `Timestamp, From Bank, Account, To Bank, Account, Amount Received, Receiving Currency, Amount Paid, Payment Currency, Payment Format, Is Laundering`

패턴 유형: FAN-OUT, FAN-IN, GATHER-SCATTER, SCATTER-GATHER, CYCLE, BIPARTITE, STACK (데이터에 따라 RANDOM 포함)

### HI-Small 실측 결과 (2026-10-31)

| 항목 | 값 |
|---|---|
| 거래 수 | 5,078,345 |
| 세탁 거래 (`Is Laundering=1`) | 5,177 (0.102%) |
| 기간 | 2022-09-01 00:00 ~ 2022-09-18 16:18 |
| 계좌 수 (거래에 등장) | 515,080 |
| 자기 거래 (같은 계좌로 송금) | 591,212 (대부분 Reinvestment) |
| 환전 거래 (송금·수취 통화 다름) | 72,170 |
| 완전히 같은 행 | 9건 (모두 정상 거래) |
| 통화 | 15종 (Bitcoin 포함) |
| 결제 방식 | Cheque, Credit Card, ACH, Cash, Reinvestment, Wire, Bitcoin |
| 세탁 거래의 결제 방식 | ACH 4,483 / Cheque 324 / Credit Card 206 / Cash 108 / Bitcoin 56 |
| Patterns 파일 거래 | 3,209건, 전부 Trans.csv와 1:1 매칭 |
| 패턴에 속하지 않는 세탁 거래 | 1,968건 (세탁 거래의 38%) |

### 전처리 규칙
- **계좌 식별자 = (은행 ID, 계좌번호)**. 같은 계좌번호가 서로 다른 은행에 존재하는 경우가 8건 있다.
- **은행 ID는 앞자리 0을 제거해 정수로 정규화**한다. Trans.csv의 은행 ID는 0 패딩이 일정하지 않으며(`014`와 `0014`가 같은 은행), 정규화하면 accounts.csv의 Bank ID와 일치한다.
- **txId는 Trans.csv의 행 번호로 부여**한다. 완전히 같은 행 9건도 서로 다른 거래로 취급한다.
- **통화 환산(amountUsd)**: 데이터셋 내부 환전 거래에서 계산한 통화별 환율 중앙값을 사용한다 (외부 환율 데이터 불필요, 재현 가능). 예: Euro 0.8534, Yuan 6.6976, Yen 105.4, Bitcoin 0.0000842 (1 USD당).
- 시각에 시간대가 없다. 분 단위 `LocalDateTime`으로 다룬다.
- `patternType`이 없는 세탁 거래가 많으므로, 유형별 분석은 패턴이 있는 거래로 한정한다.
- accounts.csv의 은행명(예: `Portugal Bank #4507`)에 국가가 들어 있는 경우가 있어, 해외 송금 규칙에 활용할 수 있다 (검토 필요).

## 2. Neo4j 그래프 스키마

```text
(:Account {accountKey, bankId, accountNumber})
   -[:TRANSFER {txId, timestamp, amountPaid, paymentCurrency,
                amountReceived, receivingCurrency, amountUsd,
                paymentFormat, isLaundering, patternType, patternId}]->
(:Account)
```

| 요소 | 속성 | 설명 |
|---|---|---|
| Account | `accountKey` | `"{정규화 bankId}:{accountNumber}"`, 유일 |
| TRANSFER | `txId` | 전처리에서 부여하는 고정 ID (`TX-00000001`) |
| TRANSFER | `isLaundering` | 정답 라벨. **탐지 로직(Rule/ML/LLM 입력)에는 노출하지 않는다** |
| TRANSFER | `patternType`, `patternId` | Patterns 파일과 매칭된 경우만. 평가·분석용 |

후속 확장: `(:Entity)-[:OWNS]->(:Account)` (accounts 파일 사용 시)

인덱스: `Account.accountKey` 유일 제약, `TRANSFER.txId`, `TRANSFER.timestamp` 인덱스

## 3. Subgraph (초안, 팀 협의 필요)

- 탐지 대상: 거래 1건 (`txId`)
- 시작점: 대상 거래의 송금·수취 계좌
- 확장: 두 계좌에서 k hop (k = 1~3) 이웃 거래
- 미결정: 시간 창(대상 거래 앞뒤 며칠), 최대 노드·엣지 수, 거래가 많은 허브 계좌 처리 방식
