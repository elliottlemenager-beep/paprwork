# Build context: services/scout-commons
FROM python:3.12-slim
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY contract/commons_contract ./commons_contract
COPY api/commons_api ./commons_api
ENV PYTHONUNBUFFERED=1
CMD exec uvicorn commons_api.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 2
