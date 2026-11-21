"""双腿关闭的腿序 ack：关账成功必须落下与事件时钟一致的 first/second 回执。

修「双腿已关、腿确认序与事件时钟反序」：旧实现把 occupancy.last_event_id（最后
*到达*的腿）谎报为 first、second 留空却 legs_ok=True，占用账无法按事件时间重放。

落库形态（两种形态择一，已定）：**独立 two_leg_ack_order 表**，不采用“占用行追加
不可变 leg_ack_order 快照列”。理由：

* 与 close_legs 边表同构：一条 ack 即一次关账的腿序证据，不可改删；
* occupancy_id / close_leg_id 均 UNIQUE，与关闭边 1:1，杜绝半截 ack；
* 不改动占用行结构，老库迁移只需建表 + 回填（backfill）。

提交形态（两种形态择一，已定）：**关账同事务按 occurred_at 排序写 ack，缺一整笔
回滚**。不采用“先写 ack 草稿再与双事件时钟对撞、后原子关账”的两段式。
two_leg_close_fence._close_with_legs 在同一事务里先落 close_legs 边、再落本表
ack、最后才把占用置 released；任一步失败异常上抛，ingest / seed 外层整笔
rollback，不会留下“占用已关而 ack 半截”。

硬约束（数据库触发器兜底，旁路代码绕不过）：
1. 占用转 released 前必须已存在本表 ack（禁止关账成功而腿序 ack 缺）；
2. ack 的 first/second 必须就是 close_legs 的双腿事件，first_ts/second_ts 必须
   等于事件真实 occurred_at，且 (first_ts, first_event_id) 升序不晚于
   (second_ts, second_event_id)——人为对调 first/second 会被数据库拒绝；
3. second_event_id 非空（NOT NULL + 触发器），禁止 second 空仍 legs_ok；
4. 本表不可 UPDATE / DELETE。
"""

from datetime import datetime, timezone

__all__ = [
    "STATUS",
    "ensure_objects",
    "drop_triggers",
    "install_triggers",
    "has_table",
    "record_ack",
    "backfill",
    "ack_of",
    "list_acks",
    "verify",
    "audit",
    "overview",
]

STATUS = "ok"

TABLES = """
CREATE TABLE IF NOT EXISTS two_leg_ack_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occupancy_id TEXT NOT NULL UNIQUE REFERENCES occupancies(id),
  close_leg_id INTEGER NOT NULL UNIQUE REFERENCES close_legs(id),
  first_event_id TEXT NOT NULL REFERENCES parcel_events(id),
  second_event_id TEXT NOT NULL REFERENCES parcel_events(id),
  first_ts TEXT NOT NULL,
  second_ts TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (first_event_id <> second_event_id),
  CHECK (first_ts <= second_ts)
);

CREATE INDEX IF NOT EXISTS ix_two_leg_ack_order_events
  ON two_leg_ack_order(first_event_id, second_event_id);
"""

TRIGGER_NAMES = (
    "trg_occupancies_release_requires_leg_ack",
    "trg_two_leg_ack_order_validate",
    "trg_two_leg_ack_order_no_update",
    "trg_two_leg_ack_order_no_delete",
)

