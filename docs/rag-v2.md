# RAG V2 — 공식 사례 검색과 검토 보고서

## 현재 동작 범위

실제 Neo4j 그래프를 입력으로 `룰 결과 확인 → 거래 사실·문서 검색 → 검토 초안 생성 → 구조화 검증`을 연결했다. RAG 서비스는 백엔드의 Graph JSON을 받으며 DB에 직접 접근하지 않는다. 관리자 검증 스크립트만 기존 Neo4j 조회 도구를 사용한다.

기본 초안은 검증된 사실·설명 문구·문서 근거로 만든다. OpenAI 연동은 모델이 제공된 문구와 근거 ID의 순서·선택을 결정하는 방식이다. 아직 실제 모델을 호출하지 않았으며, API 키·모델 설정이 필요하다. Spring Graph API·ML 결합·관제 UI·최종 STR 제출은 후속 통합 대상이다.

## 실행과 산출물

프로젝트 루트는 `C:\Users\gjwlg\Desktop\P실무\KB국민은행\TraceOn`이다. Python 표준 라이브러리로 실행한다.

```powershell
# 최초 색인 생성: 이미 받은 공식 HTML은 재사용한다.
python rag-service/corpus.py
.\scripts\start-local-services.ps1

# 테스트와 검색 평가
python -m unittest discover -s rag-service/tests -q
python rag-service/evaluate_v2.py

# 실제 DB의 12개 MVP 사례 → 검토 보고서
python scripts/verify-rag-v2.py
```

현재 산출물:

- `artifacts/rag-v2-reports/`: 거래별 검토 초안 Markdown·JSON, 12개 사례.
- `artifacts/rag-v2-verification.json`: 실제 DB 연동·정상 초안·변조 거부·미설정 모델 거부 결과.
- `artifacts/rag-v2-retrieval-evaluation.json`: 고정 개발 질문 26개 검색 결과·질문/코퍼스 해시.
- `data/rag/index.json`: 현재 5문서·26청크 색인.
- `data/rag/versions/v1-*.json`: 이전 3문서·8청크 색인 보존.

평가·검증 스크립트는 기존 결과를 덮어쓰지 않는다. 재실행할 때는 새 `--output` 경로를 지정한다. 문서 색인을 갱신하면 자신이 실행한 RAG 프로세스만 종료하고 시작 스크립트로 다시 실행해야 한다. 현재 API는 `http://127.0.0.1:8092/health`와 `/openapi.json`에서 확인한다.

## 자료 확장과 범위

