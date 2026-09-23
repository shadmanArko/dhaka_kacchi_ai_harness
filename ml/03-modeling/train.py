"""Stage 03 execution: baselines -> tuned classical candidates -> calibration
-> threshold -> single final test evaluation -> dl_gate evidence.

Run from the repo root with the isolated ml venv:
    ml/.venv/bin/python ml/03-modeling/train.py
"""

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    roc_auc_score,
)
from sklearn.model_selection import RandomizedSearchCV, TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from features import ALL_FEATURE_COLUMNS, NOMINAL_COLUMNS, NUMERIC_COLUMNS, load_dataset

warnings.filterwarnings("ignore", category=FutureWarning)

HERE = Path(__file__).parent
DATA_DIR = HERE.parent / "01-data" / "artifacts"
SPLIT_DIR = HERE.parent / "02-split" / "artifacts"
ARTIFACTS = HERE / "artifacts"
SEED = 0
CV = TimeSeriesSplit(n_splits=5)
PRIMARY_METRIC = "balanced_accuracy"
FLOOR = 0.60


def make_preprocessor() -> ColumnTransformer:
    nominal = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    numeric = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]
    )
    return ColumnTransformer(
        transformers=[
            ("nominal", nominal, NOMINAL_COLUMNS),
            ("numeric", numeric, NUMERIC_COLUMNS),
        ]
    )


def expanding_window_oof(pipeline: Pipeline, X: pd.DataFrame, y: pd.Series, method: str = "predict"):
    """TimeSeriesSplit's folds do not cover the whole index (the earliest chunk
    is never a test fold), so sklearn's cross_val_predict refuses it outright
    ('only works for partitions'). This does the same expanding-window
    out-of-fold prediction by hand, leaving the never-tested earliest rows out
    of the result entirely rather than forcing a partition that isn't faithful
    to time order.
    """
    import sklearn

    oof_index = []
    oof_preds = []
    for train_idx, test_idx in CV.split(X):
        est = sklearn.base.clone(pipeline)
        est.fit(X.iloc[train_idx], y.iloc[train_idx])
        if method == "predict_proba":
            preds = est.predict_proba(X.iloc[test_idx])[:, 1]
        else:
            preds = est.predict(X.iloc[test_idx])
        oof_index.extend(X.index[test_idx].tolist())
        oof_preds.extend(preds.tolist())
    order = np.argsort(oof_index)
    idx_sorted = np.array(oof_index)[order]
    preds_sorted = np.array(oof_preds)[order]
    return idx_sorted, preds_sorted


def cv_balanced_accuracy(pipeline: Pipeline, X: pd.DataFrame, y: pd.Series) -> float:
    idx, preds = expanding_window_oof(pipeline, X, y, method="predict")
    return balanced_accuracy_score(y.loc[idx], preds)