TRIGGERS = """
-- 1) 释放占用必须已经落腿序 ack（禁止关账成功而 ack 缺）。
CREATE TRIGGER IF NOT EXISTS trg_occupancies_release_requires_leg_ack
BEFORE UPDATE OF status ON occupancies
FOR EACH ROW
WHEN NEW.status = 'released' AND OLD.status <> 'released'
  AND NOT EXISTS (
    SELECT 1 FROM two_leg_ack_order a WHERE a.occupancy_id = NEW.id
  )
BEGIN
  SELECT RAISE(ABORT, 'two_leg_ack_order: occupied -> released requires leg-order ack');
END;

-- 2) ack 必须与关闭边同源、与事件时钟升序一致：人为对调 first/second 即失败。
CREATE TRIGGER IF NOT EXISTS trg_two_leg_ack_order_validate
BEFORE INSERT ON two_leg_ack_order
FOR EACH ROW
WHEN NOT (
  NEW.first_event_id <> NEW.second_event_id
  AND (
    NEW.first_ts < NEW.second_ts
    OR (NEW.first_ts = NEW.second_ts AND NEW.first_event_id < NEW.second_event_id)
  )
  AND EXISTS (
    SELECT 1 FROM close_legs cl
    WHERE cl.id = NEW.close_leg_id
      AND cl.occupancy_id = NEW.occupancy_id
      AND (
        (cl.pickup_id = NEW.first_event_id AND cl.confirm_id = NEW.second_event_id)
        OR (cl.pickup_id = NEW.second_event_id AND cl.confirm_id = NEW.first_event_id)
      )
  )
  AND EXISTS (
    SELECT 1 FROM parcel_events f
    WHERE f.id = NEW.first_event_id AND f.event_ts = NEW.first_ts
  )
  AND EXISTS (
    SELECT 1 FROM parcel_events s
    WHERE s.id = NEW.second_event_id AND s.event_ts = NEW.second_ts
  )
)
BEGIN
  SELECT RAISE(ABORT, 'two_leg_ack_order: ack must match close legs and event clock ascending');
END;

-- 3) ack 不可变：已落腿的腿序证据不可改写或删除。
CREATE TRIGGER IF NOT EXISTS trg_two_leg_ack_order_no_update
BEFORE UPDATE ON two_leg_ack_order
BEGIN
  SELECT RAISE(ABORT, 'two_leg_ack_order: ack is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_two_leg_ack_order_no_delete
BEFORE DELETE ON two_leg_ack_order
BEGIN
  SELECT RAISE(ABORT, 'two_leg_ack_order: ack is immutable');
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock_key(ev) -> tuple:
    """事件时钟：occurred_at（event_ts）升序，并列按事件 id 升序，确定性可重放。"""
    return (ev["event_ts"], ev["id"])


# ----------------------------------------------------------------  schema

def ensure_objects(conn) -> None:
    """建腿序 ack 表与索引（幂等）；不动触发器。"""
    conn.executescript(TABLES)


def drop_triggers(conn) -> None:
    for name in TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def install_triggers(conn) -> None:
    """重装全部腿序 ack 触发器（先删后建，保证定义随代码更新）。"""
    drop_triggers(conn)
    conn.executescript(TRIGGERS)


def has_table(conn) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='two_leg_ack_order'"
        ).fetchone()
        is not None
    )


# ----------------------------------------------------------------  写入（关账同事务）

def record_ack(conn, occupancy_id, close_leg_id, pickup, confirm, now=None) -> dict:
    """按事件时钟排序落腿序 ack；须在关账同一事务内调用，失败异常上抛整笔回滚。

    first 是 occurred_at 更早的那条腿事件（并列按事件 id 升序），与到达顺序无关。
    """
    now = now or _now()
    if pickup is None or confirm is None:
        raise RuntimeError("two_leg_ack_order: both leg events are required")
    first, second = sorted((pickup, confirm), key=_clock_key)
    if first["id"] == second["id"]:
        raise RuntimeError("two_leg_ack_order: duplicate leg event")
    cur = conn.execute(
        """INSERT INTO two_leg_ack_order(occupancy_id, close_leg_id,
                                          first_event_id, second_event_id,
                                          first_ts, second_ts, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            occupancy_id,
            close_leg_id,
            first["id"],
            second["id"],
            first["event_ts"],
            second["event_ts"],
            now,
        ),
    )
    return {
        "ack_id": cur.lastrowid,
        "occupancy_id": occupancy_id,
        "close_leg_id": close_leg_id,
        "first_event_id": first["id"],
        "second_event_id": second["id"],
        "first_ts": first["event_ts"],
        "second_ts": second["event_ts"],
    }


def backfill(conn, now=None) -> int:
    """老库回填：已有 close_legs 边但缺腿序 ack 的，按事件时钟补落。返回补落条数。"""
    if not has_table(conn):
        return 0
    rows = conn.execute(
        """SELECT cl.id AS close_leg_id, cl.occupancy_id, cl.pickup_id, cl.confirm_id
           FROM close_legs cl
           LEFT JOIN two_leg_ack_order a ON a.occupancy_id = cl.occupancy_id
           WHERE a.id IS NULL
           ORDER BY cl.id"""
    ).fetchall()
    filled = 0
    for row in rows:
        pickup = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (row["pickup_id"],)
        ).fetchone()
        confirm = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (row["confirm_id"],)
        ).fetchone()
        if pickup is None or confirm is None:
            # 事件缺失的边无法重放时钟：留给 audit 曝光，不凭空造 ack。
            continue
        record_ack(conn, row["occupancy_id"], row["close_leg_id"], pickup, confirm, now)
        filled += 1
    return filled


