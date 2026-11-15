# RAG 검색 개선 — 48개 질문 비교 기록

같은 5문서·26청크와 같은 질문을 사용했다. 보존한 V2 원본 코드와 최종 검색 구현을 비교했다. 질문은 수작업 개발·회귀 세트이며 독립 최종평가가 아니다.

질문 48개: 관련 근거가 있는 질문 32개, 자료 범위 밖 질문 16개.

| 지표 | 기존 V2 | 개선 후 |
|---|---:|---:|
| 필요한 절 Recall@3 | 78.1% | 95.3% |
| 근거 하나 이상 발견 HitRate@3 | 81.2% | 96.9% |
| 대표 본문에 필요한 인용 포함 | 78.1% | 89.1% |
| 보조 본문 포함 필요한 인용 확보 | 78.1% | 95.3% |
| 필요한 모든 근거 확보 질문 비율 | 75.0% | 93.8% |
| 자료 없는 질문 보류 | 81.2% | 100.0% |
| 상위 결과 중복 자리 비율 | 11.8% | 0.0% |

상위 결과는 양쪽 모두 최대 3개다. 개선 후에는 같은 절의 보조 청크를 최대 2개씩 따로 제공한다. 관련 질문의 평균 본문 수는 2.19 → 3.72, 본문 문자 수는 1179 → 2098다. 보조 본문 포함 지표는 문맥 예산이 더 큰 조건임을 구분해야 한다.

## 남은 누락과 해석

- DEVELOPMENT-02: STR은 어떤 경우에 하는 의심거래 보고인가요
  - 미확보 정답: str-def / 의심되는 합당한 근거
- VERIFICATION-07: 의심스러운 합당한 사유를 적는 STR과 전산 자동 보고하는 CTR의 차이
  - 미확보 정답: str-def / 의심되는 합당한 근거

남은 두 질문은 기대한 STR 정의 절 대신 보고 대상·절차 절 등이 검색됐다. 정답 절·인용 기준을 그대로 적용해 누락으로 남겼다. 정답 후보의 완전성과 해석은 별도 검토가 필요하다.

## 전체 질문과 결과

