package com.traceon.graph;

import static com.traceon.common.Json.*;
import com.traceon.common.ApiException;
import java.time.*;
import java.util.*;

/** Validates the RAG Judge envelope and archived attribution without running a model. */
public final class JudgeResult {
    private JudgeResult() {}
    private static final Map<String,Integer> SCORES=Map.of("supported",100,"insufficient",50,"contradicted",0);
    private static final Map<String,String> FAILURES=Map.of(
            "judge_not_configured","configuration","judge_api_error","provider_error",
            "judge_model_unavailable","transport","judge_invalid_response","model_response",
            "judge_model_incomplete","model_response","judge_model_refused","model_response",
            "judge_id_schema_unavailable","input_limit","judge_input_too_large","input_limit",
            "judge_transport_unavailable","transport");
    public static Map<String,Object> identity(Map<String,Object> graph,Map<String,Object> rules,Map<String,Object> context,Map<String,Object> report) {
        return obj("snapshotId","AGENT-"+hash(obj("graph",graph,"ruleResult",rules)).substring(0,24),
                "graphSha256",hash(graph),"ruleResultSha256",hash(rules),"reportSha256",hash(report),
                "contextId",context.get("contextId"),"contextVersion",context.get("contextVersion"),
                "corpusSha256",context.get("corpusSha256"),"targetTxId",graph.get("targetTxId"),"reportId",report.get("reportId"));
    }
    public static Map<String,Object> transportFailure(Map<String,Object> record) {
        var result=child(record,"result");var inputs=child(record,"inputs");
        return obj("schemaVersion","1.0","assessmentId","J-"+UUID.randomUUID().toString().replace("-","").substring(0,24),
                "evaluatedAt",Instant.now().toString(),"strategy","single_judge","strategyVersion","traceon-single-v2",
                "rubricVersion","traceon-judge-v1","evidenceIdSchema","allowlist_v1","model","",
                "identity",identity(child(inputs,"graph"),child(inputs,"ruleResult"),child(result,"context"),child(result,"report")),
                "inputSha256",null,"status","held","decisionStatus","not_judged","reasonCode","judge_transport_unavailable",
                "evaluationCompleted",false,"outcomeCategory","transport","verificationScore",null,"items",List.of(),
                "factIds",List.of(),"chunkIds",List.of(),"evidenceTxIds",List.of(),"modelCallAttempts",null,
                "modelCallAttemptsKnown",false,"latencyMs",null,"humanReviewRequired",true,"readyForSubmission",false);
    }
    public static void validate(Map<String,Object> judge,Map<String,Object> graph,Map<String,Object> rules,
            Map<String,Object> context,Map<String,Object> report) {
        try { validateFields(judge,graph,rules,context,report); }
        catch(RuntimeException ex) {throw new ApiException(502,"judge_identity_mismatch","Judge 판정과 같은 조회의 초안·사실·인용 연결을 확인하지 못했습니다.");}
    }
    private static void check(boolean valid){if(!valid)throw new IllegalArgumentException("Judge envelope");}
    private static Set<String> ids(Object value) {
        if(!(value instanceof List<?> list))throw new IllegalArgumentException("Judge ID list");
        var result=new TreeSet<String>();for(Object item:list)check(item instanceof String text && !text.isBlank() && result.add(text));return result;
    }
    private static void validateFields(Map<String,Object> j,Map<String,Object> graph,Map<String,Object> rules,Map<String,Object> context,Map<String,Object> report) {
        check("1.0".equals(j.get("schemaVersion")) && str(j,"assessmentId").matches("J-[0-9a-f]{24}"));
        OffsetDateTime.parse(str(j,"evaluatedAt"));
        check(identity(graph,rules,context,report).equals(j.get("identity")));
        check("single_judge".equals(j.get("strategy")) && "traceon-single-v2".equals(j.get("strategyVersion"))
                && "traceon-judge-v1".equals(j.get("rubricVersion")) && "allowlist_v1".equals(j.get("evidenceIdSchema")));
        check(j.get("model") instanceof String model && model.length()<=200);
        check(Boolean.TRUE.equals(j.get("humanReviewRequired")) && Boolean.FALSE.equals(j.get("readyForSubmission")));
        check(j.get("evaluationCompleted") instanceof Boolean);
        String reason=str(j,"reasonCode");boolean transport="judge_transport_unavailable".equals(reason);
        check(transport ? j.get("inputSha256")==null : str(j,"inputSha256").matches("[0-9a-f]{64}"));
        check(transport ? Boolean.FALSE.equals(j.get("modelCallAttemptsKnown")) && j.get("modelCallAttempts")==null
                : Boolean.TRUE.equals(j.get("modelCallAttemptsKnown")) && Set.of(0,1).contains(j.get("modelCallAttempts")));
        check(j.get("latencyMs")==null || j.get("latencyMs") instanceof Number n && Double.isFinite(n.doubleValue()) && n.doubleValue()>=0 && n.doubleValue()<=180000);
        var items=rows(j.get("items"));
        Set<String> factIds=ids(j.get("factIds")),chunkIds=ids(j.get("chunkIds")),txIds=ids(j.get("evidenceTxIds"));
        if(!Boolean.TRUE.equals(j.get("evaluationCompleted"))) {
            boolean insufficient="judge_evidence_missing".equals(reason);
            check(j.get("verificationScore")==null && items.isEmpty() && factIds.isEmpty() && chunkIds.isEmpty() && txIds.isEmpty());
            check("held".equals(j.get("status")) && (insufficient?"held":"not_judged").equals(j.get("decisionStatus")));
            check((insufficient?"insufficient_evidence":FAILURES.get(reason))!=null
                    && Objects.equals(insufficient?"insufficient_evidence":FAILURES.get(reason),j.get("outcomeCategory")));
            return;
        }
        var expected=new LinkedHashMap<String,String>();
        for(var observation:rows(report.get("observations")))check(expected.put("claim:"+str(observation,"observationId"),"claim_support")==null);
        expected.put("document_support","document_support");expected.put("normal_trade_consideration","normal_trade_consideration");
        Map<String,Map<String,Object>> facts=new HashMap<>();for(var fact:rows(context.get("reportFacts")))facts.put(str(fact,"factId"),fact);
        Set<String> allowedChunks=new HashSet<>();for(var c:rows(report.get("citations")))allowedChunks.add(str(c,"chunkId"));
        check(items.size()==expected.size());Set<String> seen=new HashSet<>(),usedFacts=new TreeSet<>(),usedChunks=new TreeSet<>(),usedTxs=new TreeSet<>();int score=100;
        for(var item:items) {
            check(item.keySet().equals(Set.of("itemId","verdict","factIds","chunkIds","reason","criterion","verificationScore","evidenceTxIds")));
            String itemId=str(item,"itemId"),verdict=str(item,"verdict");
            check(seen.add(itemId) && expected.containsKey(itemId) && expected.get(itemId).equals(item.get("criterion")) && SCORES.containsKey(verdict));
            check(str(item,"reason").length()<=1500 && SCORES.get(verdict).equals(item.get("verificationScore")));
            var refs=ids(item.get("factIds"));var chunks=ids(item.get("chunkIds"));
            check(facts.keySet().containsAll(refs) && allowedChunks.containsAll(chunks));
            if(!"insufficient".equals(verdict))check(!refs.isEmpty() || !chunks.isEmpty());
            if("document_support".equals(item.get("criterion")) && !"insufficient".equals(verdict))check(!chunks.isEmpty());
            var transactions=new TreeSet<String>();for(String ref:refs)transactions.addAll(ids(facts.get(ref).get("evidenceTxIds")));
            check(transactions.equals(ids(item.get("evidenceTxIds"))));
            usedFacts.addAll(refs);usedChunks.addAll(chunks);usedTxs.addAll(transactions);score=Math.min(score,SCORES.get(verdict));
        }
        String status=score==100?"passed":score==50?"held":"rejected";
        check(Integer.valueOf(score).equals(j.get("verificationScore")) && status.equals(j.get("status")) && status.equals(j.get("decisionStatus")));
        check((score==100?"judge_supported":score==50?"judge_insufficient_evidence":"judge_contradicted").equals(reason));
        check((score==50?"insufficient_evidence":"semantic_decision").equals(j.get("outcomeCategory")));
        check(factIds.equals(usedFacts) && chunkIds.equals(usedChunks) && txIds.equals(usedTxs));
    }
}
