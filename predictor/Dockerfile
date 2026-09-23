# Build context is the repo root (see deploy/docker-compose.yml) so this can
# pull ml/03-modeling/features.py and ml/05-production/artifacts/ into the
# image - the same feature code and the same artifact the ML pipeline
# produced, not a reimplementation or a hand-copied approximation.
FROM python:3.12-slim

WORKDIR /app

COPY predictor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY predictor/app ./app
COPY ml/03-modeling/features.py ./app/features.py
COPY ml/05-production/artifacts/model.joblib ./model/model.joblib
COPY ml/05-production/artifacts/model.sha256 ./model/model.sha256
COPY ml/05-production/artifacts/model_metadata.json ./model/model_metadata.json

# Internal-only service: never bind to 0.0.0.0 with a published port in
# docker-compose. No --reload, no debug endpoints.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
