# Data constraints and portfolio transparency

This is both a real business system for Dhaka Kacchi and a portfolio piece. Keep this file honest
and current: every component that touches data or a model falls into exactly one of three states.
Update it in the same commit that changes the underlying status — it decays fast otherwise.

1. **Real.** Running on genuine Dhaka Kacchi data, genuinely trustworthy.
2. **Stand-in dataset.** Pipeline built and validated, but not yet validated on real Dhaka Kacchi
   data — named public dataset stands in until volume supports the real thing.
3. **Designed, not built.** Documented in `ARCHITECTURE.md` but no code exists yet. Useful for
   design-conversation context; never describe it as working.

| Component | Status | Note |
|---|---|---|
| Order/COGS warehouse (`orders`, `order_line`, `recipe`, `menu_item`, `ingredient`, ...) | Real | Live since the Phase 1 spine (`ARCHITECTURE.md` §9). |
| Review intelligence — sentiment pipeline | Real | Deployed at `reviews.dhakakacchi.com`. |
| Review intelligence — model training data | Stand-in dataset | Yelp dataset stood in for training; live reviews ingest for real going forward. |
| Warehouse `event` / `event_taxonomy` tables | Real | Migration `0014`, applied and verified (`make verify`, `make gate`, `make verify-idempotent`). Empty — nothing has been ingested into them yet, see the ingest-job row below. |
| Marketing attribution schema (`channel`, `campaign`, `campaign_variant`, `social_post`, `social_metrics_snapshot`, `order_attribution`, `promotion`, `experiment`) | Designed, not built | `ARCHITECTURE.md` §4.7. Not yet migrated. |
| Website event capture (`dhaka-kacchi-connect`'s `POST /v1/events` + `events` table) | Real, but dormant | Live in production, tested end-to-end (2026-09-21): valid events insert, invalid input rejects, IP throttle works. Nothing on the actual website calls it yet — no frontend wiring — so it exists but currently receives zero real traffic. |
| Warehouse ingest job reading `dhaka-kacchi-connect`'s `events` table | Designed, not built | Would mirror `warehouse/ingest/direct.py`'s `ordering_reader` pattern. Blocked on the frontend actually emitting events — nothing to ingest yet even once built. |
| Paid campaign ingestion (Meta/Instagram/TikTok/LinkedIn ad spend) | Designed, not built | No paid campaign exists yet — nothing to ingest until one runs. |
| CLV / churn models | Not built | Gated on order-history volume; will use a public stand-in dataset first per `ARCHITECTURE.md` §4.7's data-sufficiency note. |
| Dish-photo classifier (Vision Transformer) | Real | Existing deployed service, outside this repo. |
