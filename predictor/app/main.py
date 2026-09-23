"""Internal-only post-engagement predictor service.

Serves ml/03-modeling's winning model (logistic regression, dl_gate=not_warranted,
see ml/03-modeling/report.md). Not exposed to the internet - reached only by the
dhaka-kacchi-connect admin backend over the docker-compose network.
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .features import build_features  # copied verbatim from ml/03-modeling at build time
from .model import LoadedModel, ModelIntegrityError, load_verified_model
from .schemas import PredictRequest, PredictResponse, Reason

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("predictor")

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _state["model"] = load_verified_model()
        logger.info(
            '{"event":"model_loaded","model_version":"%s","threshold":%s}',
            _state["model"].model_version,
            _state["model"].threshold,
        )
    except ModelIntegrityError as exc:
        # Fail fast and loud rather than serving with no model or, worse, an
        # unverified one. /ready reports this; the process still starts so a
        # human can inspect logs rather than crash-looping silently.
        logger.error('{"event":"model_load_failed","error":%r}', str(exc))
        _state["model"] = None
        _state["model_error"] = str(exc)
    yield


app = FastAPI(title="dhaka-kacchi-predictor", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    """Liveness - the process is up. Does not imply the model is loaded."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness - the model is actually loaded and verified, not just that the process is up."""
    model: LoadedModel | None = _state.get("model")
    if model is None:
        raise HTTPException(status_code=503, detail=_state.get("model_error", "model not loaded"))
    return {"status": "ready", "model_version": model.model_version}


def _explain(model: LoadedModel, row: pd.DataFrame) -> list[Reason]:
    """Per-request contribution = coefficient * encoded feature value. Only
    meaningful because the winning model is linear (logistic regression) -
    this would need a different method (e.g. SHAP) for a tree model.
    """
    pre = model.pipeline.named_steps["pre"]
    clf = model.pipeline.named_steps["clf"]
    encoded = pre.transform(row)
    if hasattr(encoded, "toarray"):
        encoded = encoded.toarray()
    feature_names = list(pre.get_feature_names_out())
    contributions = encoded[0] * clf.coef_[0]
    ranked = sorted(
        zip(feature_names, contributions, strict=False), key=lambda kv: abs(kv[1]), reverse=True
    )
    return [Reason(feature=name, contribution=round(float(val), 4)) for name, val in ranked[:5] if val != 0]


@app.post("/v1/predict", response_model=PredictResponse)
async def predict(body: PredictRequest, request: Request):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    start = time.monotonic()

    model: LoadedModel | None = _state.get("model")
    if model is None:
        raise HTTPException(status_code=503, detail="model not loaded")

    row = pd.DataFrame(
        [
            {
                "platform": body.platform,
                "content_type": body.content_type,
                "caption": body.caption,
                "posted_at": body.planned_posted_at,
            }
        ]
    )
    try:
        features = build_features(row)
    except Exception as exc:  # noqa: BLE001 - convert any feature-building failure to a 400
        raise HTTPException(status_code=400, detail=f"could not build features from input: {exc}") from exc

    proba = float(model.pipeline.predict_proba(features)[0, 1])
    label = "likely_at_or_above_typical" if proba >= model.threshold else "likely_below_typical"
    reasons = _explain(model, features)

    latency_ms = round((time.monotonic() - start) * 1000, 2)
    # Structured log: identifiers and derived values only, never the raw caption text.
    logger.info(
        '{"event":"prediction","request_id":"%s","model_version":"%s","platform":"%s",'
        '"label":"%s","probability":%.4f,"latency_ms":%s}',
        request_id,
        model.model_version,
        body.platform,
        label,
        proba,
        latency_ms,
    )

    return PredictResponse(
        label=label,
        probability=round(proba, 4),
        threshold=model.threshold,
        model_version=model.model_version,
        top_reasons=reasons,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error('{"event":"unhandled_error","path":"%s","error":%r}', request.url.path, str(exc))
    return JSONResponse(status_code=500, content={"detail": "internal error"})
