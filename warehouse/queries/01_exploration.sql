-- =============================================================================
-- Exploration — orientation queries for a warehouse you haven't looked at yet.
-- Read-only. Safe to run anytime, against any environment.
-- =============================================================================


-- All tables in the warehouse, alphabetically.
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY 1;


-- Row count per table — the fastest way to see what has data and what's empty.
-- Expect: menu_item=3, ingredient=7, supplier=3, recipe=13 from the seed
-- migration; everything else is 0 until ingestion jobs exist.
SELECT 'customer'      AS table_name, count(*) FROM customer
UNION ALL SELECT 'menu_item',      count(*) FROM menu_item
UNION ALL SELECT 'supplier',       count(*) FROM supplier
UNION ALL SELECT 'ingredient',     count(*) FROM ingredient
UNION ALL SELECT 'recipe',         count(*) FROM recipe
UNION ALL SELECT 'orders',         count(*) FROM orders
UNION ALL SELECT 'order_line',     count(*) FROM order_line
UNION ALL SELECT 'review',         count(*) FROM review
UNION ALL SELECT 'ad_spend',       count(*) FROM ad_spend
UNION ALL SELECT 'cockpit_alert',  count(*) FROM cockpit_alert
UNION ALL SELECT 'agent_action',   count(*) FROM agent_action
ORDER BY 1;


-- The seeded menu, cheapest first.
SELECT slug, name_en, category, current_price
FROM menu_item
ORDER BY current_price;


-- Ingredients with their supplier and allergen codes.
-- allergen_codes uses the German gastronomy letter scheme (EU FIC 1169/2011
-- Annex II) — 'G' is milk. Prices are PLACEHOLDERS; see price_history.source.
SELECT i.slug, i.unit, i.current_price, i.allergen_codes, s.name AS supplier
FROM ingredient i
LEFT JOIN supplier s ON s.id = i.supplier_id
ORDER BY i.slug;


-- Which ingredient prices are still unverified placeholders.
-- The menu cost model should refuse to publish a margin while this is non-empty.
SELECT slug, current_price, price_history -> -1 ->> 'source' AS latest_price_source
FROM ingredient
WHERE price_history -> -1 ->> 'source' = 'placeholder';
