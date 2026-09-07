# Dhaka Kacchi — AI Harness Architecture

**Status:** draft v0.2
**Owner:** Arko
**Scope:** end-to-end agentic + ML operating system for a Berlin cloud kitchen

---

## 1. Purpose

Run Dhaka Kacchi as a mostly self-operating business. Every recurring decision and every recurring
piece of work is owned by a named agent with a defined autonomy level, a defined data dependency,
and a defined failure mode.

This document is the map. It is deliberately exhaustive on *domains* and deliberately conservative on
*autonomy*. Twenty-four domains are catalogued. Almost none of them should be fully autonomous in
year one.

**Non-goals for v1:** multi-city expansion, franchise tooling, a general-purpose "AI CEO", any agent
that can sign, spend above threshold, or answer a food-safety question without a deterministic lookup.

---

## 2. Design principles

1. **Substrate before agents.** A clever agent on top of fragmented data is worse than no agent,
   because it produces confident answers from an incomplete world model.
2. **Autonomy is per-action, not per-agent.** The same agent may draft one thing and execute another.
3. **Policy is data.** Autonomy tiers, spend caps, and escalation rules live in a versioned YAML file,
   enforced in code, never in a prompt.
4. **Everything reversible is logged with its reversal token. Everything irreversible needs a human.**
5. **Rejections are training data.** Every edit or rejection in the approval queue feeds the eval set.
6. **ML comes last.** Most of this system is agentic and rules-based work that pays off at any volume.
   The statistical layer needs history the business has not yet produced.
7. **The kitchen is the constraint.** No agent may commit to demand the production calendar cannot
   serve. Capacity check is a hard gate, not an advisory.
8. **Exception-first monitoring.** The default is silence. Agents surface things only when they need
   attention. The morning brief is not a summary of everything — it is a prioritised list of
   exceptions plus a short health check. A dashboard you read every morning is better than an alert
   you ignore every five minutes.

---

## 3. System overview

```
┌─ LAYER 0 · SUBSTRATE ──────────────────────────────────────────────────┐
│  Data warehouse      Tool layer (MCP)      Company brain (git)         │
│  one ID per order    one server/system     SOPs, voice, menu truth     │
├─────────────────────────────────────────────────────────────────────────┤
│  Control plane                        Eval harness                     │
│  approvals · audit log · policy       golden sets · adversarial cases  │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
┌─ LAYER 1 · DERIVED ASSETS ─────────────────────────────────────────────┐
│  Menu cost model     Demand forecast       Review signals              │
│  recipe → margin     (needs ~12mo data)    already shipped             │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
┌─ LAYER 2 · DOMAIN AGENTS (24) ─────────────────────────────────────────┐
│  Revenue (11)   Operations (5)   Customer (2)   Back office (6)        │
└─────────────────────────────────────────────────────────────────────────┘
```

Rule: nothing in a lower band is built before the band above it exists for that agent's specific
dependencies. An agent may skip a Layer 1 asset it does not need, but never a Layer 0 one.

### 3.1 Agent orchestration pattern

Agents are not independent. They can call each other through the orchestrator. The pattern is always:
initiating agent → orchestrator → consulted agents → synthesised decision → CEO cockpit.

Here is a worked example — the Saturday promotion scenario — showing how coordination actually flows:

```
1. brand-marketing agent
   "Propose a Saturday 10% discount campaign targeting Neukölln."
   → writes proposal to orchestrator

2. orchestrator calls:

   demand-planning agent
   → reads forecast model + historical Saturday data
   → returns: "expected +28% order volume if promo runs"

   procurement-inventory agent
   → reads current stock + supplier lead times
   → returns: "beef stock insufficient for +28% — can cover +12% only"

   finance agent
   → reads margin model + promo cost
   → returns: "at +12% volume, 10% discount reduces net margin by 3.1% — still above floor"

   kitchen-quality agent (via staffing agent)
   → reads production calendar + shift schedule
   → returns: "Saturday capacity 94 orders max; +12% = 91 orders — within limit"

3. orchestrator synthesises:
   decision: "run promo, cap orders at 90, add sold-out message after"
   confidence: 0.87
   requires_approval: true   ← promo involves public-facing price change

4. CEO cockpit surfaces the card:
   "Saturday promo ready — capped at 90 orders. Approve?"
   [Approve] [Edit] [Reject]
```

