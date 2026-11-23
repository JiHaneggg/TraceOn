# Spring 그래프 조회·계좌 확장·분석 저장 — 2026-11-23

`backend/`의 Spring Boot 4.1.2 / Java 21이 기존 로컬 시연 API 계약을 구현한다. 그래프는 Spring이 Neo4j의 고정 transactional HTTP 읽기 템플릿으로 직접 조회한다. Python 조회 서버를 경유하지 않는다. 룰은 기존 `/v1/rules/score`, 초안·검증·사실·원문은 기존 RAG API를 호출한다. 공통 Graph JSON 1.0, 기본 룰 설정과 평가 엔진은 변경하지 않았다.

## 실행

현재 저장소의 `.env`, 선별 시연 자료 `data/processed/HI-Small-MVP/manifest.json`, 같은 자료가 적재된 Neo4j가 필요하다. 초안 기능은 RAG 색인이 필요하다. 기존 `.env`에는 새 변수가 없어도 기본값을 사용한다.

```powershell
.\scripts\start-local-services.ps1
.\scripts\start-spring-backend.ps1
# 또는 backend/에서 JDK 21로 실행
cd backend
.\gradlew.bat bootRun
```

| 설정 | 기본값 |
|---|---|
| `SERVER_PORT` / `SERVER_ADDRESS` | `8080` / `127.0.0.1` |
| `NEO4J_HTTP_URL` | `http://127.0.0.1:7474` |
| `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | 기존 `.env` 설정 |
| `RULE_URL` | `http://127.0.0.1:8091/v1/rules/score` |
| `RAG_URL` | `http://127.0.0.1:8092` |
| `TRACEON_MANIFEST` | `../data/processed/HI-Small-MVP/manifest.json` |
| `TRACEON_HISTORY_DB` | `../data/history/spring-analysis.sqlite3` |

상대 경로는 `backend/` 기준이다. 시연 목록이 없을 때는 서버·저장본 기능을 시작할 수 있지만 실시간 그래프/목록은 503이다. 목록을 설치한 뒤 재시작한다. `.env`는 서버에서만 읽으며 클라이언트에 자격증명·DB 주소·개인 파일 경로를 반환하지 않는다.

컨테이너 설정은 Neo4j HTTP 주소, 시연 목록 읽기 전용 마운트, 별도 `backend_history` 볼륨과 컨테이너 수신 주소를 지정한다. Docker에서 접근 가능한 룰/RAG 서비스가 별도로 필요하며 `DOCKER_RULE_URL`, `DOCKER_RAG_URL`로 지정한다. 기존 Python 서비스는 loopback 전용이므로 컨테이너에서의 도달성을 별도 확인해야 한다. 이번 검증은 Docker 없는 로컬 실행이다.

## 프론트 연결 계약

`/api/health`, `/api/cases`, `/api/graph`와 [계좌 추가 조회](account-expansion.md), [초안·사실·인용](graph-rag-integration.md), [저장·다시 열기](analysis-history.md)의 API 경로·입출력 필드를 유지한다. 프론트는 동일 Origin의 개발 프록시로 연결한다. 임의 Origin을 허용하는 CORS는 제공하지 않는다.

```text
GET  /api/cases
GET  /api/graph?txId=TX-000619283&hops=2&windowDays=7
POST /api/graph/expand       {"snapshotId":"<reviewSnapshotId>","accountKey":"<응답 후보>"}
POST /api/review/prepare     {"snapshotId":"<새 reviewSnapshotId>","findingIndex":0}
POST /api/history/save       {"snapshotId":"<같은 ID>","reviewId":"<검증한 reviewId>"}
GET  /api/history
GET  /api/history/open?historyId=<저장 ID>
```

초안 없이 저장할 때 `reviewId:null`을 명시한다. 서버의 현재 스냅샷 ID만 받으며 클라이언트 그래프·점수·초안·커서·Cypher·저장 경로는 받지 않는다. JSON 중복 키, 미정의 필드, 중복 쿼리 파라미터, 교차 Origin, 2KB 초과 POST를 거부한다.

