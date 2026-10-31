# 데이터 모델 (초안)

## 1. 원천 데이터: IBM AML

| 파일 | 내용 |
|---|---|
| `*_Trans.csv` | 전체 거래. 탐지 대상 |
| `*_Patterns.txt` | 세탁 거래를 유형별 묶음(BEGIN/END LAUNDERING ATTEMPT)으로 나열 |
| `*_accounts.csv` | 계좌 → 은행·소유 개체(Entity) 매핑 |

거래 컬럼: `Timestamp, From Bank, Account, To Bank, Account, Amount Received, Receiving Currency, Amount Paid, Payment Currency, Payment Format, Is Laundering`

패턴 유형: FAN-OUT, FAN-IN, GATHER-SCATTER, SCATTER-GATHER, CYCLE, BIPARTITE, STACK (데이터에 따라 RANDOM 포함)

### 전처리 시 주의점
- **계좌 식별자는 (은행, 계좌번호) 쌍**으로 만든다. 은행 ID의 앞자리 0(`00952`, `0111632`)이 의미를 가지므로 문자열로 읽는다.
- **통화가 섞여 있다** (US Dollar, Euro, Yuan, Yen, Ruble ...). 금액 비교·임계값 규칙은 공통 기준 통화로 환산한 값(`amountUsd`)을 사용해야 한다.
- 송금 금액과 수취 금액·통화가 다를 수 있다 (환전 거래).
- 같은 계좌로 보내는 자기 거래가 있을 수 있다.
- 시각에 시간대가 없다. 분 단위 `LocalDateTime`으로 다룬다.
- 세탁 거래 비율이 매우 낮다 (클래스 불균형).

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
| Account | `accountKey` | `"{bankId}:{accountNumber}"`, 유일 |
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