This is the reference pattern. Every multi-agent flow follows this shape:
propose → consult in parallel → synthesise → route to cockpit if approval needed → execute → log.

---

## 4. CEO cockpit — the primary interface

Every agent in this system ultimately reports to one screen: the CEO cockpit. This is how you
interact with the harness. Not dashboards, not Slack notifications, not email — one screen,
phone-first, checked once in the morning.

### 4.1 Structure

The cockpit has two columns and a health summary. Nothing else.

```
┌─ Dhaka Kacchi · Monday 7 Sep ────────────────────────────────────────┐
│                                                                        │
│  YESTERDAY                                                             │
│  Revenue €312 · Orders 24 · Avg €13.00 · Margin 34%                  │
│                                                                        │
│  GOOD                          NEEDS YOUR DECISION                     │
│  ✓ Repeat customers +12%       1. Approve beef order €180 →           │
│  ✓ Avg order value up €0.80    2. Approve Saturday promo →            │
│  ✓ Review reply sent (Google)  3. Review catering quote (40 pax) →    │
│  ✓ Stock reordered (basmati)                                           │
│                                PROBLEMS DETECTED                       │
│  AGENTS WORKING                ⚠ Cold food mentions up 300% (2 wk)   │
│  procurement: monitoring       ⚠ Instagram conversion -18%            │
│  seo: crawl in progress        ⚠ Packaging cost above budget          │
│  review: 2 replies queued                                              │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Rules

- **Silence is the default.** An agent that has nothing to flag writes nothing to the cockpit.
- **"Handled" column only shows actions taken autonomously** — things you don't need to review,
  but should know happened.
- **"Needs your decision" is limited to 5 items max.** If more than 5 items queue up, the system
  has misconfigured autonomy tiers — too many things are being escalated that should be handled.
- **Problems are surfaced by exception detectors**, not reported on a schedule. The cold food
  mention spike appears because the `reputation` agent crossed a threshold, not because it's Monday.
- **The cockpit is the only required morning action.** If you process it in under 5 minutes and
  everything is handled, the system is working.

### 4.3 Implementation

The cockpit is a single FastAPI endpoint + minimal mobile HTML page at `admin.dhakakacchi.com/cockpit`.
It aggregates from the `agent_action` table (pending approvals, recent autonomous actions) and from
a `cockpit_alert` table that agents write to when they detect exceptions. It does not call any agent
at render time — everything is pre-computed and waiting.

```sql
cockpit_alert   id, agent, alert_key, severity (info|warn|critical), title,
                detail, detected_at, acknowledged_at, resolved_at, resolution
                -- alert_key is a stable condition id with a partial UNIQUE on
                -- unresolved rows, so re-detection upserts instead of queuing a
                -- duplicate card. title is free text and cannot dedup.
```

Agents write to `cockpit_alert` when a threshold is crossed. You acknowledge from the cockpit.
Resolution is automatic when the underlying metric normalises, or manual when you close it.

---

## 5. Layer 0 — Substrate

### 4.1 Data warehouse

Single Postgres instance. No lakehouse, no streaming platform, no feature store. The business does not
generate enough data to justify the operational surface, and a solo operator cannot maintain it.

#### Core entities

```sql
customer        id, contact_hashes jsonb, first_order_at, channel_first_seen,
                consent_flags jsonb, deleted_at
                -- contact_hashes is jsonb, not an array: an array loses WHICH
                -- hash is which, and identity resolution must never match an
                -- email hash against a phone hash.

