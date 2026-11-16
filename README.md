# TraceOn

금융 이상 거래 탐지 및 자율 분석을 위한 지능형 AI 에이전트 시스템 구축 / Intelligent AI Agent System for Financial Anomaly Detection and Autonomous Analysis

그래프 데이터(Neo4j) 기반 위험 거래 탐지 시스템입니다. 동일한 타겟 데이터에 대해 세 가지 탐지 방식을 구현하고 성능을 비교합니다.

현재 MVP는 **룰·ML 점수 결합 + LLM의 근거 설명**을 목표로 하며, DB·룰 검증 기반과 RAG 검색·검토 초안 API를 구현했습니다. 로컬 화면에서 그래프→RAG 근거→검토 초안까지 연결했습니다. ML·Spring 운영 Graph API·실제 LLM 호출·지속적인 분석 이력 저장은 추가 통합이 필요합니다. 아래 표는 초기 비교 실험 계획을 포함합니다.

팀원이 처음 실행할 때는 [팀 공유용 실행 안내](docs/team-handoff.md)를 따르세요. 데이터·Docker·API 키 없이 140개 Python 테스트부터 실행할 수 있습니다.

| 방식 | 설명 |
|---|---|
| Rule-based | 금액·거래 패턴·연결 관계 규칙으로 탐지 |
| LLM Judge / Agent | LLM이 그래프 구조와 거래를 평가 |
| Hybrid | Rule 점수 + ML 점수를 가중 결합하고, LLM이 근거 기반으로 설명 |

## 아키텍처 원칙

최신 코드 공유 브랜치: `codex/rag-safe-share`. 개인정보·비밀키·개인 접속 주소·실제 데이터/산출물을 제외한 코드입니다. [팀 실행 안내](docs/team-handoff.md)와 [완료 작업·다음 역할](docs/team-next-steps.md)을 참고하세요. 검색 누락 보완, 공식 근거 확대, 검증 평가, 그래프→RAG→검토 초안 화면까지 포함합니다. 기존 V4 질문의 근거 확보 40/40·보류 20/20은 개발 회귀 결과이며, 다음 검색 평가는 별도 작성·검수한 질문으로 진행합니다.

은행 내규·특정 고객정보·미수집 원문 요청의 보류와 API 0.7.0: [요청 범위·검색 보류 보완](docs/rag-abstention.md).

새 질문 60개의 고정 평가 결과와 남은 검색/보류 문제: [RAG 새 질문 평가 V4](docs/rag-evaluation-v4.md).

룰별 근거 부족·확인 조건·보고서 보류 상태: [근거 적합성 점검](docs/rag-evidence-fit.md).

국내 법령·안내/해외 사례의 검색 범위와 표시: [국내 공식 근거 자료 확대](docs/rag-domestic-sources.md).

문서 추가·변경 감지·과거 근거 보존은 [RAG 문서 등록·갱신 관리](docs/rag-document-management.md)를 참고하세요.

최신 인용·사실 조회와 보고서 검사는 [근거 조회·검증 API](docs/rag-evidence-api.md)를 참고하세요.

현재 팀장·RAG 담당의 구현·검증 결과와 다음 작업: [RAG 작업 현황](docs/rag-owner-status.md), [검색 중복 제거·48개 질문 평가](docs/rag-search-v3.md), [보고서 API](docs/rag-v2.md).

이전에 진행한 DB·룰 기반 작업과 남은 시각화·통합 작업: [DB·룰 작업 현황](docs/db-owner-status.md).

DB·룰의 첫 구현과 실행 순서: [DB·룰 MVP 가이드](docs/db-rule-mvp.md).

확장한 시간순 500건 평가: [V2 실행 방법](docs/evaluation-v2.md), [실측 결과와 한계](docs/db-rule-v2.md).

