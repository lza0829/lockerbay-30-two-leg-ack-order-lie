"""双腿关闭栅栏的库表与不可绕过约束。

落库形态选定：**close_legs 边表**（occupancy_id + pickup_id + confirm_id），
不采用“占用关闭行上追加双事件 id 列”的写法。理由：

* 边表天然 immutable，一条边即一次双腿关闭的完整证据；
* pickup/confirm 事件 id 各自 UNIQUE，杜绝同一事件被两条关闭边复用；
* 占用释放前必须存在与占用同 tracking_no、同 cell_id 的边，可由触发器兜底，
  任何旁路代码都无法把占用改成 released。

失败闭约束（数据库层，非仅服务层）：
1. occupancies 插入/更新为 released 时，必须已存在匹配的 close_legs 边；
2. close_legs 插入时，pickup/confirm 必须真实存在、类型正确、运单与格口一致；
3. close_legs 不允许 UPDATE / DELETE；released 为终态，不可回滚或改写关闭时刻。

建表顺序：ensure_objects → （迁移/遗留修复）→ install_triggers。遗留修复需要临时
回滚已释放占用，触发器必须在修复完成后才安装。
"""


TABLES = """
CREATE TABLE IF NOT EXISTS close_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occupancy_id TEXT NOT NULL UNIQUE REFERENCES occupancies(id),
  cell_id TEXT NOT NULL,
  tracking_no TEXT NOT NULL,
  pickup_id TEXT NOT NULL UNIQUE REFERENCES parcel_events(id),
  confirm_id TEXT NOT NULL UNIQUE REFERENCES parcel_events(id),
  pickup_ts TEXT NOT NULL,
  confirm_ts TEXT NOT NULL,
  closed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (pickup_id <> confirm_id)
);

CREATE INDEX IF NOT EXISTS ix_close_legs_cell ON close_legs(cell_id, tracking_no);

CREATE TABLE IF NOT EXISTS close_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occupancy_id TEXT NOT NULL REFERENCES occupancies(id),
  cell_id TEXT NOT NULL,
  tracking_no TEXT NOT NULL,
  event_id TEXT NOT NULL UNIQUE REFERENCES parcel_events(id),
  event_type TEXT NOT NULL CHECK (event_type IN ('pickup', 'confirm')),
  event_ts TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'closed', 'expired', 'rejected')),
  reason TEXT,
  close_leg_id INTEGER REFERENCES close_legs(id),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_close_candidates_group
  ON close_candidates(occupancy_id, status);
"""

# 老库的 quarantine 只有四列；补列保证搁置记录能关联候选与事件。
QUARANTINE_COLUMNS = (
    ("status", "TEXT NOT NULL DEFAULT 'open'"),
    ("event_id", "TEXT"),
    ("occupancy_id", "TEXT"),
    ("candidate_id", "INTEGER"),
    ("resolved_at", "TEXT"),
)

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_quarantine_open_event
  ON quarantine(event_id) WHERE status = 'open' AND event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_quarantine_status ON quarantine(status, id);
"""

TRIGGER_NAMES = (
    "trg_occupancies_release_requires_two_legs",
    "trg_occupancies_insert_not_released",
    "trg_occupancies_released_immutable",
    "trg_close_legs_validate",
    "trg_close_legs_no_update",
    "trg_close_legs_no_delete",
)

TRIGGERS = """
-- 1) 释放占用必须已经落双腿边。
CREATE TRIGGER IF NOT EXISTS trg_occupancies_release_requires_two_legs
BEFORE UPDATE OF status ON occupancies
FOR EACH ROW
WHEN NEW.status = 'released' AND OLD.status <> 'released'
  AND NOT EXISTS (
    SELECT 1 FROM close_legs cl
    WHERE cl.occupancy_id = NEW.id
      AND cl.cell_id = NEW.cell_id
      AND cl.tracking_no = NEW.tracking_no
  )
BEGIN
  SELECT RAISE(ABORT, 'two_leg_close_fence: occupied -> released requires paired close_legs');
END;

CREATE TRIGGER IF NOT EXISTS trg_occupancies_insert_not_released
BEFORE INSERT ON occupancies
FOR EACH ROW
WHEN NEW.status = 'released'
BEGIN
  SELECT RAISE(ABORT, 'two_leg_close_fence: cannot insert a released occupancy');
END;

-- 1b) released 是终态：不可回滚成 occupied，也不可改写关闭时刻。
CREATE TRIGGER IF NOT EXISTS trg_occupancies_released_immutable
BEFORE UPDATE ON occupancies
FOR EACH ROW
WHEN OLD.status = 'released'
  AND (NEW.status <> 'released' OR NEW.closed_at IS NOT OLD.closed_at)
BEGIN
  SELECT RAISE(ABORT, 'two_leg_close_fence: released occupancy is immutable');
END;

-- 2) 边只接受同占用、同运单、同格口的 pickup + confirm。
CREATE TRIGGER IF NOT EXISTS trg_close_legs_validate
BEFORE INSERT ON close_legs
FOR EACH ROW
WHEN NOT (
  NEW.pickup_id <> NEW.confirm_id
  AND EXISTS (
    SELECT 1 FROM parcel_events p
    WHERE p.id = NEW.pickup_id
      AND p.event_type = 'pickup'
      AND p.cell_id = NEW.cell_id
      AND p.tracking_no = NEW.tracking_no
  )
  AND EXISTS (
    SELECT 1 FROM parcel_events c
    WHERE c.id = NEW.confirm_id
      AND c.event_type = 'confirm'
      AND c.cell_id = NEW.cell_id
      AND c.tracking_no = NEW.tracking_no
  )
  AND EXISTS (
    SELECT 1 FROM occupancies o
    WHERE o.id = NEW.occupancy_id
      AND o.cell_id = NEW.cell_id
      AND o.tracking_no = NEW.tracking_no
      AND o.status = 'occupied'
  )
)
BEGIN
  SELECT RAISE(ABORT, 'two_leg_close_fence: close_legs requires matching pickup and confirm legs');
END;

-- 3) 边表不可变：已关闭的双腿证据不可改写或删除。
CREATE TRIGGER IF NOT EXISTS trg_close_legs_no_update
BEFORE UPDATE ON close_legs
BEGIN
  SELECT RAISE(ABORT, 'two_leg_close_fence: close_legs is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_close_legs_no_delete
BEFORE DELETE ON close_legs
BEGIN
  SELECT RAISE(ABORT, 'two_leg_close_fence: close_legs is immutable');
END;
"""


def _ensure_quarantine_columns(conn) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(quarantine)").fetchall()}
    for name, decl in QUARANTINE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE quarantine ADD COLUMN {name} {decl}")


def ensure_objects(conn) -> None:
    """建边表、候选区、索引与 quarantine 补列；不动触发器。"""
    conn.executescript(TABLES)
    _ensure_quarantine_columns(conn)
    conn.executescript(INDEXES)


def drop_triggers(conn) -> None:
    for name in TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def install_triggers(conn) -> None:
    """重装全部栅栏触发器（先删后建，保证定义随代码更新）。"""
    drop_triggers(conn)
    conn.executescript(TRIGGERS)


def has_close_legs(conn) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='close_legs'"
        ).fetchone()
        is not None
    )
