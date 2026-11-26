package com.traceon.graph;

import static com.traceon.common.Json.*;
import com.traceon.common.*;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.Semaphore;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;

@Service
public class HistoryService {
    private final GraphService graphs;private final ReviewService reviews;private final HistoryStore store;
    private final Semaphore gate=new Semaphore(1);
    public HistoryService(GraphService graphs,ReviewService reviews,HistoryStore store){this.graphs=graphs;this.reviews=reviews;this.store=store;}
    public <T>T bounded(Supplier<T> action) {
        if(!gate.tryAcquire())throw new ApiException(429,"history_busy","저장본을 확인 중입니다. 잠시 후 다시 시도해 주세요.");
        try{return action.get();}finally{gate.release();}
    }
    public Map<String,Object> save(String snapshotId,String reviewId) {
        var payload=graphs.current(snapshotId);payload.remove("reviewSnapshotId");
        var capture=obj("payload",payload,"review",null,"citations",obj(),"componentSha256",components());
        if(reviewId!=null) {
            long deadline=System.nanoTime()+Duration.ofSeconds(30).toNanos();
            var record=reviews.recordForSave(reviewId);
            if(!snapshotId.equals(record.get("snapshotId")))throw new ApiException(409,"history_review_mismatch","같은 그래프의 초안만 저장할 수 있습니다.");
            var checked=reviews.verify(reviewId);
            if(!Boolean.TRUE.equals(child(checked,"verification").get("valid")))throw new ApiException(409,"review_not_verified","초안 검사에 실패해 저장하지 않았습니다.");
            var review=copy(child(record,"result"));review.put("verification",checked.get("verification"));
            review.put("focus",record.get("focus"));review.put("source",record.get("source"));capture.put("review",review);
            var citations=rows(child(review,"report").get("citations"));
            if(citations.size()>32)throw new ApiException(413,"history_citation_limit","함께 보관할 인용은 최대 32개입니다.");
            for(var citation:citations){checkDeadline(deadline);var resolved=reviews.citation(reviewId,str(citation,"chunkId"));
                var kept=obj();for(String key:List.of("schemaVersion","corpusSha256","citation","section","sourceVerification"))kept.put(key,resolved.get(key));
                child(capture,"citations").put(str(citation,"chunkId"),kept);}
            checkDeadline(deadline);
        }
        validate(capture);
        var saved=graphs.ifCurrent(snapshotId,()->{
            if(reviewId==null)return store.put(capture);
            reviews.record(reviewId);
            return reviews.ifStableForSave(reviewId,child(capture,"review").get("judge"),()->store.put(capture));
        });
        return obj("schemaVersion","1.0","history",metadata(saved),"created",saved.created());
    }
    private static void checkDeadline(long deadline){if(System.nanoTime()>=deadline)throw new ApiException(503,"history_capture_timeout","원문 수집 시간이 초과됐습니다. 저장본은 생성하지 않았습니다.");}
    private static Map<String,Object> components() {
        var out=obj();for(Class<?> type:List.of(GraphService.class,ReviewService.class,HistoryService.class,HistoryStore.class,JudgeResult.class)){
            try(var stream=type.getResourceAsStream("/"+type.getName().replace('.','/')+".class")){out.put(type.getSimpleName(),sha(Objects.requireNonNull(stream).readAllBytes()));}
            catch(java.io.IOException ex){throw ApiException.unavailable();}
        }return out;
    }
    public static void validate(Map<String,Object> capture) {
        var payload=child(capture,"payload");var graph=child(payload,"graph");var edges=rows(graph.get("edges"));
        edges.forEach(GraphService::validateEdge);var byId=GraphService.index(edges);
        check(!edges.isEmpty() && edges.size()<=1000 && Objects.equals(byId.get(str(graph,"targetTxId")),payload.get("target")));
        check(byId.keySet().containsAll((List<?>)payload.get("highlightTxIds")));
        var evidence=new TreeSet<String>();for(var finding:rows(child(payload,"ruleResult").get("findings")))
            for(Object id:(List<?>)finding.get("evidenceTxIds"))evidence.add((String)id);
        check(evidence.equals(new HashSet<>((List<?>)payload.get("highlightTxIds"))));
        check(num(child(payload,"summary"),"transactions")==edges.size());
        var citations=child(capture,"citations");
        if(capture.get("review")==null){check(citations.isEmpty());return;}
        var review=child(capture,"review");var context=child(review,"context");var report=child(review,"report");var verification=child(review,"verification");
        ReviewService.checkVerification(verification,report);
        check(Boolean.TRUE.equals(verification.get("valid")) && hash(graph).equals(context.get("graphSha256"))
                && context.get("graphSha256").equals(report.get("graphSha256")) && context.get("contextId").equals(report.get("contextId"))
                && context.get("corpusSha256").equals(report.get("corpusSha256")) && graph.get("targetTxId").equals(context.get("targetTxId"))
                && payload.get("ruleResult").equals(context.get("ruleResult")) && Boolean.TRUE.equals(report.get("humanReviewRequired"))
                && obj("method","deterministic_template","llmCalled",false).equals(report.get("generation")));
        if(review.get("judge")!=null)JudgeResult.validate(child(review,"judge"),graph,child(payload,"ruleResult"),context,report);
        Set<String> chunkIds=new HashSet<>();
        for(var row:rows(report.get("citations"))) {
            String chunkId=str(row,"chunkId");check(chunkIds.add(chunkId));var resolved=child(citations,chunkId);
            check(context.get("corpusSha256").equals(resolved.get("corpusSha256")) && "snapshot_checked".equals(child(resolved,"sourceVerification").get("status")));
            for(String key:List.of("chunkId","documentId","sourceSha256","textSha256","text","url"))check(Objects.equals(row.get(key),child(resolved,"citation").get(key)));
            var section=child(resolved,"section");String text=str(section,"text");var highlight=child(section,"highlight");int start=num(highlight,"start"),end=num(highlight,"end");
            check(start>=0 && start<=end && end<=text.codePointCount(0,text.length()));
            check(text.substring(text.offsetByCodePoints(0,start),text.offsetByCodePoints(0,end)).equals(row.get("text"))
                    && sha(str(row,"text").getBytes(StandardCharsets.UTF_8)).equals(row.get("textSha256")));
        }
        check(chunkIds.equals(citations.keySet()));
        for(var fact:rows(context.get("reportFacts")))check(byId.keySet().containsAll((List<?>)fact.get("evidenceTxIds")));
        for(var tx:rows(context.get("evidenceTransactions")))check(tx.equals(byId.get(str(tx,"txId"))));
        var focus=child(review,"focus");var findings=rows(child(payload,"ruleResult").get("findings"));
        Object expected=null;
        if(focus.get("findingIndex")==null && findings.isEmpty())expected=List.of(graph.get("targetTxId"));
        else if(focus.get("findingIndex") instanceof Number number && number.intValue()>=0 && number.intValue()<findings.size())expected=findings.get(number.intValue()).get("evidenceTxIds");
        check(expected!=null && expected.equals(focus.get("evidenceTxIds")));
    }
    private static void check(boolean condition){if(!condition)throw new ApiException(409,"history_integrity_failed","저장된 거래·초안·원문의 연결을 확인하지 못했습니다.");}
    public static Map<String,Object> metadata(HistoryStore.Saved saved) {
        var body=saved.body();var capture=child(body,"capture");var payload=child(capture,"payload");var graph=child(payload,"graph");
        var review=capture.get("review")==null?null:child(capture,"review");
        return obj("historyId",body.get("historyId"),"savedAt",body.get("savedAt"),"snapshotSha256",saved.sha(),"targetTxId",graph.get("targetTxId"),
                "transactions",rows(graph.get("edges")).size(),"accounts",rows(graph.get("nodes")).size(),"ruleScore",child(payload,"ruleResult").get("ruleScore"),
                "hops",child(graph,"meta").get("hops"),"asOf",graph.get("asOf"),"windowStart",graph.get("windowStart"),
                "additionalReadPages",child(payload,"accountExpansion").get("pagesUsed"),"hasReview",review!=null,
                "reportId",review==null?null:child(review,"report").get("reportId"),"corpusSha256",review==null?null:child(review,"context").get("corpusSha256"),
                "graphSha256",hash(graph),"integrity",obj("valid",true,"basis","saved_bundle_sha256_and_identity_checks","currentSourcesChecked",false),
                "humanReviewRequired",true,"readyForSubmission",false,"sourceGraphComplete",false);
    }
    public Map<String,Object> listing(String cursor) {
        var page=store.list(cursor);return obj("schemaVersion","1.0","items",page.items().stream().map(HistoryService::metadata).toList(),
                "nextCursor",page.cursor(),"total",page.total(),"maxEntries",100);
    }
    public Map<String,Object> open(String id) {
        var saved=store.get(id);var meta=metadata(saved);var capture=child(saved.body(),"capture");var payload=copy(child(capture,"payload"));payload.put("history",meta);
        return obj("schemaVersion","1.0","history",meta,"payload",payload,"review",capture.get("review"));
    }
    public Map<String,Object> verify(String id){return obj("schemaVersion","1.0","history",metadata(store.get(id)));}
    public Map<String,Object> citation(String id,String chunkId) {
        var saved=store.get(id);var citations=child(child(saved.body(),"capture"),"citations");
        if(!citations.containsKey(chunkId))throw new ApiException(404,"citation_not_in_report","이 저장본에 보관한 인용만 조회할 수 있습니다.");
        var result=copy(child(citations,chunkId));result.put("history",metadata(saved));return result;
    }
    public Map<String,Object> fact(String id,String factId) {
        var saved=store.get(id);var capture=child(saved.body(),"capture");
        if(capture.get("review")==null)throw new ApiException(404,"fact_not_in_report","초안이 없는 저장본입니다.");
        var context=child(child(capture,"review"),"context");var fact=rows(context.get("reportFacts")).stream().filter(f->factId.equals(f.get("factId"))).findFirst()
                .orElseThrow(()->new ApiException(404,"fact_not_in_report","이 저장본에 포함된 사실만 조회할 수 있습니다."));
        var ids=(List<?>)fact.get("evidenceTxIds");var transactions=rows(child(child(capture,"payload"),"graph").get("edges")).stream().filter(t->ids.contains(t.get("txId"))).toList();
        return obj("schemaVersion","1.0","contextId",context.get("contextId"),"graphSha256",context.get("graphSha256"),"corpusSha256",context.get("corpusSha256"),
                "fact",fact,"transactions",transactions,"history",metadata(saved),"verification",obj("recomputed",false,"basis","archived_verified_context_and_graph","externalLedgerChecked",false));
    }
    public Map<String,Object> archive(String id) {
        var saved=store.get(id);var result=copy(saved.body());result.putAll(obj("snapshotSha256",saved.sha(),"currentSourcesChecked",false,"humanReviewRequired",true));return result;
    }
}
