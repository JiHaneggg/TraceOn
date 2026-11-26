# 거래 탐지 벤치마크

같은 IBM HI-Small 합성 거래에서 룰 단독, 룰+ML, 에이전트 단독, 룰 또는 에이전트의 합집합을 비교한다. 후보 선별 지표와 보고서 Judge의 의미 검증 지표는 각각 기록한다. 모델에는 현재 거래의 목표 정답, 기존 룰 점수나 후보 판정을 전달하지 않는다.

## 조회 범위 비교

기존 [룰·ML 결합 새 표본](recipient-hybrid-evaluation.md)의 250건을 재현했다. 양성 100건과 정상 150건이며, 평가 대상은 거래다. 네 탐지 방식은 같은 시간창·원본 거래와 선택한 그래프 범위를 공유한다. 조회 조건은 대상 시점까지 7일, 계좌별 50건, 그래프당 최대 1,000건이다. 선택 확장은 기존 2홉 조회에 포함된 계좌 중 가까운 계좌부터 최대 8개를 확인하고, 거래가 생략된 계좌의 과거 페이지를 최대 2개 더 읽는 오프라인 정책이다.

| 조회 방식 | 그래프별 거래 수 합계 | 2홉에서 없던 패턴 근거 | 기존 룰 미탐 / 양성 100건 |
| --- | ---: | --- | ---: |
| 2홉 | 64,306 | 기준 | 12 |
| 3홉 | 146,574 | 0 | 12 |
| 선택 확장 | 67,616 | 집금 1, 집금 후 분산 7 | 12 |

세 방식의 그래프는 각각 대상 거래 ID, 시점, 기간, 2홉 원본 거래 값을 모두 보존했다. 선택 확장은 총 252페이지를 읽었다. 새 근거 8건 중 양성은 1건이었고, 기존 룰에서 이미 후보로 분류된 사례였다. 이번 250건 비교의 공통 기본 입력은 2홉이다. 분산 후 집금 등 희소 유형의 성능은 해당 유형을 포함한 별도 검수 표본으로 확인한다.

2홉에서 대상 거래의 양쪽 계좌가 2-core에 포함될 때만 후보로 삼으면 후보는 41건이고 양성 77건을 놓친다. 기존 룰의 후보 198건·미탐 12건과 함께 비교한다. 룰 후보, 복합 패턴, 2-core 후보를 합치면 후보 211건·미탐 9건으로 보정 Recall은 87.78%에서 90.77%로 오르고 오탐률은 73.33%에서 80.00%로 오른다. 후보 필터의 효과는 같은 250건에서 측정했다.

## 에이전트 입력과 집계

에이전트는 대상 거래, 원본 금액과 통화·송금 방향·시각, 동일 그래프의 순환·집금·분산·복합 패턴의 근거 거래 ID, 밀도·조회 범위 정보를 받는다. 전체 거래가 64건 이하이면 모두 제시한다. 64건을 넘으면 대상·패턴 근거를 먼저 보존하고 대상 인접 거래와 최근 거래 순으로 채운다. 표시한 거래 수와 원래 그래프 거래 수를 함께 전달한다. 룰과 ML은 선택한 범위의 전체 조회 결과로 계산한다.

에이전트는 검토 후보, 후보 아님, 판단 보류 중 하나를 내며 후보는 대상 거래 ID가 포함된 실제 근거를 제시한다. 구조 오류와 모델 요청 실패는 무점수로 집계한다. 판단 보류·무점수인 양성 중 후보로 올라가지 않은 거래는 에이전트 탐지의 미탐에 포함한다. 결합 방식은 룰 후보 또는 에이전트 후보의 합집합이며, 두 방법에 같은 에이전트 응답을 사용한다. 판정 비교에는 250건 전부를 포함해 기존 룰에서 빠진 거래도 평가한다.

## 실행

저장소 루트에서 실행한다. 조회 범위 조사와 모의 비교에는 모델 키가 필요 없다. 입력 데이터와 실행 결과는 Git에서 제외한 `artifacts` 아래에 저장한다.

```powershell
python -B scripts/evaluate-transaction-detection.py prepare --freeze artifacts/recipient-hybrid-frozen-20261123.json --evaluation artifacts/recipient-hybrid-fresh-20261123.json --graph-scope 2hop --output artifacts/detection-comparison/shared-2hop-next.json
python -B scripts/evaluate-transaction-detection.py compare --packet artifacts/detection-comparison/shared-2hop-next.json --mode mock --output artifacts/detection-comparison/mock-next.json
```

