package com.traceon.graph;

import static com.traceon.common.Json.*;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

/** Read-only, versioned scores for exact demo graph snapshots. */
final class DemoScores {
    static final DemoScores EMPTY = new DemoScores(Map.of());
    private final Map<String, Map<String, Object>> scores;

    private DemoScores(Map<String, Map<String, Object>> scores) {
        this.scores = Map.copyOf(scores);
    }

    static DemoScores load(String filename) throws IOException {
        if (filename.isBlank() || !Files.exists(Path.of(filename))) return EMPTY;
        var file = Path.of(filename);
        if (Files.size(file) > 1_000_000) throw new IllegalArgumentException("Demo score file size");
        var body = parse(Files.readAllBytes(file));
        if (!"traceon-demo-hybrid-v1".equals(body.get("schemaVersion"))) {
            throw new IllegalArgumentException("Demo score version");
        }
        String model = checksum(body, "modelSha256");
        String rules = checksum(body, "ruleFreezeSha256");
        var parsed = new HashMap<String, Map<String, Object>>();
        for (var row : rows(body.get("scores"))) {
            String id = str(row, "targetTxId");
            String asOf = str(row, "asOf");
            String graphHash = checksum(row, "graphSha256");
            if (!(row.get("score") instanceof Number score) || !Double.isFinite(score.doubleValue())
                    || score.doubleValue() < 0 || score.doubleValue() > 1) {
                throw new IllegalArgumentException("Demo score outside 0..1");
            }
            if (parsed.putIfAbsent(graphHash, obj("targetTxId", id, "asOf", asOf, "graphSha256", graphHash,
                    "score", score, "modelSha256", model, "ruleFreezeSha256", rules,
                    "source", "frozen_demo_scores")) != null) {
                throw new IllegalArgumentException("Duplicate demo graph hash");
            }
        }
        return new DemoScores(parsed);
    }

    private static String checksum(Map<String, Object> row, String field) {
        String value = str(row, field);
        if (!value.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("Invalid demo score hash");
        return value;
    }

    Map<String, Object> forGraph(Map<String, Object> graph) {
        if (scores.isEmpty()) return null;
        var match = scores.get(hash(graph));
        if (match == null || !graph.get("targetTxId").equals(match.get("targetTxId"))
                || !graph.get("asOf").equals(match.get("asOf"))) return null;
        return copy(match);
    }
}
