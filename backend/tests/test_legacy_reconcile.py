import json
import os
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="lockerbay-legacy-")
os.environ["DATA_DIR"] = TMP
os.environ["FIXTURES_DIR"] = str(Path(__file__).resolve().parents[2] / "fixtures")

from app import seed  # noqa: E402
from app.modules.two_leg_close_fence import migrate as fence_migrate  # noqa: E402


@pytest.fixture()
def conn(monkeypatch):
    import app.db as dbmod

    path = Path(TMP) / f"legacy-{os.urandom(4).hex()}.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    c = dbmod.connect()
    # 老库形态：只有基座表，没有双腿栅栏表、没有 quarantine 扩展列
    c.executescript(seed.SCHEMA)
    c.execute("INSERT INTO cabinets(id,label,site) VALUES ('CAB-L','旧柜','老库')")
    for code in ("L1", "L2"):
        c.execute(
            "INSERT INTO cells(id,cabinet_id,code,size_code,status) VALUES (?,?,?,?, 'free')",
            (f"CAB-L-{code}", "CAB-L", code, "M"),
        )
    # 老种子逻辑：pickup 单独到达即释放
    c.execute(
        "INSERT INTO parcel_events(id,cabinet_id,cell_id,event_type,event_ts,tracking_no,payload_json,channel) "
        "VALUES ('OLD-D','CAB-L','CAB-L-L1','drop','2026-11-18T08:00:00Z','OLD1','{}','primary'),"
        "       ('OLD-P','CAB-L','CAB-L-L1','pickup','2026-11-18T09:00:00Z','OLD1','{}','primary')"
    )
    c.execute(
        "INSERT INTO occupancies(id,cell_id,tracking_no,opened_at,closed_at,status,last_event_id) "
        "VALUES ('OCC-OLD','CAB-L-L1','OLD1','2026-11-18T08:00:00Z','2026-11-18T09:00:00Z','released','OLD-P')"
    )
    c.commit()
    yield c
    c.close()


def test_legacy_single_leg_release_is_reopened(conn):
    result = fence_migrate.migrate(conn)
    assert result == {"reconciled": 1, "first_run": True}
    occ = conn.execute("SELECT * FROM occupancies WHERE id='OCC-OLD'").fetchone()
    assert occ["status"] == "occupied" and occ["closed_at"] is None
    assert conn.execute("SELECT status FROM cells WHERE id='CAB-L-L1'").fetchone()["status"] == "occupied"
    cand = conn.execute("SELECT * FROM close_candidates WHERE occupancy_id='OCC-OLD'").fetchone()
    assert cand["event_type"] == "pickup" and cand["status"] == "pending" and cand["event_id"] == "OLD-P"
    held = conn.execute(
        "SELECT * FROM quarantine WHERE occupancy_id='OCC-OLD' AND status='open'"
    ).fetchone()
    assert held["reason"] == "two_leg:await_confirm"
    assert json.loads(held["member_json"])["source"] == "legacy_reconcile"


def test_migrate_is_idempotent_and_installs_triggers(conn):
    fence_migrate.migrate(conn)
    again = fence_migrate.migrate(conn)
    assert again == {"reconciled": 0, "first_run": False}
    assert conn.execute("SELECT COUNT(*) AS c FROM close_candidates").fetchone()["c"] == 1
    occ = conn.execute("SELECT * FROM occupancies WHERE id='OCC-OLD'").fetchone()
    assert occ["status"] == "occupied"
    with pytest.raises(Exception):
        conn.execute("UPDATE occupancies SET status='released' WHERE id='OCC-OLD'")


def test_legacy_unsafe_release_is_quarantined(conn):
    # 该格口后续又有新占用：旧释放行无法安全重开，只留搁置
    conn.execute(
        "INSERT INTO occupancies(id,cell_id,tracking_no,opened_at,closed_at,status,last_event_id) "
        "VALUES ('OCC-NEW','CAB-L-L1','OLD1','2026-11-18T10:00:00Z',NULL,'occupied','X')"
    )
    conn.commit()
    result = fence_migrate.migrate(conn)
    assert result["reconciled"] == 0
    assert conn.execute("SELECT status FROM occupancies WHERE id='OCC-OLD'").fetchone()["status"] == "released"
    held = conn.execute("SELECT * FROM quarantine WHERE occupancy_id='OCC-OLD'").fetchone()
    assert held["reason"] == "two_leg:legacy_released_unsafe"


def test_backfill_after_legs_arrive(conn):
    fence_migrate.migrate(conn)
    conn.execute(
        "INSERT INTO parcel_events(id,cabinet_id,cell_id,event_type,event_ts,tracking_no,payload_json,channel) "
        "VALUES ('OLD-C','CAB-L','CAB-L-L1','confirm','2026-11-18T09:05:00Z','OLD1','{}','primary')"
    )
    from app.services import pipeline

    out = pipeline.apply_event(
        conn,
        {
            "id": "OLD-C",
            "cabinet_id": "CAB-L",
            "cell_id": "CAB-L-L1",
            "event_type": "confirm",
            "event_ts": "2026-11-18T09:05:00Z",
            "tracking_no": "OLD1",
        },
    )
    assert out["action"] == "closed"
    leg = conn.execute("SELECT * FROM close_legs WHERE occupancy_id='OCC-OLD'").fetchone()
    assert (leg["pickup_id"], leg["confirm_id"]) == ("OLD-P", "OLD-C")
    assert conn.execute("SELECT status FROM occupancies WHERE id='OCC-OLD'").fetchone()["status"] == "released"
