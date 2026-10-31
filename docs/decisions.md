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

## ADR-004. Rule Engine은 별도 서비스로 구현한다고 가정한다 (2026-10-30, 가정)

- 결정: Rule Engine은 Rule 담당자가 별도 서비스로 구현한다. 백엔드는 `RuleDetectionClient`가 HTTP로 호출한다.
- 영향: Rule / ML 클라이언트가 같은 형태(Subgraph JSON 요청 → 점수 + 근거 응답)가 되므로 공통 규격으로 설계한다.
- 확정 필요: Rule 담당자와 구현 언어·포트·API 규격 합의.

## ADR-005. 백엔드 기본 설정 (2026-10-30)

- 패키지: `com.traceon`
- Java 21을 Gradle toolchain으로 고정한다. 개발 PC에 JDK 25만 있어도 Gradle이 JDK 21을 자동으로 내려받아 빌드한다 (foojay resolver).
- 이유: 팀원마다 설치된 JDK가 달라도 같은 버전으로 컴파일·테스트되도록 하기 위함.

## ADR-006. 데이터셋과 그래프 모델: 거래 = 관계, 탐지·평가 단위 = 거래 (2026-10-31)

- 데이터셋: IBM Transactions for Anti-Money Laundering (AML), 합성 데이터. 사용할 규모(HI/LI, Small/Medium/Large)는 확정 필요.
- 결정: `(:Account)-[:TRANSFER]->(:Account)`. 거래 1건 = TRANSFER 관계 1개. 정답 라벨 `isLaundering`은 관계 속성.
- 이유
  1. 정답 라벨이 거래 단위(`Is Laundering`)이므로 탐지·평가 단위는 거래다. 라벨·점수는 관계 속성으로 충분히 저장된다.
  2. 데이터셋의 패턴 정의("Max 12 hops", "9-degree Fan-In")가 계좌→계좌 hop 기준이라 Hop 의미가 일치한다.
  3. IBM AML의 대표 ML 접근(GNN 엣지 분류)과 그래프 구조가 같아 ML 담당자가 그대로 내보내 쓸 수 있다.
  4. 거래를 노드로 만들면 노드 수와 hop 수가 2배가 되고, 거래에 연결할 다른 개체(기기·IP 등)가 데이터셋에 없다.
- 보완: 데이터셋에 거래 ID가 없으므로 전처리에서 결정적 `txId`를 부여하고, 모든 담당자가 같은 ID를 사용한다.

## ADR-007. 전처리는 백엔드 담당이 Python으로 구현하고, 결과를 팀 공통 데이터로 배포한다 (2026-10-31)

- 위치: `data-pipeline/`
- 이유: txId·계좌 식별자·환율이 담당자마다 다르면 Rule / ML / LLM / Hybrid 결과를 거래 단위로 비교할 수 없다.
- 구현: 표준 라이브러리만 사용하는 스트리밍 처리. 별도 패키지 설치가 필요 없고, 500만 행도 적은 메모리로 처리한다.
- 산출물: `data/processed/{데이터셋}/` 아래 공통 CSV와 manifest.json(입력 파일 해시, 행 수, 환율). 대용량이므로 Git에 올리지 않는다.

## ADR-008. Neo4j 적재는 neo4j-admin database import로 한다 (2026-10-31)

- 이유: 실험용 고정 데이터라 매번 빈 DB에 새로 적재해도 문제없고, 500만 관계를 가장 빠르고 재현성 있게 넣을 수 있다.
- 방식: `data/processed` → `neo4j_export.py` → `data/import` → 컨테이너 안에서 import → `schema.cypher`로 제약조건·인덱스 생성.
- 제약: 적재 중 DB 중지 필요, 기존 데이터 덮어씀. 증분 추가가 필요해지면 LOAD CSV 방식을 추가 검토한다.
