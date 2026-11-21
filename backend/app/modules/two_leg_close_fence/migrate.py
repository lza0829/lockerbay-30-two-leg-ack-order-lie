"""老库迁移：把“单腿 pickup 即 released”的历史占用改回开放，交还双腿栅栏。

迁移只做一次（settings 里留 legacy_reconcile_done），步骤：
1. 摘掉旧触发器 → 2. 建新表/补列 → 3. 重开可安全重开的历史释放 → 4. 装新触发器。

可安全重开的判定：该占用仍是本格口最新一条占用，且格口当前空闲（没有后继占用
歧义）。不满足的留在 released，并写搁置记录 `two_leg:legacy_released_unsafe`
交人工复核——宁可留一条可疑的旧账，也不凭空改动有后继占用的历史。
"""

import json
from datetime import datetime, timezone

from app.modules import two_leg_ack_order as leg_ack

from . import schema


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quarantine(
    conn, occ, now, reason, event_id=None, candidate_id=None, extra=None
) -> None:
    member = {
        "occupancy_id": occ["id"],
        "cell_id": occ["cell_id"],
        "tracking_no": occ["tracking_no"],
        "released_at": occ["closed_at"],
        "source": "legacy_reconcile",
    }
    member.update(extra or {})
    conn.execute(
        """INSERT INTO quarantine(reason, member_json, created_at, status, event_id,
                                  occupancy_id, candidate_id)
           VALUES (?,?,?,'open',?,?,?)""",
        (
            reason,
            json.dumps(member, ensure_ascii=False),
            now,
            event_id,
            occ["id"],
            candidate_id,
        ),
    )


def _try_reopen(conn, occ, now) -> bool:
    """重开一条历史释放，返回是否重开成功。"""
    newest = conn.execute(
        "SELECT id FROM occupancies WHERE cell_id=? ORDER BY opened_at DESC, id DESC LIMIT 1",
        (occ["cell_id"],),
    ).fetchone()
    cell = conn.execute("SELECT status FROM cells WHERE id=?", (occ["cell_id"],)).fetchone()
    if not (newest and newest["id"] == occ["id"] and cell and cell["status"] == "free"):
        _quarantine(conn, occ, now, "two_leg:legacy_released_unsafe")
        return False

    conn.execute(
        "UPDATE occupancies SET status='occupied', closed_at=NULL WHERE id=?",
        (occ["id"],),
    )
    conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (occ["cell_id"],))

    # 历史释放源自某条 pickup：把它放回候选区等 confirm，而不是直接认账。
    pickup = conn.execute(
        """SELECT * FROM parcel_events
           WHERE cell_id=? AND tracking_no=? AND event_type='pickup'
           ORDER BY event_ts DESC LIMIT 1""",
        (occ["cell_id"], occ["tracking_no"]),
    ).fetchone()
    if not pickup:
        _quarantine(conn, occ, now, "two_leg:legacy_released_unsafe")
        return True
    existing = conn.execute(
        "SELECT id FROM close_candidates WHERE event_id=?", (pickup["id"],)
    ).fetchone()
    if existing:
        candidate_id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO close_candidates(occupancy_id, cell_id, tracking_no, event_id,
                                            event_type, event_ts, status, created_at)
               VALUES (?,?,?,?,?,?,'pending',?)""",
            (
                occ["id"],
                occ["cell_id"],
                occ["tracking_no"],
                pickup["id"],
                "pickup",
                pickup["event_ts"],
                now,
            ),
        )
        candidate_id = cur.lastrowid
    _quarantine(
        conn,
        occ,
        now,
        "two_leg:await_confirm",
        event_id=pickup["id"],
        candidate_id=candidate_id,
        extra={"legacy_pickup_id": pickup["id"]},
    )
    return True


def reconcile_legacy(conn) -> int:
    """重开可安全重开的历史单腿释放，返回重开条数（触发器需已摘下）。"""
    if not schema.has_close_legs(conn):
        return 0
    rows = conn.execute(
        """SELECT o.* FROM occupancies o
           LEFT JOIN close_legs cl ON cl.occupancy_id = o.id
           WHERE o.status='released' AND cl.id IS NULL"""
    ).fetchall()
    now = _now()
    return sum(1 for occ in rows if _try_reopen(conn, occ, now))


def migrate(conn) -> dict:
    """幂等迁移：返回重开条数与是否首次执行。

    腿序 ack 与双腿栅栏同生共死：建表、老边回填（按事件时钟补 ack）、触发器
    安装都随本次迁移一起完成。
    """
    done = conn.execute(
        "SELECT value FROM settings WHERE key='legacy_reconcile_done'"
    ).fetchone()
    schema.ensure_objects(conn)
    leg_ack.ensure_objects(conn)
    if done:
        # 老库可能已有 close_legs 边但还没有腿序 ack：按事件时钟补齐。
        leg_ack.backfill(conn)
        # 触发器定义随时可随代码更新，重装是幂等的。
        schema.install_triggers(conn)
        leg_ack.install_triggers(conn)
        return {"reconciled": 0, "first_run": False}
    schema.drop_triggers(conn)
    leg_ack.drop_triggers(conn)
    reopened = reconcile_legacy(conn)
    leg_ack.backfill(conn)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('legacy_reconcile_done', ?)",
        (_now(),),
    )
    schema.install_triggers(conn)
    leg_ack.install_triggers(conn)
    return {"reconciled": reopened, "first_run": True}


def apply_schema(conn) -> int:
    """建立/升级双腿栅栏（幂等），返回本次重开的历史释放条数。"""
    return migrate(conn)["reconciled"]
