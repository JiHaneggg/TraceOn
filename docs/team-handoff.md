# 팀 공유용 실행 안내

최신 공유 브랜치는 **`codex/rag-safe-share`**다. 이번 코드에는 DB·룰 기반, RAG 검색 누락/보류 보완, 공식 자료 10문서·61청크, 초안 검증 평가, 그래프→RAG 근거→검토 초안 화면을 포함한다. [완료 작업·다음 역할](team-next-steps.md)과 [화면 사용 방법](graph-rag-integration.md)을 먼저 확인한다.

공유 코드는 `main` 초기 공개 커밋에서 만든 안전한 스냅샷의 후속이다. 이전 비공개 개발 이력을 가져오지 않으며 `main` 병합은 별도다. 로컬 시연 흐름은 연결됐고 Spring 운영 API·ML 결합·실제 LLM·영속 분석/검토 상태 저장은 남아 있다.

V4의 지원 근거 40/40·미지원 보류 20/20은 개선에 사용한 질문의 회귀 결과다. 멘토 검수 대기 질문 24개는 실제 검색 평가를 실행하지 않았다. 작은 개발 세트 결과를 일반 금융 업무 정확도나 독립 최종평가로 사용하지 않는다.

## 데이터 없이 코드부터 확인

개발 PC의 Python 3.14와 Node.js 24에서 검증했다. JavaScript 검사에는 Node.js가 필요하다. Python 자동 테스트는 표준 라이브러리를 사용하며, JavaScript 검사는 저장소에 포함된 Cytoscape 배포본을 쓴다. Kaggle 데이터·실제 Neo4j·Docker·API 키·npm 설치 없이 실행한다. 저장소 루트에서 각각 별도 Python 프로세스로 실행한다.

```powershell
git clone --single-branch --branch codex/rag-safe-share https://github.com/JiHaneggg/TraceOn.git
cd TraceOn

python -m unittest discover -s data-pipeline/tests -q
python -m unittest discover -s rule-service/tests -q
python -m unittest discover -s rag-service/tests -q
python -m unittest discover -s graph-viewer/tests -q
python -m unittest discover -s ml-service/tests -q
node --test graph-viewer/tests/*.test.mjs
```

공통 기준 커밋에서는 데이터 34·룰 24·RAG 211·그래프 Python 65·JavaScript 40·ML 3개, 총 377개를 확인했다. HTTP 계약 테스트는 테스트용 로컬 포트를 사용한다. 테스트 통과는 실제 금융 업무의 정확도나 최종 UI 완성을 의미하지 않는다.

Python 소스는 `.gitattributes`로 LF에 고정하고, 이미 평가에 사용한 `evaluation_queries*.json`은 원본 바이트를 보존한다. 운영체제별 자동 줄바꿈 변환으로 기록된 코드·질문 해시가 달라지는 것을 방지하기 위함이다. 기존 셸·Windows 스크립트·이미지 속성 규칙은 유지한다.

## RAG API 실행

```powershell
# 최초 1회: 공식 공개 HTML로 로컬 색인 생성
python rag-service/corpus.py

# 문서 등록부 연결 (이후 갱신은 check → diff → publish)
python rag-service/corpus_manager.py init

# 현재 색인 해시 확인 후 요청 범위 보류 기준 반영
python rag-service/corpus_manager.py status
python rag-service/corpus_manager.py set-query-scope --version 1.0 --expected-corpus-sha256 확인한_색인_해시

# RAG 단독 실행
python rag-service/rag_server.py --port 8092
```

`http://127.0.0.1:8092/health`와 `/openapi.json`에서 상태·계약을 확인한다. 최신 API 0.7.0은 [보류 기능](rag-abstention.md), [적합성 점검](rag-evidence-fit.md), [근거 조회](rag-evidence-api.md)를 따른다. `template` 모드는 LLM 호출이 없다. `openai` 모드는 서버 환경의 키·모델 설정과 별도 실호출 검증이 필요하다. RAG는 `.env`를 자동으로 읽지 않는다. 이 주소들은 각 PC의 루프백 개발 예시다.

원문 사이트가 점검 중이거나 지정한 문서 버전이 바뀌면 색인 생성이 중단될 수 있다. 오류 화면을 규정 문서로 넣거나 버전 확인을 생략하지 않는다. 고정된 검색 평가를 똑같이 재현하려면 팀에서 사용한 `data/rag/` 스냅샷을 별도로 받아야 한다. 새로 받은 색인은 수집 시각·본문 때문에 해시가 달라질 수 있다.

