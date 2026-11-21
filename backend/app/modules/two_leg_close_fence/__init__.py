"""关闭占用双腿事件栅栏：single-leg pickup 绝不释放，必须 pickup + confirm 双腿。

落库形态：**close_legs 边表**（occupancy_id + pickup_id + confirm_id），
不使用“占用关闭行追加双事件 id 列”；理由与不可绕过约束见 schema.py。

规则（失败闭）：
* 占用转 released 之前，必须存在 close_legs 边：同一占用、同一格口、同一运单的
  pickup 与 confirm 两条事件缺一不可；单腿绝不释放。
* 事件先到候选区（close_candidates）搁置，另一腿到达后在同一事务内原子关账；
  超时未配对的整组保持搁置（expired + quarantine 仍为 open），不得借超时放行。
* confirm 只能与同运单同格口的占用配对，别的运单的 confirm 不构成腿。
* close_legs 为不可变边表，数据库触发器保证 released 占用必有边、边不可改删。
"""

import json
from datetime import datetime, timedelta, timezone

from app.modules import two_leg_ack_order as leg_ack

from .migrate import apply_schema

__all__ = [
    "apply_schema",
    "LEG_TYPES",
    "record_leg",
    "sweep",
    "verify",
    "audit",
    "overview",
    "list_legs",
    "list_candidates",
    "list_held_quarantine",
]

