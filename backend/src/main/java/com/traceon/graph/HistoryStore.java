package com.traceon.graph;

import static com.traceon.common.Json.*;
import com.traceon.common.*;
import java.nio.file.*;
import java.sql.*;
import java.time.Instant;
import java.util.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/** Immutable SQLite archive, compatible with history_store.py schema version 1. */
@Component
public class HistoryStore {
    static final int MAX_ENTRY=8_000_000,MAX_TOTAL=64_000_000,MAX_ENTRIES=100;
    private final Path path;
    public record Saved(Map<String,Object> body,String sha,boolean created) {}
    public record Page(List<Saved> items,String cursor,int total) {}
    public HistoryStore(@Value("${traceon.history-db}") String path) { this.path=Path.of(path).toAbsolutePath(); }
    public static void validId(String id) {
        if(id==null || !id.matches("H-[0-9a-f]{24}"))throw new ApiException(400,"invalid_history_id","저장 목록에서 분석을 다시 선택해 주세요.");
    }
    private Connection open() throws Exception {
        Files.createDirectories(path.getParent());Connection db=DriverManager.getConnection("jdbc:sqlite:"+path);
        try {
            try(var statement=db.createStatement()) {
                statement.execute("PRAGMA busy_timeout=2000");statement.execute("PRAGMA synchronous=FULL");
                int version;try(var r=statement.executeQuery("PRAGMA user_version")){r.next();version=r.getInt(1);}
                Set<String> tables=new HashSet<>();
                try(var r=statement.executeQuery("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")){while(r.next())tables.add(r.getString(1));}
                if(version==0 && tables.isEmpty()) {
                    statement.execute("BEGIN IMMEDIATE");
                    statement.execute("CREATE TABLE analysis_history (id TEXT PRIMARY KEY,saved_at TEXT NOT NULL,tx_id TEXT NOT NULL,has_review INTEGER NOT NULL,capture_sha256 TEXT NOT NULL UNIQUE,sha256 TEXT NOT NULL,body BLOB NOT NULL,bytes INTEGER NOT NULL)");
                    statement.execute("CREATE INDEX history_order ON analysis_history(saved_at DESC,id DESC)");
                    statement.execute("PRAGMA user_version=1");statement.execute("COMMIT");
                } else {
                    List<String> columns=new ArrayList<>();try(var r=statement.executeQuery("PRAGMA table_info(analysis_history)")){while(r.next())columns.add(r.getString("name"));}
                    if(version!=1 || !tables.equals(Set.of("analysis_history")) || !columns.equals(List.of("id","saved_at","tx_id","has_review","capture_sha256","sha256","body","bytes")))
                        throw new ApiException(409,"history_schema_mismatch","분석 이력 전용 저장소가 아닙니다. 기존 내용을 변경하지 않았습니다.");
                }
            }
            return db;
        }catch(Exception ex){db.close();throw ex;}
    }
    private static ApiException failure(Exception ex) {
        if(ex instanceof ApiException api)return api;
        return new ApiException(503,"history_storage_unavailable","이력 저장소를 확인하지 못했습니다. 기존 저장본은 덮어쓰지 않았습니다.");
    }
    private Saved read(Connection db,String id) throws SQLException {
        try(var query=db.prepareStatement("SELECT id,saved_at,tx_id,has_review,capture_sha256,sha256,substr(body,1,8000001) AS body,bytes FROM analysis_history WHERE id=?")) {
            query.setString(1,id);try(var row=query.executeQuery()) {
                if(!row.next())throw new ApiException(404,"history_not_found","저장된 분석을 찾지 못했습니다.");
                try {
                    byte[] raw=row.getBytes("body");String sha=row.getString("sha256");
                    if(raw==null || raw.length<1 || raw.length>MAX_ENTRY || raw.length!=row.getInt("bytes") || !Json.sha(raw).equals(sha))throw new IllegalArgumentException();
                    var body=parse(raw);var capture=child(body,"capture");
                    if(!"1.0".equals(body.get("schemaVersion")) || !id.equals(body.get("historyId"))
                            || !row.getString("saved_at").equals(body.get("savedAt"))
                            || !row.getString("tx_id").equals(child(child(capture,"payload"),"graph").get("targetTxId"))
                            || (capture.get("review")!=null)!=(row.getInt("has_review")!=0)
                            || !hash(capture).equals(row.getString("capture_sha256")))throw new IllegalArgumentException();
                    HistoryService.validate(capture);return new Saved(body,sha,false);
                }catch(RuntimeException ex) {
                    throw new ApiException(409,"history_integrity_failed","저장된 거래·초안·원문의 일치를 확인하지 못했습니다.");
                }
            }
        }
    }
    public Saved put(Map<String,Object> capture) {
        try(var db=open();var statement=db.createStatement()) {
            statement.execute("BEGIN IMMEDIATE");String captureHash=hash(capture);
            try(var query=db.prepareStatement("SELECT id FROM analysis_history WHERE capture_sha256=?")) {
                query.setString(1,captureHash);String id=null;try(var rows=query.executeQuery()){if(rows.next())id=rows.getString(1);}
                if(id!=null){var saved=read(db,id);statement.execute("COMMIT");return saved;}
            }
            String id="H-"+UUID.randomUUID().toString().replace("-","").substring(0,24);
            String savedAt=java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSSxxx")
                    .withZone(java.time.ZoneOffset.UTC).format(Instant.now());
            var body=obj("schemaVersion","1.0","historyId",id,"savedAt",savedAt,"capture",capture);byte[] raw=bytes(body);
            if(raw.length>MAX_ENTRY)throw new ApiException(413,"history_entry_too_large","저장본 크기가 8MB를 넘었습니다.");
            try(var count=statement.executeQuery("SELECT count(*),coalesce(sum(bytes),0) FROM analysis_history")) {
                count.next();
                if(count.getInt(1)>=MAX_ENTRIES || count.getLong(2)+raw.length>MAX_TOTAL)
                    throw new ApiException(409,"history_capacity","분석 이력 저장 한도에 도달했습니다. 기존 저장본은 보존합니다.");
            }
            String sha=Json.sha(raw);
            try(var insert=db.prepareStatement("INSERT INTO analysis_history VALUES (?,?,?,?,?,?,?,?)")) {
                insert.setString(1,id);insert.setString(2,str(body,"savedAt"));insert.setString(3,str(child(child(capture,"payload"),"graph"),"targetTxId"));
                insert.setInt(4,capture.get("review")==null?0:1);insert.setString(5,captureHash);insert.setString(6,sha);insert.setBytes(7,raw);insert.setInt(8,raw.length);insert.executeUpdate();
            }
            statement.execute("COMMIT");return new Saved(body,sha,true);
        }catch(Exception ex){throw failure(ex);}
    }
    public Saved get(String id) {
        validId(id);try(var db=open()){return read(db,id);}catch(Exception ex){throw failure(ex);}
    }
    public Page list(String cursor) {
        if(cursor!=null)validId(cursor);
        try(var db=open()) {
            String savedAt=null;if(cursor!=null)savedAt=str(read(db,cursor).body,"savedAt");
            String sql="SELECT id FROM analysis_history "+(cursor==null?"":"WHERE (saved_at,id)<(?,?) ")+"ORDER BY saved_at DESC,id DESC LIMIT 21";
            List<String> ids=new ArrayList<>();try(var query=db.prepareStatement(sql)) {
                if(cursor!=null){query.setString(1,savedAt);query.setString(2,cursor);}
                try(var rows=query.executeQuery()){while(rows.next())ids.add(rows.getString(1));}
            }
            List<Saved> items=new ArrayList<>();for(String id:ids.subList(0,Math.min(20,ids.size())))items.add(read(db,id));
            int total;try(var statement=db.createStatement();var rows=statement.executeQuery("SELECT count(*) FROM analysis_history")){rows.next();total=rows.getInt(1);}
            return new Page(items,ids.size()>20?ids.get(19):null,total);
        }catch(Exception ex){throw failure(ex);}
    }
}
