# The answer service (src/civspeak/answer_service.py) for Google Cloud Run. Built from the repository
# root by service/deploy.sh: gcloud builds submit --config service/cloudbuild.yaml
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY service/requirements.txt service/requirements.txt
RUN pip install -r service/requirements.txt
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps . && rm -rf src build
USER nobody
CMD exec uvicorn civspeak.answer_service:app --host 0.0.0.0 --port "${PORT:-8080}" --timeout-keep-alive 30 --no-access-log
