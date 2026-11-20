import json

from app.db import FIXTURES_DIR, connect
from app.modules.two_leg_close_fence import migrate as fence_migrate
from app.services import pipeline

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cabinets (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  site TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
  id TEXT PRIMARY KEY,
  cabinet_id TEXT NOT NULL REFERENCES cabinets(id),
  code TEXT NOT NULL,
  size_code TEXT NOT NULL DEFAULT 'M',
  status TEXT NOT NULL DEFAULT 'free',
  UNIQUE(cabinet_id, code)
);

CREATE TABLE IF NOT EXISTS parcel_events (
  id TEXT PRIMARY KEY,
  cabinet_id TEXT NOT NULL,
  cell_id TEXT,
  event_type TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  tracking_no TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  channel TEXT NOT NULL DEFAULT 'primary'
);

CREATE TABLE IF NOT EXISTS occupancies (
  id TEXT PRIMARY KEY,
  cell_id TEXT NOT NULL REFERENCES cells(id),
  tracking_no TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  status TEXT NOT NULL DEFAULT 'occupied',
  last_event_id TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS occupancy_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occupancy_id TEXT NOT NULL,
  cell_id TEXT NOT NULL,
  tracking_no TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  event_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def overdue_hours(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    return int(row["value"]) if row else 48


def pair_timeout_minutes(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='pair_timeout_minutes'").fetchone()
    return int(row["value"]) if row else 30


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM settings WHERE key='overdue_hours'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('overdue_hours', '48')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='pair_timeout_minutes'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('pair_timeout_minutes', '30')")
    # 双腿栅栏建表 + 老账迁移；必须在写 settings 之后（迁移会读 settings）。
    fence_migrate.migrate(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM cabinets").fetchone()["c"] == 0:
        _seed(conn)
    conn.commit()
    conn.close()


def _seed(conn):
    path = FIXTURES_DIR / "seed_bay.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for c in data.get("cabinets", []):
        conn.execute(
            "INSERT INTO cabinets(id, label, site) VALUES (?,?,?)",
            (c["id"], c["label"], c["site"]),
        )
    for cell in data.get("cells", []):
        conn.execute(
            "INSERT INTO cells(id, cabinet_id, code, size_code, status) VALUES (?,?,?,?,?)",
            (cell["id"], cell["cabinet_id"], cell["code"], cell.get("size_code", "M"), "free"),
        )
    for ev in sorted(data.get("events", []), key=lambda e: (e["event_ts"], e["id"])):
        conn.execute(
            "INSERT INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel) VALUES (?,?,?,?,?,?,?,?)",
            (
                ev["id"],
                ev["cabinet_id"],
                ev.get("cell_id"),
                ev["event_type"],
                ev["event_ts"],
                ev.get("tracking_no"),
                json.dumps(ev.get("payload", {}), ensure_ascii=False),
                "primary",
            ),
        )
        pipeline.apply_event(conn, ev)

