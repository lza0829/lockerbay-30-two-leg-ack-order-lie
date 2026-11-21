"""腿序 ack：关账必须落 first/second，且 first 是 occurred_at 更早的腿事件。"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="lockerbay-ack-")
os.environ["DATA_DIR"] = TMP
os.environ["FIXTURES_DIR"] = str(Path(__file__).resolve().parents[2] / "fixtures")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.main import app  # noqa: E402
from app.modules import two_leg_ack_order as leg_ack  # noqa: E402
from app.modules import two_leg_close_fence as fence  # noqa: E402
from app.modules.two_leg_close_fence import migrate as fence_migrate  # noqa: E402
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


@pytest.fixture()
def client(monkeypatch):
    import app.db as dbmod

    path = Path(TMP) / f"api-{os.urandom(4).hex()}.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    with TestClient(app) as cl:
        yield cl


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


def raw_leg_events(conn):
    """绕过服务层，直接落一对合法腿事件（供手工造边/对调用）。"""
    conn.execute(
        "INSERT INTO parcel_events(id,cabinet_id,cell_id,event_type,event_ts,tracking_no,payload_json,channel) "
        "VALUES ('P1','CAB-T','CAB-T-T1','pickup','2026-11-18T09:00:00Z','TN1','{}','primary'),"
        "       ('C1','CAB-T','CAB-T-T1','confirm','2026-11-18T09:05:00Z','TN1','{}','primary')"
    )


def manual_edge(conn, occupancy_id):
    cur = conn.execute(
        "INSERT INTO close_legs(occupancy_id,cell_id,tracking_no,pickup_id,confirm_id,"
        "pickup_ts,confirm_ts,closed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            occupancy_id,
            "CAB-T-T1",
            "TN1",
            "P1",
            "C1",
            "2026-11-18T09:00:00Z",
            "2026-11-18T09:05:00Z",
            "2026-11-18T09:05:00Z",
            "now",
        ),
    )
    return cur.lastrowid


def insert_ack(conn, occupancy_id, leg_id, first, second, first_ts, second_ts):
    return conn.execute(
        "INSERT INTO two_leg_ack_order(occupancy_id,close_leg_id,first_event_id,second_event_id,"
        "first_ts,second_ts,created_at) VALUES (?,?,?,?,?,?,?)",
        (occupancy_id, leg_id, first, second, first_ts, second_ts, "now"),
    )


# ---------------------------------------------------------------- 时钟序

def test_close_writes_ack_first_is_earlier_occurred_at(db):
    """pickup 先到达但 occurred_at 更晚：first 必须是更早的 confirm。"""
    drop(db)
    assert leg(db, "P1", "pickup", ts="2026-11-18T09:10:00Z")["action"] == "held"
    out = leg(db, "C1", "confirm", ts="2026-11-18T09:05:00Z")
    assert out["action"] == "closed"
    # 装载回执带腿序
    assert out["first_event_id"] == "C1" and out["second_event_id"] == "P1"
    row = occ(db)
    ack = db.execute(
        "SELECT * FROM two_leg_ack_order WHERE occupancy_id=?", (row["id"],)
    ).fetchone()
    assert (ack["first_event_id"], ack["second_event_id"]) == ("C1", "P1")
    assert ack["first_ts"] == "2026-11-18T09:05:00Z"
    assert ack["second_ts"] == "2026-11-18T09:10:00Z"
    # 关闭边仍是 pickup/confirm 语义，ack 才是事件时钟序
    edge = db.execute(
        "SELECT * FROM close_legs WHERE occupancy_id=?", (row["id"],)
    ).fetchone()
    assert (edge["pickup_id"], edge["confirm_id"]) == ("P1", "C1")
    receipt = leg_ack.ack_of(db, row["id"])
    assert receipt["legs_ok"] is True
    assert receipt["first_event_id"] == "C1"
    assert receipt["second_event_id"] == "P1"


def test_ack_follows_event_clock_not_arrival_order(db):
    """confirm 先到达但 occurred_at 更晚：first 仍是 pickup。"""
    drop(db)
    assert leg(db, "C1", "confirm", ts="2026-11-18T09:20:00Z")["action"] == "held"
    out = leg(db, "P1", "pickup", ts="2026-11-18T09:10:00Z")
    assert out["action"] == "closed"
    row = occ(db)
    ack = db.execute(
        "SELECT * FROM two_leg_ack_order WHERE occupancy_id=?", (row["id"],)
    ).fetchone()
    assert (ack["first_event_id"], ack["second_event_id"]) == ("P1", "C1")


def test_ack_tie_breaks_by_event_id(db):
    """occurred_at 并列：按事件 id 升序，保证可重放的确定性。"""
    drop(db)
    leg(db, "P1", "pickup", ts="2026-11-18T09:00:00Z")
    out = leg(db, "C1", "confirm", ts="2026-11-18T09:00:00Z")
    assert out["action"] == "closed"
    ack = db.execute("SELECT * FROM two_leg_ack_order").fetchone()
    assert (ack["first_event_id"], ack["second_event_id"]) == ("C1", "P1")
    assert ack["first_ts"] == ack["second_ts"]


# ---------------------------------------------------------------- 硬约束（数据库兜底）

def test_manual_swap_rejected_by_db(db):
    """人为对调 first/second：无论用真实时刻还是谎报时刻，都必须失败。"""
    drop(db)
    row = occ(db)
    raw_leg_events(db)
    leg_id = manual_edge(db, row["id"])
    # 对调 + 真实时刻：first_ts > second_ts，违反升序
    with pytest.raises(Exception) as err:
        insert_ack(db, row["id"], leg_id, "C1", "P1",
                   "2026-11-18T09:05:00Z", "2026-11-18T09:00:00Z")
    assert "two_leg_ack_order" in str(err.value)
    # 对调 + 谎报时刻（把 confirm 的 occurred_at 改小冒充 first）
    with pytest.raises(Exception) as err:
        insert_ack(db, row["id"], leg_id, "C1", "P1",
                   "2026-11-18T08:59:00Z", "2026-11-18T09:00:00Z")
    assert "event clock" in str(err.value)
    # second 空仍想落 ack
    with pytest.raises(Exception):
        insert_ack(db, row["id"], leg_id, "P1", "",
                   "2026-11-18T09:00:00Z", "2026-11-18T09:00:00Z")
    assert db.execute("SELECT COUNT(*) AS c FROM two_leg_ack_order").fetchone()["c"] == 0


def test_release_without_ack_blocked_by_trigger(db):
    """禁止关账成功而腿序 ack 缺：有关闭边但没有 ack，released 被数据库拦下。"""
    drop(db)
    row = occ(db)
    raw_leg_events(db)
    manual_edge(db, row["id"])
    with pytest.raises(Exception) as err:
        db.execute(
            "UPDATE occupancies SET status='released', closed_at='2026-11-18T09:05:00Z' WHERE id=?",
            (row["id"],),
        )
    assert "two_leg_ack_order" in str(err.value)
    assert occ(db)["status"] == "occupied"


def test_ack_table_is_immutable(db):
    drop(db)
    leg(db, "P1", "pickup")
    leg(db, "C1", "confirm", ts="2026-11-18T09:05:00Z")
    with pytest.raises(Exception) as err:
        db.execute("UPDATE two_leg_ack_order SET first_event_id=second_event_id")
    assert "immutable" in str(err.value)
    with pytest.raises(Exception):
        db.execute("DELETE FROM two_leg_ack_order")


def test_failure_rolls_back_no_half_ack(db, monkeypatch):
    """ack 写入失败：整笔回滚，不留已关占用、不留半截边/ack。"""
    drop(db)
    leg(db, "P1", "pickup")
    db.commit()  # 占用与单腿候选已落账

    def boom(*args, **kwargs):
        raise RuntimeError("ack store unavailable")

    monkeypatch.setattr(leg_ack, "record_ack", boom)
    with pytest.raises(RuntimeError):
        leg(db, "C1", "confirm", ts="2026-11-18T09:05:00Z")
    db.rollback()
    row = occ(db)
    assert row["status"] == "occupied" and row["closed_at"] is None
    assert db.execute("SELECT COUNT(*) AS c FROM close_legs").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM two_leg_ack_order").fetchone()["c"] == 0
    # 已提交的单腿候选仍在等 confirm，未被失败关账污染
    cand = db.execute("SELECT status FROM close_candidates WHERE event_id='P1'").fetchone()
    assert cand["status"] == "pending"


# ---------------------------------------------------------------- 回填与核证

def test_backfill_fills_ack_for_legacy_edge(db):
    """老库已有关闭边但缺 ack：迁移按事件时钟补落，且幂等。"""
    drop(db)
    row = occ(db)
    raw_leg_events(db)
    manual_edge(db, row["id"])
    assert db.execute("SELECT COUNT(*) AS c FROM two_leg_ack_order").fetchone()["c"] == 0
    assert leg_ack.backfill(db) == 1
    ack = db.execute("SELECT * FROM two_leg_ack_order").fetchone()
    assert (ack["first_event_id"], ack["second_event_id"]) == ("P1", "C1")
    assert leg_ack.backfill(db) == 0


def test_verify_and_audit_unit(db):
    drop(db)
    leg(db, "P1", "pickup")
    leg(db, "C1", "confirm", ts="2026-11-18T09:05:00Z")
    row = occ(db)
    v = leg_ack.verify(db, row["id"])
    assert v["verified"] is True
    assert all(c["ok"] for c in v["checks"])
    assert leg_ack.audit(db) == []
    # 未关账占用：ack 缺失，核证不通过
    drop(db, eid="D2", cell="CAB-T-T2", tn="TN2")
    v2 = leg_ack.verify(db, occ(db, "CAB-T-T2")["id"])
    assert v2["verified"] is False
    assert v2["ack"] is None
    # 栅栏核证同样带腿序检查
    fv = fence.verify(db, row["id"])
    assert fv["verified"] is True
    assert fv["ack"]["first_event_id"] == "P1"
    assert "腿序 ack 按事件时钟升序" in [c["name"] for c in fv["checks"]]


# ---------------------------------------------------------------- API

def test_ack_endpoints_seeded(client):
    acks = client.get("/api/two-leg-acks").json()
    assert len(acks) == 1
    ack = acks[0]
    assert ack["occupancy_id"] == "OCC-CAB-A-03-E5"
    assert (ack["first_event_id"], ack["second_event_id"]) == ("E6", "E7")
    receipt = client.get("/api/two-leg-ack/OCC-CAB-A-03-E5").json()
    assert receipt["legs_ok"] is True
    assert receipt["first_event_id"] == "E6" and receipt["second_event_id"] == "E7"
    verdict = client.get("/api/two-leg-ack/verify/OCC-CAB-A-03-E5").json()
    assert verdict["verified"] is True
    assert client.get("/api/two-leg-ack/audit").json() == {"ok": True, "violations": []}
    assert client.get("/api/two-leg-ack/NOPE").status_code == 404
    assert client.get("/api/two-leg-ack/verify/NOPE").status_code == 404
    ov = client.get("/api/overview").json()
    assert ov["two_leg_ack"]["leg_acks"] == 1
    assert ov["two_leg_ack"]["violations"] == 0


def test_ack_written_for_ingested_closes(client):
    result = client.post("/api/ingest/run").json()
    actions = {o["event_id"]: o for o in result["outcomes"]}
    # 装载回执带腿序：E3 pickup(12:00) 早于 E9 confirm(次日 02:30)
    assert actions["E9"]["action"] == "closed"
    assert actions["E9"]["first_event_id"] == "E3"
    assert actions["E9"]["second_event_id"] == "E9"
    assert actions["E12"]["first_event_id"] == "E11"
    assert actions["E12"]["second_event_id"] == "E12"
    e1 = client.get("/api/two-leg-ack/OCC-CAB-A-01-E1").json()
    assert (e1["first_event_id"], e1["second_event_id"]) == ("E3", "E9")
    e10 = client.get("/api/two-leg-ack/OCC-CAB-A-04-E10").json()
    assert (e10["first_event_id"], e10["second_event_id"]) == ("E11", "E12")
    assert client.get("/api/two-leg-ack/audit").json()["ok"] is True
    ov = client.get("/api/overview").json()
    assert ov["two_leg_ack"]["leg_acks"] == 3


def test_fence_verify_carries_leg_order(client):
    verdict = client.get("/api/two-leg/verify/OCC-CAB-A-03-E5").json()
    assert verdict["verified"] is True
    assert verdict["ack"]["first_event_id"] == "E6"
    assert verdict["ack"]["second_event_id"] == "E7"
    names = [c["name"] for c in verdict["checks"]]
    assert "腿序 ack 按事件时钟升序" in names
