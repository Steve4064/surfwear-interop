FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install httpx==0.28.1
COPY infra/zitadel/bootstrap.py /bootstrap.py
CMD ["python", "/bootstrap.py"]
