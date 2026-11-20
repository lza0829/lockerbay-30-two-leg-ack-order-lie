"""事件 → 占用物化的唯一入口。

投柜与双腿关闭都必须经过这里，避免 seed / ingest 各自复制一套释放逻辑。
"""

import json
from datetime import datetime, timezone

from app.modules import two_leg_close_fence as fence


def apply_event(conn, ev) -> dict:
    """应用一条已落库的 parcel_event，返回处理结果。"""
    etype = ev["event_type"]
    if etype == "drop" and ev.get("cell_id"):
        return _apply_drop(conn, ev)
    if etype in fence.LEG_TYPES:
        return fence.record_leg(conn, ev)
    return {"action": "ignored", "reason": f"unsupported_type:{etype}"}


def _apply_drop(conn, ev) -> dict:
    cell = conn.execute("SELECT status FROM cells WHERE id=?", (ev["cell_id"],)).fetchone()
    if not cell or cell["status"] != "free":
        return {"action": "ignored", "reason": "cell_not_free"}
    oid = f"OCC-{ev['cell_id']}-{ev['id']}"
    conn.execute(
        "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (oid, ev["cell_id"], ev["tracking_no"], ev["event_ts"], None, "occupied", ev["id"]),
    )
    conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (ev["cell_id"],))
    conn.execute(
        """INSERT INTO occupancy_proofs(occupancy_id, cell_id, tracking_no, opened_at,
                                        closed_at, event_ids_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (oid, ev["cell_id"], ev["tracking_no"], ev["event_ts"], None,
         json.dumps([ev["id"]], ensure_ascii=False), _now()),
    )
    return {"action": "opened", "occupancy_id": oid}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