| 출처 | 관할·종류 | 포함한 범위 |
|---|---|---|
| [KoFIU STR](https://kofiu.go.kr/kor/policy/amls03.do) | KR 공식 안내 | 정의·보고 대상·방법/절차 |
| [KoFIU CTR](https://www.kofiu.go.kr/kor/policy/amls04.do) | KR 공식 안내 | 개념·STR과의 차이 |
| [KoFIU CDD](https://www.kofiu.go.kr/kor/policy/amls05.do) | KR 공식 안내 | 개념·고객확인 대상 |
| [FinCEN FIN-2014-A005](https://www.fincen.gov/resources/advisories/fincen-advisory-fin-2014-a005) | US 해외 유형 참고, 발행 2014-05-28 | Funnel account 정의·징후·판단 시 유의사항 |
| [FINTRAC 금융회사 징후 안내](https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation/indicators-indicateurs/fin_mltf-eng) | CA 해외 유형 참고, 페이지 수정일 2025-11-24 | 복수 사실·맥락 검토, 계좌 활동 변화, 분할 거래, 전자 송금 징후 |

총 13개 선별 절을 800자·중복 100자 기준으로 26청크로 만들었다. 해외 자료는 `sourceKind=foreign_typology`, 관할은 US/CA로 보존한다. 해외 금액 기준·보고 절차를 한국의 기준으로 적용하지 않는다. 특히 FinCEN 문서는 과거 미국 사례 참고 자료이며 현재 한국법에 대한 근거가 아니다.

HTML 원문·절·수집 시각·문자 위치·원본/본문/코퍼스 해시를 보존한다. FINTRAC 목차와 실제 제목이 반복되는 경우 선별 규격의 `occurrence=last`로 본문을 지정한다. 원문 버전 표지가 없어지거나 절 제목이 달라지면 재수집을 실패 처리한다.

국가법령정보센터 직접 요청은 이번 작업 시 시스템 점검 페이지로 이동했다. 이를 법령으로 색인하지 않았다. FATF PDF 직접 다운로드는 403으로 거절되어 포함하지 않았다. `rag-service/pending_sources.json`에 미확보 출처와 재개 조건을 기록했다. 한국 법령 원문·시행일·은행 내부 규정은 여전히 확보가 필요하다.

## 검색 범위 검사

```json
{"query":"집금 funnel account의 징후", "topK":3, "jurisdiction":"ALL", "sourceKind":"foreign_typology"}
```

기본 관할은 KR이다. 해외 사례는 ALL/US/CA를 명시하거나 Context 내부의 유형 참고 조회를 통해 검색한다. 집금·분산·쪼개기·신속 등 일부 한국어 질의에는 영어 검색어를 추가한다. 이는 검색 확장이며 사실·법적 의미의 번역이나 등가 판정이 아니다.

다음처럼 필요한 출처가 없으면 `status=insufficient_scope`, 빈 hits와 이유를 반환한다.

- 은행 내규·내부 룰 점수 기준 요청.
- 룰/모델 점수만으로 보고 의무·확정 여부를 묻는 요청.
- 법령·조문·시행일, `sourceKind=law`, `legalAsOf` 요청.

```json
{"query":"STR 보고의 법률 근거", "sourceKind":"law", "legalAsOf":"2022-09-01"}
```

`legalAsOf`는 YYYY-MM-DD 형식이며 검증된 법령 유효 구간이 있어야 한다. 수집일·페이지 수정일을 법령 시행일로 사용하지 않는다. 일반 검색의 `matched`는 관련 본문이 있다는 뜻이며 `answerability=not_assessed`다. 이 검사는 제한된 표현·자료 종류를 사용하는 방어 로직이다. 질문과 본문의 의미적 뒷받침을 일반적으로 보장하지 않는다.

## 검토 보고서 API

```text
POST /v1/rag/report
{
  "graph": <백엔드 Graph JSON 1.0>,
  "ruleResult": <룰 API 결과>,
  "mode": "template"
}
```

응답은 reportId, contextId, humanReviewRequired, generation, plan, observations, draft, citations, validation, markdown이다. `template` 모드에서는 실제 LLM 호출이 없다. 보고서의 모든 수치는 검산된 reportFacts에서 가져오고 factId·거래 ID를 함께 표시한다. 발견한 순환·집금·분산 구조와 추가 확인이 필요한 내용을 설명한다.

OpenAI 모드는 `mode=openai`로 명시한다. 서버 프로세스 환경에 `OPENAI_API_KEY`와 `OPENAI_MODEL`이 모두 있어야 한다. RAG는 `.env`를 자동 로드하지 않는다. 키를 채팅·Git·로그에 남기지 않고 로컬 실행 환경에 설정한다. 이 설정 후 서버 재시작과 실제 모델 검증은 별도로 진행해야 한다.

[OpenAI Structured Outputs 공식 규격](https://developers.openai.com/api/docs/guides/structured-outputs)에 맞춰 Responses API의 `text.format` JSON Schema·strict 모드를 사용하고 `store=false`를 전달한다. `store=false`만으로 모든 보관 정책이나 조직의 금융정보 전송 요건을 충족한다는 뜻은 아니다.

모델에는 전체 그래프 대신 검증된 설명 목록과 최소 문서 메타데이터를 전달한다. 그래도 거래 ID·계좌 키·금액·시각이 설명에 포함되므로 해당 요청은 외부 전송이다. 현재 자료는 IBM 합성 데이터다. 실거래 적용 시 조직의 전송·접근 정책을 별도로 적용해야 한다.

모델 출력은 contextId·observationIds·citationIds·reviewItemIds로 제한한다. 허용한 ID·Context 일치·중복 여부·필수 거래 설명·법률/범위의 사람 검토 항목을 검사한 뒤 렌더링한다. 모델은 금액·조문·새로운 자유 문장을 추가할 수 없다. API 오류·거절·미완료·ID 검증 실패 시 보고서를 생성하지 않는다. 미설정 상태는 HTTP 503이며 template으로 몰래 전환하지 않는다.

검증 통과는 이 구조화된 사실·원문 인용에 대한 결과다. 데이터 원천의 정확성, 인용이 결론을 의미적으로 뒷받침하는지, 법 적용, 실제 범죄 여부까지 검증한 결과는 아니다. 최종 STR 판단·제출은 사람 검토가 필요하다.

Agent 도구는 기존 3개에 `generate_review_report`를 더한 4개다. 계약은 `rag-service/agent_tools.json`, 실행 어댑터는 `agent_tools.py`다. Agent가 DB 자격증명이나 임의 Cypher를 받지 않는 구조를 유지한다.

## 이번 검증 결과

- 자동 테스트 **93개 통과**: 데이터 16, 룰 24, RAG 53.
- 실제 Neo4j의 기존 MVP 12사례에서 API를 통해 보고서 12개를 만들었다. 순환·집금·분산 탐지와 룰 점수 0/65/90을 포함한다. 모든 구조화 초안 검증 통과·변조 금액 거부를 확인했다.
- 실제 호출은 template 모드이며 LLM 호출 수는 **0**이다. 미설정 openai 모드의 503 응답도 확인했다. OpenAI 정상/거절/미완료/위조 응답 테스트는 모의 전송으로 수행했으며 실제 모델 품질 검증이 아니다.
- 5개 원문 스냅샷과 26개 본문 위치·해시를 대조했다. 사례별 1회 흐름은 약 153~437ms였지만 안정적인 응답시간 보장이나 부하 시험은 아니다.
- 고정 개발 질의 26개: 관련 질의 18개 HitRate@3 **100%**, MRR@3 **0.963**, 복수 정답 절의 평균 Recall@3 **97.2%**. 미지원 질의 8개는 모두 보류했다.
- 복수 정답 사례에서 동일 FINTRAC 절의 여러 청크가 상위 3개를 차지해 다른 관련 출처 하나를 놓쳤다. 다음에는 절·문서 중복 제거와 재정렬을 별도 평가한다.
- 질의는 작은 수작업 개발 세트이며 최종평가가 아니다. 미지원 질문에 대한 이 결과를 모든 금융 질문의 의미 검증 성공률로 일반화하지 않는다.

기존 룰 500건 평가, 연구용 후보 설정, 1차 RAG 결과는 변경하지 않았다. 이번 12사례는 연결 검증이며 새로운 탐지 성능 평가가 아니다.
