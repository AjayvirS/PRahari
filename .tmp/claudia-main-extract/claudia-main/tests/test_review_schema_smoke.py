"""Smoke test: new review-delivery tables must be creatable via SCHEMA_SQL.

The pg_conn fixture runs SCHEMA_SQL inside a throwaway schema on setup.
If the new DDL is broken, this test blows up immediately.
"""


def test_review_tables_exist_after_migrate(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s), to_regclass(%s)",
            ("pr_review_announcements", "pr_review_digests"),
        )
        ann, dig = cur.fetchone()
    assert ann is not None, "pr_review_announcements missing after SCHEMA_SQL"
    assert dig is not None, "pr_review_digests missing after SCHEMA_SQL"