실측 비교는 키와 모델을 설정한 PowerShell에서 같은 패킷으로 시작한다. 중단한 실행은 같은 출력 경로로 호출하면 이미 저장한 판정 이후부터 재개된다. 인증·쿼터 문제에서 멈추며 완료한 사례와 호출 시도 수를 보존한다.

```powershell
python -B scripts/evaluate-transaction-detection.py compare --packet artifacts/detection-comparison/20261123-shared-2hop-v2.json --mode openai --output artifacts/detection-comparison/20261123-four-way-openai.json
```

첫 전체 실행은 거래당 최대 1회, 총 250회 호출을 계획한다. 결과는 Recall, 정상 오탐률, 후보 수, 모델 호출 시도 수와 보류·무점수 건수를 기록한다. 기존 250건 실험을 비교 기준으로 삼고, 별도의 새 표본으로 운영 성능을 평가한다.

## 첫 실제 실행과 무점수 재평가

동결한 250건에서 `gpt-6-luna`를 250회 호출 시도했다. 149건에서 유효한 판정(후보 135건, 후보 아님 14건)을 얻었다. 첫 결과의 무점수 101건은 요청 연결 실패 100건과 근거 거래 ID 검증 실패 1건이다. 아래 지표는 무점수인 양성 중 각 방식이 후보로 올리지 못한 건을 미탐에 포함했다.

| 방식 | 보정 Recall | 정상 오탐률 | 후보 수 | 이상거래 미탐 |
| --- | ---: | ---: | ---: | ---: |
| 룰 | 87.78% | 73.33% | 198 | 12 |
| 룰+ML | 83.80% | 60.67% | 175 | 16 |
| 에이전트 단독 | 56.00% | 52.00% | 135 | 43 |
| 룰 또는 에이전트 | 94.75% | 84.67% | 222 | 5 |

첫 결합 결과에서 기존 룰이 놓친 양성 7건을 추가 탐지했고 정상 후보 17건도 늘었다. 실제 에이전트 후보와 결합 성능의 최종 비교는 동일 입력으로 무점수 사례를 재평가한 뒤 확정한다.

```powershell
python -B scripts/evaluate-transaction-detection.py compare --packet artifacts/detection-comparison/20261123-shared-2hop-v2.json --mode openai --retry-unscored-from artifacts/detection-comparison/20261123-four-way-openai.json --output artifacts/detection-comparison/20261123-four-way-openai-retry.json
```

유효한 기존 판정 149건을 보존하고 무점수 101건만 같은 모델과 동일 요청으로 실행한다. 연결·인증·쿼터 문제가 반복되면 해당 거래 앞에서 멈추고, 같은 명령을 실행하면 저장된 판정 다음부터 이어간다. 첫 결과는 원본 파일에 남으며, 새 결과에는 재사용 건수와 재평가 호출 시도 수를 각각 기록한다.

## 무점수 재평가 후 네 가지 방식의 결과

같은 250건·같은 2홉 그래프에서 첫 실행의 유효 판정 149건을 재사용했다. 무점수 101건에 102회 호출을 시도해 100건을 복구했다. 누적 호출 시도는 352회다. 유효한 에이전트 판정은 후보 230건, 후보 아님 19건이다. 유효 판정의 요청 평균 지연은 8.08초였다.

| 방식 | 보정 Recall | 정상 오탐률 | 후보 수 | 이상거래 미탐 |
| --- | ---: | ---: | ---: | ---: |
| 룰 | 87.78% | 73.33% | 198 | 12 |
| 룰+ML | 83.80% | 60.67% | 175 | 16 |
| 에이전트 단독 | 94.03% | 90.67% | 230 | 6 |
| 룰 또는 에이전트 | 99.01% | 97.33% | 245 | 1 |

룰이 놓친 양성 12건 중 에이전트는 11건을 후보에 올렸다. 결합 방식은 기존 룰보다 양성 11건과 정상 36건을 더 올렸다. 결합에서 검토량이 245/250건이 된 만큼, 다음 비교는 동일한 후보 건수 한도에서 확보하는 Recall을 본다. 순위·판정 기준은 개발 표본에서 정한 후 별도 최종 표본에 적용한다.

