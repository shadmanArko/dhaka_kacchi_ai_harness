"""seed

Revision ID: 0012
Revises: 0011

Idempotent seed. Safe to re-run.

PRICE WARNING: every ingredient.current_price and every recipe qty below is a
PLACEHOLDER. None has been verified against a supplier invoice or a kitchen
weigh-out. Each ingredient is stamped {"source": "placeholder"} in
price_history so the section 6 menu cost model can REFUSE to publish a margin
while any contributing ingredient's latest price entry is unverified.
Menu PRICES are verbatim truth from the live backend; only COSTS are guesses.

ALLERGEN WARNING: German gastronomy letter scheme under EU FIC 1169/2011
Annex II. Per section 4.3 rule 1, allergen truth ORIGINATES in
brain/menu/items/<slug>.md and flows INTO the warehouse, never the reverse.
These are stand-ins until those files exist, and must be verified.

NOTE: `alembic upgrade head` will NOT replay this once applied, however
idempotent its body is - the version table short-circuits it. After editing
this file run `make reseed`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa

from warehouse.migrations.helpers import (  # noqa: F401
    JSONB,
    MONEY,
    QTY,
    RATIO,
    TEXT,
    TEXT_ARRAY,
    TIMESTAMPTZ,
    UNIT_MONEY,
    UUID,
    YIELD,
    delete_by,
    upsert,
)


def _jsonb(value: object) -> sa.sql.ColumnElement:
    """Render a JSON value as CAST('...' AS JSONB).

    Passing a bare Python dict/list works online but EXPLODES in offline mode
    (`alembic upgrade base:head --sql`), where literal_binds=True and SQLAlchemy
    has no literal renderer for JSONB. Casting from a text literal renders
    identically in both modes.
    """
    return sa.cast(sa.literal(json.dumps(value), TEXT), JSONB)


def _text_array(values: Sequence[str]) -> sa.sql.ColumnElement:
    """Render a Postgres text[] literal, offline-safe for the same reason."""
    inner = ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values)
    return sa.cast(sa.literal("{" + inner + "}", TEXT), TEXT_ARRAY)


revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed ids rather than gen_random_uuid(): recipe rows need a stable parent to
# point at without a lookup round-trip, and downgrade() can then delete exactly
# what this migration inserted.
SUPPLIER_IDS = {
    "halal_butcher_berlin": uuid.UUID("a0000000-0000-4000-8000-000000000001"),
    "asia_wholesale_berlin": uuid.UUID("a0000000-0000-4000-8000-000000000002"),
    "dairy_wholesale_berlin": uuid.UUID("a0000000-0000-4000-8000-000000000003"),
}
INGREDIENT_IDS = {
    slug: uuid.UUID(f"b0000000-0000-4000-8000-00000000000{i}")
    for i, slug in enumerate(
        [
            "young_mutton",
            "basmati_rice",
            "potato",
            "ghee",
            "spice_mix_bulk",
            "yogurt_plain",
            "mint_fresh",
        ],
        start=1,
    )
}
MENU_ITEM_IDS = {
    slug: uuid.UUID(f"c0000000-0000-4000-8000-00000000000{i}")
    for i, slug in enumerate(["kacchi_taster", "kacchi_regular", "borhani"], start=1)
}

# Lightweight table constructs, NOT ORM models: this revision has to keep
# working after the real schema has moved on.
supplier = sa.table(
    "supplier",
    sa.column("id", UUID),
    sa.column("slug", TEXT),
    sa.column("name", TEXT),
    sa.column("lead_time_days", sa.Integer),
    sa.column("min_order_eur", MONEY),
    sa.column("updated_at", TIMESTAMPTZ),
)
ingredient = sa.table(
    "ingredient",
    sa.column("id", UUID),
    sa.column("slug", TEXT),
    sa.column("name", TEXT),
    sa.column("supplier_id", UUID),
    sa.column("unit", TEXT),
    sa.column("current_price", UNIT_MONEY),
    sa.column("allergen_codes", TEXT_ARRAY),
    sa.column("price_history", JSONB),
    sa.column("updated_at", TIMESTAMPTZ),
)
menu_item = sa.table(
    "menu_item",
    sa.column("id", UUID),
    sa.column("slug", TEXT),
    sa.column("name_en", TEXT),
    sa.column("category", TEXT),
    sa.column("current_price", MONEY),
    sa.column("updated_at", TIMESTAMPTZ),
)
recipe = sa.table(
    "recipe",
    sa.column("menu_item_id", UUID),
    sa.column("ingredient_id", UUID),
    sa.column("qty", QTY),
    sa.column("yield_factor", YIELD),
    sa.column("version", sa.Integer),
    sa.column("updated_at", TIMESTAMPTZ),
)

SUPPLIER_ROWS = [
    # Named in the section 3.1 worked example.
    ("halal_butcher_berlin", "Halal Butcher Berlin", 3, "150.00"),
    ("asia_wholesale_berlin", "Asia Wholesale Berlin", 2, "100.00"),
    ("dairy_wholesale_berlin", "Dairy Wholesale Berlin", 1, "50.00"),
]

# Allergen notes:
#   G = Milch (milk). ghee -> G (dairy fat, casein traces); yogurt_plain -> G,
#       which is what carries the milk allergen onto the yogurt-based borhani.
#   basmati_rice is NOT marked with the gluten-cereal letter: rice is naturally
#       gluten-free, and a false allergen claim is a compliance problem in the
#       other direction.
#   spice_mix_bulk is left EMPTY PENDING VERIFICATION - commercial garam masala
#       blends frequently contain mustard and celery. Confirm the actual blend
#       spec before this reaches a channel listing.
INGREDIENT_ROWS = [
    # slug, name, supplier, unit, EUR/unit (PLACEHOLDER), allergen codes
    ("young_mutton", "Young mutton (lamb), halal", "halal_butcher_berlin", "kg", "14.5000", []),
    ("basmati_rice", "Basmati rice, aged", "asia_wholesale_berlin", "kg", "2.8000", []),
    ("potato", "Potato, waxy", "asia_wholesale_berlin", "kg", "1.2000", []),
    ("ghee", "Ghee (clarified butter)", "asia_wholesale_berlin", "kg", "9.5000", ["G"]),
    ("spice_mix_bulk", "Kacchi spice mix, bulk", "asia_wholesale_berlin", "kg", "18.0000", []),
    ("yogurt_plain", "Plain yogurt, full fat", "dairy_wholesale_berlin", "l", "2.2000", ["G"]),
    ("mint_fresh", "Fresh mint", "asia_wholesale_berlin", "kg", "12.0000", []),
]

# VERBATIM from dhaka-kacchi-connect/worker/src/data.ts, which states it is the
# single source of truth and prices the order server-side. slug = its sku, so
# warehouse rows join to real orders. Note the spelling is "Biriyani".
# name_de / name_bn are deliberately not seeded - see 0002_menu_item.py.
MENU_ITEM_ROWS = [
    ("kacchi_taster", "Kacchi Biriyani — Taster Box (750ml, 1 person)", "main", "9.99"),
    ("kacchi_regular", "Kacchi Biriyani — Regular Box (1000ml, 2 people)", "main", "15.00"),
    ("borhani", "Shahi Borhani (500ml)", "drink", "6.00"),
]

# qty = NET quantity in the finished portion, in the ingredient's own unit.
# yield_factor = usable fraction after trim/cook loss; gross to buy = qty / yield.
# ALL PLACEHOLDERS pending a kitchen weigh-out.
#
# GAP: 'fresh salad' and 'homemade chutney' appear in the menu descriptions but
# have no ingredient rows, so per-portion COGS is UNDERSTATED until they exist.
RECIPE_ROWS = [
    # taster: 1 piece of lamb, 1 whole potato, fresh salad
    ("kacchi_taster", "basmati_rice", "0.1200", "1.0000"),
    ("kacchi_taster", "young_mutton", "0.1000", "0.8500"),  # bone / trim loss
    ("kacchi_taster", "potato", "0.0800", "0.8000"),  # peeling loss
    ("kacchi_taster", "ghee", "0.0250", "1.0000"),
    ("kacchi_taster", "spice_mix_bulk", "0.0080", "1.0000"),
    # regular: 2 pieces of lamb, 1 whole potato, fresh salad, homemade chutney
    ("kacchi_regular", "basmati_rice", "0.2000", "1.0000"),
    ("kacchi_regular", "young_mutton", "0.2000", "0.8500"),
    ("kacchi_regular", "potato", "0.0800", "0.8000"),
    ("kacchi_regular", "ghee", "0.0400", "1.0000"),
    ("kacchi_regular", "spice_mix_bulk", "0.0130", "1.0000"),
    # borhani: yogurt-based drink with mint and aromatic spices
    ("borhani", "yogurt_plain", "0.4000", "1.0000"),
    ("borhani", "mint_fresh", "0.0050", "0.7000"),
    ("borhani", "spice_mix_bulk", "0.0030", "1.0000"),
]


def upgrade() -> None:
    now = sa.func.now()

    upsert(
        supplier,
        [
            {
                "id": SUPPLIER_IDS[slug],
                "slug": slug,
                "name": name,
                "lead_time_days": lead,
                "min_order_eur": Decimal(minimum),
                "updated_at": now,
            }
            for slug, name, lead, minimum in SUPPLIER_ROWS
        ],
        conflict_on=["slug"],
        # reliability_score is deliberately absent: it is agent-earned, and a
        # redeploy must not reset what procurement has scored.
        update=["name", "lead_time_days", "min_order_eur", "updated_at"],
    )

    upsert(
        ingredient,
        [
            {
                "id": INGREDIENT_IDS[slug],
                "slug": slug,
                "name": name,
                "supplier_id": SUPPLIER_IDS[supplier_slug],
                "unit": unit,
                "current_price": Decimal(price),
                "allergen_codes": _text_array(allergens),
                "price_history": _jsonb(
                    [{"price": price, "valid_from": "2026-01-01", "source": "placeholder"}]
                ),
                "updated_at": now,
            }
            for slug, name, supplier_slug, unit, price, allergens in INGREDIENT_ROWS
        ],
        conflict_on=["slug"],
        # current_price / price_history are deliberately absent: once a real
        # invoice lands, a redeploy must not silently restore the placeholder.
        update=["name", "supplier_id", "unit", "allergen_codes", "updated_at"],
    )

    upsert(
        menu_item,
        [
            {
                "id": MENU_ITEM_IDS[slug],
                "slug": slug,
                "name_en": name_en,
                "category": category,
                "current_price": Decimal(price),
                "updated_at": now,
            }
            for slug, name_en, category, price in MENU_ITEM_ROWS
        ],
        conflict_on=["slug"],
        # name_de / name_bn are deliberately absent: once content-production
        # translates them, a redeploy must not reset them to NULL.
        update=["name_en", "category", "current_price", "updated_at"],
    )

    upsert(
        recipe,
        [
            {
                "menu_item_id": MENU_ITEM_IDS[menu_slug],
                "ingredient_id": INGREDIENT_IDS[ing_slug],
                "qty": Decimal(qty),
                "yield_factor": Decimal(yield_factor),
                "version": 1,
                "updated_at": now,
            }
            for menu_slug, ing_slug, qty, yield_factor in RECIPE_ROWS
        ],
        conflict_on=["menu_item_id", "ingredient_id", "version"],
        # active_from is deliberately absent: changing it could trip
        # excl_recipe_no_overlap, and it SHOULD fail loudly rather than silently
        # rewrite a historical window. A recipe change is a version bump.
        update=["qty", "yield_factor", "updated_at"],
    )


def downgrade() -> None:
    # Reverse dependency order; delete only what this migration inserted.
    delete_by(recipe, "menu_item_id", MENU_ITEM_IDS.values())
    delete_by(menu_item, "slug", MENU_ITEM_IDS.keys())
    delete_by(ingredient, "slug", INGREDIENT_IDS.keys())
    delete_by(supplier, "slug", SUPPLIER_IDS.keys())
