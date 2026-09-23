# Model card: social post engagement predictor

**Version:** 2026-09-23-logreg-v1 · **Status:** deployed, internal-only

## Intended use

Advisory-only: flags whether a drafted, not-yet-published Instagram or
Facebook post is likely to land above or below that platform's own typical
engagement, so Dhaka Kacchi's operator can revise the caption/media/timing
before publishing, or post anyway knowing the odds. Used through the admin
panel's "Post predictor" page (`dhaka-kacchi-connect`'s `/admin/post-predict`).

**Not intended for:** automated posting decisions, any decision beyond a
single operator's own judgment call, or any platform other than Facebook and
Instagram (Threads is explicitly out of scope — see below).

## How it was built

Full pipeline in `ml/00-problem-framing` through `ml/03-modeling` in this
repo. In short: logistic regression on 5 pre-publish features (platform,
content_type, day-of-week, hour-bucket, caption length/hashtag count),
selected over random forest and gradient-boosted trees because — on 289
training rows — the more flexible models scored *worse*, not better.

## Performance

- **Primary metric:** balanced accuracy. **Floor:** 0.60.
- **Test result:** 0.6179 (71 held-out posts, most recent 2026-08-22 to
  2026-09-22). CV estimate: 0.5904 — treat the floor-clearing as fragile,
  not comfortable (see `ml/03-modeling/report.md`).
- **Per-platform (small samples, not independently reliable):** facebook
  0.5811 (n=43), instagram 0.6889 (n=28).
- Beats a coin-flip-equivalent dummy baseline by ~12 points.

## Limitations

- Trained on **360 posts total** (228 Facebook + 132 Instagram); this is a
  small-business, single-brand dataset — nothing here generalizes to another
  business's posting behavior.
- **Threads is excluded entirely** — its real engagement data is
  near-zero (56/57 posts with zero likes+comments+shares), incompatible with
  this label definition. A Threads post cannot be checked with this tool.
- Caption is represented only by length and hashtag count — no NLP beyond
  that. It cannot judge whether a caption is well-written, only cheap
  surface statistics about it.
- Probabilities are not recalibrated (Brier score checked, not corrected) —
  usable for ranking/thresholding, not for a precise expected-value
  calculation.
- No fairness audit — `risk_tier=minimal`, no protected attributes involved
  (this predicts post performance, not anything about people).

## Deep-learning gate

`not_warranted` (see `ml/03-modeling/handoff.json`). Revisit if per-platform
post volume roughly doubles, or a pretrained caption embedding is shown via
ablation to carry real signal on its own.

## Serving

Internal-only FastAPI service (`predictor/`), reached only by the
`dhaka-kacchi-connect` admin backend over the docker-compose network —
never exposed to the internet. See `ml/05-production/handoff.json` for the
full serving spec, artifact hash, and monitoring/retraining plan.

## Retraining

No automatic trigger. Manual: re-run `ml/00` through `ml/03` (or at minimum
`ml/01-data/extract.py` → `ml/03-modeling/train.py` → `ml/05-production/
build_artifact.py`) once real post volume has grown meaningfully — there is
no value in retraining on the same 360 rows. See
`ml/05-production/handoff.json`'s `serving_spec.retraining` for the recorded
rationale.
