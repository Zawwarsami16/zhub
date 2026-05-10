# syntax=docker/dockerfile:1.6
# zhub — multi-stage container build
# Build:  docker build -t zhub .
# Run:    docker run -p 8080:8080 -e GROQ_API_KEY=gsk_... zhub
# With persistence: -v zhub-data:/data and pass --db /data/zhub.db

FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY zhub/ ./zhub/
RUN pip install --no-cache-dir --target=/install '.[server,brains]'

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /install /usr/local/lib/python3.12/site-packages
COPY zhub/ ./zhub/

# Non-root user for runtime
RUN useradd --create-home --uid 1000 zhub && \
    mkdir -p /data && chown zhub:zhub /data
USER zhub

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "zhub.server", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--db", "/data/zhub.db"]
