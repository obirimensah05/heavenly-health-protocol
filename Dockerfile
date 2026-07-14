FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HEAVENLY_MCP_HOST=0.0.0.0 \
    HEAVENLY_MCP_PORT=8791

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && \
    addgroup -g 10001 heavenly && \
    adduser -D -u 10001 -G heavenly heavenly && \
    mkdir -p /home/heavenly/data && \
    chown -R heavenly:heavenly /home/heavenly/data

USER heavenly
WORKDIR /home/heavenly

EXPOSE 8791
ENTRYPOINT ["heavenly-mcp"]
