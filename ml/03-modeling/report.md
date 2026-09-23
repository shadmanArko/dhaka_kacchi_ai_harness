# Stage 03 — Modeling

**Project:** dhaka_kacchi_ai_harness — social post engagement prediction · **Run:** 2026-09-23 · **Skill:** ml-modeling v1.0.0
**Status:** complete · **Confidence:** full

---

## What this stage did

Trained and compared four model families on the real 289-row training split,
tuned via 5-fold expanding-window CV, and evaluated the winner once on the
71-row test holdout. **Plain logistic regression on all five features won**,
beating both baselines and — notably — beating gradient-boosted trees, which
scored worse than even the untuned baseline. That inversion is itself the
finding: with only 289 training rows, more model capacity found noise, not
signal, and it's the core evidence behind this stage's deep-learning gate
verdict of `not_warranted`.

## Bootstrap

- **Mode:** handoff
- **Consumed:** `00-problem-framing`, `01-data`, `02-split` handoffs
- **What this means:** the persisted split was loaded, not regenerated; the
  metric, floor, and baselines came from `problem_spec` directly.

## Decisions

| # | Decision | Why | Reversible |
|---|----------|-----|------------|
| 1 | Manual expanding-window out-of-fold loop instead of `cross_val_predict` | `TimeSeriesSplit`'s first fold is never anyone's test fold, so sklearn refuses it as "not a partition" — the manual version is the honest equivalent for time-ordered data | yes |
| 2 | One shared `ColumnTransformer` per candidate, cloned fresh per fold | Makes preprocessing leakage structurally impossible and keeps the family comparison fair | yes |
| 3 | Threshold (0.49) chosen on CV predictions, never test | A test-set threshold is a training score | **no** |
| 4 | Checked calibration (Brier, ROC-AUC) but did not apply Platt/isotonic recalibration | Would need its own held-out slice out of an already-small 289-row set; Brier (0.2644) is already close to what an uninformative-but-correctly-calibrated classifier scores at this base rate | yes |
| 5 | No fairness audit | `risk_tier=minimal`, no protected attributes declared — schema doesn't require one | yes |
| 6 | ML deps isolated in `ml/.venv`, not added to the warehouse's own `pyproject.toml` | The warehouse repo is deliberately dependency-lean; `make verify`/`gate`/`upgrade` never need scikit-learn | yes |

## Verified

| Check | Method | Result |
|-------|--------|--------|
| Both baselines scored before tuning | Scored dummy + untuned logreg first, unconditionally | pass — dummy 0.4983, untuned logreg 0.5653 |
| No `.fit()` outside the pipeline | Code review: every candidate is a `Pipeline`, cloned per fold | pass |
| Split loaded, not regenerated | `features.load_dataset()` reads `02-split`'s persisted id files | pass |
| Tuning scored on primary metric | `scoring="balanced_accuracy"` on every `RandomizedSearchCV` | pass |
| Threshold chosen on CV, not test | Sweep run against out-of-fold CV probabilities only | pass — 0.49, CV balanced_accuracy 0.6040 at that point |
| Calibration checked before using probabilities | Brier score + ROC-AUC on OOF predictions | pass — Brier 0.2644, ROC-AUC 0.5948 |
| Test set evaluated exactly once | Single `predict_proba` call on test, no re-tuning after | pass |
| Top feature importances plausible | Inspected top 10 logistic coefficients by magnitude | pass — all timing/format effects, nothing implausibly dominant |
| Gate argues against the actual floor | `dl_gate.evidence.phase0_floor` read directly from `problem_spec` | pass |
| Metrics match task type | All binary-classification metrics | pass |

## Results across families (CV, balanced_accuracy)

| Model | Score | Notes |
|---|---|---|
| Dummy (stratified) | 0.4983 | Non-degenerate — label is ~50/50 by construction |
| Logistic regression, platform+content_type only (untuned) | 0.5653 | Required baseline |
| **Logistic regression, all features, tuned** | **0.5904** | **Winner.** C=2.64 |
| Random forest, tuned | 0.5509 | max_depth=3, min_samples_leaf=8, n_estimators=200 |
| Gradient-boosted trees, tuned | 0.5227 | Worse than both logistic variants |