| ID | 종류 | 질문 | 기존 → 개선 상태 | 개선 절 Recall@3 | 개선 문맥 근거 확보 |
|---|---|---|---|---:|---:|
| DEVELOPMENT-01 | paraphrase | 수수한 재산이 불법재산이라고 의심되는 합당한 근거가 있으면 어디에 보고하나요 | matched → matched | 100% | 100% |
| DEVELOPMENT-02 | paraphrase | STR은 어떤 경우에 하는 의심거래 보고인가요 | matched → matched | 0% | 0% |
| DEVELOPMENT-03 | paraphrase | 영업점 직원은 고객의 직업과 평소 거래상황을 고려하나요 | matched → matched | 100% | 100% |
| DEVELOPMENT-04 | paraphrase | 의심스러운 거래보고서에 보고기관과 거래상대방 외에 무엇을 기재하나요 | matched → matched | 100% | 100% |
| DEVELOPMENT-05 | paraphrase | 고객이 신원 정보 제공을 거부해 고객확인이 안 되면 신규 거래를 거절하나요 | matched → matched | 100% | 100% |
| DEVELOPMENT-06 | paraphrase | 실제 소유자와 거래 목적 및 자금 원천을 알아보는 고객확인의 의미 | matched → matched | 100% | 100% |
| DEVELOPMENT-07 | paraphrase | 새 계좌를 만들거나 일회성 금융거래를 할 때 신원을 확인하는 대상 | matched → matched | 100% | 100% |
| DEVELOPMENT-08 | paraphrase | 고위험 고객의 강화된 고객확인 EDD가 추가 확인하는 사항 | matched → matched | 100% | 100% |
| DEVELOPMENT-09 | str_ctr_confusion | STR과 CTR 중 합당한 의심 사유와 객관적인 현금 기준은 각각 어떤 제도인가요 | matched → matched | 100% | 100% |
| DEVELOPMENT-10 | str_ctr_confusion | 고액현금거래보고와 의심거래보고를 왜 구분하나요 | matched → matched | 100% | 100% |
| DEVELOPMENT-11 | str_ctr_confusion | CTR 기준은 같은 금융회사에서 같은 사람이 하루 동안 거래한 현금인가요 | matched → matched | 100% | 100% |
| DEVELOPMENT-12 | multi_evidence | STR은 누가 받으며 영업점 직원은 누구에게 먼저 보고하나요 | matched → matched | 100% | 100% |
| DEVELOPMENT-13 | multi_evidence | 계좌 신규개설 고객확인 대상과 고위험 고객의 강화된 확인 내용을 함께 알려주세요 | matched → matched | 100% | 100% |
| DEVELOPMENT-14 | foreign_korean | 집금 funnel account는 입금과 출금 사이 시간이 짧은 계좌인가요 | no_evidence → matched | 100% | 100% |
| DEVELOPMENT-15 | foreign_korean | inactive account가 갑자기 deposits나 wire transfers를 시작하면 어떤 징후인가요 | matched → matched | 100% | 100% |
| DEVELOPMENT-16 | multi_source | single indicator와 facts context를 함께 평가해야 한다는 두 해외 기관의 유의사항 | no_evidence → matched | 100% | 100% |
| DEVELOPMENT-17 | unsupported_policy | KB국민은행 내부 보고 승인 기준과 룰 점수표를 알려주세요 | insufficient_scope → insufficient_scope | — | 보류 |
| DEVELOPMENT-18 | unsupported_policy | 은행 내규상 STR 보고책임자의 결재 단계는 몇 단계인가요 | insufficient_scope → insufficient_scope | — | 보류 |
| DEVELOPMENT-19 | unsupported_score | 룰 점수 85점이면 자금세탁으로 확정 보고해야 하나요 | insufficient_scope → insufficient_scope | — | 보류 |
| DEVELOPMENT-20 | unsupported_law | 특정금융정보법 제4조 조문 전문을 알려주세요 | insufficient_scope → insufficient_scope | — | 보류 |
| DEVELOPMENT-21 | unrelated | 목성 위성의 공전 주기와 표면 중력 | no_evidence → no_evidence | — | 보류 |
| DEVELOPMENT-22 | unrelated | 이진 탐색 트리의 회전 연산과 시간 복잡도 | no_evidence → no_evidence | — | 보류 |
| DEVELOPMENT-23 | unsupported_customer | 김철수 고객의 직업과 주소를 알려주세요 | matched → insufficient_scope | — | 보류 |
| DEVELOPMENT-24 | unsupported_product | 국민은행 계좌의 오늘 적용되는 예금 이자율은 얼마인가요 | no_evidence → insufficient_scope | — | 보류 |
| VERIFICATION-01 | paraphrase | 금융회사가 불법재산 의심의 합당한 근거를 바탕으로 FIU에 알리는 제도는 | matched → matched | 100% | 100% |
| VERIFICATION-02 | paraphrase | 현장에서 의심거래를 발견한 직원은 평소 고객의 사업내용도 보나요 | matched → matched | 100% | 100% |
| VERIFICATION-03 | paraphrase | 긴급한 의심거래는 우선 전화나 FAX로 보고하고 추후 보완할 수 있나요 | matched → matched | 100% | 100% |
| VERIFICATION-04 | paraphrase | 고객확인용 정보를 거부한 기존 고객과의 거래관계는 어떻게 하나요 | matched → matched | 100% | 100% |
| VERIFICATION-05 | paraphrase | 법인 고객의 실제 소유자를 찾지 못하면 대표자를 확인하는 단계가 있나요 | matched → matched | 100% | 100% |
| VERIFICATION-06 | paraphrase | 자금세탁 위험이 높은 거래는 EDD로 거래자금의 원천을 더 확인하나요 | matched → matched | 100% | 100% |
| VERIFICATION-07 | str_ctr_confusion | 의심스러운 합당한 사유를 적는 STR과 전산 자동 보고하는 CTR의 차이 | matched → matched | 50% | 50% |
| VERIFICATION-08 | str_ctr_confusion | 한 거래일 현금 입출금 1천만원을 기준으로 하는 것은 CTR인가요 | matched → matched | 100% | 100% |
| VERIFICATION-09 | str_ctr_confusion | CTR 설명에서 동일인의 명의와 동일 금융회사라는 조건은 무엇인가요 | matched → matched | 100% | 100% |
| VERIFICATION-10 | multi_evidence | CDD가 확인하는 실제 소유자 자금 원천과 STR 보고서에 적는 의심 근거 자료를 함께 찾아주세요 | matched → matched | 100% | 100% |
| VERIFICATION-11 | multi_evidence | 무통장입금 등 일회성 금융거래의 예와 법인 실소유자 확인 순서를 함께 찾아주세요 | matched → matched | 100% | 100% |
| VERIFICATION-12 | multi_evidence | STR을 할 합당한 의심 근거와 보고서 작성 방법 절차를 함께 찾아주세요 | matched → matched | 100% | 100% |
| VERIFICATION-13 | foreign_korean | 서로 관계없는 여러 사람이 한 수취인에게 전자 송금하는 징후 | no_evidence → matched | 100% | 100% |
| VERIFICATION-14 | foreign_korean | 분산 multiple beneficiaries wire transfers가 평소 계좌 사용 목적과 맞지 않는 징후 | no_evidence → matched | 100% | 100% |
| VERIFICATION-15 | foreign_korean | 단일 징후만으로 범죄를 확정하지 않고 사실과 맥락을 함께 보아야 하나요 | no_evidence → matched | 100% | 100% |
| VERIFICATION-16 | multi_source | FinCEN과 FINTRAC은 single activity single indicator additional facts context를 어떻게 설명하나요 | matched → matched | 100% | 100% |
| VERIFICATION-17 | unsupported_policy | KB 내부 AML 스코어별 의심거래 승인 규정을 알려주세요 | no_evidence → insufficient_scope | — | 보류 |
| VERIFICATION-18 | unsupported_policy | 국민은행 내규에서 STR를 승인하는 담당자의 이름은 누구인가요 | insufficient_scope → insufficient_scope | — | 보류 |
| VERIFICATION-19 | unsupported_law | 현재 시행령의 고액현금거래보고 조문 전체를 보여주세요 | insufficient_scope → insufficient_scope | — | 보류 |
| VERIFICATION-20 | unsupported_law | 2022년 기준 STR 보고 법률 근거를 찾아주세요 | insufficient_scope → insufficient_scope | — | 보류 |
| VERIFICATION-21 | unrelated | 엽록체의 틸라코이드 막에서 광합성하는 과정 | no_evidence → no_evidence | — | 보류 |
| VERIFICATION-22 | unrelated | 파이썬 비동기 함수의 이벤트 루프 예제 | no_evidence → no_evidence | — | 보류 |
| VERIFICATION-23 | unsupported_customer | TX-000619283 송금인의 직업과 실제 소유자 이름을 알려주세요 | matched → insufficient_scope | — | 보류 |
| VERIFICATION-24 | unsupported_product | 고객확인 자료를 제출하면 대출 금리가 몇 퍼센트 할인되나요 | matched → insufficient_scope | — | 보류 |

## 재현·한계

- 코퍼스 SHA256: `a811aa50fffe640332a25843af96b1ba39ba77aada9a3020b20acf8693c51263`
- 질문 SHA256: `03233f6bcfe6025e9638debcc815d985960c8d10a32dae8bdb4ac1ff36f968f5`
- 기존 V2 코드 SHA256: `5c10640ae37c703c8afefdd63215a8c6a441dc5784133a3663d7ee5f0a5bf5da`
- 첫 중복 제거 결과와 중간 개선 결과는 artifacts/rag-v3-development.json, rag-v3-verification.json, rag-v3-1-comparison.json에 보존했다.
- 원래 development/verification 구분은 최초 실행 순서를 나타낸다. 반복 개발 후에는 전체를 개발·회귀 질문으로 취급한다.
- 인용 문자열의 포함 여부를 검사하며 문장의 의미적 뒷받침이나 법률 적용까지 평가한 것은 아니다.
- 같은 절의 대표 청크 하나만 보는 소비자는 세부 사실을 놓칠 수 있다. supportingChunks와 함께 사용한다.
- 보류율 100%는 이번 16개 질문에 한정된다. 모든 고객정보·상품·내규 질문에 대한 의미 판별 정확도를 뜻하지 않는다.
