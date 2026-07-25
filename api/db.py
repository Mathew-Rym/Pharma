"""Postgres access + Supabase Storage.

Raw SQL on purpose. An ORM buys you nothing here and hides the ledger invariants
that actually matter in this system.
"""
import logging
from contextlib import contextmanager
from typing import Any

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from supabase import create_client

from config import settings

log = logging.getLogger(__name__)

# prepare_threshold=None is REQUIRED if you use Supabase's pgbouncer pooler (port 6543),
# which runs in transaction mode and chokes on prepared statements.
pool = ConnectionPool(
    settings.DATABASE_URL,
    min_size=1,
    max_size=8,
    open=True,
    kwargs={"row_factory": dict_row, "prepare_threshold": None},
)

sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


# ---------------------------------------------------------------- SQL helpers
def q(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a SELECT, return all rows as dicts."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def q1(sql: str, params: tuple | dict | None = None) -> dict | None:
    """Run a SELECT, return the first row or None."""
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql: str, params: tuple | dict | None = None) -> None:
    """Run an INSERT/UPDATE/DELETE with no return value."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def ex1(sql: str, params: tuple | dict | None = None) -> dict | None:
    """Run an INSERT/UPDATE with a RETURNING clause."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


@contextmanager
def tx():
    """Explicit transaction for multi-statement writes that must be atomic.

    Usage:
        with tx() as cur:
            cur.execute(...)
            cur.execute(...)
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            yield cur
        # psycopg3 commits on clean context exit, rolls back on exception


# ------------------------------------------------------------ stock movements
def apply_movement(cur, batch_id: str, delta: int, reason: str,
                   actor_staff: str | None = None, ref_table: str | None = None,
                   ref_id: str | None = None, note: str | None = None) -> None:
    """The ONLY sanctioned way to change stock.

    Writes the ledger row and moves the batch quantity in one statement pair,
    inside the caller's transaction. Never UPDATE batches.qty_pieces anywhere else.
    """
    cur.execute(
        """insert into stock_movements
             (pharmacy_id, batch_id, delta_pieces, reason, actor_staff, ref_table, ref_id, note)
           values (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (settings.PHARMACY_ID, batch_id, delta, reason, actor_staff, ref_table, ref_id, note),
    )
    cur.execute(
        "update batches set qty_pieces = qty_pieces + %s where id = %s",
        (delta, batch_id),
    )


# ------------------------------------------------------------------- storage
def upload(bucket: str, path: str, data: bytes, content_type: str = "image/jpeg") -> str:
    """Upload bytes to Supabase Storage. Returns the storage path."""
    sb.storage.from_(bucket).upload(
        path, data, {"content-type": content_type, "upsert": "true"}
    )
    return path


def download(bucket: str, path: str) -> bytes:
    return sb.storage.from_(bucket).download(path)


def signed_url(bucket: str, path: str, seconds: int = 3600) -> str:
    res = sb.storage.from_(bucket).create_signed_url(path, seconds)
    return res.get("signedURL") or res.get("signedUrl", "")


def ensure_buckets() -> None:
    """Idempotent bucket creation, so a fresh clone just works."""
    existing = {b.name for b in sb.storage.list_buckets()}
    for name in (settings.BUCKET_INVOICES, settings.BUCKET_RX, settings.BUCKET_DOCS):
        if name not in existing:
            try:
                sb.storage.create_bucket(name, options={"public": False})
                log.info("created bucket %s", name)
            except Exception as e:  # already exists / race
                log.warning("bucket %s: %s", name, e)
