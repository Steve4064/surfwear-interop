# Contexte de build : racine du monorepo (voir docker-compose.yml)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY services/mcp-server/requirements.txt .
RUN pip install -r requirements.txt
COPY services/mcp-server/mcp_server ./mcp_server

RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "--factory", "mcp_server.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