LEG_TYPES = ("pickup", "confirm")
DEFAULT_PAIR_TIMEOUT_MINUTES = 30


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _member(ev, **extra) -> str:
    body = {
        "event_id": ev["id"],
        "event_type": ev["event_type"],
        "event_ts": ev["event_ts"],
        "cell_id": ev.get("cell_id"),
        "tracking_no": ev.get("tracking_no"),
    }
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def _quarantine(conn, reason, member_json, event_id, occupancy_id, candidate_id, now):
    row = conn.execute(
        "SELECT id FROM quarantine WHERE event_id=? AND status='open'", (event_id,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        """INSERT INTO quarantine(reason, member_json, created_at, status, event_id, occupancy_id, candidate_id)
           VALUES (?,?,?,'open',?,?,?)""",
        (reason, member_json, now, event_id, occupancy_id, candidate_id),
    )
    return cur.lastrowid


def _append_proof(conn, occ, pickup_id, confirm_id, closed_at, now):
    """关闭核证留痕：开账时创建的 proof 行补上双腿事件与关闭时刻。"""
    row = conn.execute(
        "SELECT * FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
        (occ["id"],),
    ).fetchone()
    ids = []
    if row:
        try:
            ids = json.loads(row["event_ids_json"] or "[]")
        except (TypeError, ValueError):
            ids = []
    for event_id in (pickup_id, confirm_id):
        if event_id not in ids:
            ids.append(event_id)
    payload = json.dumps(ids, ensure_ascii=False)
    if row:
        conn.execute(
            "UPDATE occupancy_proofs SET closed_at=?, event_ids_json=? WHERE id=?",
            (closed_at, payload, row["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO occupancy_proofs(occupancy_id, cell_id, tracking_no, opened_at, closed_at, event_ids_json, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (occ["id"], occ["cell_id"], occ["tracking_no"], occ["opened_at"], closed_at, payload, now),
        )


def _close_with_legs(conn, occ, ev, mate, now):
    """两腿齐全：同一事务里落边、释放占用、腾格、收口候选与搁置。"""
    current_id = ev["id"]
    mate_id = mate["event_id"]
    if ev["event_type"] == "pickup":
        pickup_id, confirm_id = current_id, mate_id
    else:
        pickup_id, confirm_id = mate_id, current_id

    pickup = conn.execute("SELECT * FROM parcel_events WHERE id=?", (pickup_id,)).fetchone()
    confirm = conn.execute("SELECT * FROM parcel_events WHERE id=?", (confirm_id,)).fetchone()
    valid = (
        pickup is not None
        and confirm is not None
        and pickup["event_type"] == "pickup"
        and confirm["event_type"] == "confirm"
        and pickup["cell_id"] == occ["cell_id"] == confirm["cell_id"]
        and pickup["tracking_no"] == occ["tracking_no"] == confirm["tracking_no"]
    )
    if not valid:
        conn.execute(
            "UPDATE close_candidates SET status='rejected', reason='two_leg:invalid_pair', resolved_at=? WHERE id=?",
            (now, mate["id"]),
        )
        qid = _quarantine(
            conn,
            "two_leg:invalid_pair",
            _member(ev, occupancy_id=occ["id"], mate_event_id=mate_id),
            current_id,
            occ["id"],
            None,
            now,
        )
        return {
            "action": "held",
            "reason": "two_leg:invalid_pair",
            "occupancy_id": occ["id"],
            "quarantine_id": qid,
        }

    closed_at = max(pickup["event_ts"], confirm["event_ts"])
    cur = conn.execute(
        """INSERT INTO close_legs(occupancy_id, cell_id, tracking_no, pickup_id, confirm_id,
                                  pickup_ts, confirm_ts, closed_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            occ["id"],
            occ["cell_id"],
            occ["tracking_no"],
            pickup_id,
            confirm_id,
            pickup["event_ts"],
            confirm["event_ts"],
            closed_at,
            now,
        ),
    )
    leg_id = cur.lastrowid

    # 腿序 ack：同一事务内按事件时钟（occurred_at 升序）落 ack；
    # 失败异常上抛，外层整笔回滚，不留半截 ack。
    ack = leg_ack.record_ack(conn, occ["id"], leg_id, pickup, confirm, now)

    updated = conn.execute(
        """UPDATE occupancies SET closed_at=?, status='released', last_event_id=?
           WHERE id=? AND status='occupied'""",
        (closed_at, current_id, occ["id"]),
    )
    if updated.rowcount != 1:
        raise RuntimeError("two_leg_close_fence: occupancy vanished before close")
    conn.execute("UPDATE cells SET status='free' WHERE id=?", (occ["cell_id"],))
    conn.execute(
        "UPDATE close_candidates SET status='closed', close_leg_id=?, resolved_at=? WHERE id=?",
        (leg_id, now, mate["id"]),
    )
    conn.execute(
        "UPDATE quarantine SET status='resolved', resolved_at=? "
        "WHERE status='open' AND event_id IN (?,?)",
        (now, pickup_id, confirm_id),
    )
    _append_proof(conn, occ, pickup_id, confirm_id, closed_at, now)
    return {
        "action": "closed",
        "reason": "two_leg:paired",
        "occupancy_id": occ["id"],
        "cell_id": occ["cell_id"],
        "tracking_no": occ["tracking_no"],
        "pickup_id": pickup_id,
        "confirm_id": confirm_id,
        "close_leg_id": leg_id,
        "leg_ack_id": ack["ack_id"],
        "first_event_id": ack["first_event_id"],
        "second_event_id": ack["second_event_id"],
    }


def record_leg(conn, ev, now=None):
    """记录一条 pickup/confirm 腿；能配对则原子关账，否则进候选区搁置。"""
    now = now or _now()
    etype = ev["event_type"]
    if etype not in LEG_TYPES:
        raise ValueError(f"record_leg only accepts {LEG_TYPES}, got {etype!r}")

    cell_id = ev.get("cell_id")
    tracking_no = ev.get("tracking_no")
    if not cell_id or not tracking_no:
        qid = _quarantine(conn, "two_leg:missing_key", _member(ev), ev["id"], None, None, now)
        return {"action": "held", "reason": "two_leg:missing_key", "quarantine_id": qid}

    occ = conn.execute(
        """SELECT * FROM occupancies
           WHERE cell_id=? AND tracking_no=? AND status='occupied'
           ORDER BY opened_at DESC, id DESC LIMIT 1""",
        (cell_id, tracking_no),
    ).fetchone()

    if not occ:
        occupant = conn.execute(
            "SELECT id, tracking_no FROM occupancies WHERE cell_id=? AND status='occupied' "
            "ORDER BY opened_at DESC, id DESC LIMIT 1",
            (cell_id,),
        ).fetchone()
        if occupant:
            reason = "two_leg:tracking_mismatch"
            extra = {
                "open_occupancy_id": occupant["id"],
                "open_tracking_no": occupant["tracking_no"],
                "attempted_tracking_no": tracking_no,
            }
            occupancy_id = occupant["id"]
        else:
            reason = "two_leg:no_open_occupancy"
            extra = {"attempted_tracking_no": tracking_no}
            occupancy_id = None
        qid = _quarantine(
            conn, reason, _member(ev, **extra), ev["id"], occupancy_id, None, now
        )
        return {
            "action": "held",
            "reason": reason,
            "occupancy_id": occupancy_id,
            "quarantine_id": qid,
        }

    opposite = "confirm" if etype == "pickup" else "pickup"
    expired_group = conn.execute(
        """SELECT id FROM close_candidates
           WHERE occupancy_id=? AND status='expired'
           ORDER BY id DESC LIMIT 1""",
        (occ["id"],),
    ).fetchone()
    if expired_group:
        # 超时过期的整组已交人复核；后续腿一律搁置，不得借此补关账。
        qid = _quarantine(
            conn,
            "two_leg:pair_timeout",
            _member(ev, occupancy_id=occ["id"], expired_candidate_id=expired_group["id"]),
            ev["id"],
            occ["id"],
            None,
            now,
        )
        return {
            "action": "held",
            "reason": "two_leg:pair_timeout",
            "occupancy_id": occ["id"],
            "quarantine_id": qid,
        }

    mate = conn.execute(
        """SELECT * FROM close_candidates
           WHERE occupancy_id=? AND event_type=? AND status='pending'
           ORDER BY id LIMIT 1""",
        (occ["id"], opposite),
    ).fetchone()

    duplicate = conn.execute(
        """SELECT id FROM close_candidates
           WHERE occupancy_id=? AND event_type=? AND status='pending'
           ORDER BY id LIMIT 1""",
        (occ["id"], etype),
    ).fetchone()
    if duplicate:
        # 同占用同类型腿重入：不建第二个候选，直接搁置，避免配对歧义。
        qid = _quarantine(
            conn,
            "two_leg:duplicate_leg",
            _member(
                ev,
                occupancy_id=occ["id"],
                pending_candidate_id=duplicate["id"],
            ),
            ev["id"],
            occ["id"],
            None,
            now,
        )
        return {
            "action": "held",
            "reason": "two_leg:duplicate_leg",
            "occupancy_id": occ["id"],
            "quarantine_id": qid,
        }

    if mate:
        return _close_with_legs(conn, occ, ev, mate, now)

    cur = conn.execute(
        """INSERT INTO close_candidates(occupancy_id, cell_id, tracking_no, event_id, event_type,
                                        event_ts, status, created_at)
           VALUES (?,?,?,?,?,?,'pending',?)""",
        (occ["id"], cell_id, tracking_no, ev["id"], etype, ev["event_ts"], now),
    )
    candidate_id = cur.lastrowid
    reason = f"two_leg:await_{opposite}"
    qid = _quarantine(
        conn,
        reason,
        _member(ev, occupancy_id=occ["id"], candidate_id=candidate_id),
        ev["id"],
        occ["id"],
        candidate_id,
        now,
    )
    return {
        "action": "held",
        "reason": reason,
        "occupancy_id": occ["id"],
        "candidate_id": candidate_id,
        "quarantine_id": qid,
    }


def sweep(conn, timeout_minutes=None, now=None):
    """超时扫描：未配对的整组候选转 expired，搁置仍保持 open（不放行）。"""
    now = now or _now()
    if timeout_minutes is None:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='pair_timeout_minutes'"
        ).fetchone()
        timeout_minutes = int(row["value"]) if row else DEFAULT_PAIR_TIMEOUT_MINUTES
    cutoff = (_parse_ts(now) - timedelta(minutes=int(timeout_minutes))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    groups = conn.execute(
        """SELECT occupancy_id, MIN(created_at) AS first_pending
           FROM close_candidates
           WHERE status='pending'
           GROUP BY occupancy_id
           HAVING first_pending <= ?
           ORDER BY first_pending""",
        (cutoff,),
    ).fetchall()

    expired = []
    for group in groups:
        rows = conn.execute(
            "SELECT * FROM close_candidates WHERE occupancy_id=? AND status='pending'",
            (group["occupancy_id"],),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE close_candidates SET status='expired', reason='two_leg:pair_timeout', resolved_at=? WHERE id=?",
                (now, row["id"]),
            )
            conn.execute(
                "UPDATE quarantine SET reason='two_leg:pair_timeout' "
                "WHERE event_id=? AND status='open'",
                (row["event_id"],),
            )
            expired.append(
                {
                    "candidate_id": row["id"],
                    "occupancy_id": row["occupancy_id"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                }
            )
    return {
        "timeout_minutes": int(timeout_minutes),
        "swept_at": now,
        "expired": len(expired),
        "groups": len(groups),
        "candidates": expired,
    }


# ---------------------------------------------------------------- 只读核证

def list_legs(conn, limit=200):
    rows = conn.execute(
        """SELECT cl.*,
                  p.event_ts AS pickup_event_ts,
                  c.event_ts AS confirm_event_ts,
                  p.tracking_no AS pickup_tracking_no,
                  c.tracking_no AS confirm_tracking_no
           FROM close_legs cl
           JOIN parcel_events p ON p.id = cl.pickup_id
           JOIN parcel_events c ON c.id = cl.confirm_id
           ORDER BY cl.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_candidates(conn, limit=200):
    rows = conn.execute(
        """SELECT cc.*, q.id AS quarantine_id, q.status AS quarantine_status,
                  q.reason AS quarantine_reason
           FROM close_candidates cc
           LEFT JOIN quarantine q ON q.event_id = cc.event_id AND q.status='open'
           ORDER BY cc.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_held_quarantine(conn, limit=200):
    rows = conn.execute(
        """SELECT id, reason, member_json, created_at, status, event_id, occupancy_id,
                  candidate_id, resolved_at
           FROM quarantine
           WHERE reason LIKE 'two_leg:%'
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def verify(conn, occupancy_id):
    """单个占用的双腿关闭核证（只读）。"""
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if not occ:
        return None
    leg = conn.execute(
        "SELECT * FROM close_legs WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    pickup = confirm = None
    if leg:
        pickup = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (leg["pickup_id"],)
        ).fetchone()
        confirm = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (leg["confirm_id"],)
        ).fetchone()
    candidates = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM close_candidates WHERE occupancy_id=? ORDER BY id",
            (occupancy_id,),
        ).fetchall()
    ]
    checks = [
        {
            "name": "双腿边存在",
            "ok": leg is not None,
            "detail": None if leg else "占用关闭缺少 close_legs 边",
        }
    ]
    if leg:
        checks.extend(
            [
                {
                    "name": "占用同格同运单",
                    "ok": leg["cell_id"] == occ["cell_id"]
                    and leg["tracking_no"] == occ["tracking_no"],
                },
                {
                    "name": "pickup 腿类型正确",
                    "ok": bool(pickup and pickup["event_type"] == "pickup"),
                },
                {
                    "name": "confirm 腿类型正确",
                    "ok": bool(confirm and confirm["event_type"] == "confirm"),
                },
                {
                    "name": "两腿同格同运单",
                    "ok": bool(
                        pickup
                        and confirm
                        and pickup["cell_id"] == confirm["cell_id"] == occ["cell_id"]
                        and pickup["tracking_no"] == confirm["tracking_no"] == occ["tracking_no"]
                    ),
                },
            ]
        )
    ack = leg_ack.ack_of(conn, occupancy_id)
    checks.append(
        {
            "name": "腿序 ack 按事件时钟升序",
            "ok": bool(ack and ack["legs_ok"]),
            "detail": None
            if (ack and ack["legs_ok"])
            else "腿序 ack 缺失或 first/second 与事件时钟反序",
        }
    )
    checks.append(
        {
            "name": "占用已 released",
            "ok": occ["status"] == "released",
            "detail": None if occ["status"] == "released" else "占用仍未释放（双腿未齐）",
        }
    )
    return {
        "occupancy": dict(occ),
        "leg": dict(leg) if leg else None,
        "ack": ack,
        "pickup_event": dict(pickup) if pickup else None,
        "confirm_event": dict(confirm) if confirm else None,
        "candidates": candidates,
        "checks": checks,
        "verified": all(c["ok"] for c in checks),
    }


def audit(conn):
    """全库一致性审计：released 必有边、边必须对应 released，且两腿齐备。"""
    violations = []
    for row in conn.execute(
        """SELECT o.id, o.cell_id, o.tracking_no
           FROM occupancies o
           LEFT JOIN close_legs cl ON cl.occupancy_id = o.id
           WHERE o.status='released' AND cl.id IS NULL"""
    ).fetchall():
        violations.append(
            {"kind": "released_without_legs", "occupancy": dict(row)}
        )
    for row in conn.execute(
        """SELECT cl.id AS close_leg_id, cl.occupancy_id, o.status
           FROM close_legs cl
           LEFT JOIN occupancies o ON o.id = cl.occupancy_id
           WHERE o.id IS NULL OR o.status <> 'released'"""
    ).fetchall():
        violations.append({"kind": "leg_without_released_occupancy", **dict(row)})
    for row in conn.execute(
        """SELECT cl.id AS close_leg_id, cl.occupancy_id
           FROM close_legs cl
           LEFT JOIN parcel_events p ON p.id = cl.pickup_id AND p.event_type='pickup'
           LEFT JOIN parcel_events c ON c.id = cl.confirm_id AND c.event_type='confirm'
           WHERE p.id IS NULL OR c.id IS NULL"""
    ).fetchall():
        violations.append({"kind": "leg_event_missing", **dict(row)})
    return violations


def overview(conn):
    one = lambda sql, *args: conn.execute(sql, args).fetchone()["c"]
    return {
        "close_legs": one("SELECT COUNT(*) AS c FROM close_legs"),
        "pending_legs": one(
            "SELECT COUNT(*) AS c FROM close_candidates WHERE status='pending'"
        ),
        "expired_legs": one(
            "SELECT COUNT(*) AS c FROM close_candidates WHERE status='expired'"
        ),
        "held_quarantine": one(
            "SELECT COUNT(*) AS c FROM quarantine WHERE status='open' AND reason LIKE 'two_leg:%'"
        ),
        "released": one("SELECT COUNT(*) AS c FROM occupancies WHERE status='released'"),
        "open_occupancies": one(
            "SELECT COUNT(*) AS c FROM occupancies WHERE status='occupied'"
        ),
        "violations": len(audit(conn)),
    }
