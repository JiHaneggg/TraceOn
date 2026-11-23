package com.traceon.common;

public class ApiException extends RuntimeException {
    public final int status;
    public final String code;
    public ApiException(int status,String code,String message) { super(message);this.status=status;this.code=code; }
    public static ApiException stale() { return new ApiException(409,"snapshot_expired","조회가 만료되거나 변경됐습니다. 거래를 다시 조회해 주세요."); }
    public static ApiException unavailable() { return new ApiException(503,"upstream_unavailable","조회 서비스를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요."); }
}
