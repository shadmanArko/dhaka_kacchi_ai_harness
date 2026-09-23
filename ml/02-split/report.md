# Stage 02 — Split

**Project:** dhaka_kacchi_ai_harness — social post engagement prediction · **Run:** 2026-09-23 · **Skill:** ml-split v1.0.0
**Status:** complete · **Confidence:** full

---

## What this stage did

Resolved 01-data's open question: split by time, not randomly, because the
real deployment scenario (predicting a not-yet-published post) always looks
forward. Cutoff at 2026-08-22 gives 289 training posts (Apr 26 – Aug 21) and
71 test posts (Aug 22 – Sep 22), verified with zero id overlap and every test
timestamp strictly after every training timestamp. One real gap surfaced and
recorded rather than hidden: Instagram's test-split positive rate (64.3%) runs
notably hotter than its train rate (50.0%), plausible noise at n=28 but not
confirmed as such.

## Bootstrap

- **Mode:** handoff
- **Consumed:** `ml/00-problem-framing/handoff.json`, `ml/01-data/handoff.json`
- **What this means:** `prediction_time`, `target_definition`, and the
  verified join/grain from 01-data were read directly, not reconstructed.

## Decisions

| # | Decision | Why | Reversible |
|---|----------|-----|------------|
| 1 | Temporal split, not random | Deployment always predicts a future, not-yet-published post; a random split would let the model see September posts during training while being "tested" on May posts | **no** |
| 2 | Cutoff = 2026-08-22 (289 train / 71 test) | ~80th percentile of posted_at, confirmed to land within a day for both platforms independently, so one global cutoff serves both | **no** |
| 3 | No gap between train and test | This is a retrospective offline dataset with fully-materialized labels, not a live stream — verified zero timestamp overlap directly rather than assuming a gap is needed | yes |
| 4 | Expanding-window CV (5 folds) for tuning within the 289-row train set | n=289 is small enough that a single validation slice would be unstable; folds stay time-ordered, never shuffled | yes |
| 5 | No group key applied | Grain is one row per post; no repeated entity (confirmed: all 360 ids unique) | yes |

## Verified

| Check | Method | Result |
|-------|--------|--------|
| No id in both splits | Set intersection of train/test id lists | pass — 0 overlap, 289+71=360 |
| No group in both | n/a, no group key for this problem | not_run (recorded, not silently skipped) |
| No duplicate rows across boundary | Intersected the set of duplicate captions (15 exist) against each side | pass — 0 straddle the boundary |
| Every test timestamp after every train timestamp | max(train.posted_at) vs. min(test.posted_at) | pass — 2026-08-21 18:32 < 2026-08-22 10:27 |
| Class balance within tolerance | Positive rate per platform, train vs. test | facebook: 42.7% vs 41.9% (close). instagram: 50.0% vs 64.3% (a real, flagged gap) |
| Column sets identical | Both splits are id-lists against the same dataset.csv | pass |
| Nothing fit outside training fold | No scaler/imputer/encoder touched in this stage | pass |
| Every feature exists at prediction_time | Re-affirms 01-data's own check; no new features introduced here | pass |
| Test set touched zero times | No model trained yet; only structural (timestamp/balance) checks ran against test rows | pass |

## Risks and gaps

| Severity | Issue | Mitigation |
|----------|-------|------------|
| medium | Instagram test positive rate (64.3%, 18/28) runs hot vs. train (50.0%) | 03-modeling should report balanced accuracy per-platform, not just combined, so this isn't averaged away |
| low | Single holdout gives one sample of forecast performance, not a distribution | Accepted at this scale; expanding-window CV during tuning partially compensates |

## Outputs

| Artifact | Path | What it is |
|----------|------|------------|
| Train ids | `ml/02-split/artifacts/train_ids.csv` | 289 rows: id, platform, posted_at |
| Test ids | `ml/02-split/artifacts/test_ids.csv` | 71 rows: id, platform, posted_at — single final evaluation only |

## Split specification

- **Strategy:** temporal, global cutoff (not per-platform — verified both
  platforms' 80th-percentile dates agree within a day)
- **Cutoff:** `2026-08-22T00:00:00+02:00`
- **Train:** 289 posts (2026-04-26 → 2026-08-21), facebook 185 / instagram 104
- **Test:** 71 posts (2026-08-22 → 2026-09-22), facebook 43 / instagram 28
- **Gap:** none (offline dataset, fully-materialized labels)
- **Within-train tuning:** 5-fold expanding-window (time-ordered) CV, seed 0
- **Group key:** none (one row per post, no repeated entity)
- **Stratify:** approximate, on `platform` only, since temporal ordering takes
  priority over exact stratification

## For the next stage

03-modeling: load the two id lists here and join against
`ml/01-data/artifacts/dataset.csv` rather than re-deriving a split. Use
expanding-window CV (not shuffled k-fold) for tuning. Touch
`test_ids.csv` exactly once, at the final evaluation. Report balanced
accuracy per-platform given the noted Instagram drift.

---

*Machine-readable version: `handoff.json` in this directory. Validate with
`validate_handoff.py --chain ml/`.*