orders          id, customer_id, channel, external_id, placed_at, promised_at,
                delivered_at, gross, discounts, channel_fee, packaging_cost,
                delivery_cost, net_margin, status
                -- PLURAL, and the only plural table. `order` is reserved:
                -- CREATE TABLE order is a syntax error, and "order" needs
                -- quotes in every query forever, including the SQL the
                -- warehouse MCP server hands to agents.
                -- UNIQUE (channel, external_id) is what makes daily
                -- re-ingestion an upsert instead of a duplicate.

order_line      id, order_id, line_no, menu_item_id, qty, unit_price,
                unit_cogs_at_time
                -- surrogate id + line_no, NOT a composite (order_id,
                -- menu_item_id): the same dish can appear twice on one order
                -- at two prices. UNIQUE (order_id, line_no) is the
                -- re-ingestion arbiter.

menu_item       id, slug, name_de, name_en, name_bn, active_from, active_to,
                current_price, category
                -- slug = the ordering backend's sku, UNIQUE. One row per dish,
                -- not per version: order_line already snapshots price + COGS.

recipe          menu_item_id, ingredient_id, qty, yield_factor, version,
                active_from, active_to

ingredient      id, slug, name, supplier_id, unit, current_price,
                allergen_codes[], additive_codes[], price_history jsonb

supplier        id, slug, name, lead_time_days, min_order_eur, reliability_score

review          id, source, external_id, customer_id, text, rating,
                posted_at, aspects jsonb, replied_at, reply_text, redacted_at
                -- redacted_at records that free-text PII in text/reply_text was
                -- scrubbed for a section 10 erasure - distinct from the review
                -- simply never having been replied to.

payout_line     source, payout_id, external_order_id, gross, fee, adjustment,
                net, reconciled_order_id, mismatch_reason

ad_spend        id, date, platform, campaign_id, spend, impressions, clicks,
                attributed_orders, attributed_margin
                -- campaign_id is NOT NULL with an '__account__' sentinel: on
                -- PG14 NULLs are distinct in a UNIQUE index, so a NULL would
                -- silently permit duplicate rows.

staff_shift     staff_id, start_at, end_at, role, labor_cost

agent_action    id, agent, action_type, payload jsonb, tier, status,
                proposed_at, expires_at, approved_by, rejection_reason_code,
                edit_diff jsonb, executed_at, idempotency_key, reversible,
                reversal_token, reversed_at, outcome jsonb,
                decision text, reason text, confidence numeric,
                expected_impact text, risk text, requires_approval bool
                -- expires_at carries expires_h from autonomy-tiers.yaml; without
                -- it "unapproved actions auto-cancel at expiry" is
                -- unimplementable. requires_approval is NOT derivable from tier
                -- alone (place_purchase_order is act <=EUR 200, draft above), so
                -- the policy verdict is stored as evaluated at proposal time.

cockpit_alert   id, agent, severity (info|warn|critical), title, detail,
                detected_at, acknowledged_at, resolved_at, resolution

creator_collab  id, creator_name, platform, campaign_name, promo_code,
                cost_eur, collab_date, attributed_orders, attributed_revenue,
                roi_x, notes
