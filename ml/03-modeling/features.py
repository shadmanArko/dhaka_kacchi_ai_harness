"""Deterministic feature construction shared by training and (eventually)
serving. No learned parameters live here - those belong in the sklearn
pipeline in train.py, fit per fold. This module is imported by both, per the
kernel rule that training and serving share transformation code.
"""

import re

import pandas as pd

NOMINAL_COLUMNS = ["platform", "content_type_filled", "is_video", "day_of_week", "hour_bucket"]
NUMERIC_COLUMNS = ["caption_length", "hashtag_count"]
ALL_FEATURE_COLUMNS = NOMINAL_COLUMNS + NUMERIC_COLUMNS

_HASHTAG_RE = re.compile(r"#\w+")


def _hour_bucket(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Pure, deterministic transform of raw dataset.csv columns into the
    feature columns the model pipeline consumes. Never touches label,
    engagement, impressions, or reach - those are all post-hoc.
    """
    out = pd.DataFrame(index=df.index)
    out["platform"] = df["platform"]

    content_type_filled = df["content_type"].fillna("unknown")
    out["content_type_filled"] = content_type_filled
    out["is_video"] = content_type_filled.isin(["video", "reel"])

    posted_at = pd.to_datetime(df["posted_at"], utc=True)
    out["day_of_week"] = posted_at.dt.day_name()
    out["hour_bucket"] = posted_at.dt.hour.apply(_hour_bucket)

    caption = df["caption"].fillna("")
    out["caption_length"] = caption.str.len()
    out["hashtag_count"] = caption.apply(lambda c: len(_HASHTAG_RE.findall(c)))

    return out[ALL_FEATURE_COLUMNS]


def load_dataset(dataset_csv: str, id_csv: str) -> tuple[pd.DataFrame, pd.Series]:
    """Load the cached dataset extract, restrict to the ids in id_csv (a
    persisted split file from ml/02-split), and return (features, label) in
    posted_at order.
    """
    full = pd.read_csv(dataset_csv)
    ids = pd.read_csv(id_csv)
    subset = full[full["id"].isin(ids["id"])].copy()
    subset["posted_at"] = pd.to_datetime(subset["posted_at"], utc=True)
    subset = subset.sort_values("posted_at").reset_index(drop=True)

    X = build_features(subset)
    y = subset["label"].astype(int)
    meta = subset[["id", "platform", "posted_at"]]
    return X, y, meta
