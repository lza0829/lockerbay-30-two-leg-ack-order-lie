import json
from datetime import datetime, timezone
from uuid import uuid4

from app.db import FIXTURES_DIR, connect
from app.services import pipeline


def _load_batch():
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("events", [])


def run_batch():
    events = _load_batch()
    if not events:
        return {"batch_id": None, "accepted": 0, "message": "无夹具"}
    batch_id = f"B-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect()
    accepted = 0
    outcomes = []
    try:
        for ev in events:
            cur = conn.execute(
                "INSERT OR IGNORE INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel) VALUES (?,?,?,?,?,?,?,?)",
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
            if cur.rowcount != 1:
                outcomes.append({"event_id": ev["id"], "action": "duplicate"})
                continue
            accepted += 1
            result = pipeline.apply_event(conn, ev)
            outcomes.append({"event_id": ev["id"], **result})
        conn.execute(
            "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (
                batch_id,
                now,
                json.dumps(
                    {"accepted": accepted, "total": len(events), "outcomes": outcomes},
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
        return {
            "batch_id": batch_id,
            "accepted": accepted,
            "total": len(events),
            "outcomes": outcomes,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "occupancy_id" in out and "occ_id" not in out:
        out["occ_id"] = out.get("occupancy_id")
    if "cell_id" in out and "locker_cell" not in out:
        out["locker_cell"] = out.get("cell_id")
    if "tracking_no" in out and "parcel_key" not in out:
        out["parcel_key"] = out.get("tracking_no")
    if "verified" not in out:
        out["verified"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True}
    if extra:
        data.update(extra)
    return data

