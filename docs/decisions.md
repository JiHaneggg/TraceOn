# 설계 결정 기록 (ADR)

결정이 바뀌면 기존 항목을 지우지 않고 새 항목을 추가합니다.

## ADR-001. Neo4j 접근과 Subgraph 추출은 백엔드가 단독으로 담당한다 (2026-10-30)

- 배경: Subgraph 추출 로직이 여러 언어·서비스에 중복되면, Rule / LLM / Hybrid가 서로 다른 입력으로 평가되어 비교 실험이 무효가 된다.
- 결정: Spring Boot 백엔드만 Neo4j에 접근한다. Rule·ML·LLM 서비스는 백엔드가 전달한 Subgraph JSON만 사용한다.
- 영향: 각 서비스는 Neo4j 드라이버가 필요 없다. 백엔드에 Subgraph 조회 API를 제공해야 한다.

## ADR-002. 팀 프로토타입(aml-prototype)은 코드를 재사용하지 않는다 (2026-10-30)

- 결정: 다른 팀원의 Python 프로토타입은 Docker 구성(Neo4j 이미지 버전, 포트, 헬스체크 등)만 참고하고, 코드는 처음부터 새로 구현한다.

## ADR-003. 기본 기술 및 저장소 구성 (2026-10-30)

- 모노레포: backend, ml-service, docs를 한 저장소에서 관리한다.
- Spring Boot 4.x + Java 21 + Gradle. (3.5는 2026-06 OSS 지원 종료)
- docker-compose.yml은 저장소 루트에 둔다. (하위 폴더에 두면 빌드 경로가 복잡해짐)
- Neo4j는 핵심 구성요소이므로 compose profile 없이 항상 실행한다.
- 브랜치: main / develop / feature/*.
