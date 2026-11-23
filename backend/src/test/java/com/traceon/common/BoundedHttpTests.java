package com.traceon.common;

import static org.junit.jupiter.api.Assertions.*;
import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.time.Duration;
import java.util.concurrent.*;
import org.junit.jupiter.api.*;

class BoundedHttpTests {
    HttpServer server;BoundedHttp http=new BoundedHttp();
    @BeforeEach void setup() throws Exception {server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);}
    @AfterEach void close(){server.stop(0);}
    URI uri(){return URI.create("http://127.0.0.1:"+server.getAddress().getPort()+"/fixed");}
    @Test void stopsOversizedStream(){server.createContext("/fixed",exchange->{byte[] raw=new byte[4096];exchange.sendResponseHeaders(200,raw.length);try(var out=exchange.getResponseBody()){out.write(raw);}});server.start();
        assertEquals(503,assertThrows(ApiException.class,()->http.call(uri(),null,null,1024,Duration.ofSeconds(2))).status);}
    @Test void deadlineCoversStalledBodyAfterHeaders(){var release=new CountDownLatch(1);
        server.createContext("/fixed",exchange->{exchange.sendResponseHeaders(200,20);try{release.await(3,TimeUnit.SECONDS);}catch(InterruptedException ex){Thread.currentThread().interrupt();}finally{exchange.close();}});server.start();
        try{assertEquals(503,assertThrows(ApiException.class,()->http.call(uri(),null,null,1024,Duration.ofMillis(200))).status);}finally{release.countDown();}}
    @Test void redirectsDoNotForwardRequests(){var reached=new java.util.concurrent.atomic.AtomicInteger();
        server.createContext("/fixed",exchange->{exchange.getResponseHeaders().add("Location","/other");exchange.sendResponseHeaders(302,-1);exchange.close();});
        server.createContext("/other",exchange->{reached.incrementAndGet();exchange.sendResponseHeaders(200,-1);exchange.close();});server.start();
        assertThrows(ApiException.class,()->http.call(uri(),null,null,1024,Duration.ofSeconds(2)));assertEquals(0,reached.get());}
}
