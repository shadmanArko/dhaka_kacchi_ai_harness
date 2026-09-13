-- How to add data by hand in DataGrip (or psql) - the warehouse database
-- (dhaka_kacchi). Plain-English notes above each example; copy the INSERT,
-- change the values, run it.
--
-- GOLDEN RULES:
--   1. Run a SELECT to check what you're about to change BEFORE an UPDATE/DELETE.
--   2. Never write a raw id (UUID) by hand - let Postgres generate it, or
--      look it up by its `slug` (see the examples below).
--   3. `slug` columns must be lowercase letters/numbers/underscores only
--      (e.g. "basmati_rice", not "Basmati Rice").
--   4. If a statement fails with a "violates check constraint" error, the
--      message tells you exactly which value is invalid and why - read it,
--      don't guess.


-- =====================================================================
-- SUPPLIER - a vendor you buy ingredients from
-- =====================================================================
INSERT INTO supplier (slug, name, lead_time_days, min_order_eur, reliability_score)
VALUES (
  'metro_berlin',        -- unique short code, lowercase_with_underscores
  'Metro Berlin',        -- the real name
  3,                     -- days between ordering and delivery (or NULL if unknown)
  50.00,                 -- minimum order in euros (or NULL if none)
  0.90                   -- 0.00-1.00, how reliable they've been (or NULL if unknown)
);


-- =====================================================================
-- INGREDIENT - something you buy and cook with
-- =====================================================================
-- `unit` MUST be one of: 'kg', 'l', 'piece', 'pack' (anything else is rejected).
-- `current_price` is the price per ONE unit (e.g. price per kg, not per bag).
INSERT INTO ingredient (slug, name, supplier_id, unit, current_price)
VALUES (
  'basmati_rice',
  'Basmati Rice',
  (SELECT id FROM supplier WHERE slug = 'metro_berlin'),  -- look up the supplier by its slug
  'kg',
  3.20
);

-- Changing a price later? Don't just UPDATE current_price - that loses the
-- history. Do this instead (keeps a record of what it used to cost):
UPDATE ingredient
SET price_history = price_history || jsonb_build_object(
      'price', current_price, 'changed_at', now(), 'source', 'manual'
    ),
    current_price = 3.45   -- the NEW price
WHERE slug = 'basmati_rice';


-- =====================================================================
-- MENU ITEM - something a customer can order
-- =====================================================================
-- `category` MUST be one of: 'main', 'side', 'drink', 'dessert', 'bundle'.
INSERT INTO menu_item (slug, name_en, category, current_price)
VALUES (
  'kacchi_taster',
  'Kacchi Biriyani — Taster Box',
  'main',
  9.99
);


-- =====================================================================
-- RECIPE - how much of an ingredient goes into a menu item
-- =====================================================================
-- This is what lets the system compute a real cost (COGS) per menu item.
-- `qty` is how much of the ingredient one portion uses, in the ingredient's
-- own `unit` (e.g. 0.150 kg of rice). `yield_factor` accounts for cooking
-- loss/waste - use 1.0 if you don't know, meaning "no loss assumed."
INSERT INTO recipe (menu_item_id, ingredient_id, qty, yield_factor, active_from)
VALUES (
  (SELECT id FROM menu_item WHERE slug = 'kacchi_taster'),
  (SELECT id FROM ingredient WHERE slug = 'basmati_rice'),
  0.150,
  1.0,
  now()
);

-- Ingredient quantity actually changed (new recipe version)? Don't edit the
-- old row - close it out and add a new one, so past orders still show the
-- cost that was true AT THE TIME. `active_to` must be strictly AFTER the
-- existing row's `active_from` - use `clock_timestamp()`, not `now()`, for
-- this: `now()` is frozen to when your whole script/transaction started,
-- so if you run the close-out and the new insert back to back, `now()`
-- would be the exact same instant both times and Postgres would reject it.
-- `clock_timestamp()` always gives the real current moment.
UPDATE recipe
SET active_to = clock_timestamp()
WHERE menu_item_id = (SELECT id FROM menu_item WHERE slug = 'kacchi_taster')
  AND ingredient_id = (SELECT id FROM ingredient WHERE slug = 'basmati_rice')
  AND active_to IS NULL;

INSERT INTO recipe (menu_item_id, ingredient_id, qty, yield_factor, version, active_from)
VALUES (
  (SELECT id FROM menu_item WHERE slug = 'kacchi_taster'),
  (SELECT id FROM ingredient WHERE slug = 'basmati_rice'),
  0.180,   -- the new quantity
  1.0,
  2,       -- bump the version number
  clock_timestamp()
);


-- =====================================================================
-- AD SPEND - daily marketing spend, if you're tracking it by hand
-- =====================================================================
-- `platform` MUST be one of: 'meta_ads', 'google_ads', 'lieferando_ads',
-- 'wolt_ads', 'uber_eats_ads'.
INSERT INTO ad_spend (date, platform, spend, impressions, clicks)
VALUES (
  '2026-09-09',
  'meta_ads',
  25.00,
  4000,
  120
);


-- =====================================================================
-- REVIEW - a customer review you want on record
-- =====================================================================
-- `source` MUST be one of: 'google', 'lieferando', 'wolt', 'uber_eats',
-- 'instagram', 'direct'. `external_id` just needs to be unique per source -
-- use the review's own ID from that platform, or make one up consistently.
INSERT INTO review (source, external_id, text, rating, posted_at)
VALUES (
  'google',
  'google_review_001',
  'Best biryani in Berlin!',
  5.0,
  now()
);


-- =====================================================================
-- Tables you normally do NOT insert into by hand
-- =====================================================================
-- `customer` and `orders`/`order_line` in THIS database are filled
-- automatically by the ingestion job (`make ingest-direct`), which pulls
-- real orders from the ordering website. Adding a row here by hand only
-- makes sense for backfilling old, pre-website order history (e.g. old
-- WhatsApp orders) - ask before doing this, since it needs a deliberate
-- decision about customer identity (see the warehouse's own privacy rules
-- in ARCHITECTURE.md - raw names/phone numbers should never land here).
--
-- `raw_orders_direct`, `agent_action`, `cockpit_alert` are internal/
-- not-yet-used tables - there's no reason to insert into these by hand.
