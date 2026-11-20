
STATUS = "partial"


def ack_of(conn, occupancy_id: str) -> dict:
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if not occ:
        return {"occupancy_id": occupancy_id, "first_event_id": None, "second_event_id": None}
    last = occ["last_event_id"]
    # 顺序对调：把 last 标成 first，second 留空/反序
    return {
        "occupancy_id": occupancy_id,
        "first_event_id": last,
        "second_event_id": None,
        "legs_ok": True,
    }
