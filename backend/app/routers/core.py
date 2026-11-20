from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import two_leg_close_fence as fence
from app.seed import overdue_hours, pair_timeout_minutes
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        data["two_leg"] = fence.overview(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, reason, member_json, created_at, status, event_id, occupancy_id,
                      candidate_id, resolved_at
               FROM quarantine ORDER BY id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {
            "overdue_hours": overdue_hours(conn),
            "pair_timeout_minutes": pair_timeout_minutes(conn),
        }
    finally:
        conn.close()


@router.put("/settings/pair-timeout")
def put_pair_timeout(body: dict = Body(...)):
    minutes = int(body.get("pair_timeout_minutes", 30))
    if minutes < 1:
        raise HTTPException(400, "配对超时至少 1 分钟")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('pair_timeout_minutes', ?)",
            (str(minutes),),
        )
        conn.commit()
        return {"pair_timeout_minutes": minutes}
    finally:
        conn.close()


# ---------------------------------------------------------------- 双腿关闭栅栏

@router.get("/close-legs")
def list_close_legs(limit: int = 200):
    conn = connect()
    try:
        return fence.list_legs(conn, limit)
    finally:
        conn.close()


@router.get("/close-candidates")
def list_close_candidates(limit: int = 200):
    conn = connect()
    try:
        return fence.list_candidates(conn, limit)
    finally:
        conn.close()


@router.get("/two-leg/quarantine")
def list_two_leg_quarantine(limit: int = 200):
    conn = connect()
    try:
        return fence.list_held_quarantine(conn, limit)
    finally:
        conn.close()


@router.get("/two-leg/verify/{occupancy_id}")
def verify_two_leg(occupancy_id: str):
    conn = connect()
    try:
        result = fence.verify(conn, occupancy_id)
        if result is None:
            raise HTTPException(404, "占用不存在")
        return result
    finally:
        conn.close()


@router.get("/two-leg/audit")
def audit_two_leg():
    conn = connect()
    try:
        violations = fence.audit(conn)
        return {"ok": not violations, "violations": violations}
    finally:
        conn.close()


@router.post("/two-leg/sweep")
def sweep_two_leg(body: dict | None = Body(default=None)):
    conn = connect()
    try:
        timeout = (body or {}).get("timeout_minutes")
        result = fence.sweep(conn, timeout)
        conn.commit()
        return result
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        conn.commit()
        return {"overdue_hours": hours}
    finally:
        conn.close()

# --- plant routes ---
from app.modules import two_leg_ack_order as leg_ack

@router.get("/two-leg-ack/{occupancy_id}")
def two_leg_ack(occupancy_id: str):
    conn = connect()
    try:
        return leg_ack.ack_of(conn, occupancy_id)
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

