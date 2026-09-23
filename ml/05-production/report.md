# Stage 05 — Production

**Project:** dhaka_kacchi_ai_harness — social post engagement prediction · **Run:** 2026-09-23 · **Skill:** ml-production v1.0.0
**Status:** complete · **Confidence:** reduced

---

## What this stage did

Shipped stage 03's winning model behind a small, internal-only FastAPI
service, reached only by `dhaka-kacchi-connect`'s admin backend, plus a new
admin page where the operator types in a draft post and gets a prediction +
plain-language reasons before publishing. Verified the full chain live —
predictor service → Node backend → real browser session — not just in
isolation. 04-deep-learning was correctly skipped: its gate said
`not_warranted` and nothing overrode it.

## Bootstrap

- **Mode:** reconstructed (confidence: reduced) — technically, per the
  kernel's bootstrap protocol, because `04-deep-learning`, the immediately
  preceding stage in the numbered chain, has no `handoff.json`: its gate
  verdict was `not_warranted` and correctly never overridden, so it never ran.
- **Consumed:** `ml/03-modeling/handoff.json` directly, with full confidence
  in that specific read (sha256 verified) — the threshold (0.49), the winning
  model (`logreg_full_tuned`), and the constraints
  (`explainability_required`, `inference_mode`) all came from
  `problem_spec`/`model_result` there.

## Decisions

| # | Decision | Why | Reversible |
|---|----------|-----|------------|
| 1 | Internal FastAPI service (`predictor/`), not batch-only or a Node reimplementation | User's own choice, confirmed via AskUserQuestion — keeps Python model logic in Python, no manual retraining-to-JS port | yes |
| 2 | Shipped artifact refit on all 360 rows (train+test), fixed hyperparameters | Test's job (evaluation) is done; more data for the final fit, no re-tuning | yes |
| 3 | `features.py` copied verbatim into the image at build time | The kernel's "one code path" defense against training-serving skew, in its simplest form | yes |
| 4 | Artifact SHA-256 verified before `joblib.load()` runs | joblib.load executes arbitrary code — refuse on mismatch rather than degrade silently | **no** |
| 5 | Explainability via linear coefficient × feature value | Exact (not an approximation) because the winning model is linear; no extra dependency | yes |
| 6 | No shadow/canary rollout | Single internal operator, advisory-only, low volume — the infrastructure would cost more than the risk it protects against | yes |
| 7 | Manual retraining trigger, no scheduled pipeline | ~15-20 new posts/month wouldn't move a retrain meaningfully | yes |
| 8 | Logs only, no monitoring dashboard | Scoped to the project's real size rather than over-built | yes |

## Verified

| Check | Method | Result |
|-------|--------|--------|
| Served threshold matches stage 03 | Read from `model_metadata.json`, confirmed via live response | pass — 0.49 |
| Same pipeline object served | `joblib.dump`/`load` of the whole `Pipeline`, no reimplementation | pass |
| Parity test (offline vs. online features) | Not automated — file-identity copy + manual live verification instead | **not_run**, stated plainly |
| Latency vs. budget | Manual curl timing against a "not latency-sensitive" budget | pass — sub-100ms |
| Rollback tested | No prior model version exists yet to roll back to | **not_run**, stated plainly |
| Shadow run completed | Deliberately skipped at this scale | **not_run**, by decision |
| Artifact hash recorded + verified | `verify_artifact.py` + a deliberately-corrupted-hash test (service correctly refused to load, `/ready` returned 503) | pass |
| Secrets not in image/config | Manual review — service takes no credentials at all | pass |
| Raw input not logged | Code review of the one logging call | pass — caption text never logged |
| Endpoint versioned, returns model version | `/v1/admin/post-predict`, `model_version` in every response | pass |
| **Full chain verified live** | Ran predictor + Node worker + Vite frontend locally, created a throwaway admin test account, logged in through the real browser, submitted a real draft post, got a real rendered prediction, deleted the test account afterward | pass |

## Live verification (the part that actually matters)

Submitted through the real admin UI: a Facebook video post, planned for a
Wednesday evening, with the caption *"Weekend special mutton kacchi biryani!
Pre-order now for Saturday pickup. #kacchi #biryani #dhaka #berlin
#foodie"*. Result: **"Likely below typical engagement," 47% confidence**,
top reasons `is_video=True` (+0.45), `hashtag_count` (-0.43),
`hour_bucket=evening` (-0.31) — consistent with the coefficients reported in
`ml/03-modeling/report.md`. The whole path — browser form → Node backend →
internal predictor service → back to the browser — worked without
intervention.

## Risks and gaps

| Severity | Issue | Mitigation |
|----------|-------|------------|
| medium | `docker compose build predictor` never actually run (no Docker daemon available this session) | Verified via an identical file layout run through a real `uvicorn` process instead; the real build happens on the first VPS deploy, gated by `deploy.sh`'s health-check before `ordering-backend` is touched |
| low | No automated parity test between training and serving features | File-copy-at-build-time is the real defense; a CI parity test is a reasonable future addition |
| low | No monitoring dashboard, only stdout logs | Matches the project's actual scale; `docker compose logs predictor` is the real inspection path |
| low | Redundant one-hot columns for `is_video` carried into the shipped artifact | Cosmetic; fix on the next real retrain |

## Outputs

| Artifact | Path | What it is |
|----------|------|------------|
| Model artifact | `ml/05-production/artifacts/model.joblib` | Fitted pipeline, refit on all 360 rows |
| Build script | `ml/05-production/build_artifact.py` | Reproducible artifact production |
| Model card | `ml/05-production/model_card.md` | Intended use, performance, limitations |
| Predictor service | `predictor/` | FastAPI app + Dockerfile, wired into `deploy/docker-compose.yml` and `deploy/deploy.sh` |
| Admin route | `dhaka-kacchi-connect/worker/src/index.ts` + `lib/predictorClient.ts` | `POST /v1/admin/post-predict` |
| Admin page | `dhaka-kacchi-connect/src/routes/admin/_layout.post-predict.tsx` | Form + result, in the admin nav |

## For the next stage

There is no stage 06 — this is the end of the pipeline for this project.
Ongoing: watch the first real VPS deploy build `predictor/Dockerfile` for
the first time; informally sanity-check a handful of real predictions
against what those posts actually did after publishing, once there's
enough real usage to compare; retrain manually once post volume has grown
meaningfully.

---

*Machine-readable version: `handoff.json` in this directory. Validate with
`validate_handoff.py --chain ml/`.*
