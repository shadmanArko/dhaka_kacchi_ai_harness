"""Model loading with the mandatory hash check before joblib.load() runs.

joblib.load wraps pickle: loading the artifact executes whatever is inside it.
model.sha256 is written alongside model.joblib by ml/05-production/build_artifact.py
at training time, and this file exists purely to make sure the artifact this
service is about to execute is the one that was actually produced there - not a
substituted or corrupted file. See ml-kernel's own security section.
"""

import hashlib
import json
import logging
from pathlib import Path

import joblib

logger = logging.getLogger("predictor.model")

MODEL_DIR = Path(__file__).parent.parent / "model"
MODEL_PATH = MODEL_DIR / "model.joblib"
HASH_PATH = MODEL_DIR / "model.sha256"
METADATA_PATH = MODEL_DIR / "model_metadata.json"


class ModelIntegrityError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class LoadedModel:
    def __init__(self, pipeline, threshold: float, model_version: str, feature_columns: list[str]):
        self.pipeline = pipeline
        self.threshold = threshold
        self.model_version = model_version
        self.feature_columns = feature_columns


def load_verified_model() -> LoadedModel:
    if not MODEL_PATH.exists() or not HASH_PATH.exists():
        raise ModelIntegrityError(f"model artifact or hash file missing under {MODEL_DIR}")

    expected = HASH_PATH.read_text().strip()
    actual = _sha256(MODEL_PATH)
    if actual != expected:
        raise ModelIntegrityError(
            f"model.joblib hash mismatch: expected {expected}, got {actual}. "
            "Refusing to load - this artifact is not the one build_artifact.py produced."
        )
    logger.info("model artifact hash verified", extra={"sha256": actual})

    pipeline = joblib.load(MODEL_PATH)
    metadata = json.loads(METADATA_PATH.read_text())

    return LoadedModel(
        pipeline=pipeline,
        threshold=metadata["threshold"],
        model_version=metadata["model_version"],
        feature_columns=metadata["feature_columns"],
    )
