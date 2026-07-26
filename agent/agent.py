"""Dishii bridge agent — runs on the pharmacy's own Windows PC.

Three jobs, in order of business value:

  1. BACKFILL history once. phAMACore has 12-24 months of demand signal. Without it
     the cloud starts from amnesia and cannot forecast anything for 90 days.
  2. INGEST sales continuously. phAMACore stays the till. If we don't see its sales,
     our stock number drifts from reality within a day and the product starts lying.
  3. OBEY commands. It long-polls the cloud for work. The cloud never connects to it,
     so there are no inbound ports and nothing to ask the pharmacy's router for.

Ingestion is layered and self-downgrading. It tries direct DB read, falls back to
folder watching, falls back to manual upload — and reports which mode it landed in.
You therefore do not need to know what phAMACore is before writing this.

READ-ONLY against phAMACore. No INSERT, no UPDATE, no DDL, ever. If this agent ever
writes to their production database you have destroyed the trust that the whole
business depends on.

Run for the demo:
    python agent.py --config config.ini
Probe first, before writing any DB code:
    python agent.py --probe-only
Run as a Windows service later:
    pythonw agent.py --config config.ini   (via Task Scheduler, "at startup")
"""
import argparse
import configparser
import csv
import hashlib
import json
import logging
import platform
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("agent")

AGENT_VERSION = "0.2.0"
POLL_SECONDS = 60
INGEST_SECONDS = 900          # 15 min — matches the spec's default
HTTP_TIMEOUT = 60
MAX_ATTEMPTS = 12             # after this an outbox batch is parked, not silently lost


