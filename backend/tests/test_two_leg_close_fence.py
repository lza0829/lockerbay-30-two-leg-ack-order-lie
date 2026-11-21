import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="lockerbay-test-")
os.environ["DATA_DIR"] = TMP
os.environ["FIXTURES_DIR"] = str(Path(__file__).resolve().parents[2] / "fixtures")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import seed  # noqa: E402
from app.modules.two_leg_close_fence import migrate as fence_migrate  # noqa: E402
from app.modules import two_leg_close_fence as fence  # noqa: E402
from app.services import pipeline  # noqa: E402


@pytest.fixture()
def db(monkeypatch):
    """每个用例独占一个空库；夹具目录指向被测仓库 fixtures。"""
    import app.db as dbmod

    path = Path(TMP) / f"case-{os.urandom(4).hex()}.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    conn = dbmod.connect()
    conn.executescript(seed.SCHEMA)
    fence_migrate.apply_schema(conn)
    conn.execute("INSERT INTO cabinets(id,label,site) VALUES ('CAB-T','试验柜','本地')")
    for code in ("T1", "T2"):
        conn.execute(
            "INSERT INTO cells(id,cabinet_id,code,size_code,status) VALUES (?,?,?,?, 'free')",
            (f"CAB-T-{code}", "CAB-T", code, "M"),
        )
    conn.commit()
    yield conn
    conn.close()


def put_event(conn, ev):
    conn.execute(
        "INSERT INTO parcel_events(id,cabinet_id,cell_id,event_type,event_ts,tracking_no,payload_json,channel) "
        "VALUES (?,?,?,?,?,?,'{}','primary')",
        (ev["id"], ev["cabinet_id"], ev.get("cell_id"), ev["event_type"], ev["event_ts"], ev.get("tracking_no")),
    )
    return pipeline.apply_event(conn, ev)


def drop(conn, eid="D1", cell="CAB-T-T1", tn="TN1", ts="2026-11-18T08:00:00Z"):
    return put_event(
        conn,
        {"id": eid, "cabinet_id": "CAB-T", "cell_id": cell, "event_type": "drop", "event_ts": ts, "tracking_no": tn},
    )


def leg(conn, eid, etype, cell="CAB-T-T1", tn="TN1", ts="2026-11-18T09:00:00Z"):
    return put_event(
        conn,
        {"id": eid, "cabinet_id": "CAB-T", "cell_id": cell, "event_type": etype, "event_ts": ts, "tracking_no": tn},
    )


def occ(conn, cell="CAB-T-T1"):
    return conn.execute(
        "SELECT * FROM occupancies WHERE cell_id=? ORDER BY opened_at DESC LIMIT 1", (cell,)
    ).fetchone()


def test_single_pickup_holds_and_never_releases(db):
    """夹具 A：只有 pickup 腿 → 占用保持 occupied，事件进候选区与搁置。"""
    drop(db)
    out = leg(db, "P1", "pickup")
    assert out["action"] == "held"
    assert out["reason"] == "two_leg:await_confirm"
    row = occ(db)
    assert row["status"] == "occupied"
    assert row["closed_at"] is None
    assert db.execute("SELECT COUNT(*) AS c FROM close_legs").fetchone()["c"] == 0
    cand = db.execute("SELECT * FROM close_candidates WHERE event_id='P1'").fetchone()
    assert cand["status"] == "pending"
    held = db.execute("SELECT * FROM quarantine WHERE event_id='P1'").fetchone()
    assert held["status"] == "open" and held["candidate_id"] == cand["id"]
    assert db.execute("SELECT status FROM cells WHERE id='CAB-T-T1'").fetchone()["status"] == "occupied"


def test_pickup_then_confirm_releases_with_legs(db):
    """夹具 B：pickup + confirm 双腿 → 原子关账，边表留双腿 id。"""
    drop(db)
    assert leg(db, "P1", "pickup")["action"] == "held"
    out = leg(db, "C1", "confirm", ts="2026-11-18T09:05:00Z")
    assert out["action"] == "closed"
    row = occ(db)
    assert row["status"] == "released"
    assert row["closed_at"] == "2026-11-18T09:05:00Z"
    close = db.execute("SELECT * FROM close_legs WHERE occupancy_id=?", (row["id"],)).fetchone()
    assert (close["pickup_id"], close["confirm_id"]) == ("P1", "C1")
    assert close["cell_id"] == row["cell_id"] and close["tracking_no"] == row["tracking_no"]
    assert db.execute("SELECT status FROM cells WHERE id='CAB-T-T1'").fetchone()["status"] == "free"
    assert db.execute("SELECT status FROM close_candidates WHERE event_id='P1'").fetchone()["status"] == "closed"
    assert db.execute("SELECT status FROM quarantine WHERE event_id='P1'").fetchone()["status"] == "resolved"
    assert fence.audit(db) == []


def test_confirm_first_then_pickup_releases(db):
    """两腿倒序到达同样关账：先到腿搁置，后到腿触发配对。"""
    drop(db)
    assert leg(db, "C1", "confirm")["action"] == "held"
    out = leg(db, "P1", "pickup", ts="2026-11-18T09:10:00Z")
    assert out["action"] == "closed"
    close = db.execute("SELECT * FROM close_legs").fetchone()
    assert (close["pickup_id"], close["confirm_id"]) == ("P1", "C1")
    assert occ(db)["status"] == "released"


