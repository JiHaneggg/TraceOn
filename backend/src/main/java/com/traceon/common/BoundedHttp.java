package com.traceon.common;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.*;
import java.util.concurrent.Flow;
import org.springframework.stereotype.Component;

/** Limits the response while streaming; deadline covers headers AND the full body. */
@Component
public class BoundedHttp {
    private final HttpClient client=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(8))
            .followRedirects(HttpClient.Redirect.NEVER).build();

    public Map<String,Object> call(URI uri,Object body,String authorization,int limit,Duration timeout) {
        var request=HttpRequest.newBuilder(uri).timeout(timeout).header("Content-Type","application/json");
        if (authorization!=null) request.header("Authorization",authorization);
        if (body==null) request.GET(); else request.POST(HttpRequest.BodyPublishers.ofByteArray(Json.bytes(body)));
        var future=client.sendAsync(request.build(),info->new LimitedBody(limit));
        try {
            var response=future.get(Math.max(1,timeout.toMillis()),TimeUnit.MILLISECONDS);
            if (response.statusCode()==409) throw new ApiException(409,"evidence_version_conflict","원문 자료 버전이 바뀌었습니다. 다시 조회해 주세요.");
            if (response.statusCode()!=200) throw ApiException.unavailable();
            return Json.parse(response.body());
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();future.cancel(true);throw ApiException.unavailable();
        } catch (ExecutionException|TimeoutException ex) {
            future.cancel(true);throw ApiException.unavailable();
        } catch (RuntimeException ex) {
            future.cancel(true);if(ex instanceof ApiException api)throw api;throw ApiException.unavailable();
        }
    }
    private static final class LimitedBody implements HttpResponse.BodySubscriber<byte[]> {
        final int limit; final ByteArrayOutputStream output=new ByteArrayOutputStream();
        final CompletableFuture<byte[]> result=new CompletableFuture<>(); Flow.Subscription subscription;
        LimitedBody(int limit) { this.limit=limit; }
        public CompletionStage<byte[]> getBody() { return result; }
        public void onSubscribe(Flow.Subscription value) { subscription=value;value.request(1); }
        public void onNext(List<ByteBuffer> buffers) {
            for (var buffer:buffers) {
                if (buffer.remaining()>limit-output.size()) {
                    subscription.cancel();result.completeExceptionally(new IllegalArgumentException("Response size"));return;
                }
                byte[] part=new byte[buffer.remaining()];buffer.get(part);output.writeBytes(part);
            }
            subscription.request(1);
        }
        public void onError(Throwable error) { result.completeExceptionally(error); }
        public void onComplete() { result.complete(output.toByteArray()); }
    }
}
