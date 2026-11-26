# TraceOn rag-service (Python 표준 라이브러리만 사용, 추가 패키지 없음)
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 코드의 ROOT(= 상위 폴더) 기준 경로를 유지하기 위해 /app/rag-service 에 둔다.
WORKDIR /app
RUN useradd --system --uid 10001 traceon
COPY --chown=traceon . ./rag-service
COPY --from=rule_sources --chown=traceon /rules.py ./rule-service/rules.py
USER traceon
EXPOSE 8092
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8092/health', timeout=2)"
CMD ["python", "rag-service/rag_server.py", "--host", "0.0.0.0", "--port", "8092"]