남은 무점수 1건은 근거 거래 ID 검증에 실패한 정상 라벨 거래다. 룰이 그 거래를 후보에 올렸으므로 결합 결과는 영향을 받지 않는다. 에이전트 단독 오탐률은 그 거래가 후보로 판정될 경우 최대 91.33%다. 첫 실행과 재평가 결과는 각각 `artifacts/detection-comparison/20261123-four-way-openai.json`, `artifacts/detection-comparison/20261123-four-way-openai-retry.json`에 보존한다.

## 동일 검토량 가중치 실험

실제 단일 에이전트 판정이 포함된 기존 250건을 가중치 탐색에 재사용했다. 대상 거래마다 정규화한 룰 점수 또는 룰+ML 점수에 에이전트 후보 신호를 섞었다. 에이전트가 후보로 올리면 1, 나머지는 긍정표 0이다. 기존 무점수 1건에서도 룰 또는 룰+ML 점수는 유지한다. 룰 측과 에이전트 후보 신호의 몫을 0:100부터 100:0까지 5% 단위로 바꿨다. 동점은 모든 방식에서 대상 거래 ID의 고정 해시로 정한다.

| 동일 검토 한도 | 룰+ML Recall / 오탐률 | 선택한 가중치 후보 | 혼합 Recall / 오탐률 |
| ---: | --- | --- | --- |
| 125건 | 71.83% / 35.33% | 룰+ML 50 · 에이전트 50 | 71.87% / 35.33% |
| 150건 | 79.82% / 46.67% | 룰+ML 95 · 에이전트 5 | 79.82% / 46.67% |
| 175건 | 83.80% / 60.67% | 룰+ML 90 · 에이전트 10 | 83.80% / 60.67% |
| 198건 | 87.78% / 73.33% | 룰+ML 50 · 에이전트 50 | 94.03% / 69.33% |

주 비교 한도 175건은 기존 룰+ML의 후보 개수에서 정했다. 175건에서는 혼합 방식과 룰+ML 모두 양성 84건을 잡고 정상 91건을 올렸다. 198건의 혼합에서는 양성 94건과 정상 104건을 올려 같은 한도의 룰+ML보다 양성을 6건 더 잡고 정상 후보를 6건 줄였다. 이 표는 혼합 비율을 선택하는 탐색 결과다. 대안별 지표와 동점 수는 `artifacts/detection-comparison/20261123-weight-lock.json`에 보존한다.

새 평가 표본은 같은 IBM 합성 원본에서 양성 100건·정상 150건으로 뽑았다. 이전 실험과 목표 거래 및 알려진 패턴 가족의 중복은 0개다. 새 표본의 에이전트 답변에 탐색 단계에서 잠근 각 한도의 가중치를 적용한다. 아래 명령은 로컬에 준비한 새 패킷의 실제 판정을 저장한 뒤 가중치 성능을 계산한다.

```powershell
python -B scripts/evaluate-transaction-detection.py compare --packet artifacts/detection-comparison/20261123-weight-holdout-packet.json --mode openai --output artifacts/detection-comparison/20261123-weight-holdout-openai.json
python -B scripts/evaluate-detection-weights.py evaluate --packet artifacts/detection-comparison/20261123-weight-holdout-packet.json --result artifacts/detection-comparison/20261123-weight-holdout-openai.json --lock artifacts/detection-comparison/20261123-weight-lock.json --output artifacts/detection-comparison/20261123-weight-holdout-result.json
```

### 별도 250건에서 고정 가중치 검증

탐색에 쓰지 않은 양성 100건·정상 150건을 `gpt-6-luna`에 250회 요청했다. 후보 217건, 후보 아님 30건, 근거 거래 ID 검증 실패로 무점수 3건이다. 무점수는 기존 실험과 동일하게 에이전트 긍정표 0으로 처리하고 룰 신호를 유지했다. 탐색에서 잠근 비율을 각 한도에 그대로 사용했다.

| 동일 검토 한도 | 룰+ML 보정 Recall / 정상 오탐률 | 고정 혼합 비율 | 혼합 보정 Recall / 정상 오탐률 |
| ---: | --- | --- | --- |
| 125건 | 71.70% / 36.00% | 룰+ML 50 · 에이전트 50 | 69.04% / 37.33% |
| 150건 | 81.02% / 46.00% | 룰+ML 95 · 에이전트 5 | 79.02% / 47.33% |
| 175건 | 85.02% / 60.00% | 룰+ML 90 · 에이전트 10 | 84.02% / 60.67% |
| 198건 | 91.01% / 71.33% | 룰+ML 50 · 에이전트 50 | 88.01% / 73.33% |

