"""Standalone DB helpers for dashboard modules loaded before app.py finishes.

app.py defines q()/ex() too, but signin.py runs during app.py's import, so importing
them back would be circular. These are the same two queries against the same
DATABASE_URL -- deliberately duplicated rather than restructured, because untangling
Streamlit's top-to-bottom execution order for two four-line functions is not worth the
risk of breaking the login page.
"""
import os

import psycopg
from psycopg.rows import dict_row


def q_(sql: str, params=None) -> list[dict]:
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def ex_(sql: str, params=None) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
