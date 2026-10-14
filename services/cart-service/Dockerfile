# Contexte de build : racine du monorepo (voir docker-compose.yml)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Bibliothèque commune du monorepo, puis dépendances du service
COPY libs/common /opt/libs/common
COPY services/cart-service/requirements.txt .
RUN pip install -r requirements.txt /opt/libs/common

COPY services/cart-service/cart_service ./cart_service

RUN useradd --create-home --uid 1000 app && mkdir -p /data /exchange && chown -R app /app /data /exchange
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "--factory", "cart_service.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