The GBDT result is the headline evidence for this stage's gate: escalating to
a more flexible model *hurt*, not helped, on 289 rows. That's the kernel's own
named signature of "the ceiling is in the data, not the model."

## Final test evaluation (touched once)

- **Model:** logistic regression, all 5 features, C=2.64
- **Threshold:** 0.49 (chosen on CV predictions)
- **Test balanced_accuracy: 0.6179** — clears the 0.60 floor, beats the dummy
  baseline by 11.95 points (well above the 5-point stopping bar)
- **Confusion matrix** (rows=actual, cols=predicted; [below-median, above-median]):

  |  | Predicted below | Predicted above |
  |---|---|---|
  | **Actual below** | 17 | 18 |
  | **Actual above** | 9 | 27 |

- **Per-platform:** facebook 0.5811 (n=43), instagram 0.6889 (n=28)

**Honest caveat, not hidden:** the CV estimate (0.5904) sits just *below* the
floor; only the single test evaluation (0.6179) clears it, on 71 rows. That
gap is well within plausible sampling noise. Treat "clears the floor" here as
a fragile result, not a comfortable one — which is exactly what a floor set
before results exist is supposed to produce.

## Error analysis / interpretation

Top logistic coefficients (by magnitude): posting at **night** (+0.86) and on
**Saturday** (+0.81) or **Tuesday** (+0.60) push toward above-median
engagement; **Friday** (-0.58) and **morning** (-0.53) push the other way;
**reel**-type content (+0.60) and **video** content (+0.42) both push
positive relative to image/carousel. All plausible posting-schedule and
format effects — nothing that looks like a leaked post-hoc signal.

## Limitations, stated plainly

- n=289 train / 71 test is small; per-platform test scores (43 and 28 rows)
  are individually too small to trust on their own.
- No calibration correction applied (see decision #4) — probabilities are
  usable for ranking/thresholding but not for a precise expected-value
  calculation.
- Caption text is only represented via two cheap derived stats (length,
  hashtag count) — no embedding feature was tried this pass.

## Deep-learning gate

**Verdict: `not_warranted`**

- **phase0_floor:** 0.60 · **baseline_dummy:** 0.4983 · **best_classical:**
  logreg_full_tuned, 0.5904 (CV) / 0.6179 (test)
- **Headroom:** negative-to-flat across families — GBDT (0.5227) underperformed
  even the untuned baseline (0.5653). On 289 rows, more model capacity found
  noise, not signal.
- **Rationale:** the input is tabular/short-categorical plus two cheap derived
  numeric features — no text embeddings or pixel features were used, per
  00-problem-framing's own decision that those would only be added once a
  classical baseline showed enough promise to justify the complexity. A neural
  network trained on 289 rows of five sparse categorical features has no
  realistic path to beat a well-regularized linear model here, and would cost
  the explainability the operator explicitly needs
  (`problem_spec.constraints.explainability_required=true`).
- **Revisit if:** per-platform post volume roughly doubles; a pretrained
  caption embedding is added and shown via ablation to carry real signal on
  its own; or Threads' engagement data becomes usable and the combined
  dataset supports real per-platform sample sizes.

## Outputs

| Artifact | Path | What it is |
|----------|------|------------|
| Feature builder | `ml/03-modeling/features.py` | Deterministic, shared by training and (future) serving |
| Training script | `ml/03-modeling/train.py` | Re-runnable end to end against `ml/.venv` |
| Results | `ml/03-modeling/artifacts/results.json` | All scores, threshold, calibration, confusion matrix |
| OOF predictions | `ml/03-modeling/artifacts/cv_predictions.csv` | y_true + predicted probability for every row held out in some CV fold |

## For the next stage

**04-deep-learning:** this handoff's `dl_gate` verdict is `not_warranted`. Do
not build a deep model against this project without an explicit, recorded
`gate_override`.

**05-production (if this project proceeds to serving):** serve
`logreg_full_tuned` at threshold 0.49, importing `ml/03-modeling/features.py`
for both training and serving feature construction — do not reimplement
feature logic separately for the API. Minor cleanup worth doing first:
`OneHotEncoder(drop='if_binary')` for `is_video` to remove the redundant
mirror-image column pair.

---

*Machine-readable version: `handoff.json` in this directory. Validate with
`validate_handoff.py --chain ml/`.*