# ============================================================ local queue
class LocalStore:
    """SQLite outbox. Kenyan small-business connectivity is intermittent; the agent
    must survive days offline without losing data or double-sending."""

    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""create table if not exists outbox (
            id text primary key, endpoint text, payload text,
            attempts integer default 0, created_at text, last_error text,
            last_attempt_at text)""")
        self.db.execute("""create table if not exists state (
            k text primary key, v text)""")
        self.db.execute("""create table if not exists seen (
            fingerprint text primary key, seen_at text)""")
        # older installs predate last_attempt_at
        cols = {r[1] for r in self.db.execute("pragma table_info(outbox)")}
        if "last_attempt_at" not in cols:
            self.db.execute("alter table outbox add column last_attempt_at text")
        self.db.commit()

    def get(self, k: str, default=None):
        r = self.db.execute("select v from state where k=?", (k,)).fetchone()
        return r[0] if r else default

    def put(self, k: str, v: str):
        self.db.execute("insert into state(k,v) values(?,?) "
                        "on conflict(k) do update set v=excluded.v", (k, str(v)))
        self.db.commit()

    def enqueue(self, endpoint: str, payload: dict):
        self.db.execute(
            "insert into outbox(id,endpoint,payload,created_at) values(?,?,?,?)",
            (str(uuid.uuid4()), endpoint, json.dumps(payload, default=str),
             datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def pending(self, limit: int = 20):
        return self.db.execute(
            "select id,endpoint,payload,attempts,last_attempt_at from outbox "
            "where attempts < ? order by created_at limit ?",
            (MAX_ATTEMPTS, limit)).fetchall()

    def parked_count(self) -> int:
        return self.db.execute("select count(*) from outbox where attempts >= ?",
                               (MAX_ATTEMPTS,)).fetchone()[0]

    def drop(self, batch_id: str):
        self.db.execute("delete from outbox where id=?", (batch_id,))
        self.db.commit()

    def fail(self, batch_id: str, err: str):
        self.db.execute(
            "update outbox set attempts=attempts+1, last_error=?, last_attempt_at=? "
            "where id=?",
            (err[:500], datetime.now(timezone.utc).isoformat(), batch_id))
        self.db.commit()

    def already_seen(self, fingerprint: str) -> bool:
        r = self.db.execute("select 1 from seen where fingerprint=?",
                            (fingerprint,)).fetchone()
        if r:
            return True
        self.db.execute("insert into seen(fingerprint,seen_at) values(?,?)",
                        (fingerprint, datetime.now(timezone.utc).isoformat()))
        self.db.commit()
        return False


# ============================================================ cloud client
class Cloud:
    def __init__(self, base_url: str, store: LocalStore, pharmacy_id: str):
        self.base = base_url.rstrip("/")
        self.store = store
        self.pharmacy_id = pharmacy_id
        self.token = store.get("agent_token")
        self.agent_id = store.get("agent_id")

    def _headers(self) -> dict:
        return {"x-agent-token": self.token or "", "content-type": "application/json"}

    def enrol(self, enrolment_token: str) -> bool:
        """One-time handshake. Exchanges the install token for a per-install token,
        so a leaked token never affects another pharmacy."""
        try:
            r = requests.post(f"{self.base}/agent/enrol", timeout=HTTP_TIMEOUT, json={
                "enrolment_token": enrolment_token,
                "pharmacy_id": self.pharmacy_id,
                "machine_name": platform.node(),
                "agent_version": AGENT_VERSION,
                "os": platform.platform(),
            })
            r.raise_for_status()
            d = r.json()
            self.token = d["agent_token"]
            self.agent_id = d["agent_id"]
            self.store.put("agent_token", self.token)
            self.store.put("agent_id", self.agent_id)
            log.info("enrolled as agent %s", self.agent_id)
            return True
        except Exception as e:
            log.error("enrolment failed: %s", e)
            return False

    def heartbeat(self, ingest_mode: str, db_detail: dict) -> dict:
        try:
            r = requests.post(f"{self.base}/agent/heartbeat", headers=self._headers(),
                              timeout=HTTP_TIMEOUT, json={
                                  "agent_version": AGENT_VERSION,
                                  "ingest_mode": ingest_mode,
                                  "db_detail": db_detail,
                              })
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("heartbeat failed: %s", e)
            return {}

    def poll_commands(self) -> list[dict]:
        try:
            r = requests.get(f"{self.base}/agent/commands", headers=self._headers(),
                             timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json().get("commands", [])
        except Exception as e:
            log.debug("command poll failed: %s", e)
            return []

    def command_result(self, command_id: str, ok: bool, result: dict):
        try:
            requests.post(f"{self.base}/agent/commands/{command_id}/result",
                          headers=self._headers(), timeout=HTTP_TIMEOUT,
                          json={"ok": ok, "result": result})
        except Exception as e:
            log.warning("could not return command result: %s", e)

    def flush(self):
        """Drain the outbox with exponential backoff. Idempotent server-side.

        Backoff is measured against last_attempt_at. An earlier version compared a
        hardcoded age of 0, which meant any batch past 3 attempts was skipped on
        every pass forever and the queue stalled silently.
        """
        now = datetime.now(timezone.utc)
        for batch_id, endpoint, payload, attempts, last_at in self.store.pending():
            if attempts > 0 and last_at:
                backoff = min(2 ** attempts, 3600)
                try:
                    age = (now - datetime.fromisoformat(last_at)).total_seconds()
                except ValueError:
                    age = backoff
                if age < backoff:
                    continue
            try:
                r = requests.post(f"{self.base}{endpoint}", headers=self._headers(),
                                  timeout=HTTP_TIMEOUT, data=payload)
                if r.status_code in (200, 201, 409):   # 409 = already ingested, fine
                    self.store.drop(batch_id)
                    log.info("sent %s -> %s", endpoint, r.status_code)
                else:
                    self.store.fail(batch_id, f"HTTP {r.status_code}: {r.text[:200]}")
            except Exception as e:
                self.store.fail(batch_id, str(e))
                log.warning("flush failed, will retry: %s", e)
                return       # stop on first network failure, preserve order

        parked = self.store.parked_count()
        if parked:
            log.error("%s outbox batch(es) parked after %s failed attempts — "
                      "inspect agent.db outbox.last_error", parked, MAX_ATTEMPTS)


# ============================================================ DB probe
def probe_database(install_dir: str | None = None) -> dict:
    """Discover, don't assume. Records what it found; never guesses silently.

    Answers the question we could not answer from Nairobi: what IS phAMACore
    on that machine.
    """
    found: dict = {"engine": None, "candidates": [], "odbc_dsns": [],
                   "services": [], "open_ports": [], "files": []}

    # 1. listening ports — cheapest signal, works without admin rights
    import socket
    for port, engine in ((3050, "firebird"), (1433, "mssql"),
                         (2638, "sqlanywhere"), (5432, "postgres"),
                         (3306, "mysql")):
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            found["open_ports"].append({"port": port, "engine": engine})
            found["engine"] = found["engine"] or engine
        except Exception:
            pass
        finally:
            s.close()

    # 2. Windows services
    if platform.system() == "Windows":
        try:
            import subprocess
            out = subprocess.run(["sc", "query", "state=", "all"],
                                 capture_output=True, text=True, timeout=30).stdout
            for token, engine in (("Firebird", "firebird"), ("MSSQL", "mssql"),
                                  ("SQLANY", "sqlanywhere"), ("Pervasive", "pervasive")):
                if token.lower() in out.lower():
                    found["services"].append(token)
                    found["engine"] = found["engine"] or engine
        except Exception as e:
            log.debug("service enumeration failed: %s", e)

        # 3. ODBC DSNs
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k = winreg.OpenKey(root, r"SOFTWARE\ODBC\ODBC.INI\ODBC Data Sources")
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(k, i)
                            found["odbc_dsns"].append({"dsn": name, "driver": value})
                            i += 1
                        except OSError:
                            break
                except FileNotFoundError:
                    continue
        except Exception as e:
            log.debug("odbc enumeration failed: %s", e)

    # 4. database files on disk
    roots = [install_dir] if install_dir else []
    roots += [r"C:\phAMACore", r"C:\Program Files\phAMACore",
              r"C:\Program Files (x86)\phAMACore", r"C:\Pharmacy", "C:\\"]
    exts = (".fdb", ".gdb", ".mdb", ".accdb", ".mdf", ".db", ".dbf")
    for root in roots:
        p = Path(root)
        if not p.exists():
            continue
        try:
            depth_limit = 3 if root == "C:\\" else 6
            for f in p.rglob("*"):
                if len(f.relative_to(p).parts) > depth_limit:
                    continue
                if f.suffix.lower() in exts and f.is_file():
                    found["files"].append({
                        "path": str(f),
                        "size_mb": round(f.stat().st_size / 1_048_576, 1),
                        "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    })
                    if len(found["files"]) >= 40:
                        break
        except (PermissionError, OSError):
            continue
        if len(found["files"]) >= 40:
            break

    # a file modified in the last two hours is almost certainly the live database
    live = [f for f in found["files"]
            if datetime.fromisoformat(f["modified"]) >
            datetime.now() - timedelta(hours=2)]
    if live:
        found["likely_live_db"] = max(live, key=lambda f: f["size_mb"])
        ext = Path(found["likely_live_db"]["path"]).suffix.lower()
        found["engine"] = found["engine"] or {
            ".fdb": "firebird", ".gdb": "firebird",
            ".mdb": "access", ".accdb": "access",
            ".mdf": "mssql", ".dbf": "dbase",
        }.get(ext)

    found["recommended_mode"] = ("db_poll" if found["engine"] and
                                 (found["open_ports"] or found.get("likely_live_db"))
                                 else "folder_watch")
    return found


# ============================================================ ingestion: folder
CSV_ALIASES = {
    "sold_at":     ["date", "saledate", "sale date", "datetime", "transdate", "time"],
    "external_id": ["saleid", "sale id", "invoiceno", "receiptno", "transno", "id",
                    "docno"],
    "legacy_code": ["itemcode", "item code", "code", "productcode"],
    "description": ["itemname", "item name", "description", "product", "name"],
    # Ordered by specificity — first match wins. The on-hand spellings (instore,
    # qtyonhand, closingqty, balance) come from seed/import_phamacore.py's HEADER_MAP,
    # which was built from the actual phAMACore export screens. Omitting them made
    # every stock-snapshot quantity parse as 0, which silently turned every variance
    # figure into "till says 0".
    "qty":         ["qty", "quantity", "salesqty", "qtysold", "units",
                    "instore", "qtyonhand", "qty on hand", "onhand", "stock",
                    "closingqty", "balance"],
    "unit_price":  ["price", "unitprice", "sellprice", "rate"],
    "line_total":  ["total", "linetotal", "amount", "value", "salesvalue"],
    "payment":     ["payment", "paymentmethod", "paytype", "mode", "tender"],
    "period":      ["stockperiod", "period", "month"],
}


def _norm(h: str) -> str:
    return "".join(c for c in (h or "").strip().lower() if c.isalnum() or c == " ").strip()


def _map_headers(headers: list[str]) -> dict:
    normed = [_norm(h) for h in headers]
    idx = {}
    for field, aliases in CSV_ALIASES.items():
        for a in aliases:
            if a in normed:
                idx[field] = normed.index(a)
                break
    return idx


def _read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    if path.suffix.lower() in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [["" if c is None else str(c) for c in r]
                for r in wb.active.iter_rows(values_only=True)]
        return (rows[0], rows[1:]) if rows else ([], [])
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(f, dialect))
    return (rows[0], rows[1:]) if rows else ([], [])


def _parse_qty(raw) -> tuple[int, int, bool]:
    """Parse a phAMACore quantity cell.

    Returns (packs, loose_pieces, is_pack_notation).

    phAMACore writes '5W0P' = 5 whole packs + 0 loose pieces, and '2WOP' is the same
    thing with the zero misprinted as a letter O. A plain number is already a piece
    count.

    The agent CANNOT convert packs to pieces — it does not know pack_size, only the
    cloud does. So it reports the components and a flag, and the cloud resolves it
    after matching the product. Collapsing '5W0P' to the integer 5 would understate a
    30s pack by 30x, which would corrupt both the stock ledger and every variance
    figure the owner is shown.
    """
    s = str(raw or "").strip().upper().replace(",", "")
    m = re.match(r"^(-?\d+)\s*W\s*(?:(\d+|O)\s*P?)?$", s)
    if m:
        loose_raw = m.group(2)
        loose = 0 if loose_raw in (None, "O") else int(loose_raw)
        return int(m.group(1)), loose, True
    try:
        return 0, int(float(s)), False
    except ValueError:
        return 0, 0, False


def _qty_fields(raw) -> dict:
    """The three fields every emitted row carries so the cloud can resolve units."""
    packs, loose, is_packs = _parse_qty(raw)
    return {
        # Best-effort piece count assuming pack_size=1. Correct for plain numbers;
        # the cloud recomputes it as (packs * pack_size + loose) when qty_is_packs.
        "qty_pieces": loose if is_packs else loose,
        "qty_packs": packs,
        "qty_loose": loose,
        "qty_is_packs": is_packs,
        "qty_raw": str(raw or "").strip() or None,
    }


def _parse_dt(raw) -> str | None:
    s = str(raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                "%d-%m-%Y", "%m/%d/%Y", "%d-%b-%Y", "%b-%Y", "%B-%Y", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


def _money(raw):
    try:
        return float(str(raw).replace(",", "").replace("KES", "").strip())
    except (TypeError, ValueError):
        return None


def classify_and_parse(path: Path) -> tuple[str, list[dict]]:
    """Detect what kind of export this is and parse accordingly.

    This is the 'Mix / not sure' answer made operational: the agent absorbs
    whatever arrives rather than demanding one schema.
    """
    headers, rows = _read_table(path)
    if not headers:
        return "unknown", []
    idx = _map_headers(headers)

    def cell(row, field, default=""):
        i = idx.get(field)
        return row[i] if i is not None and i < len(row) else default

    # monthly totals (phAMACore's Monthly Stock Activity screen)
    if "period" in idx and "qty" in idx and "sold_at" not in idx:
        out = []
        for row in rows:
            period = _parse_dt(cell(row, "period"))
            if not period:
                continue
            out.append({
                "kind": "history_monthly",
                "legacy_code": str(cell(row, "legacy_code")).strip() or None,
                "description": str(cell(row, "description")).strip() or None,
                "period": period[:10],
                "value": _money(cell(row, "line_total")),
                **_qty_fields(cell(row, "qty")),
            })
        return "history_monthly", out

    # transaction-level sales lines — the good case
    if "sold_at" in idx and "qty" in idx:
        out = []
        for row in rows:
            sold = _parse_dt(cell(row, "sold_at"))
            if not sold:
                continue
            code = str(cell(row, "legacy_code")).strip()
            desc = str(cell(row, "description")).strip()
            if not code and not desc:
                continue
            ext = str(cell(row, "external_id")).strip()
            if not ext:
                # stable synthetic id so a re-exported file does not double-count
                ext = "h" + hashlib.sha256(
                    f"{sold}|{code}|{desc}|{cell(row,'qty')}".encode()).hexdigest()[:20]
            out.append({
                "kind": "sale",
                "external_id": ext,
                "sold_at": sold,
                "legacy_code": code or None,
                "description": desc or None,
                "unit_price": _money(cell(row, "unit_price")),
                "line_total": _money(cell(row, "line_total")),
                "payment_method": str(cell(row, "payment")).strip() or None,
                **_qty_fields(cell(row, "qty")),
            })
        return "sale", out

    # stock snapshot — no demand signal, but useful for reconciliation
    if "legacy_code" in idx or "description" in idx:
        out = []
        for row in rows:
            desc = str(cell(row, "description")).strip()
            if not desc:
                continue
            out.append({
                "kind": "snapshot",
                "legacy_code": str(cell(row, "legacy_code")).strip() or None,
                "description": desc,
                **_qty_fields(cell(row, "qty")),
            })
        return "snapshot", out

    return "unknown", []


def scan_folder(watch_dir: Path, store: LocalStore, cloud: Cloud) -> dict:
    """Sweep, not just FileSystemWatcher — network shares miss events."""
    processed = watch_dir / "processed" / datetime.now().strftime("%Y-%m")
    rejected = watch_dir / "rejected"
    processed.mkdir(parents=True, exist_ok=True)
    rejected.mkdir(parents=True, exist_ok=True)

    counts = {"files": 0, "sales": 0, "history": 0, "snapshot": 0, "rejected": 0}
    for f in sorted(watch_dir.glob("*")):
        if f.is_dir() or f.suffix.lower() not in (".csv", ".xlsx", ".xls", ".txt"):
            continue
        # debounce: skip half-written files
        if time.time() - f.stat().st_mtime < 5:
            continue

        fingerprint = hashlib.sha256(f.read_bytes()).hexdigest()
        if store.already_seen(fingerprint):
            log.info("already ingested %s, moving on", f.name)
            f.rename(processed / f.name)
            continue

        try:
            kind, rows = classify_and_parse(f)
        except Exception as e:
            log.error("parse failed %s: %s", f.name, e)
            (rejected / f"{f.name}.reason.txt").write_text(str(e))
            f.rename(rejected / f.name)
            counts["rejected"] += 1
            continue

        if not rows:
            (rejected / f"{f.name}.reason.txt").write_text(
                "No recognisable rows. Detected headers did not match any known "
                "phAMACore export shape.")
            f.rename(rejected / f.name)
            counts["rejected"] += 1
            continue

        endpoint = {"sale": "/agent/pos-sales",
                    "history_monthly": "/agent/history",
                    "snapshot": "/agent/snapshot"}.get(kind)
        if not endpoint:
            f.rename(rejected / f.name)
            counts["rejected"] += 1
            continue

        # chunk so a 50k-row export doesn't become one doomed 40 MB request
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            store.enqueue(endpoint, {
                "batch_id": str(uuid.uuid4()),
                "source_file": f.name,
                "rows": rows[i:i + CHUNK],
            })
        counts["files"] += 1
        counts[{"sale": "sales", "history_monthly": "history",
                "snapshot": "snapshot"}[kind]] += len(rows)
        f.rename(processed / f.name)
        log.info("queued %s rows of %s from %s", len(rows), kind, f.name)

    cloud.flush()
    return counts


# ============================================================ ingestion: direct DB
def poll_database(cfg: dict, store: LocalStore, cloud: Cloud) -> dict:
    """Read-only poll with a high-water mark. Only runs if the operator has
    filled in [database] in config.ini after the probe told them what to fill in.

    Deliberately requires a hand-written SELECT. Auto-generating SQL against an
    unknown pharmacy schema is how you accidentally read a patient name column.
    """
    conn_str = cfg.get("connection_string")
    query = cfg.get("sales_query")
    if not conn_str or not query:
        return {"skipped": "database not configured"}

    engine = cfg.get("engine", "odbc")
    last = store.get("hwm_sales", "0")

    try:
        if engine == "odbc":
            import pyodbc
            conn = pyodbc.connect(conn_str, readonly=True, timeout=30)
        elif engine == "firebird":
            import fdb
            conn = fdb.connect(dsn=cfg["dsn"], user=cfg.get("user", "SYSDBA"),
                               password=cfg.get("password", "masterkey"))
        else:
            return {"error": f"unsupported engine {engine}"}

        cur = conn.cursor()
        cur.execute(query, (last,))
        cols = [d[0].lower() for d in cur.description]
        rows = []
        max_hwm = last
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            ext = str(d.get("sale_id") or d.get("external_id") or "")
            rows.append({
                "kind": "sale",
                "external_id": ext,
                "sold_at": str(d.get("sale_datetime") or d.get("sold_at")),
                "legacy_code": d.get("item_code") or d.get("legacy_code"),
                "description": d.get("description"),
                "unit_price": _money(d.get("unit_price")),
                "line_total": _money(d.get("line_total")),
                "payment_method": d.get("payment_method"),
                **_qty_fields(d.get("quantity") or d.get("qty")),
            })
            # NOTE: lexicographic compare. Fine for zero-padded/monotonic ids; if
            # phAMACore uses unpadded integers, switch sales_query to order by a
            # timestamp and track that instead.
            if ext > max_hwm:
                max_hwm = ext
        conn.close()
    except Exception as e:
        log.error("db poll failed: %s", e)
        return {"error": str(e)}

    if rows:
        for i in range(0, len(rows), 500):
            store.enqueue("/agent/pos-sales", {
                "batch_id": str(uuid.uuid4()),
                "source_file": "db_poll",
                "rows": rows[i:i + 500],
            })
        store.put("hwm_sales", max_hwm)
        cloud.flush()
    return {"rows": len(rows), "hwm": max_hwm}


# ============================================================ commands
def execute_command(cmd: dict, cfg, store: LocalStore, cloud: Cloud) -> tuple[bool, dict]:
    name = cmd.get("command")
    log.info("executing command %s", name)

    if name == "ping":
        return True, {"pong": True, "machine": platform.node(),
                      "agent_version": AGENT_VERSION}

    if name == "probe":
        found = probe_database(cfg.get("phamacore", "install_dir", fallback=None))
        return True, found

    if name == "export_now":
        # We cannot click phAMACore's UI for them. What we CAN do is sweep the
        # folder immediately and report whether anything new turned up — which is
        # what "resync now" from WhatsApp should actually mean.
        counts = scan_folder(Path(cfg.get("ingest", "watch_dir")), store, cloud)
        return True, counts

    if name in ("resync", "full_backfill"):
        counts = scan_folder(Path(cfg.get("ingest", "watch_dir")), store, cloud)
        db = poll_database(dict(cfg["database"]) if cfg.has_section("database") else {},
                           store, cloud)
        return True, {"folder": counts, "database": db}

    return False, {"error": f"unknown command {name}"}


# ============================================================ main loop
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--probe-only", action="store_true",
                    help="run the discovery probe, print JSON, exit. Run this FIRST "
                         "on the pharmacy PC and paste the output back.")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(args.config)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg.get("agent", "log_file",
                                        fallback="dishii-agent.log"), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    if args.probe_only:
        print(json.dumps(probe_database(
            cfg.get("phamacore", "install_dir", fallback=None)), indent=2))
        return

    data_dir = Path(cfg.get("agent", "data_dir", fallback="."))
    data_dir.mkdir(parents=True, exist_ok=True)
    store = LocalStore(data_dir / "agent.db")
    cloud = Cloud(cfg.get("cloud", "api_url"), store, cfg.get("cloud", "pharmacy_id"))

    while not cloud.token:
        if cloud.enrol(cfg.get("cloud", "enrolment_token")):
            break
        log.error("cannot enrol; check api_url and enrolment_token. Retrying in 60s.")
        time.sleep(60)

    watch_dir = Path(cfg.get("ingest", "watch_dir"))
    watch_dir.mkdir(parents=True, exist_ok=True)

    probe = probe_database(cfg.get("phamacore", "install_dir", fallback=None))
    mode = cfg.get("ingest", "mode", fallback="") or probe["recommended_mode"]
    log.info("agent %s up · mode=%s · engine=%s · watching %s",
             AGENT_VERSION, mode, probe.get("engine"), watch_dir)

    last_ingest = 0.0
    while True:
        try:
            hb = cloud.heartbeat(mode, probe)
            if hb.get("suspended"):
                log.warning("suspended by server; sleeping")
                time.sleep(300)
                continue

            for cmd in cloud.poll_commands():
                try:
                    ok, result = execute_command(cmd, cfg, store, cloud)
                except Exception as e:
                    log.exception("command failed")
                    ok, result = False, {"error": f"{type(e).__name__}: {e}"}
                cloud.command_result(cmd["id"], ok, result)

            if time.time() - last_ingest > INGEST_SECONDS:
                scan_folder(watch_dir, store, cloud)
                if mode == "db_poll" and cfg.has_section("database"):
                    poll_database(dict(cfg["database"]), store, cloud)
                last_ingest = time.time()

            cloud.flush()
        except KeyboardInterrupt:
            log.info("shutting down")
            return
        except Exception:
            log.exception("main loop error; continuing")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
