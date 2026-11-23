package com.traceon.common;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import java.util.*;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/** Local demo boundary. Production user/role authorization is a separate contract. */
@Component
public class ApiBoundary extends OncePerRequestFilter {
    @Override protected boolean shouldNotFilter(HttpServletRequest request){return !request.getRequestURI().startsWith("/api/");}
    @Override protected void doFilterInternal(HttpServletRequest request,HttpServletResponse response,FilterChain chain) throws ServletException,IOException {
        response.setHeader("Cache-Control","no-store");response.setHeader("X-Content-Type-Options","nosniff");response.setHeader("Referrer-Policy","no-referrer");
        String authority=request.getHeader("Host");String expected=request.getServerName()+":"+request.getServerPort();
        if(!Set.of("127.0.0.1","localhost","[::1]").contains(request.getServerName())
                || authority==null || !(authority.equals(expected) || (request.getServerPort()==80 && authority.equals(request.getServerName())))){
            error(response,421,"invalid_host");return;
        }
        String origin=request.getHeader("Origin");
        if("cross-site".equals(request.getHeader("Sec-Fetch-Site")) || (origin!=null && !origin.equals(request.getScheme()+"://"+authority))){error(response,403,"same_origin_required");return;}
        if("POST".equals(request.getMethod())) {
            String type=request.getContentType();long size=request.getContentLengthLong();
            if(size>2048){error(response,413,"body_size");return;}
            if(size<=0 || request.getHeader("Transfer-Encoding")!=null || type==null || !type.split(";",2)[0].equalsIgnoreCase("application/json")){
                error(response,400,"invalid_body");return;
            }
        }
        chain.doFilter(request,response);
    }
    private static void error(HttpServletResponse response,int status,String code) throws IOException {
        response.setStatus(status);response.setContentType("application/json; charset=utf-8");response.getOutputStream().write(Json.bytes(Json.obj("error",code)));
    }
}
