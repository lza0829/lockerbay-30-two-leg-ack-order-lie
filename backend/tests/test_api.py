import os
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="lockerbay-api-")
os.environ["DATA_DIR"] = TMP
os.environ["FIXTURES_DIR"] = str(Path(__file__).resolve().parents[2] / "fixtures")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    import app.db as dbmod

    path = Path(TMP) / f"api-{os.urandom(4).hex()}.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    with TestClient(app) as cl:
        yield cl


def test_health_and_two_leg_overview(client):
    assert client.get("/api/health").json()["正常"] is True
    ov = client.get("/api/overview").json()
    assert ov["two_leg"]["close_legs"] == 1
    assert ov["two_leg"]["violations"] == 0
    assert ov["two_leg"]["held_quarantine"] == 2


def test_close_legs_endpoint_returns_both_legs(client):
    legs = client.get("/api/close-legs").json()
    assert len(legs) == 1
    leg = legs[0]
    assert leg["tracking_no"] == "TN1003"
    assert leg["pickup_id"] == "E6" and leg["confirm_id"] == "E7"


def test_verify_endpoint_success_and_hold(client):
    ok = client.get("/api/two-leg/verify/OCC-CAB-A-03-E5").json()
    assert ok["verified"] is True
    hold = client.get("/api/two-leg/verify/OCC-CAB-A-01-E1").json()
    assert hold["verified"] is False
    assert hold["leg"] is None
    assert hold["occupancy"]["status"] == "occupied"
    assert client.get("/api/two-leg/verify/NOPE").status_code == 404


def test_audit_endpoint_clean(client):
    body = client.get("/api/two-leg/audit").json()
    assert body == {"ok": True, "violations": []}


def test_ingest_pairing_and_second_pickup_held(client):
    result = client.post("/api/ingest/run").json()
    actions = {o["event_id"]: o for o in result["outcomes"]}
    assert actions["E9"]["action"] == "closed"
    assert actions["E11"]["action"] == "held"
    assert actions["E12"]["action"] == "closed"
    legs = client.get("/api/close-legs").json()
    pairs = {(l["pickup_id"], l["confirm_id"]) for l in legs}
    assert ("E3", "E9") in pairs and ("E11", "E12") in pairs
    assert client.get("/api/two-leg/audit").json()["ok"] is True


def test_second_ingest_reports_duplicates(client):
    client.post("/api/ingest/run")
    second = client.post("/api/ingest/run").json()
    assert second["accepted"] == 0
    assert all(o["action"] == "duplicate" for o in second["outcomes"])


def test_sweep_endpoint_no_pending_within_timeout(client):
    body = client.post("/api/two-leg/sweep", json={}).json()
    assert body["expired"] == 0


def test_pair_timeout_setting_roundtrip(client):
    assert client.get("/api/settings").json()["pair_timeout_minutes"] == 30
    r = client.put("/api/settings/pair-timeout", json={"pair_timeout_minutes": 45})
    assert r.json()["pair_timeout_minutes"] == 45
    assert client.get("/api/settings").json()["pair_timeout_minutes"] == 45
    assert client.put("/api/settings/pair-timeout", json={"pair_timeout_minutes": 0}).status_code == 400
