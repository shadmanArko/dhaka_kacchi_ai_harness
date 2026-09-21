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
| Marketing event/attribution schema (`event`, `event_taxonomy`, `channel`, `campaign`, ...) | Designed, not built | `ARCHITECTURE.md` §4.7. Migration `0014` starts turning this into real schema. |
| Website event capture (`dhaka-kacchi-connect`) | Designed, not built | Public `POST /v1/events` endpoint + `events` table, planned but not yet written. |
| Paid campaign ingestion (Meta/Instagram/TikTok/LinkedIn ad spend) | Designed, not built | No paid campaign exists yet — nothing to ingest until one runs. |
| CLV / churn models | Not built | Gated on order-history volume; will use a public stand-in dataset first per `ARCHITECTURE.md` §4.7's data-sufficiency note. |
| Dish-photo classifier (Vision Transformer) | Real | Existing deployed service, outside this repo. |
