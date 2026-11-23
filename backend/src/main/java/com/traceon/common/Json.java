package com.traceon.common;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.json.JsonMapper;

/** Canonical UTF-8 JSON also used by the Python graph/RAG contracts. */
public final class Json {
    private Json() {}
    public static final JsonMapper MAPPER = JsonMapper.builder()
            .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();

    public static Map<String,Object> obj(Object... pairs) {
        Map<String,Object> out = new LinkedHashMap<>();
        for (int i=0;i<pairs.length;i+=2) out.put((String)pairs[i],pairs[i+1]);
        return out;
    }
    @SuppressWarnings("unchecked")
    public static Map<String,Object> map(Object value) {
        if (!(value instanceof Map<?,?>)) throw new IllegalArgumentException("Expected object");
        return (Map<String,Object>) value;
    }
    @SuppressWarnings("unchecked")
    public static List<Map<String,Object>> rows(Object value) { return (List<Map<String,Object>>) value; }
    public static String str(Map<String,Object> value,String key) {
        if (!(value.get(key) instanceof String text) || text.isEmpty()) throw new IllegalArgumentException("Expected string");
        return text;
    }
    public static int num(Map<String,Object> value,String key) { return ((Number)value.get(key)).intValue(); }
    public static Map<String,Object> child(Map<String,Object> value,String key) { return map(value.get(key)); }
    public static byte[] bytes(Object value) { return MAPPER.writeValueAsBytes(sorted(value)); }
    public static String text(Object value) { return new String(bytes(value),StandardCharsets.UTF_8); }
    public static Map<String,Object> parse(byte[] raw) { return map(MAPPER.readValue(raw,Map.class)); }
    public static Map<String,Object> copy(Map<String,Object> value) { return parse(bytes(value)); }
    private static Object sorted(Object value) {
        if (value instanceof Map<?,?> source) {
            Map<String,Object> result=new TreeMap<>();
            source.forEach((k,v)->result.put((String)k,sorted(v))); return result;
        }
        if (value instanceof List<?> list) return list.stream().map(Json::sorted).toList();
        return value;
    }
    public static String sha(byte[] bytes) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
        catch (java.security.NoSuchAlgorithmException ex) { throw new IllegalStateException(ex); }
    }
    public static String hash(Object value) { return sha(bytes(value)); }
}
