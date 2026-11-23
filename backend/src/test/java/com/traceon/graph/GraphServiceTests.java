package com.traceon.graph;

import static com.traceon.common.Json.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.*;
import com.traceon.common.*;
import java.net.URI;
import java.time.*;
import java.util.*;
import org.junit.jupiter.api.*;

class GraphServiceTests {
    GraphDatabase database;GraphDatabase.Read read;BoundedHttp http;GraphService service;
    static class MovingClock extends Clock {
        Instant now=Instant.parse("2026-01-01T00:00:00Z");
        public ZoneId getZone(){return ZoneOffset.UTC;}public Clock withZone(ZoneId zone){return this;}public Instant instant(){return now;}
    }
    List<Map<String,Object>> ledger;Map<String,Object> version=obj("name","demo","sha256","a".repeat(64));
    static Map<String,Object> edge(int id,String from,String to){return obj("txId","TX-%09d".formatted(id),"timestamp","2026-01-01T12:00:00",
            "fromAccountKey",from,"toAccountKey",to,"amountPaid","9007199254740993.01","paymentCurrency","USD",
            "amountReceived","8000000000000000.01","receivingCurrency","EUR","paymentFormat","ACH");}
    @BeforeEach void setup() {
        database=mock(GraphDatabase.class);read=mock(GraphDatabase.Read.class);http=mock(BoundedHttp.class);
        when(database.read()).thenReturn(read);when(read.version()).thenReturn(version);
        ledger=new ArrayList<>();ledger.add(edge(100,"A","B"));for(int i=1;i<=53;i++)ledger.add(edge(i,"A","X"+i));
        when(read.edges(any(),anyMap())).thenAnswer(invocation->{
            var query=invocation.getArgument(0,GraphDatabase.Query.class);Map<String,Object> p=invocation.getArgument(1);
            if(query==GraphDatabase.Query.TARGET)return List.of(ledger.getFirst());
            if(query==GraphDatabase.Query.VERIFY)return ledger.stream().filter(e->((List<?>)p.get("ids")).contains(e.get("txId"))).toList();
            if(query==GraphDatabase.Query.PAGE)return ledger.stream().filter(e->p.get("accountKey").equals(e.get("fromAccountKey"))||p.get("accountKey").equals(e.get("toAccountKey")))
                    .filter(e->p.get("cursorId")==null || str(e,"txId").compareTo((String)p.get("cursorId"))<0)
                    .sorted(GraphService.ORDER.reversed()).limit(num(p,"limit")).toList();
            throw new AssertionError(query);
        });
        when(http.call(any(),any(),isNull(),anyInt(),any())).thenAnswer(invocation->{
            var graph=child(map(invocation.getArgument(1)),"graph");return obj("txId",graph.get("targetTxId"),"ruleScore",0,"findings",List.of());});
        service=new GraphService(database,http,URI.create("http://localhost/rules"),List.of("TX-000000100"),Clock.systemUTC());
    }
    Map<String,Object> initial(){return service.initial("TX-000000100",1,7);}
    @Test void initialKeepsExactAmountsAndFiftyRowBoundary(){var p=initial();assertEquals(50,rows(child(p,"graph").get("edges")).size());
        assertEquals("450359962737049650.50",child(child(p,"summary"),"grossPaid").get("USD"));
        assertTrue((Boolean)child(child(p,"graph"),"meta").get("truncated"));assertEquals(1,rows(child(p,"accountExpansion").get("candidates")).size());}
    @Test void expandsRemainingRowsAndReplaysWithoutAnotherRead(){var p=initial();String id=str(p,"reviewSnapshotId");var result=service.expand(id,"A");
        assertEquals(54,num(child(result,"summary"),"transactions"));assertEquals(4,num(child(child(result,"accountExpansion"),"lastPage"),"readRows"));
        clearInvocations(read);assertEquals(result,service.expand(id,"A"));verifyNoInteractions(read);assertThrows(ApiException.class,()->service.current(id));}
    @Test void keepsParentOnNetworkFailure(){var p=initial();String id=str(p,"reviewSnapshotId");when(read.version()).thenThrow(ApiException.unavailable());
        assertThrows(ApiException.class,()->service.expand(id,"A"));assertEquals(p,service.current(id));}
    @Test void changedVersionInvalidatesParent(){var p=initial();String id=str(p,"reviewSnapshotId");when(read.version()).thenReturn(obj("name","demo","sha256","b".repeat(64)));
        assertEquals(409,assertThrows(ApiException.class,()->service.expand(id,"A")).status);assertThrows(ApiException.class,()->service.current(id));}
    @Test void removedMarkerInvalidatesParent(){var p=initial();String id=str(p,"reviewSnapshotId");when(read.version()).thenThrow(new ApiException(409,"dataset_unmanaged","marker absent"));
        assertEquals("dataset_changed",assertThrows(ApiException.class,()->service.expand(id,"A")).code);assertThrows(ApiException.class,()->service.current(id));}
    @Test void failedRuleRecalculationDoesNotConsumePage(){var p=initial();String id=str(p,"reviewSnapshotId");doThrow(ApiException.unavailable()).when(http).call(any(),any(),isNull(),anyInt(),any());
        assertThrows(ApiException.class,()->service.expand(id,"A"));assertEquals(p,service.current(id));}
    @Test void changedExistingAmountInvalidatesParent(){var p=initial();String id=str(p,"reviewSnapshotId");ledger.getFirst().put("amountPaid","1.00");
        assertEquals("source_changed",assertThrows(ApiException.class,()->service.expand(id,"A")).code);assertThrows(ApiException.class,()->service.current(id));}
    @Test void rejectsClientSelectedAccountOutsideCandidates(){var p=initial();assertEquals(400,assertThrows(ApiException.class,()->service.expand(str(p,"reviewSnapshotId"),"foreign")).status);}
    @Test void onlyAllowsManifestAndRanges(){assertThrows(ApiException.class,()->service.initial("TX-999999999",2,7));assertThrows(ApiException.class,()->service.initial("TX-000000100",4,7));assertThrows(ApiException.class,()->service.initial("TX-000000100",2,2));verifyNoInteractions(read);}
    @Test void pageRejectsScopeSortAndChangedDuplicate(){var first=edge(2,"A","B");var second=edge(1,"A","C");
        assertThrows(IllegalArgumentException.class,()->GraphService.validatePage(List.of(second,first),"A","2025-12-25T00:00:00","2026-01-02T00:00:00",null,26,Map.of()));
        assertThrows(IllegalArgumentException.class,()->GraphService.validatePage(List.of(first),"Z","2025-12-25T00:00:00","2026-01-02T00:00:00",null,26,Map.of()));
        var altered=copy(first);altered.put("amountPaid","0.01");assertThrows(ApiException.class,()->GraphService.validatePage(List.of(altered),"A","2025-12-25T00:00:00","2026-01-02T00:00:00",null,26,Map.of(str(first,"txId"),first)));}
    @Test void expiredSnapshotsFail(){var clock=new MovingClock();service=new GraphService(database,http,URI.create("http://localhost/rules"),List.of("TX-000000100"),clock);
        var p=initial();clock.now=clock.now.plusSeconds(1200);assertThrows(ApiException.class,()->service.current(str(p,"reviewSnapshotId")));}
    @Test void boundsSnapshotMemory(){var first=initial();for(int i=0;i<24;i++)initial();assertThrows(ApiException.class,()->service.current(str(first,"reviewSnapshotId")));}
    @Test void sameSnapshotConcurrencyIsRejectedWithoutDoubleConsumption() throws Exception {
        var p=initial();String id=str(p,"reviewSnapshotId");var entered=new java.util.concurrent.CountDownLatch(1);var release=new java.util.concurrent.CountDownLatch(1);
        when(read.version()).thenAnswer(inv->{entered.countDown();assertTrue(release.await(5,java.util.concurrent.TimeUnit.SECONDS));return version;});
        try(var executor=java.util.concurrent.Executors.newSingleThreadExecutor()) {
            var pending=executor.submit(()->service.expand(id,"A"));assertTrue(entered.await(5,java.util.concurrent.TimeUnit.SECONDS));
            try{assertEquals(429,assertThrows(ApiException.class,()->service.expand(id,"A")).status);}finally{release.countDown();}
            assertEquals(1,num(child(pending.get(5,java.util.concurrent.TimeUnit.SECONDS),"accountExpansion"),"pagesUsed"));
        }
    }
    @Test void candidatesRespectHopBoundary(){var p=initial();var graph=copy(child(p,"graph"));assertFalse(GraphService.eligible(graph).containsKey("X1"));child(graph,"meta").put("hops",2);assertTrue(GraphService.eligible(graph).containsKey("X53"));}
    @Test void budgetStatusEnforcesAnalysisAccountAndEdgeCaps(){var graph=child(initial(),"graph");var state=obj("version",version,"accounts",obj(),"pages",8,"initialTransactionCount",50,"pageHistory",List.of(),"lastPage",null);
        assertFalse((Boolean)GraphService.status(graph,state).get("canExpand"));state.put("pages",0);
        var capped=obj("hasMore",true,"pages",4,"fetchedRows",150);state.put("accounts",obj("A",capped,"B",capped));
        assertFalse((Boolean)GraphService.status(graph,state).get("canExpand"));state.put("accounts",obj());
        var thousand=new ArrayList<Map<String,Object>>();for(int i=1;i<=1000;i++)thousand.add(edge(i,"A","B"));graph.put("edges",thousand);
        assertTrue((Boolean)GraphService.status(graph,state).get("budgetReached"));assertFalse((Boolean)GraphService.status(graph,state).get("canExpand"));}
}
