# 프론트 API 연동 안내

기준은 원본 프로젝트의 `docs/team-integration-check.md`, `graph-viewer.md`, `account-expansion.md`, `graph-rag-integration.md`, `analysis-history.md`입니다. 공통 응답 버전은 `1.0`이며 기존 계약을 변경하지 않았습니다.

| 요청 | 입력 | 화면에서 사용하는 응답 |
|---|---|---|
| GET `/api/cases` | 없음 | `items`의 원본 거래, `total` |
| GET `/api/graph` | `txId`, `hops`, `windowDays` | `graph`, `target`, `ruleResult`, `highlightTxIds`, `summary`, `source`, `reviewSnapshotId`, `accountExpansion` |
| POST `/api/graph/expand` | `snapshotId`, `accountKey` | 같은 Graph 응답과 새로운 `reviewSnapshotId` |
| POST `/api/review/prepare` | `snapshotId`, `findingIndex` | `reviewId`, `snapshotId`, `focus`, `context`, `report`, `verification`, `source` |
| POST `/api/review/fact` | `reviewId`, `factId` | 원본 `transactions`, `fact`, Context/그래프/코퍼스 식별자 |
| GET `/api/review/citation` | `reviewId`, `chunkId` | `citation`, `section.text`, `section.highlight`, `sourceVerification`, `corpusSha256` |
| POST `/api/review/verify` | `reviewId` | 원래 `report`, `verification` |
| GET `/api/review/download` | `reviewId`, `format` | JSON 또는 Markdown 첨부파일 |
| POST `/api/history/save` | `snapshotId`, `reviewId` 또는 null | `created`, `history` |
| GET `/api/history` | 선택 `cursor` | `items`, `nextCursor`, `total`, `maxEntries` |
| GET `/api/history/open` | `historyId` | 임시 조회 ID가 없는 `payload`, 저장된 `review`, `history` |
| GET `/api/history/fact` | `historyId`, `factId` | 저장 당시 사실·원본 거래와 `history` |
| GET `/api/history/citation` | `historyId`, `chunkId` | 보관한 인용 본문·버전과 `history` |
| GET `/api/history/verify` | `historyId` | 저장본 내용 검사 `history` |
| GET `/api/history/download` | `historyId`, `format` | 보관 묶음 JSON 또는 당시 Markdown |

요청 본문에는 그래프·점수·초안 본문을 보내지 않습니다. 송금액/수취액은 원본 문자열이고, 은행을 포함한 `accountKey`와 방향을 유지합니다. 인용 강조 오프셋은 UTF-16 인덱스가 아닌 Unicode 코드포인트입니다.

`static/contract.mjs`는 그리기 전에 대상 거래·노드·송금선·룰 근거·요약 건수의 정합성을 확인합니다. 계좌 추가는 기존 거래값 보존·중복·시점/홉·새 스냅샷 여부도 확인합니다. 초안은 현재 그래프의 대상·룰·근거 거래와 연결돼야 합니다. 요청 교체 시 AbortController와 세대 토큰으로 이전 응답을 무시합니다.

추가 조회 성공 후 이전 스냅샷/초안의 후속 사용은 서버도 409로 차단해야 합니다. 네트워크/API 오류는 오류로 표시하며 예시 데이터를 대신 보여주지 않습니다. 409 만료는 다시 조회, 일시 오류는 재시도로 복구합니다.

저장본에는 `history.integrity.valid=true`, `currentSourcesChecked=false`, `humanReviewRequired=true`, `readyForSubmission=false`가 필요합니다. 저장본은 초안 재생성·확장·덮어쓰기를 허용하지 않습니다. 복원한 초안을 현재 원장이나 최신 규정에 대한 검증으로 표시하지 않습니다.

이 폴더의 `mock-api.mjs`와 `.local/history.json`은 프론트 시연 전용입니다. 운영 서버에서는 기존 계약에 맞는 실제 응답·권한·저장소를 제공해야 합니다. 현재 Spring 서버와의 실제 연결, 로그인/권한/감사, 실시간 Neo4j/RAG/모델은 이 작업에서 검증하지 않았습니다.
