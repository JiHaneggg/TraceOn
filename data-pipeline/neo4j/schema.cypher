// neo4j-admin import 후 1회 실행한다. 모두 IF NOT EXISTS라 반복 실행해도 안전하다.

// 계좌 식별자 유일성 (조회 시 인덱스로도 사용)
CREATE CONSTRAINT account_key_unique IF NOT EXISTS
FOR (a:Account) REQUIRE a.accountKey IS UNIQUE;

// 거래 ID로 대상 거래 조회 (Subgraph 시작점)
CREATE INDEX transfer_tx_id IF NOT EXISTS
FOR ()-[t:TRANSFER]-() ON (t.txId);

// 시간 범위 필터
CREATE INDEX transfer_timestamp IF NOT EXISTS
FOR ()-[t:TRANSFER]-() ON (t.timestamp);

CALL db.awaitIndexes(600);
