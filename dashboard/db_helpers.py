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

# connect_timeout: the Supabase pooler occasionally leaves a client connection hanging
# in the TCP/auth phase (observed during a full test run). Without a bound, a connect
# sits in select() forever -- the suite hangs with no output and the dashboard shows a
# spinner instead of an error. Ten seconds turns the ghost into a clean exception.
# prepare_threshold=None: DATABASE_URL is the transaction pooler (port 6543), which
# cannot serve prepared statements -- same reason api/db.py sets it.
_DB_KWARGS = dict(connect_timeout=10, prepare_threshold=None)


def _retry(fn):
    """Run a DB call once, and once more after a short sleep on failure.

    The Supabase pooler occasionally stalls a new connection for a few seconds under
    load (observed as the whole pytest run hanging with no output, because there was
    no bound anywhere). connect_timeout bounds each attempt; the retry absorbs the
    residual one-off. Two attempts, then raise -- never mask a real outage.
    """
    try:
        return fn()
    except Exception:
        import time
        time.sleep(1.0)
        return fn()


def q_(sql: str, params=None) -> list[dict]:
    def run():
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row,
                             **_DB_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    return _retry(run)


def ex_(sql: str, params=None) -> None:
    def run():
        with psycopg.connect(os.environ["DATABASE_URL"], **_DB_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
    _retry(run)
