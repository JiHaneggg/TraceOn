package com.traceon.common;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.converter.HttpMessageNotReadableException;
import java.util.Map;

@RestControllerAdvice
public class ApiErrors {
    @ExceptionHandler(ApiException.class)
    public ResponseEntity<Map<String,Object>> api(ApiException ex){return ResponseEntity.status(ex.status).body(Json.obj("error",ex.code,"message",ex.getMessage()));}
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<Map<String,Object>> invalid(){return ResponseEntity.badRequest().body(Json.obj("error","invalid_body"));}
    @ExceptionHandler({IllegalArgumentException.class,IllegalStateException.class,ClassCastException.class,NullPointerException.class})
    public ResponseEntity<Map<String,Object>> source(){return ResponseEntity.status(503).body(Json.obj("error","source_unavailable","message","자료의 응답 규격을 확인하지 못했습니다. 다시 조회해 주세요."));}
}