주 한도 175건에서는 룰+ML이 양성 85건·정상 90건, 혼합이 양성 84건·정상 91건을 후보로 올렸다. 탐색에서 이득이 있던 198건에서도 새 표본에서는 룰+ML이 양성 91건·정상 107건, 혼합이 양성 88건·정상 110건이었다.

무점수 3건의 후보 신호를 0 또는 1로 가정한 8가지 조합으로 민감도를 확인했다. 혼합 방식은 125·150·198건 한도에서 모든 조합의 Recall이 룰+ML 아래였다. 175건에서는 4개 조합이 동일하고 4개 조합이 낮았다. 이번 고정 비율의 탐색 이득은 새 표본에서 재현되지 않았다. 새 점수나 호출 정책은 개발 표본에서 설계한 뒤 다른 표본으로 검증한다.

결과는 `artifacts/detection-comparison/20261123-weight-holdout-openai.json`과 `artifacts/detection-comparison/20261123-weight-holdout-result.json`에 보관한다.

## 거래 위험 등급 실험

탐지 에이전트는 대상의 2홉 거래 그래프와 관찰 사실을 받아 높음, 중간, 낮음, 판단 보류를 반환한다. 정답 라벨과 룰·ML 점수는 평가 파일에서만 사용한다. 등급과 함께 제시하는 근거 거래 ID는 입력에 있는 ID로 제한한다. 높음은 대상과 추가 근거 거래가 있어야 한다. 집금이나 높은 연결 수만 관찰되면 높음의 근거로 사용하지 않도록 지시한다.

### 개발 파일럿

기존 가중치 검증에 사용한 250건을 이번 실험의 개발 표본으로 재사용한다. 룰+ML 상위 175건에 포함된 이상거래, 그 순위에서 누락된 이상거래, 정상 집금, 기타 정상 거래를 각각 6건 사용한다. 24건의 등급 분포와 근거를 보고 누락 사례와 정상 집금이 어떻게 구분되는지 검토한다.

```powershell
$comparison = 'artifacts/detection-comparison'
$gradeScript = 'scripts/evaluate-transaction-risk-grades.py'
python -B $gradeScript prepare-pilot --packet "$comparison/20261123-weight-holdout-packet.json" --output "$comparison/20261123-risk-grade-pilot-packet.json"
python -B $gradeScript grade --packet "$comparison/20261123-risk-grade-pilot-packet.json" --mode openai --output "$comparison/20261123-risk-grade-pilot-openai.json"
```

실제 요청을 보내는 PowerShell 세션에 `OPENAI_API_KEY`와 `OPENAI_MODEL`이 필요하다. 일시적 연결·쿼터 문제로 중단되면 같은 명령으로 저장된 위치에서 이어간다. 모의 실행은 입출력과 검증 흐름 확인용이다. 실행 요약에는 등급별 건수와 API 요청 시도 수가 기록된다.

`gpt-6-luna`의 실제 24건 판정은 높음 5건과 중간 19건이었다. 룰+ML이 놓친 이상거래 6건은 모두 중간이었다. 높음 5건 중 2건은 정상 라벨(정상 집금과 기타 정상 각 1건)이었다. 미탐 6건은 모두 그래프 조회가 잘렸고, 2홉·3홉·선택 확장이 기존 그래프 상한 1,000건에 도달해 새 패턴을 추가하지 못했다. 판정한 24건에만 결합 점수를 적용하고 나머지 226건에는 룰+ML 점수를 유지해 순위를 다시 계산했을 때, 에이전트 비중 25%에서 미탐 1건, 50%에서 3건이 상위 175건으로 들어왔다. 전체 개발 표본에 등급을 적용한 결과로 가중치를 결정한다.

### 동일 검토량 검증

파일럿을 검토한 뒤 개발 표본 250건에 확정된 등급 기준을 적용한다. 높음·중간·낮음의 순위 신호는 각각 1·0.5·0이다. 보류 또는 무점수에서는 해당 거래의 룰+ML 점수를 유지한다. 검토 한도 175건에서 에이전트 비중 5·10·25·50%의 결합안을 룰+ML 기준선과 비교한다. 개발 Recall, 낮은 오탐률, 낮은 에이전트 비중 순으로 하나의 결합안을 잠근다. 개발에서 기준선을 개선했는지도 함께 기록한다.

