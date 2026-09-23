"""Stage 05 execution: refit the stage-03 winner (fixed hyperparameters, no
re-tuning) on ALL labeled data - train + the now-spent test split - and
serialize it for serving. Re-tuning here would be a silent second use of the
test set; only the final fit uses it, on parameters already chosen in 03.
"""

import hashlib
import json
import sys
from pathlib import Path

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

sys.path.insert(0, str(Path(__file__).parent.parent / "03-modeling"))
from features import NOMINAL_COLUMNS, NUMERIC_COLUMNS, build_features  # noqa: E402

HERE = Path(__file__).parent
DATA_CSV = HERE.parent / "01-data" / "artifacts" / "dataset.csv"
MODELING_RESULTS = HERE.parent / "03-modeling" / "artifacts" / "results.json"
ARTIFACTS = HERE / "artifacts"

# Fixed at stage 03 selection time. Not re-tuned here.
BEST_C = 2.636650898730358
SEED = 0


def make_pipeline() -> Pipeline:
    nominal = Pipeline(
        [
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    numeric = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]
    )
    pre = ColumnTransformer(
        [("nominal", nominal, NOMINAL_COLUMNS), ("numeric", numeric, NUMERIC_COLUMNS)]
    )
    clf = LogisticRegression(
        max_iter=2000, class_weight="balanced", random_state=SEED, C=BEST_C
    )
    return Pipeline([("pre", pre), ("clf", clf)])


def main() -> None:
    with MODELING_RESULTS.open() as f:
        results = json.load(f)
    assert results["best_model"] == "logreg_full_tuned", (
        "build_artifact.py is hardcoded to stage 03's winner; re-check if that changes"
    )
    threshold = results["chosen_threshold"]

    full = pd.read_csv(DATA_CSV)
    X = build_features(full)
    y = full["label"].astype(int)

    pipeline = make_pipeline()
    pipeline.fit(X, y)

    ARTIFACTS.mkdir(exist_ok=True)
    model_path = ARTIFACTS / "model.joblib"
    joblib.dump(pipeline, model_path)

    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    (ARTIFACTS / "model.sha256").write_text(digest + "\n")

    metadata = {
        "model_version": "2026-09-23-logreg-v1",
        "sklearn_pipeline": "logreg_full_tuned",
        "C": BEST_C,
        "threshold": threshold,
        "trained_on_rows": len(X),
        "trained_on": "train + test (360 rows) - test's job as a held-out evaluation set is done; this refit uses fixed hyperparameters chosen in stage 03, no re-tuning",
        "feature_columns": NOMINAL_COLUMNS + NUMERIC_COLUMNS,
        "sha256": digest,
    }
    (ARTIFACTS / "model_metadata.json").write_text(json.dumps(metadata, indent=2))

    print(f"wrote {model_path} ({model_path.stat().st_size} bytes)")
    print(f"sha256 = {digest}")
    print(f"threshold = {threshold}")


if __name__ == "__main__":
    main()
