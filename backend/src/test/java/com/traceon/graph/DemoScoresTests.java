package com.traceon.graph;

import static com.traceon.common.Json.*;
import static org.junit.jupiter.api.Assertions.*;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class DemoScoresTests {
    @TempDir Path temporary;

    @Test
    void onlyTheSameGraphAtTheSameTimeReceivesTheScore() throws Exception {
        var graph = obj("targetTxId", "TX-000000001", "asOf", "2022-09-01T12:00:00", "edges", List.of());
        var row = obj("targetTxId", graph.get("targetTxId"), "asOf", graph.get("asOf"),
                "graphSha256", hash(graph), "score", 0.55);
        var filename = temporary.resolve("scores.json");
        Files.write(filename, bytes(obj("schemaVersion", "traceon-demo-hybrid-v1",
                "modelSha256", "a".repeat(64), "ruleFreezeSha256", "b".repeat(64), "scores", List.of(row))));
        var scores = DemoScores.load(filename.toString());
        assertEquals(0.55, ((Number) scores.forGraph(graph).get("score")).doubleValue());
        graph.put("edges", List.of(obj("txId", "TX-000000002")));
        assertNull(scores.forGraph(graph));
    }
}
