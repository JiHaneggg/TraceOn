// 적재 결과 확인. 기대값은 data/processed/{dataset}/manifest.json의 counts와 같아야 한다.
MATCH (a:Account) RETURN 'accounts' AS item, count(a) AS value
UNION ALL
MATCH ()-[t:TRANSFER]->() RETURN 'transactions' AS item, count(t) AS value
UNION ALL
MATCH ()-[t:TRANSFER]->() WHERE t.isLaundering RETURN 'laundering' AS item, count(t) AS value
UNION ALL
MATCH ()-[t:TRANSFER]->() WHERE t.isSelfLoop RETURN 'selfLoops' AS item, count(t) AS value
UNION ALL
MATCH ()-[t:TRANSFER]->() WHERE t.patternId IS NOT NULL RETURN 'patternMatched' AS item, count(t) AS value;
