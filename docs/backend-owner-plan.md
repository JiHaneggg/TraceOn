# 백엔드 담당 역할과 작업 계획 · 2026-11-24

기준 브랜치: `codex/rag-safe-share` (23e54a9). 팀장 전달 사항(백엔드: 기존 API를 Spring에 연결, 분석·보고서·검토 상태 저장, 실행 환경 구성)과 현재 코드를 대조해 정리했다.

## 1. 현재 Spring에 이미 있는 것

| 기능 | API | 상태 |
|---|---|---|
| 시연 거래 목록·그래프 조회 (Neo4j HTTP 읽기 템플릿) | `GET /api/cases`, `GET /api/graph` | 구현·검증 (36조건 대조) |
| 선택 계좌 추가 조회 | `POST /api/graph/expand` | 구현·검증 (69→72) |
| 룰 점수 호출 | 내부 → rule-service `/v1/rules/score` | 구현 |
| 초안 준비·검사·사실·인용·다운로드 | `/api/review/*` → rag-service | 구현 (템플릿 초안) |
| 분석 저장·목록·다시 열기 | `/api/history/*` (SQLite) | 구현·검증 (재시작 후 복원) |
| 자동 테스트 | `gradlew test` | 33개 |

## 2. 백엔드에 남은 것

| 순서 | 작업 | 이유 | 협업 |
|---|---|---|---|
| 0 | 내 PC에서 기준 시연 재현 (Docker) | 현재 실행 경로는 팀장 PC의 `.tools/` 전용. 백엔드가 실행 환경 담당 | 팀장 (데이터 버전) |
| 1 | **실행 환경**: rule-service·rag-service 컨테이너화, compose 한 번으로 전체 실행, 시연 데이터 적재 절차 고정 | 현재 compose의 backend는 `host.docker.internal`로 호스트 Python에 의존 | 팀장·RAG |
| 2 | **Judge 연동**: 코드 검증 통과 초안 → Agent Judge 호출 → 판정(passed/held/rejected)·사유·사실/청크 ID 저장, 스냅샷 변경 시 초기화 | 팀 다음 통합 순서 1번 | 에이전트 (응답 필드 확정) |
| 3 | **검토 상태 저장**: 사람 검토 상태(검토 대기 → 승인/반려/보류)와 변경 이력(누가·언제·사유) | 현재 `humanReviewRequired=true` 표시만 있고 상태 변경 없음 | 프론트 (화면) |
| 4 | **Hybrid 점수**: 룰 점수 + ML 점수 정규화·가중 결합을 Spring `hybrid`에 구현, 실험 설정(가중치·임계값·버전) 저장 | 원래 백엔드 담당 범위. 현재 오프라인 평가만 존재 | 룰·ML (점수 규격) |
| 5 | 코드 정리: 응답을 `Map` 대신 타입 있는 DTO로, 패키지 분리(graph / review / history) | 유지보수·발표 시 설명 가능성. 기존 계약 테스트를 그대로 통과해야 함 | — |

## 3. 확인·결정이 필요한 사항

- **브랜치**: `main`·`develop`은 아직 초기 커밋. `develop`을 `codex/rag-safe-share`로 fast-forward 할지 팀장과 합의.
- **Judge 응답 규격**: 판정 값, 점수, 사유, 사용한 사실·청크 ID 필드 (에이전트 담당).
- **DB 읽기 전용 계정**: Neo4j Community는 역할 기반 권한이 없어 읽기 전용 사용자를 만들 수 없다. 앱 레벨 읽기 전용 + 읽기 트랜잭션으로 대신할지 합의 필요.
- **ML 점수 제공 방식**: 온라인 서비스 vs 사전 계산 점수 파일.
