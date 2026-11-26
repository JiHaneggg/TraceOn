package com.traceon.graph;

import static com.traceon.common.Json.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import com.traceon.common.*;
import java.nio.file.*;
import java.sql.*;
import java.util.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;

class HistoryStoreTests {
    @TempDir Path dir;
    static Map<String,Object> capture(int id) {
        var tx=GraphServiceTests.edge(id,"A","B");var graph=obj("schemaVersion","1.0","targetTxId",tx.get("txId"),"asOf",tx.get("timestamp"),
                "windowStart","2025-12-25T12:00:00","edges",List.of(tx),"nodes",List.of(obj("accountKey","A"),obj("accountKey","B")),"meta",obj("hops",2));
        return obj("payload",obj("graph",graph,"target",tx,"ruleResult",obj("txId",tx.get("txId"),"ruleScore",0,"findings",List.of()),"highlightTxIds",List.of(),
                "summary",obj("transactions",1),"accountExpansion",obj("pagesUsed",0)),"review",null,"citations",obj(),"componentSha256",obj());
    }
    HistoryStore store(){return new HistoryStore(dir.resolve("analysis.sqlite3").toString());}
    @Test void persistsDeduplicatesAndReopensWithoutLiveServices(){var store=store();var first=store.put(capture(1));assertTrue(first.created());assertFalse(store.put(capture(1)).created());
        var fresh=store();assertEquals(first.body(),fresh.get(str(first.body(),"historyId")).body());assertEquals(1,fresh.list(null).total());
        var history=new HistoryService(mock(GraphService.class),mock(ReviewService.class),fresh);var open=history.open(str(first.body(),"historyId"));
        assertFalse((Boolean)child(child(open,"history"),"integrity").get("currentSourcesChecked"));assertFalse(child(open,"payload").containsKey("reviewSnapshotId"));}
    @Test void pagesTwentyAtATime(){var store=store();for(int i=1;i<=23;i++)store.put(capture(i));var page=store.list(null);assertEquals(20,page.items().size());assertNotNull(page.cursor());assertEquals(3,store.list(page.cursor()).items().size());}
    @Test void refusesTamperedBlob() throws Exception {var store=store();String id=str(store.put(capture(1)).body(),"historyId");
        try(var db=DriverManager.getConnection("jdbc:sqlite:"+dir.resolve("analysis.sqlite3"));var update=db.createStatement()){update.execute("UPDATE analysis_history SET body=x'7B7D'");}
        assertEquals(409,assertThrows(ApiException.class,()->store.get(id)).status);assertThrows(ApiException.class,()->store.list(null));}
    @Test void leavesForeignDatabaseUnchanged() throws Exception {Path path=dir.resolve("analysis.sqlite3");
        try(var db=DriverManager.getConnection("jdbc:sqlite:"+path);var statement=db.createStatement()){statement.execute("CREATE TABLE foreign_data(value TEXT)");}
        assertEquals("history_schema_mismatch",assertThrows(ApiException.class,()->store().put(capture(1))).code);
        try(var db=DriverManager.getConnection("jdbc:sqlite:"+path);var statement=db.createStatement();var rows=statement.executeQuery("SELECT count(*) FROM sqlite_master WHERE name='analysis_history'")){rows.next();assertEquals(0,rows.getInt(1));}}
    @Test void limitsCapacityWithoutOverwriting(){var store=store();for(int i=1;i<=100;i++)store.put(capture(i));assertEquals("history_capacity",assertThrows(ApiException.class,()->store.put(capture(101))).code);assertEquals(100,store.list(null).total());}
    @Test void idsCannotBePaths(){assertEquals(400,assertThrows(ApiException.class,()->store().get("../../secret")).status);}
    @Test void malformedCaptureCannotBeReadAsValid(){var bad=capture(1);child(bad,"payload").put("highlightTxIds",List.of("TX-999999999"));var stored=store().put(bad);
        assertThrows(ApiException.class,()->store().get(str(stored.body(),"historyId")));}
    @Test void refusesAnotherSnapshotsReview(){var graphs=mock(GraphService.class);var reviews=mock(ReviewService.class);when(graphs.current("parent")).thenReturn(child(capture(1),"payload"));
        when(reviews.recordForSave("review")).thenReturn(obj("snapshotId","other"));var history=new HistoryService(graphs,reviews,store());
        assertEquals("history_review_mismatch",assertThrows(ApiException.class,()->history.save("parent","review")).code);assertFalse(Files.exists(dir.resolve("analysis.sqlite3")));}
}