- **Neo4j 접근과 Subgraph 추출은 백엔드(Spring Boot)만 담당**합니다. 세 방식이 항상 같은 Subgraph를 입력으로 받도록 하기 위함입니다.
- Rule / ML / LLM 서비스는 백엔드가 전달한 Subgraph를 받아 결과만 반환합니다.
- 자세한 내용: [아키텍처](docs/architecture.md), [설계 결정 기록](docs/decisions.md)

## 디렉터리 구조

```text
TraceOn/
├── backend/             # Spring Boot (Java 21) - Graph, Hybrid, Experiment API
├── data-pipeline/       # 원천 데이터 → 팀 공통 데이터 전처리 (Python)
├── ml-service/          # ML 점수 산출 서비스 (ML 담당)
├── rag-service/         # 공식 문서 검색·거래 근거 검증·Agent 도구 (RAG 담당)
├── docs/                # 아키텍처, API 명세, 설계 결정
├── docker-compose.yml   # 전체 실행 환경 (backend + Neo4j)
├── .env.example         # 환경변수 예시
└── README.md
```

## 기술 스택

| 영역 | 기술 |
|---|---|
| Backend | Java 21, Spring Boot 4.x, Gradle |
| Database | Neo4j 5.26 (Community) |
| ML | Python (미확정) |
| LLM Evaluation | DeepEval |
| Container | Docker, Docker Compose |

## 브랜치 전략

| 브랜치 | 용도 |
|---|---|
| `main` | 안정 버전 (발표·제출용) |
| `develop` | 개발 통합 |
| `feature/*` | 기능 단위 작업 → `develop`으로 PR |

## 실행 방법

현재 PC의 Docker 없는 로컬 Neo4j·룰·RAG 실행은 `scripts/start-local-services.ps1`을 사용합니다. RAG 최초 색인 생성·보고서 검증은 [RAG V2 가이드](docs/rag-v2.md)를 따릅니다. 아래 Docker 실행은 팀 공통 대안입니다.

사전 준비: Docker Desktop 실행 중. (로컬 백엔드 실행은 JDK 21 이상, 없으면 Gradle이 자동 설치)

```powershell
# 1) 환경변수 파일 준비 후 NEO4J_PASSWORD(8자 이상) 입력
Copy-Item .env.example .env

# 2-a) 전체 실행 (Neo4j + 백엔드)
docker compose up -d --build

# 2-b) 개발 시: Neo4j만 컨테이너로, 백엔드는 로컬에서 실행
docker compose up -d neo4j
cd backend; .\gradlew.bat bootRun
```

| 주소 | 용도 |
|---|---|
| http://localhost:8080/actuator/health | 백엔드 및 Neo4j 연결 상태 |
| http://localhost:7474 | Neo4j Browser (neo4j / .env의 비밀번호) |

종료: `docker compose down` (데이터는 볼륨에 유지). 데이터까지 삭제: `docker compose down -v`

최신 로컬 보강: [순환 유형 공식 자료](docs/rag-cycle-sources.md)로 10문서·61청크, [멘토 검수용 질문 초안 24개](docs/mentor-review-questions.md)를 준비했습니다. [제어형 보고서 검증 평가](docs/verifier-evaluation.md)는 정상30·결함30개를 고정해 구조적 검증을 측정했으며 실제 LLM 의미 판단·재시도는 미측정입니다. 기존 데이터·룰·RAG 검사 251개와 그래프/연결 검사 55개가 포함됩니다. 검토 초안은 템플릿 기반이며 실제 LLM 의미 판단·재시도는 미측정입니다.

로컬 그래프 시각화: [실행·검증 안내](docs/graph-viewer.md). `scripts/start-graph-viewer.ps1` 실행 후 `http://127.0.0.1:8093`에서 거래 선택·실제 Neo4j 조회·근거 경로 강조·원본 상세를 확인할 수 있습니다. RAG 근거·원문·거래 사실 조회·초안 재검사·다운로드까지 연결했습니다. [그래프→RAG 연결 안내](docs/graph-rag-integration.md)를 참고하세요. Spring 운영 API와 지속적인 분석 이력 저장은 후속입니다.