근거 적합성 정책은 원본/청크 해시에 연결되어 있다. 해당 스냅샷을 준비했거나 새 원문에 맞게 검토한 정책이 있을 때만 `apply-evidence-policy`를 사용한다. 새 수집본을 기존 정책과 일치한다고 가정하지 않는다. 과거 V3 비교 스크립트는 안전한 기준 코드 `rag-service/baselines/retrieval_v3_published.py`와 당시 색인을 사용하므로 이전 비공개 개발 이력을 가져올 필요가 없다.

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
- 개인 PC 계정명이 포함된 절대경로·개인 접속 주소·비공개 이메일. 공유 문서에는 상대경로, 공식 공개 원문 URL, 각 PC의 루프백 개발 예시만 사용한다.

실측 기록은 문서에서 확인하되 원본 데이터와 실행 산출물은 필요한 담당자에게 별도로 전달한다. 작은 개발 질문 세트의 결과를 독립 최종평가로 사용하지 않는다.

## 다섯 담당자의 다음 연결점

| 담당 | 이어서 할 작업 |
|---|---|
| 백엔드 | 기존 Graph/RAG 계약을 Spring 서비스에 연결하고 분석·검토 상태 저장, 실행 환경을 구성 |
| 프론트 | 현재 로컬 시연 화면을 팀 프론트로 통합하고 그래프·원문·보고서·검토 상태·오류/대기 화면 연결 |
| 에이전트 | 준비된 도구로 식별→설명→검증 흐름과 실제 LLM, 보류·제한된 재시도·실행 기록 연결 |
| 문서 | 아키텍처·데이터/Agent 흐름·클래스/API 정의·평가표·발표/시연·회의록을 한 기준으로 통합 |
| 팀장·DB/RAG | 데이터/룰 근거와 검색·보류 품질, 새 질문 검수, DB 조회 정확성/성능, 일정·범위 관리 |

룰·ML 점수 생성/결합 책임과 비교 실험 기준은 다섯 담당자 안에서 합의한다. 모델 학습 완료나 실제 LLM 호출을 이번 시연에 포함된 것으로 표시하지 않는다.

## 최신 검증과 사용 범위

- 공식 자료 10문서·61청크, 멘토 검수 대기 질문 24개, 60건 구조적 초안 반려 평가를 포함한다. [자료 보강](rag-cycle-sources.md), [검증 평가](verifier-evaluation.md)를 따른다.
- 그래프/RAG 연결의 관련 자동 검사 55개, 실제 DB 시연 12건의 사실44·인용158 조회와 첨부파일24개 일치를 확인했다. 자동 검사 통과와 근거 충분/사람 승인은 구분한다.
- 실제 LLM·의미적 Judge·재시도는 미측정이며, 현재 초안은 검산된 사실과 허용 문구를 이용한 템플릿이다.
- 실제 DB/문서 자료는 Git에 없으므로 코드만 받은 PC에서는 아래 테스트부터 실행하고, 실제 시연은 자료 준비 후 진행한다. `scripts/start-graph-viewer.ps1`과 [연결 가이드](graph-rag-integration.md)를 따른다.

## 2026-11-17 DB 조회 점검

[읽기 전용 진단](db-query-audit.md)에 신규 검사 15개를 추가했다. 이번 단계에서는 데이터 영역 31개(기존16+신규15)를 실행했고 통과했다. 이전 공유 시 전체306개 통과 기록은 유지하며 이번에 전체321개를 재실행한 것으로 표시하지 않는다. 실제 시연12대상×3조건의 원본 정합성과 상한 생략을 측정했다. 결과의 비율은 적재된 일부 자료에 대한 조회 포함 비율이며 AML Recall이 아니다.


## 분석 이력 추가 — 2026-11-18

[사용법과 API](analysis-history.md)를 추가했다. 화면 상단에서 그래프/룰만 또는 준비한 초안/원문까지 저장하고 목록에서 다시 연다. SQLite는 `data/history/analysis.sqlite3`에 자동 생성된다. 이 폴더는 Git 제외이며 원본 HTML 전체의 복제나 운영 감사 DB가 아니다. 최신 자료로 조사하려면 저장본의 ‘현재 자료로 새 분석’을 선택한다. 팀 백엔드 이관 시 이력 ID·불변 내용·원본/코퍼스 버전·현재 자료 미조회 표시를 함께 유지한다.


## 2026-11-23 팀 연동 자료

[공동 시연 확인표](team-integration-check.md)에 순환 송금 한 건의 69→72거래 추가 조회, 근거 확인, 저장본 다시 열기와 담당별 완료 기준을 모았다. [500건 DB 조회 대조](neo4j-evaluation-replay.md)와 [RAG 검수 회신 양식](mentor-review-questions.md)을 함께 참고한다.


공통 기준은 [팀 연동 확인표](team-integration-check.md)의 `verify-team-integration.py`로 확인한다. 백엔드는 구현한 단계까지 검사할 수 있고, 프론트·에이전트·문서 담당은 같은 시연 거래와 결과를 사용한다.