기본 조회는 이전 기간 1/3/7일, 1/2/3홉, 계좌별 최근 50거래(+다음 존재 확인 1행), 최대 1,000거래다. 추가 조회는 다음 25행(+1행), 분석당 8회·계좌당 4회이며 원래 대상·기간·홉을 고정한다. 동시 그래프 요청 2개, 요청 내 DB 조회 20초 예산, 한 DB 응답 최대 8초·4MB를 적용한다. RAG는 동시 2개·20초·2MB다. 시간 제한은 응답 본문 수신까지 포함하고 리디렉션은 따르지 않는다.

새 페이지의 정렬·연결 계좌·시간창·중복 ID와 기존 거래 값을 검사한다. `TraceOnDataset` 이름/SHA-256을 앞뒤로 읽고 기존 그래프 전체를 원본과 대조한다. 관리 마커가 없는 DB는 실시간 조회를 거부한다. 성공할 때만 새 스냅샷·커서·예산·룰을 확정한다. 같은 부모·같은 계좌의 최신 성공 재전송은 같은 응답이며, 이미 바뀐 그래프의 초안/저장은 409다. 실패한 네트워크 조회는 기존 상태를 보존한다.

임시 그래프·초안은 각 24개·20분이다. `accountExpansion.pageHistory`, `datasetSha256`, `lastPage`, `sourceGraphComplete=false`를 유지한다. 더 확인할 후보가 없더라도 전체 금융 원장을 확인했다고 표시하지 않는다.

## 저장과 복원

분석 이력만 별도 SQLite에 기록한다. 기존 Python 이력을 자동 변경하지 않도록 기본 파일명을 `spring-analysis.sqlite3`로 분리했다. 표·정규 JSON·SHA-256은 기존 이력 스키마 1과 호환된다. 임시 `reviewSnapshotId`, `reviewId`는 저장하지 않는다.

서버의 원본 그래프·룰·페이지 이력과, 선택한 경우 같은 스냅샷의 재검사된 초안·Context·선택 경로·인용 절 본문/위치/해시를 저장한다. 원문 일부를 확인하지 못하면 전체 저장을 거부한다. 관련 Spring 클래스의 SHA-256과 원래 룰 설정/RAG 버전도 보관한다. 한 번에 1개 작업, 최대 100이력·개별 8MB·합계 64MB, 목록 20개, 인용 32개와 수집 예산 30초를 유지한다. SQLite 트랜잭션·`synchronous=FULL`을 사용하며 자동 삭제/덮어쓰기하지 않는다.

목록·열기·인용·사실·검사·다운로드는 저장 묶음만 읽는다. `history.integrity.valid`는 저장 당시 묶음의 해시/식별자 연결 검사다. `currentSourcesChecked=false`, `humanReviewRequired=true`, `readyForSubmission=false`, 보관 사실의 `recomputed=false`를 유지한다. 현재 DB·현재 규정 재검증이나 사람 승인을 뜻하지 않는다.

## 검증

```powershell
cd backend
.\gradlew.bat test bootJar
cd ..
python scripts/verify-spring-backend.py --output artifacts/spring-backend/new-run.json
```

통합 도구는 빌드한 JAR로 별도 포트 18080의 Spring을 직접 시작하고 자신이 만든 프로세스만 종료한다. `.tools/jdk-21.0.1/bin/java.exe`가 없으면 `--java`로 JDK 21 실행 파일을 지정한다. 기존 결과·이력 DB·로그를 덮어쓰지 않는다.