def test_confirm_from_other_waybill_never_pairs(db):
    """别的运单的 confirm 不算腿：占用不释放，错配事件进搁置。"""
    drop(db, tn="TN1")
    assert leg(db, "P1", "pickup", tn="TN1")["action"] == "held"
    out = leg(db, "C9", "confirm", tn="TN9")
    assert out["action"] == "held"
    assert out["reason"] == "two_leg:tracking_mismatch"
    assert occ(db)["status"] == "occupied"
    assert db.execute("SELECT COUNT(*) AS c FROM close_legs").fetchone()["c"] == 0
    q = db.execute("SELECT * FROM quarantine WHERE event_id='C9'").fetchone()
    assert q["status"] == "open" and q["reason"] == "two_leg:tracking_mismatch"
    # 同格口确实有开放占用，但运单不同 → 不得配对
    assert json.loads(q["member_json"])["attempted_tracking_no"] == "TN9"


def test_confirm_on_other_cell_never_pairs(db):
    """同运单但格口不同：不构成同占用配对。"""
    drop(db)
    assert leg(db, "P1", "pickup", cell="CAB-T-T1")["action"] == "held"
    out = leg(db, "C1", "confirm", cell="CAB-T-T2")
    assert out["action"] == "held"
    assert out["reason"] == "two_leg:no_open_occupancy"
    assert occ(db, "CAB-T-T1")["status"] == "occupied"
    assert db.execute("SELECT COUNT(*) AS c FROM close_legs").fetchone()["c"] == 0


def test_db_trigger_blocks_release_without_legs(db):
    """失败闭：绕过服务层直接 UPDATE 也必须被触发器拦住。"""
    drop(db)
    row = occ(db)
    with pytest.raises(Exception) as err:
        db.execute("UPDATE occupancies SET status='released', closed_at='x' WHERE id=?", (row["id"],))
    # 双腿栅栏与腿序 ack 任一触发器先 abort 都算拦住（同属 two_leg 失败闭家族）
    assert "two_leg" in str(err.value)
    assert occ(db)["status"] == "occupied"
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO occupancies(id,cell_id,tracking_no,opened_at,closed_at,status,last_event_id) "
            "VALUES ('X','CAB-T-T1','TN1','x','x','released',NULL)"
        )


def test_close_legs_table_is_immutable(db):
    drop(db)
    leg(db, "P1", "pickup")
    leg(db, "C1", "confirm")
    with pytest.raises(Exception) as err:
        db.execute("UPDATE close_legs SET confirm_id='P1'")
    assert "immutable" in str(err.value)
    with pytest.raises(Exception):
        db.execute("DELETE FROM close_legs")


def test_close_legs_trigger_rejects_other_waybill_confirm(db):
    """直接往边表塞别的运单 confirm 也会被数据库拒绝。"""
    drop(db)
    leg(db, "P1", "pickup")
    leg(db, "C9", "confirm", tn="TN9")
    row = occ(db)
    with pytest.raises(Exception) as err:
        db.execute(
            "INSERT INTO close_legs(occupancy_id,cell_id,tracking_no,pickup_id,confirm_id,pickup_ts,confirm_ts,closed_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], "CAB-T-T1", "TN1", "P1", "C9", "a", "b", "b", "now"),
        )
    assert "matching pickup and confirm" in str(err.value)


def test_timeout_keeps_group_held(db):
    """超时未配对的整组：候选 expired，占用仍 occupied，搁置仍 open。"""
    db.execute("INSERT INTO settings(key,value) VALUES ('pair_timeout_minutes','30')")
    drop(db)
    leg(db, "P1", "pickup")
    # 模拟该腿很早入候选区：搁置超时从它进入候选区时起算
    db.execute("UPDATE close_candidates SET created_at='2026-11-18T09:00:00Z' WHERE event_id='P1'")
    result = fence.sweep(db, now="2026-11-18T12:00:00Z")
    assert result["expired"] == 1
    assert db.execute("SELECT status FROM close_candidates WHERE event_id='P1'").fetchone()["status"] == "expired"
    assert db.execute("SELECT status FROM quarantine WHERE event_id='P1'").fetchone()["status"] == "open"
    assert occ(db)["status"] == "occupied"
    # 过期后再来的 confirm 不再配对成功——整组留搁置，等人复核
    out = leg(db, "C1", "confirm")
    assert out["action"] == "held"
    assert occ(db)["status"] == "occupied"
    assert db.execute("SELECT COUNT(*) AS c FROM close_legs").fetchone()["c"] == 0


def test_verify_reads_two_leg_evidence(db):
    drop(db)
    leg(db, "P1", "pickup")
    leg(db, "C1", "confirm")
    row = occ(db)
    v = fence.verify(db, row["id"])
    assert v["verified"] is True
    assert all(c["ok"] for c in v["checks"])
    assert v["leg"]["pickup_id"] == "P1" and v["leg"]["confirm_id"] == "C1"
    # 单腿搁置的占用：核证应判为未关闭且无违规边
    drop(db, eid="D2", cell="CAB-T-T2", tn="TN2")
    leg(db, "P2", "pickup", cell="CAB-T-T2", tn="TN2")
    v2 = fence.verify(db, occ(db, "CAB-T-T2")["id"])
    assert v2["verified"] is False
    assert v2["leg"] is None
    assert v2["checks"][0]["name"] == "双腿边存在" and v2["checks"][0]["ok"] is False
    assert v2["checks"][-1]["name"] == "占用已 released" and v2["checks"][-1]["ok"] is False
