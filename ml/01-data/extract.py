"""One-off extract for ml/01-data: spine (facebook+instagram social_post) joined
to its latest metrics snapshot, with the expanding per-platform median label
from ml/00-problem-framing computed inline. Deterministic - re-run anytime to
regenerate artifacts/dataset.csv; no learned parameters here (imputation/
encoding belong in the stage 03 pipeline, not here).
"""

import csv
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

QUERY = """
with base as (
    select
        sp.id,
        sp.platform,
        sp.posted_at,
        sp.content_type,
        sp.caption,
        s.likes + s.comments + s.shares as engagement,
        s.impressions,
        s.reach
    from social_post sp
    join social_metrics_snapshot s on s.social_post_id = sp.id
    where sp.platform in ('facebook', 'instagram')
),
ordered as (
    select *, row_number() over (partition by platform order by posted_at) as rn
    from base
),
labeled as (
    select
        o1.*,
        (
            select percentile_cont(0.5) within group (order by o2.engagement)
            from ordered o2
            where o2.platform = o1.platform and o2.rn <= o1.rn
        ) as expanding_median_engagement
    from ordered o1
)
select
    id,
    platform,
    posted_at,
    content_type,
    caption,
    engagement,
    impressions,
    reach,
    expanding_median_engagement,
    case when engagement > expanding_median_engagement then 1 else 0 end as label
from labeled
order by platform, posted_at;
"""


def main() -> None:
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(QUERY)
    columns = [desc.name for desc in cur.description]
    rows = cur.fetchall()

    out_path = Path(__file__).parent / "artifacts" / "dataset.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    main()
