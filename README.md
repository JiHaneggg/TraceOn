# TraceOn

금융 이상 거래 탐지 및 자율 분석을 위한 지능형 AI 에이전트 시스템 구축 / Intelligent AI Agent System for Financial Anomaly Detection and Autonomous Analysis

그래프 데이터(Neo4j) 기반 위험 거래 탐지 시스템입니다. 동일한 타겟 데이터에 대해 세 가지 탐지 방식을 구현하고 성능을 비교합니다.

| 방식 | 설명 |
|---|---|
| Rule-based | 금액·거래 패턴·연결 관계 규칙으로 탐지 |
| LLM Judge / Agent | LLM이 그래프 구조와 거래를 평가 |
| Hybrid | Rule 점수 + ML 점수를 가중 결합하고, LLM이 근거 기반으로 설명 |

## 아키텍처 원칙

- **Neo4j 접근과 Subgraph 추출은 백엔드(Spring Boot)만 담당**합니다. 세 방식이 항상 같은 Subgraph를 입력으로 받도록 하기 위함입니다.
- Rule / ML / LLM 서비스는 백엔드가 전달한 Subgraph를 받아 결과만 반환합니다.
- 자세한 내용: [아키텍처](docs/architecture.md), [설계 결정 기록](docs/decisions.md)

## 디렉터리 구조

```text
TraceOn/
├── backend/             # Spring Boot (Java 21) - Graph, Hybrid, Experiment API
├── ml-service/          # ML 점수 산출 서비스 (ML 담당)
├── docs/                # 아키텍처, API 명세, 설계 결정
├── docker-compose.yml   # 전체 실행 환경 (backend + Neo4j)
├── .env.example         # 환경변수 예시
└── README.md
```

> `backend/`, `docker-compose.yml`, `.env.example`은 다음 단계에서 추가됩니다.

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

다음 단계(Docker Compose 구성)에서 작성합니다.