```powershell
python -B $gradeScript grade --packet "$comparison/20261123-weight-holdout-packet.json" --mode openai --reuse-pilot-from "$comparison/20261123-risk-grade-pilot-openai.json" --output "$comparison/20261123-risk-grade-dev-openai.json"
python -B $gradeScript tune --packet "$comparison/20261123-weight-holdout-packet.json" --result "$comparison/20261123-risk-grade-dev-openai.json" --output "$comparison/20261123-risk-grade-lock.json"
python -B $gradeScript prepare-evaluation --freeze artifacts/recipient-hybrid-frozen-20261123.json --previous-packet "$comparison/20261123-shared-2hop-v2.json" --development-packet "$comparison/20261123-weight-holdout-packet.json" --output "$comparison/20261123-risk-grade-holdout-packet.json"
python -B $gradeScript grade --packet "$comparison/20261123-risk-grade-holdout-packet.json" --mode openai --output "$comparison/20261123-risk-grade-holdout-openai.json"
python -B $gradeScript evaluate --packet "$comparison/20261123-risk-grade-holdout-packet.json" --result "$comparison/20261123-risk-grade-holdout-openai.json" --lock "$comparison/20261123-risk-grade-lock.json" --output "$comparison/20261123-risk-grade-holdout-result.json"
```

개발 표본의 `--reuse-pilot-from`은 같은 모델·등급 기준으로 같은 그래프를 판정한 파일럿 결과를 재사용한다. 유효한 판정 24건을 그대로 적용하므로 추가로 호출할 거래는 226건이다. 요약에서 재사용 건수와 새 호출 시도 수를 함께 확인할 수 있다.

새 평가 표본은 앞선 두 패킷의 목표 거래와 알려진 패턴 가족을 제외한 IBM 합성 거래에서 이상 100건·정상 150건을 뽑는다. 모델과 가중치를 개발 단계에서 잠근다. 최종 결과에는 각 방식의 보정 Recall, 정상 오탐률, 이상거래 적중·미탐 건수, 검토 건수와 실제 요청 시도 수가 남는다. 이미 만든 패킷은 그대로 사용할 수 있다.

### 전체 개발 표본의 가중치 선택

`gpt-6-luna`로 판정한 250건에는 파일럿 재사용 24건과 새 요청 226건이 포함된다. 판정은 높음 69건, 중간 178건, 낮음 2건, 판단 보류 1건이었다. 응답 검증에 실패한 건은 0건이다.

| 에이전트 비중 | 보정 Recall | 정상 오탐률 | 적중 | 정상 후보 | 미탐 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0% (룰+ML) | 85.02% | 60.00% | 85 | 90 | 15 |
| 5% | 85.02% | 60.00% | 85 | 90 | 15 |
| 10% | 84.02% | 60.67% | 84 | 91 | 16 |
| 25% | 84.02% | 60.67% | 84 | 91 | 16 |
| 50% | 81.02% | 62.67% | 81 | 94 | 19 |

모든 방식의 검토량은 175건이다. 결합안 중 가장 높은 Recall을 기록한 비율인 룰+ML 95%와 에이전트 5%를 독립 검증용으로 고정했다. 5%에서 검토 대상은 정상 거래 한 건이 다른 정상 거래 한 건으로 바뀌었다. 이상거래 적중과 미탐은 기준선과 같았다. 이 개발 결과를 기준으로 운영 검토 순위에는 기존 룰+ML을 유지한다.

### 독립 표본 최종 결과

별도 IBM 합성 거래 250건에서 `gpt-6-luna`는 높음 64건, 중간 180건, 낮음 5건, 판단 보류 1건을 반환했다. 응답 검증 실패는 0건이었다. 보류 거래에는 기존 룰+ML 점수를 적용했다.

| 순위 방식 | 검토량 | 보정 Recall | 정상 오탐률 | 적중 | 정상 후보 | 미탐 | 기록된 모델 호출 시도 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 룰+ML | 175 | 83.05% | 61.33% | 83 | 92 | 17 | 0 |
| 룰+ML 95% · 에이전트 등급 5% | 175 | 83.05% | 61.33% | 83 | 92 | 17 | 250 |

두 방식이 잡은 이상거래는 동일한 83건이었다. 결합으로 정상 후보 두 건이 다른 정상 거래 두 건으로 바뀌었다. 고정 가중치로 검토 순위를 개선한 효과는 확인되지 않았다. 시연의 검토 순위에는 룰+ML을 유지하고, RAG 설명과 보고서 검증은 별도 단계에서 평가한다. 최종 수치는 로컬의 `artifacts/detection-comparison/20261123-risk-grade-holdout-result.json`에 저장했다.
