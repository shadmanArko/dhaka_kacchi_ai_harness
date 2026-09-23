# Stage 01 — Data

**Project:** dhaka_kacchi_ai_harness — social post engagement prediction · **Run:** 2026-09-23 · **Skill:** ml-data v1.0.0
**Status:** partial · **Confidence:** full

---

## What this stage did

Built and verified the real dataset for the social-post-engagement project: 360
posts (228 Facebook + 132 Instagram, Threads excluded per stage 00), joined to
their metrics snapshot with no fan-out, no orphans, and a spine row count that
matches the join exactly. Computed the actual label (expanding per-platform
median engagement) on this real data and confirmed it's non-degenerate (46.4%
positive overall). Found one real cross-platform vocabulary mismatch
(Facebook calls a video post "video", Instagram calls the same thing "reel")
and one real missingness gap (10 Facebook posts have no content_type) — both
handled with deterministic decisions, not silently.

## Bootstrap

- **Mode:** handoff
- **Consumed:** `ml/00-problem-framing/handoff.json` (sha256 `3e26c615...`)
- **What this means:** `prediction_time`, `target_definition`, and
  `primary_metric` were read directly from stage 00, not reconstructed.

## Decisions

| # | Decision | Why | Reversible |
|---|----------|-----|------------|
| 1 | Spine = facebook+instagram social_post, 360 rows | Matches stage 00's Threads-exclusion scope | **no** |
| 2 | Join takes social_metrics_snapshot as one row per post, checked empirically rather than assumed | The schema's own UNIQUE constraint is `(social_post_id, captured_at)`, not `social_post_id` alone — multiple snapshots per post are structurally possible even though none exist today | yes |
| 3 | Facebook's 10 NULL content_type rows recoded to literal `'unknown'` | Deterministic recode of a raw categorical, not a learned imputation — belongs here, not in the stage 03 pipeline | yes |
| 4 | Derive a cross-platform `is_video` boolean alongside raw content_type | Facebook labels video posts `'video'`; Instagram's equivalent is `'reel'` (Instagram's own CHECK constraint doesn't even allow `'video'`) — without this, the model has to re-learn that equivalence from ~360 rows | yes |
| 5 | No imputation performed in this stage | Imputation parameters are learned from data and belong in the stage 03 pipeline, fit per training fold | yes |

## Verified

| Check | Method | Result |
|-------|--------|--------|
| Spine row count counted independently before any join | `SELECT platform, count(*) ... GROUP BY platform` | pass (228 + 132 = 360) |
| Join key verified against DDL, not assumed | Queried `pg_constraint` directly | pass — composite UNIQUE `(social_post_id, captured_at)`, not what the column name alone would suggest |
| Duplicate check on the FK ran empirically | `GROUP BY social_post_id HAVING count(*) > 1` | pass — zero duplicates today |
| Orphan/null rate on the join key | Anti-join for posts with no snapshot | pass — zero orphans across all platforms |
| Join preserved spine row count | Independently-counted spine (360) vs. INNER JOIN result (360) | pass — exact match, no fan-out |
| Every feature checked against `prediction_time` | Manual review: platform/content_type/caption/posted_at are pre-publish; all engagement metrics live only in the snapshot table, populated post-hoc | pass |
| Target class balance measured on real data | SQL window-function expanding-median label, computed and counted per platform | pass — facebook 97/228 (42.5%), instagram 70/132 (53.0%), combined 167/360 (46.4%) |
| Sentinel/impossible values searched explicitly | Reviewed CHECK constraints + manual inspection of caption lengths and content_type value sets | pass — no sentinels found; content_type NULLs are genuine missingness, handled via decision #3 |

## Risks and gaps

| Severity | Issue | Mitigation |
|----------|-------|------------|
| high | Only 228/132 posts per platform | 03-modeling should pool both platforms with `platform` as a feature, not train two separate models |
| medium | content_type vocabulary mismatch across platforms | Handled via decision #4 (`is_video`); raw content_type kept too since it still carries platform-specific signal |
| medium | Caption representation undecided (raw text / derived stats / pretrained embedding) | Left to 03-modeling; start with cheap derived stats (length, hashtag count) |
| low | `campaign_variant_id` is NULL for all 360 rows | Expected — no paid campaign exists yet; excluded from features entirely |
| low | Snapshot cardinality could change (schema allows multiple per post; today there's exactly one) | `extract.py`'s plain INNER JOIN is only correct while that holds — flagged so a future re-run notices if it stops holding |

## Outputs

| Artifact | Path | What it is |
|----------|------|------------|
| Dataset extract | `ml/01-data/artifacts/dataset.csv` | 360 rows, joined + labeled, reproducible from the live warehouse |
| Extraction script | `ml/01-data/extract.py` | Deterministic SQL extract + label computation, no learned parameters |

## Modality screen

- **input_type:** `multimodal` — mostly tabular (platform, content_type,
  posted_at) plus one free-text field (caption). Recorded honestly as
  multimodal rather than dropping the text column silently.
- **n_rows:** 360 · **n_features:** 4 (platform, content_type, caption, posted_at)
- **unstructured_present:** true — `caption`
- **pretrained_models_available:** true (sentence-embedding models exist for
  caption text)
- **route:** `classical_first` — with only 360 rows, training any text/vision
  model from scratch is data-starved. The caption field's realistic path (per
  stage 00's transfer-learning decision) is a frozen pretrained embedding used
  as a feature in a classical model, not end-to-end deep learning. This route
  is advisory; 03-modeling's deep-learning gate has the final say.

## For the next stage

- **02-split:** `dataset.csv` spans 2026-04-26 to 2026-09-22 with `posted_at`
  as a natural time column. Worth deciding explicitly whether a temporal split
  (train on earlier posts, test on later ones) matches the real use case —
  predicting a not-yet-published post — better than a random split, even
  though nothing in `problem_spec` mandates one.
- **03-modeling:** recode content_type NULLs to `'unknown'`, derive `is_video`
  before one-hot-encoding raw content_type, and start caption features with
  cheap derived stats rather than defaulting straight to an embedding.

---

*Machine-readable version: `handoff.json` in this directory. Validate with
`validate_handoff.py --chain ml/`.*
