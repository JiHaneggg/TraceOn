package com.traceon.graph;

import static com.traceon.common.Json.*;
import com.traceon.common.*;
import jakarta.servlet.http.HttpServletRequest;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.springframework.http.*;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
public class GraphController {
    private final GraphService graphs;private final ReviewService reviews;private final HistoryService history;
    public GraphController(GraphService graphs,ReviewService reviews,HistoryService history){this.graphs=graphs;this.reviews=reviews;this.history=history;}
    private static ApiException invalid(){return new ApiException(400,"invalid_request","현재 화면의 거래·조회 ID와 허용된 필드로 요청해 주세요.");}
    private static Map<String,String> query(HttpServletRequest request,Set<String> allowed,Set<String> required) {
        var out=new HashMap<String,String>();request.getParameterMap().forEach((k,v)->{
            if(!allowed.contains(k) || v.length!=1 || v[0].isEmpty() || v[0].length()>250)throw invalid();out.put(k,v[0]);});
        if(!out.keySet().containsAll(required))throw invalid();return out;
    }
    private static Map<String,Object> body(byte[] raw,String... fields) {
        try{var value=parse(raw);if(!value.keySet().equals(Set.of(fields)))throw invalid();return value;}
        catch(ApiException ex){throw ex;}catch(RuntimeException ex){throw invalid();}
    }
    private static String field(Map<String,Object> value,String key) {
        if(!(value.get(key) instanceof String text) || text.isBlank() || text.length()>250)throw invalid();return text;
    }
    @GetMapping("/health") public Object health(HttpServletRequest request){query(request,Set.of(),Set.of());return graphs.health();}
    @GetMapping("/cases") public Object cases(HttpServletRequest request){query(request,Set.of(),Set.of());return graphs.cases();}
    @GetMapping("/graph") public Object graph(HttpServletRequest request) {
        var q=query(request,Set.of("txId","hops","windowDays"),Set.of("txId"));
        String hops=q.getOrDefault("hops","2"),days=q.getOrDefault("windowDays","7");
        if(!Set.of("1","2","3").contains(hops) || !Set.of("1","3","7").contains(days))throw invalid();
        return graphs.initial(q.get("txId"),Integer.parseInt(hops),Integer.parseInt(days));
    }
    @PostMapping("/graph/expand") public Object expand(@RequestBody byte[] raw,HttpServletRequest request){query(request,Set.of(),Set.of());var b=body(raw,"snapshotId","accountKey");return graphs.expand(field(b,"snapshotId"),field(b,"accountKey"));}
    @PostMapping("/review/prepare") public Object prepare(@RequestBody byte[] raw,HttpServletRequest request) {
        query(request,Set.of(),Set.of());var b=body(raw,"snapshotId","findingIndex");
        if(b.get("findingIndex")!=null && !(b.get("findingIndex") instanceof Integer))throw invalid();
        return reviews.prepare(field(b,"snapshotId"),(Integer)b.get("findingIndex"));
    }
    @PostMapping("/review/verify") public Object verify(@RequestBody byte[] raw,HttpServletRequest request){query(request,Set.of(),Set.of());var b=body(raw,"reviewId");return reviews.verify(field(b,"reviewId"));}
    @PostMapping("/review/judge") public Object judge(@RequestBody byte[] raw,HttpServletRequest request){query(request,Set.of(),Set.of());var b=body(raw,"reviewId");return reviews.judge(field(b,"reviewId"));}
    @GetMapping("/review/judge") public Object judgeStatus(HttpServletRequest request){var q=query(request,Set.of("reviewId"),Set.of("reviewId"));return reviews.judgeStatus(q.get("reviewId"));}
    @PostMapping("/review/fact") public Object fact(@RequestBody byte[] raw,HttpServletRequest request){query(request,Set.of(),Set.of());var b=body(raw,"reviewId","factId");return reviews.fact(field(b,"reviewId"),field(b,"factId"));}
    @GetMapping("/review/citation") public Object citation(HttpServletRequest request){var q=query(request,Set.of("reviewId","chunkId"),Set.of("reviewId","chunkId"));return reviews.citation(q.get("reviewId"),q.get("chunkId"));}
    @GetMapping("/review/download") public ResponseEntity<byte[]> reviewDownload(HttpServletRequest request) {
        var q=query(request,Set.of("reviewId","format"),Set.of("reviewId","format"));String format=q.get("format");
        if(!Set.of("json","markdown").contains(format))throw invalid();
        var review=reviews.download(q.get("reviewId"));boolean markdown="markdown".equals(format);
        return download("traceon-review."+(markdown?"md":"json"),markdown?"text/markdown":"application/json",
                markdown?str(child(review,"report"),"markdown").getBytes(StandardCharsets.UTF_8):bytes(review));
    }
    @PostMapping("/history/save") public Object save(@RequestBody byte[] raw,HttpServletRequest request) {
        query(request,Set.of(),Set.of());var b=body(raw,"snapshotId","reviewId");String review=b.get("reviewId")==null?null:field(b,"reviewId");
        return history.bounded(()->history.save(field(b,"snapshotId"),review));
    }
    @GetMapping("/history") public Object listing(HttpServletRequest request){var q=query(request,Set.of("cursor"),Set.of());return history.bounded(()->history.listing(q.get("cursor")));}
    @GetMapping("/history/open") public Object open(HttpServletRequest request){var q=query(request,Set.of("historyId"),Set.of("historyId"));return history.bounded(()->history.open(q.get("historyId")));}
    @GetMapping("/history/verify") public Object historyVerify(HttpServletRequest request){var q=query(request,Set.of("historyId"),Set.of("historyId"));return history.bounded(()->history.verify(q.get("historyId")));}
    @GetMapping("/history/citation") public Object historyCitation(HttpServletRequest request){var q=query(request,Set.of("historyId","chunkId"),Set.of("historyId","chunkId"));return history.bounded(()->history.citation(q.get("historyId"),q.get("chunkId")));}
    @GetMapping("/history/fact") public Object historyFact(HttpServletRequest request){var q=query(request,Set.of("historyId","factId"),Set.of("historyId","factId"));return history.bounded(()->history.fact(q.get("historyId"),q.get("factId")));}
    @GetMapping("/history/download") public ResponseEntity<byte[]> historyDownload(HttpServletRequest request) {
        var q=query(request,Set.of("historyId","format"),Set.of("historyId","format"));String format=q.get("format");
        if(!Set.of("json","markdown").contains(format))throw invalid();
        return history.bounded(()->{
            var archive=history.archive(q.get("historyId"));boolean markdown="markdown".equals(format);byte[] raw;
            if(markdown) {
                if(child(archive,"capture").get("review")==null)throw invalid();
                String text="# 저장 당시 분석 초안\n\n저장 시각: "+str(archive,"savedAt")+"\n\n저장본 SHA-256: "+str(archive,"snapshotSha256")
                        +"\n\n현재 자료는 재조회하지 않았습니다. 사람 검토가 필요합니다.\n\n"+str(child(child(child(archive,"capture"),"review"),"report"),"markdown");
                raw=text.getBytes(StandardCharsets.UTF_8);
            }else raw=bytes(archive);
            return download("traceon-"+q.get("historyId")+"-archive."+(markdown?"md":"json"),markdown?"text/markdown":"application/json",raw);
        });
    }
    private static ResponseEntity<byte[]> download(String name,String mime,byte[] bytes) {
        return ResponseEntity.ok().contentType(MediaType.parseMediaType(mime+"; charset=utf-8"))
                .header(HttpHeaders.CONTENT_DISPOSITION,"attachment; filename=\""+name+"\"").body(bytes);
    }
}
