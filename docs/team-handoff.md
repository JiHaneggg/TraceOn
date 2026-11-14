# 팀 공유용 실행 안내

이 스냅샷은 `codex/aml-rag-mvp` 브랜치로 공유한다. DB·룰 검증 기반과 RAG 검색·검토 보고서 모듈을 포함한다. 현재 최종 사용자 화면, Spring 운영 Graph API, ML 점수 결합, 실제 LLM 호출은 추가 연결이 필요하다.

## 데이터 없이 코드부터 확인

Python 3.10 이상이 필요하다. 개발 PC의 Python 3.14에서 검증했다. 아래 자동 테스트는 표준 라이브러리만 사용하며 Kaggle 데이터·Neo4j·Docker·API 키 없이 실행한다. 저장소 루트에서 각각 별도 Python 프로세스로 실행한다.

```powershell
git clone --branch codex/aml-rag-mvp https://github.com/JiHaneggg/TraceOn.git
cd TraceOn

python -m unittest discover -s data-pipeline/tests -q
python -m unittest discover -s rule-service/tests -q
python -m unittest discover -s rag-service/tests -q
```

검사 수는 데이터 16·룰 24·RAG 76, 총 116개다. HTTP 계약 테스트는 테스트용 로컬 포트를 사용한다. 테스트 통과는 실제 금융 업무의 정확도나 최종 UI 완성을 의미하지 않는다.

Python 소스는 `.gitattributes`로 LF에 고정하고, 이미 평가에 사용한 `evaluation_queries*.json`은 원본 바이트를 보존한다. 운영체제별 자동 줄바꿈 변환으로 기록된 코드·질문 해시가 달라지는 것을 방지하기 위함이다. 기존 셸·Windows 스크립트·이미지 속성 규칙은 유지한다.

## RAG API 실행

```powershell
# 최초 1회: 공식 공개 HTML로 로컬 색인 생성
python rag-service/corpus.py

# RAG 단독 실행
python rag-service/rag_server.py --port 8092
```

`http://127.0.0.1:8092/health`와 `/openapi.json`에서 상태·계약을 확인한다. 검색·Context·초안 검증·보고서 API는 [RAG 검색 가이드](rag-search-v3.md), [보고서 가이드](rag-v2.md)를 따른다. `template` 모드는 LLM 호출이 없다. `openai` 모드는 서버 환경의 키·모델 설정과 별도 실호출 검증이 필요하다. RAG는 `.env`를 자동으로 읽지 않는다.

원문 사이트가 점검 중이거나 지정한 문서 버전이 바뀌면 색인 생성이 중단될 수 있다. 오류 화면을 규정 문서로 넣거나 버전 확인을 생략하지 않는다. 고정된 검색 평가를 똑같이 재현하려면 팀에서 사용한 `data/rag/` 스냅샷을 별도로 받아야 한다. 새로 받은 색인은 수집 시각·본문 때문에 해시가 달라질 수 있다.

## 실제 Neo4j 시연

원본 IBM AML HI-Small 파일을 별도로 확보한 뒤 [DB·룰 가이드](db-rule-mvp.md)의 전처리·소량 적재 순서를 따른다. DB 내용과 `.env`는 각 개발 환경에서 준비한다. RAG 서비스는 DB 자격증명을 사용하지 않으며 백엔드의 공통 Graph JSON을 받는다.

기존 개발 PC의 `scripts/start-local-services.ps1`은 `.tools/`의 독립 Neo4j·JDK를 사용하는 선택 실행 경로다. 해당 배포 파일은 저장소에 포함하지 않는다. PowerShell 7 환경을 기준으로 한다. 다른 PC에서는 Docker 기반 DB 실행 또는 자체 개발 환경을 먼저 준비한다.

## 공유 범위

코드·테스트·문서·API 명세·평가 질문·집계 결과를 공유한다. 다음 항목은 Git에서 제외했다.

- `.env`, API 키와 실제 DB 비밀번호.
- 원본/전처리 거래 데이터, 평가용 인덱스, 실제 DB 파일.
- 규정 원문·로컬 RAG 색인·원문 스냅샷.
- 실행 중 생성한 보고서·평가 JSON·로그 등 `artifacts/` 파일.
- 가상환경과 `.tools/`의 JDK·Neo4j 배포 파일.

실측 기록은 문서에서 확인하되 원본 데이터와 실행 산출물은 필요한 담당자에게 별도로 전달한다. 작은 개발 질문 세트의 결과를 독립 최종평가로 사용하지 않는다.

## 담당자별 연결점

| 담당 | 우선 확인할 내용 |
|---|---|
| 백엔드/DB | 공통 Graph JSON, 조회 범위·시각·금액 문자열, Spring Graph API와 결과 저장 |
| 룰/ML | 룰 점수·근거 ID, 연구용 설정의 오탐 한계, ML 점수 결합 |
| RAG | 문서 버전·검색 품질·인용 추적·미지원 질문 보류 |
| Agent/LLM | 도구 정의·허용 ID·초안 생성 모드·사람 검토 흐름 |
| 프론트 | highlightTxIds, factId, chunkId, reportId를 경로·근거·보고서 화면에 연결 |

현재 상태와 남은 작업은 [RAG 담당 현황](rag-owner-status.md), [DB·룰 작업 기록](db-owner-status.md)에 정리했다.
