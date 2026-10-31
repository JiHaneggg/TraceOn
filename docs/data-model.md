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

## 3. Subgraph

### 3.1 HI-Small 분석 결과 (2026-10-31)

표본: 패턴 거래 400건, 정상 거래 400건. 자기 거래(self-loop)는 탐색에서 제외. 포함률 = 대상 거래가 속한 패턴의 거래 중 Subgraph에 들어온 비율.

**허브 계좌**
- 계좌별 거래 수(자기 거래 제외): 중앙값 3, p99 111, 최대 169,756
- 거래 1만 건 이상 계좌 12개가 전체 거래의 9.5%에 연결됨
- 세탁 거래도 허브와 연결됨: 세탁 거래 끝점의 최대 거래 수 p90 = 14,277 → **허브를 제외하면 세탁 거래를 놓친다. 제외가 아니라 제한해야 한다.**

**패턴 지속 기간**: 중앙값 3.1일, p90 4.6일, 최대 8.4일 → 시간 창 ±7일이면 패턴 전체가 들어온다.

**제한 없는 확장** (시간 창 ±7일)

| hop | 엣지 p50 / p90 / 최대 | 포함률 평균 |
|---|---|---|
| 1 | 50 / 133 / 313 | 65% |
| 2 | 432 / 80,037 / 190,932 | 77% |
| 3 | 76,535 / 292,532 / 694,335 | 80% |

2 hop부터 허브 때문에 폭증한다.

**계좌별 이웃 거래 수 제한 K** (각 계좌에서 대상 거래 시각과 가까운 순으로 K개만 따라감)

| hop | 시간 창 | K | 엣지 p50 / p90 / 최대 | 포함률 중앙값(평균) |
|---|---|---|---|---|
| 1 | ±7일 | 50 | 50 / 88 / 99 | 60% (64%) |
| 2 | ±7일 | 20 | 152 / 266 / 394 | 69% (64%) |
| **2** | **±7일** | **50** | **298 / 644 / 1,045** | **100% (76%)** |
| 2 | ±7일 | 100 | 374 / 845 / 2,211 | 100% (77%) |
| 3 | ±7일 | 20 | 546 / 1,048 / 1,701 | 77% (67%) |

**2 hop + ±7일 + K=50**이 제한 없는 2 hop과 포함률은 같으면서(76% vs 77%) 최대 크기를 190,932 → 1,045로 줄인다.

**유형별 포함률** (2 hop, ±7일, K=20): SCATTER-GATHER 89%, FAN-IN 84%, FAN-OUT 83%, GATHER-SCATTER 70%, CYCLE 65%, RANDOM 60%, BIPARTITE 18%, STACK 17%.
STACK·BIPARTITE는 한 패턴이 서로 연결되지 않은 여러 거래 묶음으로 되어 있어(Patterns 파일 참고) hop을 늘려도 포함률이 낮다. 파라미터 문제가 아니라 패턴 정의의 특성이다.

### 3.2 기본값 제안 (팀 협의 필요)

| 파라미터 | 기본값 | 근거 |
|---|---|---|
| 시작점 | 대상 거래의 송금·수취 계좌 | 탐지 단위 = 거래 |
| hops | 2 (요청 시 1~3) | 3 hop은 포함률 이득이 작고 크기가 커짐 |
| 시간 창 | 대상 거래 기준 ±7일 | 패턴 최대 지속 8.4일 |
| 계좌별 최대 거래 수 K | 50 (대상 시각에 가까운 순) | 허브 폭증 방지, 포함률 유지 |
| 자기 거래 | 탐색에서 제외 (대상 거래 자체는 허용) | 59만 건 중 세탁 11건, 구조 정보 없음 |
| 정답 속성 | 응답에서 제거 (`isLaundering`, `patternId`, `patternType`) | 정답 누출 방지 |

미결정
- 시간 창 방향: 양방향(±7일) vs **과거만**(대상 이전 7일). 과거만 쓰면 "실시간 탐지" 조건에 맞지만 포함률이 낮아진다.
- LLM 입력: 엣지 수백 개를 그대로 넣으면 토큰이 과다하다. LLM에는 요약(계좌별 집계, 상위 경로)을 별도로 만든다.