```

**Status.** Built in `warehouse/migrations/` as of the Phase-1 spine: `customer`,
`menu_item`, `supplier`, `ingredient`, `recipe`, `orders`, `order_line`, `review`,
`ad_spend`, `cockpit_alert`, `agent_action`. Still to build: `payout_line`,
`staff_shift`, `creator_collab`. `payout_line` is the one that matters — until it
exists, `orders.channel_fee` is a *trusted* aggregator number, which is exactly what
"Reconciliation, not trust" below says never to trust.

**VAT.** No VAT column exists: Dhaka Kacchi operates under the Kleinunternehmerregelung
(§19 UStG), so `gross` is full revenue. At the threshold this must be revisited —
German takeaway food is 7% and non-alcoholic drinks 19%, so a kacchi + borhani basket
is mixed-rate and needs a per-line `vat_rate`; an order-level column cannot represent
it. Until then every margin figure computed from `gross` would be optimistic by 7–19%.

#### Agent decision schema

Every agent writes a structured decision object into `agent_action` before any action is taken.
The approval queue displays it. The audit log stores it permanently. This is what makes the queue
reviewable rather than just a list of pending actions.

```json
{
  "decision": "place purchase order — Halal Butcher Berlin, 20 kg beef, €180",
  "reason": "beef stock at 4 kg; Saturday production requires 18 kg; 3-day lead time",
  "confidence": 0.94,
  "expected_impact": "prevents production gap for 87 expected Saturday orders",
  "risk": "low — reversible within 24h, within €200 autonomous cap",
  "requires_approval": false
}
```

Fields:

| Field | Type | Purpose |
|---|---|---|
| `decision` | text | One sentence: what the agent wants to do |
| `reason` | text | Why, grounded in data it read |
| `confidence` | 0.0–1.0 | Agent's self-assessed certainty |
| `expected_impact` | text | What happens if approved |
| `risk` | text | What could go wrong; reversibility |
| `requires_approval` | bool | Derived from policy tier |

The approval queue renders this as a card. You see the decision, the reasoning, and the risk in under
10 seconds, then tap approve or reject. Rejections require a one-tap reason code which feeds the
agent's eval set.

#### Three properties that matter more than the schema

**Identity resolution.** A person ordering on Lieferando Tuesday and direct Friday is one customer.
Match on hashed phone + hashed email + fuzzy address. Store hashes; keep raw contact values only where
there is a lawful basis and a defined retention window. This is the hardest problem in the build.
LTV, churn, retention, and CAC are all meaningless without it.

**Point-in-time costs.** `unit_cogs_at_time` is written onto the order line at order time. Never join
to current ingredient price for historical margin. When basmati moves 20%, last quarter's P&L must not
silently change. Same pattern via `active_from` / `active_to` on `menu_item` and `recipe`.

**Reconciliation, not trust.** Aggregator payout files never match order records — fees, refunds,
adjustments, and marketing contributions all differ. `payout_line` lands raw and a job flags
mismatches rather than overwriting. This usually finds real money in the first month.

#### Ingestion

One idempotent Python job per source, on a schedule. Land raw into a `raw_*` table first, transform
second. Plain SQL migrations are sufficient for year one; adopt dbt only when lineage across more than
~30 models becomes a real problem.

Sources in priority order:

1. Ordering backend (owned, direct DB read)
2. Google Business Profile (reviews, insights)
3. Lieferando / Wolt / Uber Eats (orders, payouts, menu state)
4. Meta Ads + Google Ads (spend, campaign metadata)
5. Accounting tool (invoices, expenses)
6. Supplier invoices (OCR → structured)
7. Instagram (comments, DMs, post performance)

---

### 4.2 Tool layer

One MCP server per external system, exposing a narrow typed interface. Agents never call raw HTTP.

Reference for MCP server design and Claude Code integration:
<https://docs.claude.com/en/docs/claude-code/overview> and the docs map at
<https://docs.claude.com/en/docs_site_map.md>.

#### Server inventory

| Server | Reads | Writes | Priority |
|---|---|---|---|
| `warehouse` | allowlisted SQL | none | P0 |
| `ordering-backend` | orders, menu, customers | menu updates, order status | P0 |
| `company-brain` | git repo files | PR only | P0 |
| `google-business` | reviews, insights, Q&A | posts, review replies | P1 |
| `lieferando` / `wolt` | orders, payouts, ranking | menu sync, promo state | P1 |
| `whatsapp-business` | inbound messages | outbound (template-gated) | P1 |
| `meta-ads` | spend, performance | budget, pause/resume, creative | P2 |
| `instagram` | comments, DMs, insights | posts, replies | P2 |
| `accounting` | ledger, invoices | draft entries only | P2 |
| `email` | inbox | send (draft-gated) | P2 |
| `supplier-portal` | catalogue, prices | purchase orders | P3 |

#### Interface contracts

- Every write tool accepts an `idempotency_key`. Agents retry; you do not want three identical POs.
- Every write tool returns a `reversal_token`, or declares `reversible: false`. The control plane
  reads this to decide which autonomy tier an action is even *eligible* for.
- Read tools live in a separate namespace from write tools, so a read-only agent cannot be handed a
  write capability through misconfiguration.
- Rate limiting, retry, and cost accounting live in the server, not the agent.
- Every tool call is logged with agent, timestamp, args hash, and result status.

---

### 4.3 Company brain

A git repository, not a vector database. At this scale the relevant slice fits in context, and diffs
plus PR review are worth more than semantic retrieval.

```
brain/
  brand/
    voice.md                 tone, register, language mix (DE/EN/BN)
    do-not-say.md            claims, comparisons, health assertions to avoid
    visual-guidelines.md
  menu/
    items/<slug>.md          ingredients, allergens, additives, portion weight,
                             prep time, provenance story, photo assets
  pricing/
    rules.md
    channel-parity.md        how commission differences map to per-channel price
    discount-policy.md       floors, stacking rules, who may approve what
  sop/
    kitchen/*.md
    delivery/*.md
    incident-response.md
  policy/
    escalation.md
    refund-limits.md
    autonomy-tiers.yaml      ← enforced in code, see 4.4
  compliance/
    obligations.md
    evidence/
    inspection-checklist.md
  decisions/
    ADR-0001-....md          one file per material decision
```

Two hard rules:

1. **Allergen and ingredient data originates here** and flows *into* the warehouse, never the reverse.
2. **Any agent producing public-facing text loads `voice.md` and `do-not-say.md` unconditionally.**

This is the existing per-project `CLAUDE.md` pattern, expanded and promoted to a shared dependency of
every agent rather than a per-session context file.

---

### 4.4 Control plane

The piece most self-built harnesses skip, and the reason most of them are abandoned within a quarter.

#### Autonomy tiers

| Tier | Meaning |
|---|---|
| `read` | Observe, analyse, alert. Cannot mutate anything. |
| `draft` | Produce output, queue for human approval, execute on approval. |
| `act` | Execute immediately, reversible within a defined window. |
| `never` | Requires a human to originate, not merely approve. |

#### Policy file

```yaml
# brain/policy/autonomy-tiers.yaml
reply_to_review:        { tier: draft, expires_h: 24 }
publish_instagram_post: { tier: act,   reversal_window_min: 30 }
adjust_ad_budget:       { tier: act,   reversal_window_min: 60, max_delta_eur: 50 }
pause_ad_campaign:      { tier: act,   reversal_window_min: 120 }
sync_menu_to_channel:   { tier: act,   reversal_window_min: 15 }
issue_refund:           { tier: draft, max_eur: 25, above: never }
place_purchase_order:   { tier: act,   max_eur: 200, above: draft }
change_menu_price:      { tier: draft }
send_bulk_campaign:     { tier: draft, min_recipients_for_review: 1 }
answer_allergen_query:  { tier: never, route: deterministic_lookup }
sign_contract:          { tier: never }
file_tax_return:        { tier: never }
publish_job_offer:      { tier: draft }
```

#### Approval queue

One screen, phone-first: approve / edit / reject. Requirements:

- Unapproved actions **auto-cancel at expiry** rather than sitting stale.
- Edits are captured as a diff, not just a final value.
- Rejections require a one-tap reason code, which feeds the eval set.
- Batched approval is allowed for same-type actions, capped at 10 per batch.

#### Audit log

`agent_action` is append-only. Every row carries the agent, the reasoning trace reference, the tool
calls made, the policy tier applied, and the outcome. This is the artefact you hand to an accountant,
an auditor, or yourself at 2am when something went wrong.

---

### 4.5 Eval harness

Per agent:

- **Golden set:** 30–50 real inputs with preferred outputs.
- **Adversarial set:** the angry review; the allergen question phrased as a preference
  ("I just don't like nuts"); the supplier email that is actually phishing; the catering enquiry that
  exceeds capacity; the refund request for an order that was never placed.
- **Regression gate:** runs on every prompt or model change, before deploy.
- **Live signal:** approval rate and edit distance, pulled from the approval queue.

The `ml-*` skill family's design → confirm → execute → report pattern maps onto this directly. Harness
agents should emit the same report shape so review is uniform across the fleet.

---

### 4.6 Observability and cost

- Per-agent token spend, per-day, against a budget. An agent that exceeds budget goes to `read` tier
  automatically rather than being killed.
- Per-action latency and success rate.
- A single daily brief aggregating every agent's output into one document. If the brief is not worth
  reading in five minutes, the fleet is producing noise and should be pruned.

---

## 6. Layer 1 — Derived assets

| Asset | Depends on | Notes |
|---|---|---|
| **Menu cost model** | `recipe`, `ingredient`, `order_line` | Recipe → per-portion COGS → margin. Recalculates when supplier prices move. Feeds pricing, menu engineering, and allergen compliance. |
| **Unit economics model** | `orders`, `payout_line`, `ad_spend` | True per-order margin by channel after commission, packaging, delivery, and attributed ad spend. |
| **Review signals** | `review` | Aspect-based sentiment. **Already built and deployed** (`reviews.dhakakacchi.com`). Remaining work: swap the Yelp-derived pipeline for live ingestion from Google, Lieferando, Wolt, and Instagram; add drift alerting; route aspect spikes to the kitchen agent as work items. |
| **Demand forecast** | ~12 months of `orders` history | Orders by dish × day × hour, conditioned on weather, paydays, public holidays, Ramadan timing, local events. Gates production planning. |
| **Customer value model** | resolved `customer` identity | RFM, churn probability, expected LTV by acquisition channel. |

Review signals is live today. Menu cost model and unit economics are buildable immediately. Demand
forecast and customer value are blocked on data volume, not on engineering.

---

## 7. Layer 2 — Domain agent catalogue

Twenty-four agents in four clusters. `Tier` is the *target* tier, reached only after the phase gates
in §8.

### 6.1 Revenue (11)

| Agent | Responsibility | Target tier |
|---|---|---|
| `brand-marketing` | Content calendar, Reels scripting, captions, posting, comment triage, trend monitoring | draft → act |
| `performance-marketing` | Campaign generation, creative variants, budget reallocation against *delivered margin* not platform ROAS, fatigue detection | draft → act (capped) |
| `seo-content` | Keyword research across DE / EN / transliterated Bengali, district and dish landing pages, menu + local business schema, GBP posts, crawl monitoring | draft |
| `community-distribution` | Diaspora Facebook and WhatsApp groups, student associations, community events; outreach drafting; festival calendar (Ramadan, Eid, Pohela Boishakh, Durga Puja) with campaigns built backwards from it | draft |
| `channel-management` | Menu sync across aggregators, per-channel price parity given commission spread, promo participation decisions, channel margin monitoring, direct-order migration | act (sync), draft (pricing) |
| `catering-sales` | Inbound lead qualification, quote generation from headcount + menu, **hard capacity check against the production calendar**, follow-up sequences, corporate and wedding pipeline | draft |
| `retention-crm` | RFM segmentation, churn scoring, win-back sequences, loyalty logic, occasion triggers | draft |
| `pricing-promotions` | Price testing, discount ROI attribution, bundle construction, cannibalisation detection | draft |
| `menu-engineering` | Margin × popularity classification, portion economics, cut/keep recommendations | read → draft |
| `content-production` | Photo direction, image enhancement, menu copy in three languages, Reels editing pipeline | draft |
| `creator-intelligence` | Creator database, campaign tracking, promo code attribution, ROI per creator (€ revenue / € cost). Reads `creator_collab` table. Surfaces when a creator's ROI drops below 1× or a collaboration is overdue for review. | read + draft |
| `product-rnd` | New dish concepts from review gaps and competitor menus, structured test protocol, limited-run launches with measured outcomes | read → draft |

### 6.2 Operations (5)

| Agent | Responsibility | Target tier |
|---|---|---|
| `demand-planning` | Forecast → prep list generation. Kacchi's long lead time makes this high-value and unforgiving. | draft |
| `procurement-inventory` | Supplier price tracking (halal meat, basmati, ghee, spices), reorder points, stock levels, expiry/FIFO, PO drafting, supplier scoring | act (capped) → draft above cap |
| `kitchen-quality` | SOP checklists, batch timing, HACCP logging, quality incidents linked to batch and shift | read + draft |
| `delivery-packaging` | Zone profitability, delivery time prediction, packaging performance (rice texture, borhani leakage are measurable complaint categories), courier scoring, failed delivery handling | read → draft |
| `staffing` | Demand-driven shift generation, availability, labour cost per production hour, break and hour compliance. Scheduling core transfers from DienstPilot. | draft |

### 6.3 Customer (2)

| Agent | Responsibility | Target tier |
|---|---|---|
| `support` | Order status, modifications, complaints, refunds across WhatsApp, phone, Instagram DM, email, in DE / EN / BN. Hard refund cap. **Allergen questions route to deterministic lookup, never generated.** | draft, narrow act |
| `reputation` | Review ingestion, aspect scoring, drift alerting, reply drafting, routing quality signals to `kitchen-quality` | act (ingest), draft (reply) |

### 6.4 Back office (6)

| Agent | Responsibility | Target tier |
|---|---|---|
| `finance` | Receipt/invoice OCR, payout reconciliation, VAT categorisation, per-order unit economics, CAC/LTV by channel, cash flow forecast, monthly close prep for the Steuerberater | read + draft |
| `compliance` | Tracks current obligations and flags gaps. See §9. | read + alert only |
| `hr` | Job ads, applicant screening, onboarding packets and training material in multiple languages, time tracking, payroll prep | draft |
| `vendor-admin` | Insurance, rent, subscriptions, renewal calendar, contract review | read + draft |
| `market-intel` | Berlin South Asian and biryani competitors: menus, prices, promos, review volume and sentiment, new entrants, aggregator ranking position | read |
| `strategy` | Decision log, experiment registry with pre-registered success metrics, weekly synthesis across the fleet, expansion modelling (new zones, second kitchen, retail line for frozen biryani or bottled borhani) | read + draft |

---

## 8. Autonomy model

Promotion is per action type, not per agent, and is earned:

```
read  →  draft   after the agent's output has been reviewed for 2 weeks
                 and is judged useful

draft →  act     after ≥80% approval rate over 2 consecutive weeks
                 AND the action is reversible
                 AND a spend/blast-radius cap is defined

act   →  act     cap raised only after 30 days with zero unreverted
                 bad actions of that type
```

Demotion is automatic and immediate on: a reverted action, a policy violation, a budget overrun, or an
eval regression.

**Permanently `never`:** contract signature, tax filing, allergen assertions, anything that binds the
business legally, anything irreversible above the cap.

---

## 9. Build sequence

| Phase | Work | Duration | Gate to exit |
|---|---|---|---|
| **1. Spine** | Warehouse schema + ingestion for ordering backend, GBP, aggregators. No agents. | 2–3 wk | One SQL query answers "true margin per order last month by channel" |
| **2. Brain + control** | Company brain repo, approval queue, audit log, policy enforcement. Stub agent only. | 1–2 wk | An action can be proposed → queued → approved → executed → logged → reversed |
| **3. Read-only fleet** | All 24 agents at `read` tier. Daily brief. | 3–4 wk | Brief running 3 weeks; you can name the 3 domains costing the most hours or euros |
| **4. Draft tier** | Only the 3 domains identified in phase 3. | 4–6 wk | ≥80% approval rate for 2 straight weeks against eval sets |
| **5. Act tier** | Promote individual action types with reversal windows. | ongoing | 30 days, zero unreverted bad actions per action type |
| **6. ML tier** | Demand forecast, churn, delivery time, price elasticity via the `ml-*` skill family. | month 12+ | ≥12 months clean history in the warehouse |

**Exception:** build `compliance` at read tier during phase 3 regardless of what the daily brief says.
The cost of a gap there is not proportional to the time it saves.

**Anti-pattern to avoid:** building draft-tier agents in all 24 domains at once. The fleet becomes
unreviewable, approval quality collapses, and the whole thing gets abandoned. Read tier scales to 24.
Draft tier scales to about 3.

---

## 10. Data protection and compliance

Two separate concerns, both easy to underestimate.

### 9.1 Data protection (GDPR / DSGVO)

This system centralises customer contact data, order history, and review text, then pipes it into LLM
calls. Decide and document **before phase 3**:

- What leaves your infrastructure, and to which processor.
- What is pseudonymised before it leaves (default: everything; agents work with hashes and IDs, not
  names and phone numbers, unless the task requires otherwise).
- Retention windows per entity, and the deletion path that actually reaches the warehouse, the brain,
  the logs, and any vendor.
- Lawful basis per processing purpose, especially for marketing and profiling.
- The subject access request path, given data is now spread across warehouse + audit log.
- Whether any profiling counts as automated decision-making with legal or similarly significant effect.

Written down in `brain/compliance/` as an artefact, not held in your head.

### 9.2 Operating obligations the `compliance` agent tracks

The agent's job is to **track current rules and flag gaps**, not to encode thresholds once and forget
them. Surfaces relevant to a Berlin food business:

- HACCP documentation and food hygiene records
- Staff food-handling certification
- Allergen and additive labelling on every menu listing, on every channel
- Packaging registration and reusable-packaging obligations for takeaway
- Cash register / POS security and record-retention requirements
- Trade registration and food inspection readiness
- Employment law: minimum wage, working time, break records, Minijob rules
- Insurance coverage adequacy

Verify all specifics with a lawyer and your Steuerberater. This document is architecture, not legal
advice.

---

## 11. Open decisions

- [ ] Identity resolution strategy: deterministic hash match only, or probabilistic address matching
      with a review queue for ambiguous pairs?
- [ ] Where the control plane UI lives: extend the existing admin, or a separate phone-first surface?
- [ ] Model routing policy: which agents justify a frontier model vs a cheaper one, and who decides?
- [ ] Whether `support` handles voice, or stays text-only in v1.
- [ ] Retention window for review text tied to an identified customer.
- [x] Whether the daily brief is push (message) or pull (dashboard). → Pull (cockpit page at
      `admin.dhakakacchi.com/cockpit`). Checked once in the morning. No push notifications unless
      severity is `critical`.

---

## Appendix A — Repository layout

```
dhaka-kacchi-harness/
  warehouse/
    migrations/
    ingest/<source>.py
    models/                 SQL views: unit economics, cohort, menu margin
  tools/                    one MCP server per directory
    warehouse/
    ordering-backend/
    google-business/
    ...
  agents/
    <cluster>/<agent>/
      agent.md              role, inputs, tools, tier, escalation
      evals/golden.jsonl
      evals/adversarial.jsonl
  control/
    queue/                  approval UI + API
    policy.py               enforces brain/policy/autonomy-tiers.yaml
    audit.py
  brain/                    (submodule or sibling repo — see §4.3)
  ops/
    daily_brief.py
    cost_report.py
  ARCHITECTURE.md           this file
  CLAUDE.md                 working context for coding sessions
```

## Appendix B — References

- Claude Code documentation: <https://docs.claude.com/en/docs/claude-code/overview>
- Claude API documentation: <https://docs.claude.com/en/api/overview>
- Docs site map: <https://docs.claude.com/en/docs_site_map.md>
- Existing review intelligence service: <https://reviews.dhakakacchi.com>
- Existing `ml-*` skill family: reused for the Layer 1 forecasting and scoring models in phase 6
