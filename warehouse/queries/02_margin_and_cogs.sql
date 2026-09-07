-- =============================================================================
-- Margin and COGS — the recipe → cost → margin chain, and the ARCHITECTURE.md
-- §9 Phase-1 exit gate: "one SQL query answers true margin per order last
-- month by channel."
--
-- The exit-gate query is also implemented in warehouse/gate.py (`make gate`),
-- which additionally asserts the query plan never touches `ingredient` — the
-- structural proof that COGS is point-in-time, not recomputed from current
-- prices. Keep both in sync if you edit the math.
-- =============================================================================


-- Per-dish COGS and gross margin, using the CURRENT recipe and CURRENT
-- ingredient prices. This is a forward-looking / planning query — it answers
-- "what would this dish cost to make today", not "what did it actually cost
-- on a given historical order" (that question is answered by
-- order_line.unit_cogs_at_time; see the next query down).
SELECT
    m.slug,
    m.current_price,
    round(sum(r.qty / r.yield_factor * i.current_price), 4) AS cogs,
    round(m.current_price
          - sum(r.qty / r.yield_factor * i.current_price), 2) AS gross_margin_eur,
    round(100 * (m.current_price
                 - sum(r.qty / r.yield_factor * i.current_price))
              / nullif(m.current_price, 0), 1)                AS gross_margin_pct
FROM recipe r
JOIN menu_item m  ON m.id = r.menu_item_id
JOIN ingredient i ON i.id = r.ingredient_id
WHERE r.active_to IS NULL       -- the currently active recipe version only
GROUP BY m.slug, m.current_price
ORDER BY m.slug;


-- Full ingredient breakdown for one dish. Swap the slug.
SELECT
    m.slug AS dish,
    i.slug AS ingredient,
    r.qty,
    i.unit,
    r.yield_factor,
    round(r.qty / r.yield_factor, 4)                  AS gross_qty_to_purchase,
    round(r.qty / r.yield_factor * i.current_price, 4) AS line_cost_eur
FROM recipe r
JOIN menu_item m  ON m.id = r.menu_item_id
JOIN ingredient i ON i.id = r.ingredient_id
WHERE m.slug = 'kacchi_regular'  -- <- swap: kacchi_taster | kacchi_regular | borhani
  AND r.active_to IS NULL
ORDER BY line_cost_eur DESC;


-- THE EXIT GATE. True margin per order, last calendar month, by channel.
-- COGS comes from order_line.unit_cogs_at_time — frozen at order time — never
-- from a join to current ingredient prices. When basmati moves 20%, last
-- quarter's P&L must not silently change (ARCHITECTURE.md §5).
--
-- No VAT term: Dhaka Kacchi operates under the Kleinunternehmerregelung
-- (§19 UStG), so `gross` is full revenue. Revisit this query at the VAT
-- threshold — see the note in warehouse/migrations/versions/0006_orders.py.
WITH bounds AS (
    SELECT date_trunc('month', now()) - interval '1 month' AS month_start,
           date_trunc('month', now())                      AS month_end
),
order_cogs AS (
    SELECT ol.order_id, sum(ol.qty * ol.unit_cogs_at_time) AS cogs
    FROM order_line ol
    GROUP BY ol.order_id
)
SELECT
    o.channel,
    count(*)                                                     AS orders,
    round(sum(o.gross), 2)                                       AS gross,
    round(sum(o.discounts), 2)                                   AS discounts,
    round(sum(o.channel_fee + o.packaging_cost + o.delivery_cost), 2) AS direct_costs,
    round(sum(oc.cogs), 2)                                       AS cogs,
    round(sum(o.gross - o.discounts - o.channel_fee
              - o.packaging_cost - o.delivery_cost - oc.cogs), 2) AS true_margin,
    round(100 * sum(o.gross - o.discounts - o.channel_fee
                    - o.packaging_cost - o.delivery_cost - oc.cogs)
              / nullif(sum(o.gross - o.discounts), 0), 1)        AS true_margin_pct
FROM orders o
JOIN order_cogs oc ON oc.order_id = o.id
CROSS JOIN bounds b
WHERE o.status = 'delivered'
  AND o.placed_at >= b.month_start
  AND o.placed_at <  b.month_end
GROUP BY o.channel
ORDER BY true_margin DESC;


-- Guard: any DELIVERED order line with zero COGS is a data-quality problem,
-- not a real free portion. NOT NULL prevents nulls; 0.0000 is the silent
-- failure mode the exit-gate query cannot otherwise detect.
SELECT o.channel, o.external_id, ol.line_no, m.name_en
FROM order_line ol
JOIN orders o     ON o.id = ol.order_id
JOIN menu_item m  ON m.id = ol.menu_item_id
WHERE o.status = 'delivered' AND ol.unit_cogs_at_time = 0;


-- Ad spend efficiency by platform. Deliberately NOT joined to orders.channel:
-- an ad network drives orders on more than one channel (meta_ads → both
-- direct and lieferando), so this attribution is a model, not a fact — treat
-- it as directional, not as truth the way order_line.unit_cogs_at_time is.
SELECT
    platform,
    sum(spend)             AS spend,
    sum(attributed_orders) AS attributed_orders,
    sum(attributed_margin) AS attributed_margin,
    round(sum(spend) / nullif(sum(attributed_orders), 0), 2) AS cac_eur
FROM ad_spend
WHERE date >= date_trunc('month', now()) - interval '1 month'
  AND date <  date_trunc('month', now())
GROUP BY platform
ORDER BY spend DESC;
