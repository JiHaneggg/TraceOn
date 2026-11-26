package com.traceon.graph;

import static com.traceon.common.Json.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;
import com.traceon.common.*;
import java.nio.file.Path;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.function.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;

class ReviewJudgeTests {
    @TempDir Path directory;
    GraphService graphs;BoundedHttp http;ReviewService reviews;Map<String,Object> fixture,judgment;
    Set<String> invalid=ConcurrentHashMap.newKeySet();boolean valid=true;AtomicInteger modelRequests=new AtomicInteger();
    Function<Map<String,Object>,Map<String,Object>> provider;
    @BeforeEach void setup() throws Exception {
        try(var input=getClass().getResourceAsStream("/judge-review.json")){fixture=parse(input.readAllBytes());}
        judgment=copy(child(fixture,"judgment"));graphs=mock(GraphService.class);http=mock(BoundedHttp.class);
        when(graphs.current(anyString())).thenAnswer(i->{if(invalid.contains(i.getArgument(0)))throw ApiException.stale();return copy(child(fixture,"payload"));});
        when(graphs.ifCurrent(anyString(),any())).thenAnswer(i->{if(invalid.contains(i.getArgument(0)))throw ApiException.stale();return ((Supplier<?>)i.getArgument(1)).get();});
        provider=request->copy(judgment);
        when(http.call(any(),any(),isNull(),anyInt(),any())).thenAnswer(i->{
            URI uri=i.getArgument(0);String path=uri.getPath();
            if(path.endsWith("/prepare"))return copy(child(fixture,"prepared"));
            if(path.endsWith("/verify")){var verified=copy(child(child(fixture,"prepared"),"verification"));if(!valid){verified.put("valid",false);child(verified,"checks").put("amounts",obj("valid",false));}return verified;}
            if(path.endsWith("/judge")){modelRequests.incrementAndGet();var request=map(i.getArgument(1));
                assertEquals(Set.of("graph","ruleResult","report"),request.keySet());assertEquals(child(child(fixture,"prepared"),"report"),request.get("report"));
                return provider.apply(request);}
            if(path.contains("/citations/")){String id=URLDecoder.decode(path.substring(path.lastIndexOf('/')+1),StandardCharsets.UTF_8);return copy(child(child(fixture,"citations"),id));}
            throw new AssertionError(path);
        });
        reviews=new ReviewService(graphs,http,"http://127.0.0.1:8092");
    }
    String prepare(){return str(reviews.prepare("snapshot-1",0),"reviewId");}
    Map<String,Object> judge(){return child(judgment,"judge");}
    HistoryService history(){return new HistoryService(graphs,reviews,new HistoryStore(directory.resolve("archive.sqlite3").toString()));}
    @Test void callsExistingJudgeOnceAndStoresSameDraftAttribution(){String id=prepare();assertNull(reviews.judgeStatus(id).get("judge"));
        var response=reviews.judge(id);assertEquals("passed",child(response,"judge").get("decisionStatus"));assertEquals(judge(),child(child(reviews.record(id),"result"),"judge"));
        assertTrue((Boolean)reviews.judge(id).get("cached"));assertEquals(1,modelRequests.get());assertEquals("finished",reviews.judgeStatus(id).get("judgeState"));}
    @Test void failedNumericOrCitationVerificationNeverCallsJudge(){String id=prepare();valid=false;
        assertEquals(409,assertThrows(ApiException.class,()->reviews.judge(id)).status);assertEquals(0,modelRequests.get());assertNull(reviews.judgeStatus(id).get("judge"));}
    @Test void distinguishesScoredInsufficiencyFromMalformedModelResponse(){String id=prepare();
        for(var item:rows(judge().get("items"))){item.put("verdict","insufficient");item.put("verificationScore",50);}
        judge().putAll(obj("status","held","decisionStatus","held","outcomeCategory","insufficient_evidence","verificationScore",50,"reasonCode","judge_insufficient_evidence"));
        var held=child(reviews.judge(id),"judge");assertTrue((Boolean)held.get("evaluationCompleted"));assertEquals(50,held.get("verificationScore"));
        String next=prepare();judgment=copy(child(fixture,"judgment"));judge().putAll(obj("status","held","decisionStatus","not_judged","outcomeCategory","model_response","verificationScore",null,
                "reasonCode","judge_invalid_response","evaluationCompleted",false,"items",List.of(),"factIds",List.of(),"chunkIds",List.of(),"evidenceTxIds",List.of(),"responseErrorCode","unknown_evidence_id"));
        var failed=child(reviews.judge(next),"judge");assertEquals("not_judged",failed.get("decisionStatus"));assertNull(failed.get("verificationScore"));assertFalse((Boolean)failed.get("evaluationCompleted"));}
    @Test void transportFailureIsStoredWithoutClaimingKnownModelCalls(){String id=prepare();provider=request->{throw ApiException.unavailable();};
        var failed=child(reviews.judge(id),"judge");assertEquals("not_judged",failed.get("decisionStatus"));assertNull(failed.get("modelCallAttempts"));assertFalse((Boolean)failed.get("modelCallAttemptsKnown"));
        assertEquals(failed,child(child(reviews.record(id),"result"),"judge"));}
    @Test void rejectsForeignDraftAndEvidenceIds(){String id=prepare();child(judge(),"identity").put("reportSha256","f".repeat(64));
        assertEquals("judge_identity_mismatch",assertThrows(ApiException.class,()->reviews.judge(id)).code);assertNull(reviews.judgeStatus(id).get("judge"));
        judgment=copy(child(fixture,"judgment"));rows(judge().get("items")).getFirst().put("factIds",List.of("foreign-fact"));
        assertEquals(502,assertThrows(ApiException.class,()->reviews.judge(id)).status);}
    @Test void concurrentJudgeAndSaveAreBlockedUntilOneResultIsComplete() throws Exception {
        String id=prepare();var entered=new CountDownLatch(1);var release=new CountDownLatch(1);
        provider=request->{entered.countDown();try{assertTrue(release.await(5,TimeUnit.SECONDS));}catch(InterruptedException ex){throw new RuntimeException(ex);}return copy(judgment);};
        try(var executor=Executors.newSingleThreadExecutor()){
            var pending=executor.submit(()->reviews.judge(id));assertTrue(entered.await(5,TimeUnit.SECONDS));
            try{assertEquals("running",reviews.judgeStatus(id).get("judgeState"));assertEquals(429,assertThrows(ApiException.class,()->reviews.judge(id)).status);
                assertEquals("judge_in_progress",assertThrows(ApiException.class,()->history().save("snapshot-1",id)).code);
            }finally{release.countDown();}
            assertEquals("passed",child(pending.get(5,TimeUnit.SECONDS),"judge").get("decisionStatus"));assertEquals(1,modelRequests.get());
        }
    }
    @Test void graphChangeDropsInflightJudgmentAndFreshReviewStartsEmpty() throws Exception {
        String id=prepare();var entered=new CountDownLatch(1);var release=new CountDownLatch(1);
        provider=request->{entered.countDown();try{assertTrue(release.await(5,TimeUnit.SECONDS));}catch(InterruptedException ex){throw new RuntimeException(ex);}return copy(judgment);};
        try(var executor=Executors.newSingleThreadExecutor()){
            var pending=executor.submit(()->reviews.judge(id));assertTrue(entered.await(5,TimeUnit.SECONDS));invalid.add("snapshot-1");release.countDown();
            var error=assertThrows(ExecutionException.class,()->pending.get(5,TimeUnit.SECONDS));assertEquals(409,((ApiException)error.getCause()).status);
        }
        assertThrows(ApiException.class,()->reviews.judgeStatus(id));var fresh=reviews.prepare("snapshot-2",0);assertNull(fresh.get("judge"));assertEquals("not_requested",fresh.get("judgeState"));}
    @Test void archivesJudgmentAndRestoresAfterRestartWithoutLiveServices(){String id=prepare();var history=history();
        var without=history.save("snapshot-1",id);var reviewed=reviews.judge(id);var saved=history.save("snapshot-1",id);
        assertNotEquals(child(without,"history").get("historyId"),child(saved,"history").get("historyId"));
        var cold=new HistoryService(mock(GraphService.class),mock(ReviewService.class),new HistoryStore(directory.resolve("archive.sqlite3").toString()));
        var opened=cold.open(str(child(saved,"history"),"historyId"));assertEquals(reviewed.get("judge"),child(opened,"review").get("judge"));
        assertFalse((Boolean)child(child(opened,"history"),"integrity").get("currentSourcesChecked"));assertFalse(child(cold.open(str(child(without,"history"),"historyId")),"review").containsKey("judge"));
        assertFalse((Boolean)history.save("snapshot-1",id).get("created"));}
    @Test void savesMalformedResponseAsNotJudgedAndReopensSameReason(){String id=prepare();provider=request->{throw ApiException.unavailable();};reviews.judge(id);var history=history();var saved=history.save("snapshot-1",id);
        var opened=history.open(str(child(saved,"history"),"historyId"));assertEquals("judge_transport_unavailable",child(child(opened,"review"),"judge").get("reasonCode"));}
    @Test void archiveRejectsRehashedForeignJudgment(){String id=prepare();reviews.judge(id);var history=history();var saved=history.save("snapshot-1",id);
        var archive=history.archive(str(child(saved,"history"),"historyId"));var capture=copy(child(archive,"capture"));
        child(child(child(capture,"review"),"judge"),"identity").put("reportSha256","0".repeat(64));var store=new HistoryStore(directory.resolve("corrupt.sqlite3").toString());
        var bad=store.put(capture);assertEquals(409,assertThrows(ApiException.class,()->store.get(str(bad.body(),"historyId"))).status);}
}
