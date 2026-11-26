package com.traceon.graph;

import static com.traceon.common.Json.*;
import com.traceon.common.*;
import java.io.IOException;
import java.math.BigDecimal;
import java.net.URI;
import java.nio.file.*;
import java.security.SecureRandom;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.concurrent.Semaphore;
import java.util.function.Supplier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

@Service
public class GraphService {
    public static final int MAX_EDGES=1000, PAGE_SIZE=25;
    private static final Set<String> EDGE_FIELDS=Set.of("txId","timestamp","fromAccountKey","toAccountKey",
            "amountPaid","paymentCurrency","amountReceived","receivingCurrency","paymentFormat");
    public static final Comparator<Map<String,Object>> ORDER=Comparator.comparing((Map<String,Object> e)->str(e,"timestamp"))
            .thenComparing(e->str(e,"txId"));
    private final GraphDatabase database; private final BoundedHttp http; private final URI ruleUrl;
    private final List<String> targets; private final Clock clock;
    private DemoScores demoScores = DemoScores.EMPTY;
    private final Semaphore gate=new Semaphore(2);
    private final Object monitor=new Object();
    private final LinkedHashMap<String,Snapshot> snapshots=new LinkedHashMap<>();
    static final class Snapshot {
        final Map<String,Object> payload,state;final Instant expires;
        boolean busy,invalid;String successor,expandedAccount;
        Snapshot(Map<String,Object> payload,Map<String,Object> state,Instant expires) {
            this.payload=copy(payload);this.state=copy(state);this.expires=expires;
        }
    }
    @org.springframework.beans.factory.annotation.Autowired
    public GraphService(GraphDatabase database,BoundedHttp http,@Value("${traceon.rule-url}") String ruleUrl,
            @Value("${traceon.manifest}") String manifest,
            @Value("${traceon.score-file:}") String scoreFile) throws IOException {
        this(database,http,URI.create(ruleUrl),readTargets(Path.of(manifest)),Clock.systemUTC());
        demoScores = DemoScores.load(scoreFile);
    }
    GraphService(GraphDatabase database,BoundedHttp http,URI ruleUrl,List<String> targets,Clock clock) {
        this.database=database;this.http=http;this.ruleUrl=ruleUrl;this.targets=List.copyOf(targets);this.clock=clock;
    }
    private static List<String> readTargets(Path path) throws IOException {
        // Health and saved archives remain usable before demo data is installed.
        if(!Files.exists(path)) return List.of();
        if (Files.size(path)>1_000_000) throw new IllegalArgumentException("Manifest size");
        var raw=(List<?>)parse(Files.readAllBytes(path)).get("targets");
        if (raw==null || raw.isEmpty() || raw.size()>100 || new HashSet<>(raw).size()!=raw.size()
                || raw.stream().anyMatch(x->!(x instanceof String s) || !s.matches("TX-\\d{9}")))
            throw new IllegalArgumentException("Manifest targets");
        return raw.stream().map(String.class::cast).toList();
    }
    public static String token() {
        byte[] bytes=new byte[24];new SecureRandom().nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }
    public <T> T bounded(Supplier<T> action) {
        if (!gate.tryAcquire()) throw new ApiException(429,"graph_busy","조회 중입니다. 잠시 후 다시 시도해 주세요.");
        try { return action.get(); } finally { gate.release(); }
    }
    public Map<String,Object> health() {
        return bounded(()->{database.read().query(GraphDatabase.Query.HEALTH,Map.of());
            return obj("service","traceon-spring","status","ok","database","connected","readOnly",true);});
    }
    public Map<String,Object> cases() {
        requireTargets();
        return bounded(()->{
            var edges=database.read().edges(GraphDatabase.Query.CASES,obj("ids",targets));
            Map<String,Map<String,Object>> byId=index(edges);
            if (edges.size()!=targets.size() || !byId.keySet().equals(new HashSet<>(targets))) throw ApiException.unavailable();
            return obj("schemaVersion","1.0","items",targets.stream().map(byId::get).toList(),
                    "source","neo4j","scope","selected_demo_subset","total",targets.size());
        });
    }
    public Map<String,Object> initial(String txId,int hops,int days) {
        requireTargets();
        if (!targets.contains(txId) || !Set.of(1,2,3).contains(hops) || !Set.of(1,3,7).contains(days))
            throw new ApiException(400,"invalid_range","목록의 거래와 허용된 조회 범위를 선택해 주세요.");
        return bounded(()->{
            var read=database.read();var version=read.version();
            var found=read.edges(GraphDatabase.Query.TARGET,obj("txId",txId));
            if (found.size()!=1) throw ApiException.unavailable();
            var target=found.getFirst();String asOf=str(target,"timestamp");
            String since=LocalDateTime.parse(asOf).minusDays(days).format(DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss"));
            Map<String,Map<String,Object>> selected=index(found);
            Set<String> frontier=new TreeSet<>(List.of(str(target,"fromAccountKey"),str(target,"toAccountKey"))),visited=new HashSet<>();
            var accounts=new LinkedHashMap<String,Object>();boolean truncated=false;
            for (int depth=0;depth<hops;depth++) {
                Set<String> next=new TreeSet<>();frontier.removeAll(visited);
                for (String account:frontier) {
                    visited.add(account);
                    var rows=read.edges(GraphDatabase.Query.PAGE,pageParams(account,since,asOf,null,51));
                    validatePage(rows,account,since,asOf,null,51,selected);
                    var visible=rows.subList(0,Math.min(50,rows.size()));
                    accounts.put(account,obj("cursor",visible.isEmpty()?null:cursor(visible.getLast()),"hasMore",rows.size()>50,
                            "fetchedRows",visible.size(),"pages",0));
                    truncated|=rows.size()>50;
                    for (var edge:visible) {
                        if (!selected.containsKey(str(edge,"txId")) && selected.size()>=MAX_EDGES) { truncated=true;continue; }
                        selected.put(str(edge,"txId"),edge);
                        next.add(str(edge,"fromAccountKey"));next.add(str(edge,"toAccountKey"));
                    }
                }
                next.removeAll(visited);frontier=next;
            }
            var graph=obj("schemaVersion","1.0","targetTxId",txId,"asOf",asOf,"windowStart",since,
                    "edges",sortedEdges(selected),"nodes",nodes(selected.values()),
                    "meta",obj("hops",hops,"maxNeighbors",50,"maxEdges",MAX_EDGES,"truncated",truncated,
                            "timeDirection","past-only","sourceScope",version.get("name"),"sourceGraphComplete",false));
            var payload=render(graph);
            if (!version.equals(read.version())) throw new ApiException(409,"dataset_changed","조회 중 자료가 변경됐습니다. 다시 조회해 주세요.");
            var state=obj("version",version,"accounts",accounts,"pages",0,"initialTransactionCount",selected.size(),
                    "pageHistory",new ArrayList<>(),"lastPage",null);
            synchronized(monitor) { return remember(payload,state); }
        });
    }
    private void requireTargets() {
        if(targets.isEmpty())throw new ApiException(503,"demo_manifest_unavailable","시연 자료와 대상 목록을 준비한 뒤 서버를 다시 시작해 주세요.");
    }
    public Map<String,Object> render(Map<String,Object> graph) {
        var rules=http.call(ruleUrl,obj("graph",graph),null,2_000_000,Duration.ofSeconds(8));
        var edges=rows(graph.get("edges"));var byId=index(edges);Set<String> highlights=new TreeSet<>();
        for (var finding:rows(rules.get("findings"))) for (Object id:(List<?>)finding.get("evidenceTxIds")) highlights.add((String)id);
        if (!byId.keySet().containsAll(highlights) || !graph.get("targetTxId").equals(rules.get("txId")))
            throw new ApiException(502,"rule_identity_mismatch","룰 근거와 조회 거래가 일치하지 않습니다.");
        var paid=new TreeMap<String,BigDecimal>();var received=new TreeMap<String,BigDecimal>();
        for (var e:edges) {
            paid.merge(str(e,"paymentCurrency"),new BigDecimal(str(e,"amountPaid")),BigDecimal::add);
            received.merge(str(e,"receivingCurrency"),new BigDecimal(str(e,"amountReceived")),BigDecimal::add);
        }
        var result = obj("schemaVersion","1.0","graph",graph,"target",byId.get(str(graph,"targetTxId")),"ruleResult",rules,
                "highlightTxIds",new ArrayList<>(highlights),"summary",obj("accounts",((List<?>)graph.get("nodes")).size(),
                        "transactions",edges.size(),"evidenceTransactions",highlights.size(),"patterns",rows(rules.get("findings")).size(),
                        "grossPaid",decimalStrings(paid),"grossReceived",decimalStrings(received)),
                "source",obj("kind","neo4j","queriedAt",clock.instant().toString(),"scope","selected_demo_subset",
                        "sourceGraphComplete",false,"readOnly",true,"ruleScoreIsProbability",false),
                "notice","시연용 부분 데이터의 관찰입니다. 룰 점수는 확률이 아니며 범죄·정상 거래를 확정하지 않습니다.");
        var hybrid = demoScores.forGraph(graph);
        if (hybrid != null) result.put("hybridScore", hybrid);
        return result;
    }
    private static Map<String,String> decimalStrings(Map<String,BigDecimal> values) {
        var out=new TreeMap<String,String>();values.forEach((k,v)->out.put(k,v.toPlainString()));return out;
    }
    private Snapshot entry(String id) {
        var entry=snapshots.get(id);
        if (entry==null || !clock.instant().isBefore(entry.expires) || entry.invalid) throw ApiException.stale();
        return entry;
    }
    public Map<String,Object> current(String id) {
        synchronized(monitor) {var entry=entry(id);if(entry.successor!=null) throw ApiException.stale();return copy(entry.payload);}
    }
    public <T> T ifCurrent(String id,Supplier<T> action) {
        synchronized(monitor) { current(id);return action.get(); }
    }
    private Map<String,Object> remember(Map<String,Object> payload,Map<String,Object> state) {
        snapshots.entrySet().removeIf(e->!clock.instant().isBefore(e.getValue().expires));
        payload=copy(payload);payload.put("accountExpansion",status(child(payload,"graph"),state));
        String id=token();payload.put("reviewSnapshotId",id);
        snapshots.put(id,new Snapshot(payload,state,clock.instant().plusSeconds(1200)));
        while(snapshots.size()>24) snapshots.remove(snapshots.keySet().iterator().next());
        return copy(payload);
    }
    public Map<String,Object> expand(String id,String account) {
        return bounded(()->expandBounded(id,account));
    }
    private Map<String,Object> expandBounded(String id,String account) {
        Snapshot original;Map<String,Object> state,graph;
        synchronized(monitor) {
            original=entry(id);
            if (original.successor!=null) {
                var successor=entry(original.successor);
                if (account.equals(original.expandedAccount) && successor.successor==null) return copy(successor.payload);
                throw ApiException.stale();
            }
            if (original.busy) throw new ApiException(429,"expansion_busy","같은 그래프를 추가 조회 중입니다.");
            state=copy(original.state);graph=child(original.payload,"graph");var status=status(graph,state);
            var candidate=rows(status.get("candidates")).stream().filter(c->account.equals(c.get("accountKey"))).findFirst();
            if (Boolean.TRUE.equals(status.get("budgetReached"))) throw new ApiException(409,"expansion_budget","추가 조회 상한에 도달했습니다.");
            if (candidate.isEmpty() || !Boolean.TRUE.equals(candidate.get().get("canExpand")))
                throw new ApiException(400,"account_not_expandable","응답의 추가 조회 가능한 계좌를 선택해 주세요.");
            original.busy=true;
        }
        try {
            var read=database.read();checkVersion(read,state);
            var existing=index(rows(graph.get("edges")));
            var live=read.edges(GraphDatabase.Query.VERIFY,obj("ids",new ArrayList<>(existing.keySet()),"limit",1001));
            if (live.size()!=existing.size() || !index(live).equals(existing)) throw changed("source_changed");
            var accounts=child(state,"accounts");
            var item=accounts.containsKey(account)?child(accounts,account):obj("cursor",null,"hasMore",true,"fetchedRows",0,"pages",0);
            int size=Math.min(PAGE_SIZE,MAX_EDGES-existing.size());
            var oldCursor=item.get("cursor");
            var page=read.edges(GraphDatabase.Query.PAGE,pageParams(account,str(graph,"windowStart"),str(graph,"asOf"),oldCursor,size+1));
            validatePage(page,account,str(graph,"windowStart"),str(graph,"asOf"),oldCursor,size+1,existing);
            checkVersion(read,state);
            var consumed=page.subList(0,Math.min(size,page.size()));Set<String> before=new HashSet<>(existing.keySet());
            for (var edge:consumed) existing.put(str(edge,"txId"),edge);
            var added=new TreeSet<>(existing.keySet());added.removeAll(before);
            accounts.put(account,obj("cursor",consumed.isEmpty()?oldCursor:cursor(consumed.getLast()),"hasMore",page.size()>size,
                    "fetchedRows",num(item,"fetchedRows")+consumed.size(),"pages",num(item,"pages")+1));
            state.put("pages",num(state,"pages")+1);
            var last=obj("accountKey",account,"readRows",consumed.size(),"addedTransactions",added.size(),
                    "duplicateTransactions",consumed.size()-added.size(),"addedTxIds",new ArrayList<>(added));
            state.put("lastPage",last);rows(state.get("pageHistory")).add(last);
            var expanded=copy(graph);expanded.put("edges",sortedEdges(existing));expanded.put("nodes",nodes(existing.values()));
            child(expanded,"meta").putAll(obj("queryMode","bounded_account_pages_v1","additionalReadPages",state.get("pages"),"additionalPageSize",PAGE_SIZE));
            child(expanded,"meta").put("truncated",!rows(status(expanded,state).get("candidates")).isEmpty() || existing.size()>=MAX_EDGES);
            var rendered=render(expanded);checkVersion(read,state);
            synchronized(monitor) {
                if (entry(id)!=original) throw ApiException.stale();
                var result=remember(rendered,state);original.successor=str(result,"reviewSnapshotId");original.expandedAccount=account;return result;
            }
        } catch (ApiException ex) {
            if (Set.of("dataset_changed","source_changed").contains(ex.code)) synchronized(monitor) { original.invalid=true; }
            throw ex;
        } finally { synchronized(monitor) {original.busy=false;} }
    }
    private static ApiException changed(String code) {return new ApiException(409,code,"원본 자료가 변경됐습니다. 거래를 다시 조회해 주세요.");}
    private static void checkVersion(GraphDatabase.Read read,Map<String,Object> state) {
        try { if (!read.version().equals(state.get("version"))) throw changed("dataset_changed"); }
        catch(ApiException ex) {if("dataset_unmanaged".equals(ex.code))throw changed("dataset_changed");throw ex;}
    }
    static Map<String,Object> status(Map<String,Object> graph,Map<String,Object> state) {
        var candidates=new ArrayList<Map<String,Object>>();var accounts=child(state,"accounts");
        eligible(graph).forEach((key,depth)->{
            Map<String,Object> item=accounts.containsKey(key)?child(accounts,key):null;
            if (item!=null && !Boolean.TRUE.equals(item.get("hasMore"))) return;
            int pages=item==null?0:num(item,"pages");
            candidates.add(obj("accountKey",key,"depth",depth,"reason",item==null?"unqueried":"more_history",
                    "fetchedRows",item==null?0:item.get("fetchedRows"),"pagesUsed",pages,"canExpand",pages<4));
        });
        boolean budget=num(state,"pages")>=8 || rows(graph.get("edges")).size()>=MAX_EDGES;
        return obj("schemaVersion","1.0","enabled",true,"canExpand",!budget && candidates.stream().anyMatch(c->Boolean.TRUE.equals(c.get("canExpand"))),
                "pageSize",PAGE_SIZE,"pagesUsed",state.get("pages"),"maxPages",8,"maxAccountPages",4,"maxTotalEdges",MAX_EDGES,
                "budgetReached",budget,"initialTransactionCount",state.get("initialTransactionCount"),
                "totalAddedTransactions",rows(graph.get("edges")).size()-num(state,"initialTransactionCount"),"candidates",candidates,
                "lastPage",state.get("lastPage"),"pageHistory",state.get("pageHistory"),"datasetSha256",child(state,"version").get("sha256"),
                "sourceGraphComplete",false,"notice","선택 계좌의 같은 기간 거래를 추가합니다. 전체 원장 완전성은 확인하지 않았습니다.");
    }
    static Map<String,Integer> eligible(Map<String,Object> graph) {
        Map<String,Set<String>> adjacency=new TreeMap<>();var edges=rows(graph.get("edges"));
        for (var e:edges) {String a=str(e,"fromAccountKey"),b=str(e,"toAccountKey");
            adjacency.computeIfAbsent(a,k->new TreeSet<>()).add(b);adjacency.computeIfAbsent(b,k->new TreeSet<>()).add(a);}
        var target=index(edges).get(str(graph,"targetTxId"));var distance=new TreeMap<String,Integer>();var queue=new ArrayDeque<String>();
        for (String key:List.of(str(target,"fromAccountKey"),str(target,"toAccountKey"))) if(distance.putIfAbsent(key,0)==null)queue.add(key);
        int hops=num(child(graph,"meta"),"hops");
        while(!queue.isEmpty()) {String key=queue.remove();int depth=distance.get(key);if(depth>=hops)continue;
            for(String other:adjacency.getOrDefault(key,Set.of()))if(!distance.containsKey(other)){distance.put(other,depth+1);queue.add(other);}}
        distance.entrySet().removeIf(e->e.getValue()>=hops);return distance;
    }
    private static Map<String,Object> pageParams(String account,String since,String asOf,Object cursor,int limit) {
        var pair=cursor==null?List.of():((List<?>)cursor);
        return obj("accountKey",account,"since",since,"asOf",asOf,"limit",limit,
                "cursorTime",pair.isEmpty()?null:pair.get(0),"cursorId",pair.isEmpty()?null:pair.get(1));
    }
    private static List<String> cursor(Map<String,Object> e) {return List.of(str(e,"timestamp"),str(e,"txId"));}
    static void validatePage(List<Map<String,Object>> page,String account,String since,String asOf,Object cursor,int limit,
            Map<String,Map<String,Object>> existing) {
        if(page.size()>limit)throw new IllegalArgumentException("Page limit");
        List<?> previous=cursor==null?null:(List<?>)cursor;Set<String> ids=new HashSet<>();
        for(var e:page){validateEdge(e);String time=str(e,"timestamp"),id=str(e,"txId");
            if(!ids.add(id) || time.compareTo(since)<0 || time.compareTo(asOf)>0
                    || (!account.equals(e.get("fromAccountKey"))&&!account.equals(e.get("toAccountKey")))
                    || e.get("fromAccountKey").equals(e.get("toAccountKey"))
                    || (previous!=null && (time.compareTo((String)previous.get(0))>0
                    || (time.equals(previous.get(0)) && id.compareTo((String)previous.get(1))>=0))))throw new IllegalArgumentException("Page scope/order");
            if(existing.containsKey(id) && !existing.get(id).equals(e))throw changed("source_changed");
            previous=cursor(e);
        }
    }
    public static void validateEdge(Map<String,Object> e) {
        if(!e.keySet().equals(EDGE_FIELDS) || e.values().stream().anyMatch(v->!(v instanceof String s)||s.isEmpty()||s.length()>200))
            throw new IllegalArgumentException("Source edge contract");
        LocalDateTime.parse(str(e,"timestamp"));
        if(!str(e,"txId").matches("TX-\\d{9}") || new BigDecimal(str(e,"amountPaid")).signum()<0
                || new BigDecimal(str(e,"amountReceived")).signum()<0)throw new IllegalArgumentException("Source amount/ID");
    }
    public static Map<String,Map<String,Object>> index(List<Map<String,Object>> edges) {
        Map<String,Map<String,Object>> result=new LinkedHashMap<>();
        for(var edge:edges)if(result.put(str(edge,"txId"),edge)!=null)throw new IllegalArgumentException("Duplicate transaction ID");
        return result;
    }
    private static List<Map<String,Object>> sortedEdges(Map<String,Map<String,Object>> selected) {return selected.values().stream().sorted(ORDER).toList();}
    private static List<Map<String,Object>> nodes(Collection<Map<String,Object>> edges) {
        var keys=new TreeSet<String>();for(var e:edges){keys.add(str(e,"fromAccountKey"));keys.add(str(e,"toAccountKey"));}
        return keys.stream().map(k->obj("accountKey",k)).toList();
    }
}
