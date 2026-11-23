package com.traceon.graph;

import static com.traceon.common.Json.*;
import com.traceon.common.*;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/** Application DB access: only these parameterized read templates are exposed. */
@Component
public class GraphDatabase {
    private static final String PROJECTION="""
        {txId:t.txId, timestamp:toString(t.timestamp),
        fromAccountKey:s.accountKey, toAccountKey:d.accountKey,
        amountPaid:t.amountPaidExact, paymentCurrency:t.paymentCurrency,
        amountReceived:t.amountReceivedExact, receivingCurrency:t.receivingCurrency,
        paymentFormat:t.paymentFormat}
        """;
    public enum Query {
        HEALTH("RETURN 1 AS connected"),
        VERSION("MATCH (m:TraceOnDataset) RETURN m.name AS name,m.sha256 AS sha256"),
        TARGET("MATCH (s:Account)-[t:TRANSFER {txId:$txId}]->(d:Account) RETURN "+PROJECTION+" AS edge"),
        CASES("MATCH (s:Account)-[t:TRANSFER]->(d:Account) WHERE t.txId IN $ids RETURN "+PROJECTION+" AS edge ORDER BY t.txId"),
        VERIFY("MATCH (s:Account)-[t:TRANSFER]->(d:Account) WHERE t.txId IN $ids RETURN "+PROJECTION+" AS edge LIMIT $limit"),
        PAGE("""
            MATCH (a:Account {accountKey:$accountKey})-[t:TRANSFER]-(b:Account)
            WHERE a <> b AND t.timestamp >= localdatetime($since) AND t.timestamp <= localdatetime($asOf)
            AND ($cursorTime IS NULL OR t.timestamp < localdatetime($cursorTime)
             OR (t.timestamp = localdatetime($cursorTime) AND t.txId < $cursorId))
            WITH t ORDER BY t.timestamp DESC,t.txId DESC LIMIT $limit
            MATCH (s:Account)-[t]->(d:Account)
            RETURN """+PROJECTION+" AS edge ORDER BY t.timestamp DESC,t.txId DESC");
        final String cypher; Query(String cypher) { this.cypher=cypher; }
    }
    private final BoundedHttp http; private final URI uri; private final String authorization;
    public GraphDatabase(BoundedHttp http,@Value("${traceon.neo4j-http-url}") String url,
            @Value("${spring.data.neo4j.database}") String database,
            @Value("${spring.neo4j.authentication.username}") String user,
            @Value("${spring.neo4j.authentication.password}") String password) {
        this.http=http;
        if (!database.matches("[A-Za-z0-9_-]+")) throw new IllegalArgumentException("Database name");
        uri=URI.create(url.replaceAll("/$","")+"/db/"+database+"/tx/commit");
        authorization="Basic "+Base64.getEncoder().encodeToString((user+":"+password).getBytes(StandardCharsets.UTF_8));
    }
    public Read read() { return new Read(); }
    public class Read {
        private final long deadline=System.nanoTime()+Duration.ofSeconds(20).toNanos();
        public List<Map<String,Object>> query(Query query,Map<String,Object> params) {
            long remaining=deadline-System.nanoTime();
            if (remaining<=0) throw ApiException.unavailable();
            var response=http.call(uri,obj("statements",List.of(obj("statement",query.cypher,"parameters",params))),
                    authorization,4_000_000,Duration.ofNanos(Math.min(remaining,Duration.ofSeconds(8).toNanos())));
            if (!((List<?>)response.get("errors")).isEmpty()) throw ApiException.unavailable();
            var block=rows(response.get("results")).getFirst();
            List<?> columns=(List<?>)block.get("columns");List<Map<String,Object>> result=new ArrayList<>();
            for (var row:rows(block.get("data"))) {
                var values=(List<?>)row.get("row"); var entry=new LinkedHashMap<String,Object>();
                for (int i=0;i<columns.size();i++) entry.put((String)columns.get(i),values.get(i));
                result.add(entry);
            }
            return result;
        }
        public Map<String,Object> version() {
            var versions=query(Query.VERSION,Map.of());
            if (versions.size()!=1 || !(versions.getFirst().get("sha256") instanceof String sha) || !sha.matches("[0-9a-f]{64}"))
                throw new ApiException(409,"dataset_unmanaged","관리된 시연 자료의 버전을 확인해 주세요.");
            str(versions.getFirst(),"name");return versions.getFirst();
        }
        public List<Map<String,Object>> edges(Query query,Map<String,Object> params) {
            return query(query,params).stream().map(row->child(row,"edge")).peek(GraphService::validateEdge).toList();
        }
    }
}
