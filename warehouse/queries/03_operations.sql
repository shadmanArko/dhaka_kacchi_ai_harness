-- =============================================================================
-- Operations — the queries a human (or the CEO cockpit, per ARCHITECTURE.md
-- §4) would run day to day, once real order and review data is flowing.
-- All empty until ingestion jobs exist; that is expected, not a bug.
-- =============================================================================


-- Most recent orders with their line items.
SELECT
    o.channel, o.external_id, o.placed_at, o.status, o.gross,
    m.name_en, ol.qty, ol.unit_price
FROM orders o
JOIN order_line ol ON ol.order_id = o.id
JOIN menu_item m   ON m.id = ol.menu_item_id
ORDER BY o.placed_at DESC
LIMIT 50;


-- Open cockpit alerts — what "PROBLEMS DETECTED" would show (§4.1).
-- Silence is the default: an empty result here is the healthy state.
SELECT agent, alert_key, severity, title, detail, detected_at
FROM cockpit_alert
WHERE resolved_at IS NULL
ORDER BY
    CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
    detected_at DESC;


-- Pending approvals — what "NEEDS YOUR DECISION" would show (§4.1).
-- §4.2: if this ever exceeds 5 rows, autonomy tiers are misconfigured —
-- something is being escalated that should be handled autonomously instead.
SELECT agent, action_type, decision, reason, confidence, tier, proposed_at, expires_at
FROM agent_action
WHERE status IN ('proposed', 'queued')
ORDER BY proposed_at;


-- Actions auto-cancelled by expiry vs. ones a human actually decided —
-- a live signal on whether the approval queue is being worked or ignored.
SELECT status, count(*)
FROM agent_action
WHERE status IN ('approved', 'rejected', 'expired')
GROUP BY status;


-- Unreplied reviews below 4 stars — the reputation agent's queue.
SELECT source, external_id, rating, text, posted_at
FROM review
WHERE replied_at IS NULL
  AND rating IS NOT NULL
  AND rating < 4
ORDER BY posted_at DESC;


-- Repeat vs. one-time customers — the crudest possible retention signal,
-- useful before any real RFM/churn model exists.
SELECT
    count(*) FILTER (WHERE order_count = 1) AS one_time,
    count(*) FILTER (WHERE order_count > 1) AS repeat_customers
FROM (
    SELECT customer_id, count(*) AS order_count
    FROM orders
    WHERE customer_id IS NOT NULL
    GROUP BY customer_id
) c;


-- Supplier reliability at a glance — NULL means "not yet rated", not zero.
SELECT slug, name, lead_time_days, min_order_eur, reliability_score
FROM supplier
ORDER BY reliability_score DESC NULLS LAST;