# ----------------------------------------------------------------  只读核证

def ack_of(conn, occupancy_id) -> dict | None:
    """单占用腿序 ack 回执；legs_ok 用 parcel 事件时钟重算，不接受库存谎报。"""
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if not occ:
        return None
    ack = conn.execute(
        "SELECT * FROM two_leg_ack_order WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    if not ack:
        return {
            "occupancy_id": occupancy_id,
            "status": occ["status"],
            "first_event_id": None,
            "second_event_id": None,
            "first_ts": None,
            "second_ts": None,
            "legs_ok": False,
            "detail": "腿序 ack 缺失",
        }
    first = conn.execute(
        "SELECT * FROM parcel_events WHERE id=?", (ack["first_event_id"],)
    ).fetchone()
    second = conn.execute(
        "SELECT * FROM parcel_events WHERE id=?", (ack["second_event_id"],)
    ).fetchone()
    legs_ok = bool(
        ack["first_event_id"]
        and ack["second_event_id"]
        and first is not None
        and second is not None
        and ack["first_ts"] == first["event_ts"]
        and ack["second_ts"] == second["event_ts"]
        and _clock_key(first) <= _clock_key(second)
    )
    return {
        "occupancy_id": occupancy_id,
        "status": occ["status"],
        "ack_id": ack["id"],
        "close_leg_id": ack["close_leg_id"],
        "first_event_id": ack["first_event_id"],
        "second_event_id": ack["second_event_id"],
        "first_ts": ack["first_ts"],
        "second_ts": ack["second_ts"],
        "legs_ok": legs_ok,
        "detail": None if legs_ok else "腿序与事件时钟不一致",
    }


def list_acks(conn, limit=200):
    """腿序 ack 列表（核账页腿序区）：关联关闭边与占用状态。"""
    rows = conn.execute(
        """SELECT a.*, cl.cell_id, cl.tracking_no, cl.pickup_id, cl.confirm_id,
                  o.status AS occupancy_status
           FROM two_leg_ack_order a
           JOIN close_legs cl ON cl.id = a.close_leg_id
           LEFT JOIN occupancies o ON o.id = a.occupancy_id
           ORDER BY a.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def verify(conn, occupancy_id):
    """单个占用的腿序 ack 核证（只读）：逐项检查并给出结论。"""
    occ = conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
    if not occ:
        return None
    ack = conn.execute(
        "SELECT * FROM two_leg_ack_order WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    leg = conn.execute(
        "SELECT * FROM close_legs WHERE occupancy_id=?", (occupancy_id,)
    ).fetchone()
    checks = [
        {
            "name": "腿序 ack 存在",
            "ok": ack is not None,
            "detail": None if ack else "关账缺少腿序 ack（禁止关账成功而 ack 缺）",
        }
    ]
    first = second = None
    if ack:
        first = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (ack["first_event_id"],)
        ).fetchone()
        second = conn.execute(
            "SELECT * FROM parcel_events WHERE id=?", (ack["second_event_id"],)
        ).fetchone()
        pair_ok = bool(leg) and {ack["first_event_id"], ack["second_event_id"]} == {
            leg["pickup_id"],
            leg["confirm_id"],
        }
        ts_ok = bool(
            first
            and second
            and ack["first_ts"] == first["event_ts"]
            and ack["second_ts"] == second["event_ts"]
        )
        order_ok = bool(first and second) and _clock_key(first) <= _clock_key(second)
        checks.extend(
            [
                {
                    "name": "second 腿非空",
                    "ok": bool(ack["second_event_id"]),
                    "detail": None
                    if ack["second_event_id"]
                    else "second 为空不得判 legs_ok",
                },
                {
                    "name": "ack 双腿与关闭边一致",
                    "ok": pair_ok,
                    "detail": None if pair_ok else "ack 事件不是 close_legs 的双腿",
                },
                {
                    "name": "ack 时刻等于事件 occurred_at",
                    "ok": ts_ok,
                    "detail": None if ts_ok else "ack 时刻与 parcel 事件时钟不符",
                },
                {
                    "name": "first 为 occurred_at 更早腿",
                    "ok": order_ok,
                    "detail": None if order_ok else "腿序与事件时钟升序分叉",
                },
            ]
        )
    checks.append(
        {
            "name": "占用已 released",
            "ok": occ["status"] == "released",
            "detail": None
            if occ["status"] == "released"
            else "占用仍未释放（双腿未齐）",
        }
    )
    return {
        "occupancy_id": occupancy_id,
        "occupancy_status": occ["status"],
        "ack": dict(ack) if ack else None,
        "first_event": dict(first) if first else None,
        "second_event": dict(second) if second else None,
        "checks": checks,
        "verified": all(c["ok"] for c in checks),
    }


def audit(conn):
    """全库腿序一致性审计：released 必有 ack、ack 必按事件时钟升序、second 非空。"""
    if not has_table(conn):
        return [{"kind": "leg_ack_table_missing"}]
    violations = []
    for row in conn.execute(
        """SELECT o.id AS occupancy_id, o.cell_id, o.tracking_no
           FROM occupancies o
           LEFT JOIN two_leg_ack_order a ON a.occupancy_id = o.id
           WHERE o.status='released' AND a.id IS NULL"""
    ).fetchall():
        violations.append({"kind": "released_without_leg_ack", **dict(row)})
    for row in conn.execute(
        """SELECT a.id AS ack_id, a.occupancy_id, o.status
           FROM two_leg_ack_order a
           LEFT JOIN occupancies o ON o.id = a.occupancy_id
           WHERE o.id IS NULL OR o.status <> 'released'"""
    ).fetchall():
        violations.append({"kind": "leg_ack_without_released_occupancy", **dict(row)})
    for row in conn.execute(
        """SELECT id AS ack_id, occupancy_id
           FROM two_leg_ack_order
           WHERE second_event_id IS NULL OR second_event_id=''"""
    ).fetchall():
        violations.append({"kind": "leg_ack_second_missing", **dict(row)})
    for row in conn.execute(
        """SELECT a.id AS ack_id, a.occupancy_id
           FROM two_leg_ack_order a
           JOIN close_legs cl ON cl.id = a.close_leg_id
           WHERE NOT (
             (a.first_event_id = cl.pickup_id AND a.second_event_id = cl.confirm_id)
             OR (a.first_event_id = cl.confirm_id AND a.second_event_id = cl.pickup_id)
           )"""
    ).fetchall():
        violations.append({"kind": "leg_ack_pair_mismatch", **dict(row)})
    for row in conn.execute(
        """SELECT a.id AS ack_id, a.occupancy_id
           FROM two_leg_ack_order a
           LEFT JOIN parcel_events f ON f.id = a.first_event_id
           LEFT JOIN parcel_events s ON s.id = a.second_event_id
           WHERE f.id IS NULL OR s.id IS NULL
              OR a.first_ts <> f.event_ts OR a.second_ts <> s.event_ts"""
    ).fetchall():
        violations.append({"kind": "leg_ack_event_clock_mismatch", **dict(row)})
    for row in conn.execute(
        """SELECT a.id AS ack_id, a.occupancy_id, a.first_event_id, a.second_event_id
           FROM two_leg_ack_order a
           JOIN parcel_events f ON f.id = a.first_event_id
           JOIN parcel_events s ON s.id = a.second_event_id
           WHERE f.event_ts > s.event_ts
              OR (f.event_ts = s.event_ts AND f.id > s.id)"""
    ).fetchall():
        violations.append({"kind": "leg_ack_order_inverted", **dict(row)})
    return violations


def overview(conn):
    return {
        "leg_acks": conn.execute(
            "SELECT COUNT(*) AS c FROM two_leg_ack_order"
        ).fetchone()["c"]
        if has_table(conn)
        else 0,
        "violations": len(audit(conn)),
    }
