# SAML-D 반복 현금 입금 검증

같은 송신·수취 계좌 쌍의 현금 입금이 7일 내 대상 거래를 포함해 4건 이상이면 새 검토 후보로 올리는 조건을 평가했다. 직전 거래는 시각이 대상보다 앞서야 하고, 조회 시작점과 같은 시각의 거래는 포함했다. 금액 열은 점수에 사용하지 않았다. SAML-D에는 원본 `Amount` 한 열이 있어 방향별 금액과 통화별 합계를 미확인으로 유지했다. [SAML-D 제작자 자료](https://github.com/BOztasUK/Anti_Money_Laundering_Transaction_Data_SAML-D)

## 개발과 새 표본

- 2023년 6월 29일~7월 26일 개발 구간에서 7일·4건 조건으로 Smurfing 38/71건과 정상 현금 입금 4/19,357건이 표시됐다. 적중 거래는 11개 방향성 계좌 쌍에 걸쳐 있었다.
- 검증 표본은 미사용 기간인 **2023년 6월 1일~6월 28일**에서 이상거래 100건과 정상 거래 150건을 추출했다. 기존 외부 평가 대상과 개발 구간의 현금 입금에서 사용한 방향성 계좌 쌍 12,830개를 새 표본의 대상 선택에서 제외했다. 대상 거래의 직전 7일 이력을 유지했다.
- 기존 IBM 룰과 ML 모델을 고정했다. 반복 입금 조건을 충족한 거래가 상위 175건 밖에 있으면 기존 순위가 높은 거래부터 최대 10건을 올렸다. 그 수만큼 상위 175건의 하위 거래를 제외했다. 조건과 자리 수는 새 표본의 결과를 보기 전에 고정했다.

## 결과

| 방식 | 검토량 | 이상거래 적중 | 미탐 | 보정 Recall | 정상 오탐률 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 기존 룰+ML | 175 | 73 | 27 | 73.00% | 68.00% |
| 반복 현금 입금 후보 추가 | 175 | 77 | 23 | 77.00% | 65.33% |

검토선 밖의 반복 입금 후보는 4건이었고, 3개 계좌 쌍의 Smurfing 거래였다. 기존 검토 대상에서 빠진 4건은 모두 정상 라벨이었다. 판정이 바뀐 8건과 중복을 제거한 근거 원본 18행을 CSV에서 대조했다. 앞서 측정한 일반 룰 경보 재배정은 기존 룰 점수로 생성된 후보를 사용했고, 이번 비교는 반복 입금으로 생성된 후보를 사용했다.

이번 개선은 하나의 SAML-D 합성 구간에서 확인한 결과다. 검증 구간은 개발 구간보다 앞선 날짜이며, 새로 잡은 거래가 3개 계좌 쌍에 걸쳐 있다. 보고한 Recall과 오탐률은 표본 추출 가중치를 적용한 값이다. 실제 은행 자료에의 적용 여부는 별도 검증이 필요하다.

## 로컬 재현

Python에 `polars`가 필요하다. 아래 예시는 기존 IBM 룰·ML 동결 파일, SAML-D 원본 CSV, 앞서 만든 외부 평가 표본과 반복 입금 개발 기록이 각 로컬 폴더에 있는 경우다. 새 출력 폴더에는 프로토콜, 표본, 평가 결과와 검증용 인덱스가 저장된다.

```powershell
$source = 'artifacts/saml-d-20261127'
$development = 'artifacts/saml-d-repeated-deposits-20261127'
python -B scripts/evaluate-saml-d-repeat-deposits.py `
  --source-csv "$source/SAML-D.csv" `
  --previous-sample "$source/sample.json" `
  --development-protocol "$development/protocol.json" `
  --development-candidate "$development/candidate.json" `
  --model-freeze artifacts/recipient-hybrid-frozen-20261123.json `
  --rule-freeze artifacts/rule-v2-frozen.json `
  --output-dir artifacts/saml-d-repeat-holdout-next
```
