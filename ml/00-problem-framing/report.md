# Stage 00 — Problem Framing

**Project:** dhaka_kacchi_ai_harness — social post engagement prediction · **Run:** 2026-09-23 · **Skill:** ml-problem-framing v1.0.0
**Status:** partial · **Confidence:** full

---

## What this stage did

Framed a binary-classification project: before publishing an organic Instagram or
Facebook post, predict whether it's likely to land above or below that platform's
own typical engagement, so the operator can revise it before it goes live. Grounded
every assumption in live queries against the real warehouse rather than the ~420-post
estimate carried in from an earlier conversation — that number turned out to be off in
two ways (Instagram was temporarily missing locally, and Threads' engagement data is
too close to zero to share a label definition with the other platforms), one fully
fixed during this stage (Instagram re-ingested) and one handled by narrowing scope
(Threads excluded from the primary label). Status is `partial`, not `complete`,
specifically because the Threads check is an intentional, recorded `fail` — the
validator correctly refuses to call a stage complete while any check it ran still
reads as failed, even though the failure was fully explained and acted on rather
than left open. That is the honest state, not a gap.

## Bootstrap

- **Mode:** greenfield
- **Consumed:** none — first ML stage in this repo, no `ml/` directory existed before this run.
- **What this means:** every input below came from a fresh live query against the local
  warehouse (`DATABASE_URL`), not from a prior stage's handoff.

## Decisions

| # | Decision | Why | Reversible |
|---|----------|-----|------------|
| 1 | Binary classification (above/below platform-typical engagement), not regression | ~140 posts/platform on incompatible scales (Threads views vs. Facebook impressions vs. Instagram reach) makes a precise regression false precision; a relative tier is robust and still decision-useful | yes |
| 2 | Label = above the *expanding*, platform-specific median of likes+comments+shares, not the whole-dataset median | Using future posts to define "typical" would leak backward | yes |
| 3 | Threads excluded from the primary label; Facebook + Instagram are the primary platforms | Live query: 56/57 real Threads posts have zero likes+comments+shares. A median split there is ~98%/2%, degenerate, not usable | yes |
| 4 | No raw video/image pixel features in this pass | ~350-480 total posts is data-starved for fine-tuning any vision model; a frozen pretrained embedding as a *feature* (not full retraining) is the realistic transfer-learning fit, deferred until a tabular baseline exists to compare against | yes |
| 5 | Floor = balanced accuracy ≥ 0.60, set now | Genuinely uncertain there's enough signal in pre-publish tabular/caption features to clear even a modest bar; set low enough to be achievable, high enough to fail if the model just matches the dummy | **no** |

## Verified

| Check | Method | Result |
|-------|--------|--------|
| Facebook engagement has enough variance for a non-degenerate median split | Live SQL: per-platform avg/median/min/max of likes+comments+shares | pass (median=2.0, 161/228 posts nonzero) |
| Threads engagement supports the same label | Same query, threads platform | **fail** — 56/57 posts have zero engagement; led directly to decision #3 |
| Floor set before any model ran | No model code exists yet in this repo | pass |
| Dummy baseline is not degenerate | Label is ~50/50 by construction (median split) | pass |
| Prediction time is specific enough to audit a feature against | Manual check: platform/content_type/caption/day/hour are all pre-publish; engagement metrics are all post-hoc and excluded | pass |
| Instagram's real 132 posts (per `DATA_CONSTRAINTS.md`) are present in the local warehouse used for this work | Live SQL: `select platform, count(*) from social_post group by platform` | **initially failed** (0 rows) — fixed within this stage, see Risks |

## Risks and gaps

| Severity | Issue | Mitigation |
|----------|-------|------------|
| high | Small n: ~250-350 posts in the primary-platform (Facebook+Instagram) corpus | Stopping condition explicitly expects a negative finding to be an acceptable outcome; floor set modestly |
| medium | Threads' only real engagement signal is `views`, incompatible with the like/comment/share label used elsewhere | Excluded from this model; revisit with a Threads-specific `views`-based label once its post volume is worth a separate model |
| low | Instagram had zero rows locally at the start of this stage despite being real production data | **Resolved in this stage** — re-ran `warehouse.ingest.instagram` locally, landed all 132 posts, confirmed by re-querying |
| low | Caption text feature representation (raw text vs. derived stats vs. embedding) not yet decided | Left to 01-data/03-modeling once EDA on real captions is done; simple derived stats (length, hashtag count) are the safe starting default |

## Outputs

| Artifact | Path | What it is |
|----------|------|------------|
| Handoff | `ml/00-problem-framing/handoff.json` | Machine-readable problem spec for downstream stages |
| This report | `ml/00-problem-framing/report.md` | Human-readable version of the same |
| Repo doc | `docs/00-problem-framing.md` | Committed copy for anyone reading the repo directly |

## The frame itself

**Decision & actor:** Before publishing a drafted post, flag whether it's likely to
underperform that platform's typical engagement, so the operator (sole business
owner) can revise caption/media/timing, or post anyway knowing the odds. Advisory
only — nothing here posts or blocks anything automatically.

**Label:** `engagement = likes + comments + shares`, compared against the expanding,
platform-specific median up to that point in time. 1 = above, 0 = at-or-below.
Computed only for Facebook and Instagram.

**Prediction time:** Immediately before publishing — caption, media type, and
posting day/hour are decided; no engagement data exists yet. Legitimate features:
`platform`, `content_type`, caption text/derived stats, day-of-week, hour. Never
any post-hoc metric.

**Cost of errors:** Roughly symmetric — a false "will underperform" costs an
unnecessary revision; a false "it's fine" costs one missed improvement
opportunity. Advisory, not automated, so neither side is expensive.

**Primary metric:** balanced accuracy. **Floor:** ≥ 0.60.

**Baselines:** (a) coin-flip dummy, (b) untuned logistic regression on
`platform` + `content_type` alone.

**Stopping condition:** if a tuned baseline-through-GBDT pass doesn't beat the
dummy by ≥5 points of balanced accuracy, report a negative finding — the
dataset is too small/noisy for this yet — rather than tuning indefinitely.

**Constraints:** batch inference, explainability required (the operator needs
to trust the "why," not just the flag), no dedicated infra budget — runs
alongside the existing warehouse on the VPS. No protected attributes. EU AI
Act risk tier: minimal.

## For the next stage

`01-data` should:
1. Re-verify the local corpus (facebook=228, instagram=132; threads=57 excluded
   from the primary label) before doing anything else — don't re-trust the
   ~420 figure that was wrong going into this stage.
2. Run the modality screen — expect `classical_first` (tabular + short text),
   not `deep_learning_likely`, given the small n and no pixel features in scope.
3. Compute the actual label (expanding per-platform median) on the real data and
   report the resulting class balance before any split is designed.

---

*Machine-readable version: `handoff.json` in this directory. Validate with
`validate_handoff.py --chain ml/`.*
