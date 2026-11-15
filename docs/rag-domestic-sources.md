# 국내 공식 근거 자료 확대 — 2026-11-15

이 문서는 2단계 자료 확대 기록이다. 이후 적용한 룰별 조건 점검·현재 색인·API 0.6.0은 [근거 적합성 점검](rag-evidence-fit.md)을 따른다.

국내 법령 원문 2문서와 금융정보분석원 안내 1문서를 추가했다. 현재 **8문서·26절·50청크**이며 국내 법령 2·국내 공식 안내 4·해외 참고 자료 2문서로 구성된다. 이번 결과는 검색/인용 API·Context·검토 보고서에 반영했다. 관제 화면은 별도 구현 대상이다.

## 확보한 원문

| 자료 | 버전·시행일 | 검색에 추가한 범위 |
|---|---|---|
| [특정 금융거래정보의 보고 및 이용 등에 관한 법률](https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=290725&efYd=20261002&chrClsCd=010202) | 법률 제21857호, 공포 2026-08-04, 시행 2026-10-02 | 제4조·제4조의2·제5조·제5조의2·제5조의3·제5조의4: 6조문·12청크 |
| [같은 법 시행령](https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=290511&efYd=20261002&chrClsCd=010202) | 대통령령 제36728호, 공포 2026-09-29, 시행 2026-10-02 | 제7조·제8조·제8조의2·제10조의4·제10조의5: 5조문·8청크 |
| [KoFIU 자금세탁방지제도 안내](https://www.kofiu.go.kr/kor/policy/amls01.do) | 공식 안내, 시행일 미표시(null) | 자금세탁방지 개요·제도 체계: 2절·4청크 |

국가법령정보센터의 제목/버전만 있는 화면과 실제 조문 본문을 구별했다. 공식 `lsInfoR.do` 본문에서 제목·시행일·법령 번호와 조문 경계를 대조했다. 전체 HTML 원본 바이트와 정규화 가시 본문의 해시·문자 위치를 보관하고 위 조문만 선별했다. 메뉴·다른 시점 부칙을 통째로 검색하거나 요약문을 법령 원문으로 저장하지 않았다.

2026-11-14의 법령 URL 수집 실패 기록을 남기고 `pending_sources.json`에 2026-11-15의 확보 결과를 추가했다. 은행 내규는 제공되지 않았고 FATF PDF도 이번 색인에 포함되지 않았다. 그 두 자료의 수집 가능 여부를 이번에 새로 확인했다는 의미는 아니다.

## 검색·인용 구분

새 색인에는 `evidencePresentationVersion=1.0`을 기록한다. 검색 대표 결과·보조 본문·인용 원문 조회에 다음 `sourceDisplay`를 제공한다.

| category | label | 사용 범위 |
|---|---|---|
| `domestic_law` | 국내 법령 원문 | 선별 조문 참고. 실제 거래 적용·유효 시점은 별도 검토 |
| `domestic_guidance` | 국내 공식 안내 | 제도 안내. 법령 원문·은행 내규로 대체하지 않음 |
| `foreign_reference` | 해외 참고 사례 | 해외 징후·유형 참고. 한국 보고 의무·금액 기준으로 적용하지 않음 |

`sourceDisplay`는 관할·자료 종류·시행일·버전·분류 안내를 포함한다. 분류 근거는 등록 메타데이터(`classificationBasis=registered_source_metadata`)이며 `legalApplicabilityVerified=false`다. 외국 법령이 추가되어도 국내 법령으로 표시하지 않는다.

검색 응답의 `sourceGroups`는 반환된 대표 결과의 `chunkIds`를 위 분류별로 묶는다. 본문을 합치거나 관련 없는 자료를 빈 검색 자리에 강제로 넣지 않는다. 기본 `jurisdiction=KR`은 국내 자료만 검색한다. 해외 사례는 `jurisdiction=ALL, sourceKind=foreign_typology`로 검색한다. 검색 예시:

```json
{"query":"법률 제4조 불법재산 의심거래 보고 합당한 근거","sourceKind":"law"}
```

```json
{"query":"시행령 제8조의2 고액현금거래 보고 기준금액","sourceKind":"law"}
```

```json
{"query":"집금 funnel account deposits","jurisdiction":"ALL","sourceKind":"foreign_typology"}
```

위 JSON을 기존 `POST /v1/rag/search`로 전달한다. OpenAPI 0.5.0의 `SourceDisplay`, `SourceGroup`, `SearchResponse`를 참고한다. 인용 선택 시 기존 버전 고정 원문 조회 API로 이동하면 된다.

## 조문·날짜의 범위

- 명시한 제4조와 제4조의2, 법률과 시행령을 구별한다. 미수집 조문이나 다른 법령을 요청하면 유사한 조문으로 대체하지 않는다. 법령 이름/조문 처리는 제한된 질의 규칙이며 일반 법률 엔진은 아니다.
- KB 내규·특정 고객정보·미수집 법령/조문은 필요한 자료가 없으면 `insufficient_scope`로 보류한다.
- 시행일은 기록했지만 전체 개정의 유효 종료일은 확정하지 않았다. `validUntil=null`을 임의의 미래 날짜로 채우지 않는다. `legalAsOf` 질문과 문장에 포함된 명시적 날짜의 법률 적용 요청은 유효 구간 확보 전까지 보류한다.
- IBM 합성 거래 시각에 2026년 법령이 적용된다고 판단하지 않는다. 수치 검증·정확한 원문 인용은 법률 적용/의미 검증의 통과를 뜻하지 않는다.

## Context·보고서·과거 버전

새 Context는 국내 STR/고객확인 법률 조문 검색을 추가하고 인용마다 출처 구분과 `sourceGroups`를 전달한다. 보고서 Markdown은 `국내 법령 원문 / 국내 공식 안내 / 해외 참고 사례`를 별도 항목으로 보여주고 버전·시행일을 표시한다. Agent의 검색도 새로 반영한 색인을 읽는다.

표현 변경은 새 색인에만 적용하고 Context 버전은 `evidence-kr-v2`다. 이전 색인의 `evidence-v1` 표현을 유지해 저장된 보고서의 Context·본문·인용을 계속 검증한다. 과거 원문/색인/보고서 파일을 덮어쓰지 않았다.

- 국내 자료 확대 단계의 8문서 색인: `98fc99a9787c5dfb1bad2897ea72f4f47ea7075b9872521c4e34db0b008100cd`.
- 이전 5문서 색인: `a811aa50fffe640332a25843af96b1ba39ba77aada9a3020b20acf8693c51263`.

과거 48개 개발 질문에는 당시 미확보 법령을 보류하는 정답이 있다. 기존 정답/결과/해시는 바꾸지 않았다. 재현할 때는 이전 색인을 지정하며, 현재 색인에 과거 범위 정답을 그대로 적용해 최종 성능을 주장하지 않는다.

## 검증과 실행

- RAG 자동 검사 **131개** 통과: 이전 119 + 자료 구분·조문 범위·과거 계약 유지 12개. 데이터 16·룰 24개와 합산한 현재 자동 검사는 171개다.
- 실제 원문 대상 검색/보류 **24건 모두 확인**, 50청크의 원문·문자 위치·해시 대조, 이전 3문서/5문서 색인 원문 조회 성공.
- 실제 Neo4j 12사례의 새 보고서 준비·검증, 원문 인용 153건·사실 32건 조회, 금액/시간/건수/인용/본문 변조 거부.
- 자료 추가 전 작성한 기존 보고서 **12건도 원래 색인으로 검증 성공**.
- 실제 LLM 호출 0회. Spring·ML·관제 화면을 모두 포함한 검사가 아니다. 24개 질의는 수작업 연결/회귀 검사이며 독립 검색 성능·법률 정확도 평가가 아니다.

```powershell
python -m unittest discover -s rag-service/tests -q
python scripts/verify-domestic-sources.py --output artifacts/new-domestic-source-check.json
python scripts/verify-evidence-api.py --output artifacts/new-domestic-evidence-check.json
```

결과는 `artifacts/rag-domestic-source-candidates.json`, `rag-domestic-source-publications.json`, `rag-domestic-source-verification.json`, `rag-domestic-evidence-verification.json`, `rag-domestic-legacy-reports-verification.json`에 보존했다. 새 보고서는 `artifacts/rag-domestic-evidence-reports/`에 있다.

코드/출처 설정/검사 질의는 `codex/rag-safe-share` 스냅샷으로 공유하고 등록부·원문·색인·실측 보고서는 Git에서 제외한다. `main` 병합은 별도이며 [팀 공유 안내](team-handoff.md)를 따른다.