def main() -> None:
    X_train, y_train, meta_train = load_dataset(
        DATA_DIR / "dataset.csv", SPLIT_DIR / "train_ids.csv"
    )
    X_test, y_test, meta_test = load_dataset(DATA_DIR / "dataset.csv", SPLIT_DIR / "test_ids.csv")

    print(f"train: {len(X_train)} rows, positive rate {y_train.mean():.3f}")
    print(f"test:  {len(X_test)} rows, positive rate {y_test.mean():.3f}")

    candidates: list[dict] = []

    # --- Baseline 1: dummy (stratified, matches the ~50/50 label by construction) ---
    dummy = DummyClassifier(strategy="stratified", random_state=SEED)
    dummy_score = cv_balanced_accuracy(dummy, X_train, y_train)
    candidates.append({"model": "dummy_stratified", "score": round(dummy_score, 4)})
    print(f"dummy (stratified):              CV balanced_accuracy = {dummy_score:.4f}")

    # --- Baseline 2: untuned logistic regression, platform + content_type only ---
    baseline_cols = ["platform", "content_type_filled"]
    baseline_pre = ColumnTransformer(
        [("nominal", OneHotEncoder(handle_unknown="ignore"), baseline_cols)]
    )
    baseline_logreg = Pipeline(
        [("pre", baseline_pre), ("clf", LogisticRegression(max_iter=1000, random_state=SEED))]
    )
    baseline_score = cv_balanced_accuracy(baseline_logreg, X_train[baseline_cols], y_train)
    candidates.append({"model": "logreg_platform_contenttype_untuned", "score": round(baseline_score, 4)})
    print(f"logreg (platform+content_type):  CV balanced_accuracy = {baseline_score:.4f}")

    # --- Candidate: full-feature logistic regression, tuned ---
    logreg_pipe = Pipeline(
        [
            ("pre", make_preprocessor()),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced", random_state=SEED)),
        ]
    )
    logreg_search = RandomizedSearchCV(
        logreg_pipe,
        param_distributions={"clf__C": np.logspace(-3, 2, 20)},
        n_iter=15,
        scoring=PRIMARY_METRIC,
        cv=CV,
        random_state=SEED,
        n_jobs=-1,
    )
    logreg_search.fit(X_train, y_train)
    candidates.append(
        {"model": "logreg_full_tuned", "score": round(logreg_search.best_score_, 4), "notes": str(logreg_search.best_params_)}
    )
    print(f"logreg (full, tuned):            CV balanced_accuracy = {logreg_search.best_score_:.4f}  {logreg_search.best_params_}")

    # --- Candidate: random forest, tuned ---
    rf_pipe = Pipeline(
        [
            ("pre", make_preprocessor()),
            ("clf", RandomForestClassifier(class_weight="balanced", random_state=SEED)),
        ]
    )
    rf_search = RandomizedSearchCV(
        rf_pipe,
        param_distributions={
            "clf__n_estimators": [100, 200, 400],
            "clf__max_depth": [3, 5, 8, None],
            "clf__min_samples_leaf": [1, 2, 4, 8],
        },
        n_iter=15,
        scoring=PRIMARY_METRIC,
        cv=CV,
        random_state=SEED,
        n_jobs=-1,
    )
    rf_search.fit(X_train, y_train)
    candidates.append(
        {"model": "random_forest_tuned", "score": round(rf_search.best_score_, 4), "notes": str(rf_search.best_params_)}
    )
    print(f"random forest (tuned):           CV balanced_accuracy = {rf_search.best_score_:.4f}  {rf_search.best_params_}")

    # --- Candidate: gradient-boosted trees (HistGradientBoostingClassifier), tuned ---
    gbdt_pipe = Pipeline(
        [
            ("pre", make_preprocessor()),
            ("clf", HistGradientBoostingClassifier(random_state=SEED)),
        ]
    )
    gbdt_search = RandomizedSearchCV(
        gbdt_pipe,
        param_distributions={
            "clf__max_iter": [50, 100, 200],
            "clf__learning_rate": [0.01, 0.05, 0.1, 0.2],
            "clf__max_leaf_nodes": [7, 15, 31],
            "clf__l2_regularization": [0.0, 0.1, 1.0],
            "clf__class_weight": [None, "balanced"],
        },
        n_iter=20,
        scoring=PRIMARY_METRIC,
        cv=CV,
        random_state=SEED,
        n_jobs=-1,
    )
    gbdt_search.fit(X_train, y_train)
    candidates.append(
        {"model": "gbdt_tuned", "score": round(gbdt_search.best_score_, 4), "notes": str(gbdt_search.best_params_)}
    )
    print(f"GBDT (tuned):                     CV balanced_accuracy = {gbdt_search.best_score_:.4f}  {gbdt_search.best_params_}")

    # --- Select the best candidate by CV score ---
    best = max(candidates, key=lambda c: c["score"])
    search_by_name = {
        "logreg_full_tuned": logreg_search,
        "random_forest_tuned": rf_search,
        "gbdt_tuned": gbdt_search,
    }
    print(f"\nBest candidate: {best['model']} (CV balanced_accuracy={best['score']:.4f})")

    if best["model"] in search_by_name:
        best_estimator = search_by_name[best["model"]].best_estimator_
    elif best["model"] == "logreg_platform_contenttype_untuned":
        best_estimator = baseline_logreg
    else:
        best_estimator = dummy

    # --- Calibration check on CV predictions ---
    cv_idx, cv_proba = expanding_window_oof(best_estimator, X_train, y_train, method="predict_proba")
    y_cv = y_train.loc[cv_idx].to_numpy()
    brier = brier_score_loss(y_cv, cv_proba)
    try:
        cv_auc = roc_auc_score(y_cv, cv_proba)
    except ValueError:
        cv_auc = float("nan")
    print(f"CV Brier score: {brier:.4f}, CV ROC-AUC: {cv_auc:.4f}")

    # --- Threshold selection on CV predictions (never on test) ---
    best_threshold = 0.5
    best_bal_acc = -1.0
    for t in np.linspace(0.05, 0.95, 91):
        preds_at_t = (cv_proba >= t).astype(int)
        bal_acc = balanced_accuracy_score(y_cv, preds_at_t)
        if bal_acc > best_bal_acc:
            best_bal_acc = bal_acc
            best_threshold = t
    print(f"Chosen threshold (on CV predictions): {best_threshold:.2f}, CV balanced_accuracy at that threshold: {best_bal_acc:.4f}")

    # --- Fit the chosen pipeline on the full training set, evaluate on test ONCE ---
    best_estimator.fit(X_train, y_train)
    test_proba = best_estimator.predict_proba(X_test)[:, 1]
    test_preds = (test_proba >= best_threshold).astype(int)
    test_bal_acc = balanced_accuracy_score(y_test, test_preds)
    cm = confusion_matrix(y_test, test_preds)

    print(f"\n=== FINAL TEST EVALUATION (touched once) ===")
    print(f"Test balanced_accuracy @ threshold {best_threshold:.2f}: {test_bal_acc:.4f}")
    print(f"Confusion matrix [[TN,FP],[FN,TP]]:\n{cm}")

    per_platform = {}
    for platform in ["facebook", "instagram"]:
        mask = meta_test["platform"] == platform
        if mask.sum() == 0:
            continue
        bal_acc_p = balanced_accuracy_score(y_test[mask], test_preds[mask])
        per_platform[platform] = round(bal_acc_p, 4)
        print(f"  {platform}: n={mask.sum()}, balanced_accuracy={bal_acc_p:.4f}")

    # --- Feature importance / plausibility check (permutation importance for the winner if tree-based) ---
    importances = {}
    if best["model"] == "gbdt_tuned":
        pre = best_estimator.named_steps["pre"]
        feature_names = list(pre.get_feature_names_out())
        clf = best_estimator.named_steps["clf"]
        for name, imp in zip(feature_names, getattr(clf, "feature_importances_", []), strict=False):
            importances[name] = float(imp)
    elif best["model"] == "random_forest_tuned":
        pre = best_estimator.named_steps["pre"]
        feature_names = list(pre.get_feature_names_out())
        clf = best_estimator.named_steps["clf"]
        for name, imp in zip(feature_names, clf.feature_importances_, strict=False):
            importances[name] = float(imp)
    elif best["model"] in ("logreg_full_tuned", "logreg_platform_contenttype_untuned"):
        pre = best_estimator.named_steps["pre"]
        feature_names = list(pre.get_feature_names_out())
        clf = best_estimator.named_steps["clf"]
        for name, coef in zip(feature_names, clf.coef_[0], strict=False):
            importances[name] = float(coef)

    top_importances = dict(sorted(importances.items(), key=lambda kv: abs(kv[1]), reverse=True)[:10])

    results = {
        "candidates": candidates,
        "best_model": best["model"],
        "best_cv_score": best["score"],
        "chosen_threshold": round(float(best_threshold), 4),
        "threshold_chosen_on": "cv",
        "cv_brier_score": round(float(brier), 4),
        "cv_roc_auc": round(float(cv_auc), 4),
        "test_balanced_accuracy": round(float(test_bal_acc), 4),
        "test_confusion_matrix": cm.tolist(),
        "test_per_platform_balanced_accuracy": per_platform,
        "top_feature_importances": top_importances,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "train_positive_rate": round(float(y_train.mean()), 4),
        "test_positive_rate": round(float(y_test.mean()), 4),
        "floor": FLOOR,
        "clears_floor": bool(test_bal_acc >= FLOOR),
        "beats_dummy_by": round(float(test_bal_acc - dummy_score), 4),
    }

    ARTIFACTS.mkdir(exist_ok=True)
    with (ARTIFACTS / "results.json").open("w") as f:
        json.dump(results, f, indent=2)

    pd.DataFrame({"row_index": cv_idx, "y_true": y_cv, "cv_proba": cv_proba}).to_csv(
        ARTIFACTS / "cv_predictions.csv", index=False
    )

    print(f"\nWrote {ARTIFACTS / 'results.json'}")


if __name__ == "__main__":
    main()
