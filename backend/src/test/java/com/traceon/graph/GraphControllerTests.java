package com.traceon.graph;

import static com.traceon.common.Json.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import com.traceon.common.*;
import org.junit.jupiter.api.*;
import org.springframework.test.web.servlet.*;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class GraphControllerTests {
    MockMvc mvc;GraphService graphs;ReviewService reviews;HistoryService history;
    @BeforeEach void setup(){graphs=mock(GraphService.class);reviews=mock(ReviewService.class);history=mock(HistoryService.class);
        mvc=MockMvcBuilders.standaloneSetup(new GraphController(graphs,reviews,history)).setControllerAdvice(new ApiErrors()).addFilters(new ApiBoundary()).build();}
    @Test void rejectsUnlistedFieldsAndDuplicateQueryParameters() throws Exception {
        mvc.perform(get("/api/graph").header("Host","localhost:80").param("txId","TX-000000001").param("cypher","MATCH (n)")) .andExpect(status().isBadRequest());
        mvc.perform(get("/api/graph").header("Host","localhost:80").param("txId","TX-000000001","TX-000000002")) .andExpect(status().isBadRequest());verifyNoInteractions(graphs);}
    @Test void rejectsDuplicateJsonFieldsBeforeExpansion() throws Exception {
        mvc.perform(post("/api/graph/expand").header("Host","localhost:80").contentType("application/json").content("{\"snapshotId\":\"a\",\"snapshotId\":\"b\",\"accountKey\":\"A\"}"))
                .andExpect(status().isBadRequest());verifyNoInteractions(graphs);}
    @Test void rejectsClientGraphsAndCursors() throws Exception {
        mvc.perform(post("/api/graph/expand").header("Host","localhost:80").contentType("application/json").content("{\"snapshotId\":\"a\",\"accountKey\":\"A\",\"cursor\":null}"))
                .andExpect(status().isBadRequest());verifyNoInteractions(graphs);}
    @Test void rejectsOversizedCrossOriginAndForeignHost() throws Exception {
        mvc.perform(post("/api/graph/expand").header("Host","localhost:80").contentType("application/json").content(" ".repeat(2049))).andExpect(status().isPayloadTooLarge());
        mvc.perform(get("/api/cases").header("Host","localhost:80").header("Origin","http://evil.example")).andExpect(status().isForbidden());
        mvc.perform(get("/api/cases").header("Host","evil.example")).andExpect(status().is(421));verifyNoInteractions(graphs);}
    @Test void hidesSourceExceptions() throws Exception {
        when(graphs.cases()).thenThrow(new IllegalArgumentException("password=TOP_SECRET file=C:/private"));
        mvc.perform(get("/api/cases").header("Host","localhost:80")).andExpect(status().isServiceUnavailable()).andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("TOP_SECRET"))));}
    @Test void successfulResponseUsesNoStoreHeaders() throws Exception {
        when(graphs.cases()).thenReturn(obj("items",java.util.List.of()));mvc.perform(get("/api/cases").header("Host","localhost:80"))
                .andExpect(status().isOk()).andExpect(header().string("Cache-Control","no-store")).andExpect(header().string("X-Content-Type-Options","nosniff"));}
}
