package com.traceon.graph;

import static com.traceon.common.Json.*;
import com.traceon.common.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.util.*;
import java.util.concurrent.Semaphore;
import java.util.function.Supplier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

@Service
public class ReviewService {
    private final GraphService graphs;private final BoundedHttp http;private final String base;
    private final Semaphore gate=new Semaphore(2);
    private final Semaphore judgeGate=new Semaphore(1);
    private final LinkedHashMap<String,Entry> reviews=new LinkedHashMap<>();
    private static final class Entry {
        final Instant expires;final Map<String,Object> value;boolean judging;
        Entry(Instant expires,Map<String,Object> value){this.expires=expires;this.value=value;}
    }
    public ReviewService(GraphService graphs,BoundedHttp http,@Value("${traceon.rag-url}") String base) {
        this.graphs=graphs;this.http=http;this.base=base.replaceAll("/$","");
    }
    private <T>T bounded(Supplier<T> action) {
        if(!gate.tryAcquire())throw new ApiException(429,"review_busy","근거 조회 중입니다. 잠시 후 다시 시도해 주세요.");
        try{return action.get();}finally{gate.release();}
    }
    private Map<String,Object> call(String path,Object body) {
        return http.call(URI.create(base+path),body,null,2_000_000,Duration.ofSeconds(20));
    }
    public static void require(boolean condition) {
        if(!condition)throw new ApiException(502,"evidence_identity_mismatch","거래·초안·원문의 연결을 확인하지 못했습니다.");
    }
    public Map<String,Object> prepare(String snapshotId,Integer findingIndex) {
        return bounded(()->{
            var payload=graphs.current(snapshotId);var graph=child(payload,"graph");var rules=child(payload,"ruleResult");
            var findings=rows(rules.get("findings"));Map<String,Object> finding=null;
            if(findings.isEmpty()){
                if(findingIndex!=null)throw new ApiException(400,"invalid_finding","현재 조회에는 선택할 룰 근거가 없습니다.");
            }else{
                if(findingIndex==null || findingIndex<0 || findingIndex>=findings.size())
                    throw new ApiException(400,"invalid_finding","현재 조회의 탐지 경로를 선택해 주세요.");
                finding=findings.get(findingIndex);
            }
            var inputs=obj("graph",graph,"ruleResult",rules);
            var body=copy(inputs);body.put("mode","template");var result=call("/v1/rag/reports/prepare",body);
            var context=child(result,"context");var report=child(result,"report");
            require(str(graph,"targetTxId").equals(context.get("targetTxId")) && hash(graph).equals(context.get("graphSha256"))
                    && hash(graph).equals(report.get("graphSha256")) && rules.equals(context.get("ruleResult"))
                    && context.get("contextId").equals(report.get("contextId")) && context.get("corpusSha256").equals(report.get("corpusSha256"))
                    && obj("method","deterministic_template","llmCalled",false).equals(report.get("generation"))
                    && Boolean.TRUE.equals(report.get("humanReviewRequired")));
            checkVerification(child(result,"verification"),report);
            var focus=obj("findingIndex",findingIndex,"ruleId",finding==null?null:finding.get("ruleId"),
                    "evidenceTxIds",finding==null?List.of(graph.get("targetTxId")):finding.get("evidenceTxIds"));
            var source=obj("targetTxId",graph.get("targetTxId"),"queriedAt",child(payload,"source").get("queriedAt"),
                    "graphSha256",hash(graph),"reportScope","complete_queried_graph","llmCalled",false);
            var record=obj("snapshotId",snapshotId,"focus",focus,"source",source,"inputs",inputs,"result",result);
            String id=graphs.ifCurrent(snapshotId,()->remember(record));
            var response=obj("reviewId",id,"snapshotId",snapshotId,"focus",focus,"source",source,"judge",null,"judgeState","not_requested");response.putAll(result);return response;
        });
    }
    private synchronized String remember(Map<String,Object> record) {
        Instant now=Instant.now();reviews.entrySet().removeIf(e->!now.isBefore(e.getValue().expires));
        String id=GraphService.token();reviews.put(id,new Entry(now.plusSeconds(1200),copy(record)));
        while(reviews.size()>24)reviews.remove(reviews.keySet().iterator().next());return id;
    }
    public Map<String,Object> record(String id) {
        Map<String,Object> record;
        synchronized(this){var entry=reviews.get(id);if(entry==null || !Instant.now().isBefore(entry.expires))throw ApiException.stale();record=copy(entry.value);}
        graphs.current(str(record,"snapshotId"));return record;
    }
    private Entry entry(String id) {
        var entry=reviews.get(id);if(entry==null || !Instant.now().isBefore(entry.expires))throw ApiException.stale();return entry;
    }
    public Map<String,Object> recordForSave(String id) {
        var record=record(id);
        synchronized(this){if(entry(id).judging)throw new ApiException(409,"judge_in_progress","Judge 판정이 끝난 뒤 초안과 함께 저장해 주세요.");}
        return record;
    }
    public <T>T ifStableForSave(String id,Object expectedJudge,Supplier<T> action) {
        // Called inside graphs.ifCurrent: snapshot replacement and commit cannot interleave.
        synchronized(this){var entry=entry(id);
            if(entry.judging || !Objects.equals(expectedJudge,child(entry.value,"result").get("judge")))
                throw new ApiException(409,"judge_changed","저장 중 Judge 상태가 바뀌었습니다. 판정을 확인한 뒤 다시 저장해 주세요.");
            return action.get();}
    }
    public Map<String,Object> judgeStatus(String id) {
        var record=record(id);
        return graphs.ifCurrent(str(record,"snapshotId"),()->{synchronized(this){var entry=entry(id);return judgeEnvelope(id,entry,false);}});
    }
    private static Map<String,Object> judgeEnvelope(String id,Entry entry,boolean cached) {
        Object result=child(entry.value,"result").get("judge");
        return obj("schemaVersion","1.0","reviewId",id,"snapshotId",entry.value.get("snapshotId"),
                "judgeState",entry.judging?"running":result==null?"not_requested":"finished","judge",result==null?null:copy(map(result)),"cached",cached);
    }
    public Map<String,Object> judge(String id) {
        var record=record(id);String snapshotId=str(record,"snapshotId");
        Map<String,Object> cached=graphs.ifCurrent(snapshotId,()->{synchronized(this){var entry=entry(id);
            return child(entry.value,"result").get("judge")==null?null:judgeEnvelope(id,entry,true);}});
        if(cached!=null)return cached;
        if(!judgeGate.tryAcquire())throw new ApiException(429,"judge_busy","Judge 판정 중입니다. 잠시 후 다시 시도해 주세요.");
        Entry claimed=null;
        try {
            cached=graphs.ifCurrent(snapshotId,()->{synchronized(this){var entry=entry(id);
                return child(entry.value,"result").get("judge")==null?null:judgeEnvelope(id,entry,true);}});
            if(cached!=null)return cached;
            claimed=graphs.ifCurrent(snapshotId,()->{synchronized(this){var entry=entry(id);
                if(entry.judging)throw new ApiException(429,"judge_busy","같은 초안을 판정 중입니다.");
                entry.judging=true;return entry;}});
            var verified=verify(id);
            if(!Boolean.TRUE.equals(child(verified,"verification").get("valid")))throw new ApiException(409,"review_not_verified","수치·인용 검증을 통과한 초안만 Judge에 보낼 수 있습니다.");
            var request=copy(child(record,"inputs"));request.put("report",child(child(record,"result"),"report"));
            Map<String,Object> judge;
            try {
                var response=http.call(URI.create(base+"/v1/rag/reports/judge"),request,null,2_000_000,Duration.ofSeconds(75));
                checkVerification(child(response,"verification"),child(child(record,"result"),"report"));
                require(Boolean.TRUE.equals(child(response,"verification").get("valid")));judge=child(response,"judge");
            }catch(ApiException ex){if(ex.status!=503)throw ex;judge=JudgeResult.transportFailure(record);}
            var inputs=child(record,"inputs");var result=child(record,"result");
            JudgeResult.validate(judge,child(inputs,"graph"),child(inputs,"ruleResult"),child(result,"context"),child(result,"report"));
            var accepted=copy(judge);Entry original=claimed;
            return graphs.ifCurrent(snapshotId,()->{synchronized(this){var entry=entry(id);if(entry!=original)throw ApiException.stale();
                child(entry.value,"result").put("judge",accepted);entry.judging=false;return judgeEnvelope(id,entry,false);}});
        }finally{if(claimed!=null)synchronized(this){claimed.judging=false;}judgeGate.release();}
    }
    public static void checkVerification(Map<String,Object> verification,Map<String,Object> report) {
        require(verification.get("valid") instanceof Boolean && report.get("contextId").equals(verification.get("contextId"))
                && report.get("reportId").equals(verification.get("reportId"))
                && Boolean.TRUE.equals(verification.get("humanReviewRequired"))
                && Boolean.FALSE.equals(verification.get("readyForSubmission"))
                && Boolean.FALSE.equals(verification.get("semanticEntailmentVerified")));
        if(Boolean.TRUE.equals(verification.get("valid"))) {
            var checks=child(verification,"checks");require(!checks.isEmpty() && checks.values().stream().allMatch(c->Boolean.TRUE.equals(map(c).get("valid"))));
        }
    }
    public Map<String,Object> verify(String id) {
        return bounded(()->{
            var record=record(id);var report=child(child(record,"result"),"report");var inputs=copy(child(record,"inputs"));inputs.put("report",report);
            var checked=call("/v1/rag/reports/verify",inputs);checkVerification(checked,report);record(id);
            return obj("reviewId",id,"report",report,"verification",checked);
        });
    }
    public Map<String,Object> citation(String id,String chunkId) {
        return bounded(()->{
            var record=record(id);var report=child(child(record,"result"),"report");
            var expected=rows(report.get("citations")).stream().filter(c->chunkId.equals(c.get("chunkId"))).findFirst()
                    .orElseThrow(()->new ApiException(404,"citation_not_in_report","이 초안의 인용만 조회할 수 있습니다."));
            String encoded=URLEncoder.encode(chunkId,StandardCharsets.UTF_8).replace("+","%20");
            var result=call("/v1/rag/citations/"+encoded+"?corpusSha256="+str(report,"corpusSha256"),null);
            require(report.get("corpusSha256").equals(result.get("corpusSha256")));
            for(String key:List.of("chunkId","documentId","sourceSha256","textSha256","text","url"))
                require(Objects.equals(expected.get(key),child(result,"citation").get(key)));
            require(Set.of("snapshot_checked","index_only").contains(child(result,"sourceVerification").get("status")));
            record(id);return result;
        });
    }
    public Map<String,Object> fact(String id,String factId) {
        return bounded(()->{
            var record=record(id);var context=child(child(record,"result"),"context");
            var fact=rows(context.get("reportFacts")).stream().filter(f->factId.equals(f.get("factId"))).findFirst()
                    .orElseThrow(()->new ApiException(404,"fact_not_in_report","이 초안의 사실만 조회할 수 있습니다."));
            var body=copy(child(record,"inputs"));body.putAll(obj("factId",factId,"contextId",context.get("contextId"),"corpusSha256",context.get("corpusSha256")));
            var result=call("/v1/rag/facts/resolve",body);
            for(String key:List.of("contextId","graphSha256","corpusSha256"))require(Objects.equals(context.get(key),result.get(key)));
            require(fact.equals(result.get("fact")));
            var transactions=rows(result.get("transactions"));var ids=new HashSet<>((List<?>)fact.get("evidenceTxIds"));
            var byId=GraphService.index(rows(child(child(record,"inputs"),"graph").get("edges")));
            require(transactions.size()==ids.size() && GraphService.index(transactions).keySet().equals(ids)
                    && transactions.stream().allMatch(t->t.equals(byId.get(str(t,"txId")))));
            record(id);return result;
        });
    }
    public Map<String,Object> download(String id) {
        var record=recordForSave(id);var checked=verify(id);
        if(!Boolean.TRUE.equals(child(checked,"verification").get("valid")))throw new ApiException(409,"review_not_verified","초안 검사를 통과하지 못했습니다.");
        var result=obj("reviewId",id,"snapshotId",record.get("snapshotId"),"focus",record.get("focus"),"source",record.get("source"));
        result.putAll(child(record,"result"));result.put("verification",checked.get("verification"));
        return graphs.ifCurrent(str(record,"snapshotId"),()->ifStableForSave(id,child(record,"result").get("judge"),()->result));
    }
}