- Spring 자동 검사 **33개 통과**: API 입력 경계·예외 비밀값 차단·본문 크기/전체 수신 시간 제한·리디렉션 차단·50행 경계·정확한 큰 금액·확장 동시성·만료/퇴출·자료 변경·실패 시 상태 보존·SQLite 지속성/중복 저장/페이지/손상/타 저장소/상한을 검사한다.
- 실제 Neo4j의 시연 12거래 × 3조건 = 36조회에서 공통 그래프 전체·룰 전체·근거 ID·집계가 기존 추출과 일치했다.
- 같은 36조건에서 가능한 추가 페이지 **35회**의 그래프·룰·페이지 상태 전체를 기존 구현과 대조했다. 기준 사례 재전송을 포함한 **36회**의 멱등 페이지 재전송도 일치했다. 잘못된 범위·중복 키·필드 주입·교차 Origin·폐기된 스냅샷/초안 등 13요청을 거부했다.
- `TX-000619283 / 7일 / 2홉`: 최초 69거래, 룰 90, 근거 ID는 `TX-000619281`, `TX-000619283`. 한 번 확장 후 72거래이며 원본 4행 중 신규 3·중복 1이다. 추가 ID는 `TX-000384563`, `TX-000384564`, `TX-000384565`다.
- 69거래 그래프만 저장, 72거래+초안 저장을 각각 만들고 Spring을 종료/재시작했다. Neo4j·룰·RAG 주소를 호출 감지용 거부 서버로 바꾼 상태에서 두 저장본과 사실 17개·인용 14개를 복원했다. 실시간 의존성 호출은 0회였다. 별도 Python 이력 리더로도 두 저장본을 동일하게 열었다.
- Neo4j 쓰기·실제 LLM 호출은 0회다. 이는 조회·근거 연결·보관 정확성 검사이며 AML 탐지 정확도·500건 새 평가·운영 지연 SLA가 아니다. 원본 거래/실행 결과·이력 DB·로그는 Git 제외 `artifacts/spring-backend/`와 `data/history/`에 둔다.

실행 근거는 Git 제외 `artifacts/spring-backend/verification-20261123-v2.json`과 같은 이름의 SQLite/로그에 보관한다. 이 결과와 저장본을 재사용해 원래 검증 기록을 덮어쓰지 않는다.

## DB·RAG 담당 연결 요청

공통 Graph JSON·룰 조건·기존 500건 평가와 RAG 고정 평가에는 변경을 요청하지 않는다. 이번 구현은 동일 시연 자료와 평가 엔진을 소비한다.

| 요청 | 근거·현재 경계 |
|---|---|
| DB: 운영용 읽기 전용 계정과 HTTP 엔드포인트 제공 여부 합의 | 현재 Spring은 고정 HTTP 템플릿으로 직접 조회한다. 앱이 쓰기 경로를 제공하지 않는 것과 DB 계정 자체의 쓰기 권한 제한은 별개다. Bolt 이관 시에도 현재 36조건/페이지 대조를 통과해야 한다. |
| DB: 불변 데이터 버전 또는 일관된 조사 스냅샷 계약 | 현재 마커/기존 거래 재대조는 관리형 시연 자료의 변경 방지 전제다. 마커를 바꾸지 않은 과거 거래 삽입 전체나 다중 사용자 원장 일관성을 보장하지 않는다. |
| RAG: 인용 당시 버전 보존·32인용 상한·원문 대조 상태 유지 | 저장은 `snapshot_checked` 원문과 해시/코드포인트 위치가 모두 일치할 때만 완료한다. `index_only`·불완전 원문은 저장 실패로 유지하며 승인으로 올리지 않는다. |
| 팀 백엔드/프론트: 운영 사용자·역할·검토 상태·감사/보존 정책 | 현재는 loopback 시연 경계와 불변 로컬 이력이다. 사용자별 소유권·인증·암호화/백업·보존/삭제·사람 승인 상태는 후속 설계가 필요하다. 프론트 연결 시 같은 Origin의 프록시와 새 스냅샷에 따른 이전 초안 초기화를 사용한다. |
