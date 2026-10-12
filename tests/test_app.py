import os
import tempfile
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_PATH"] = tmp.name
os.environ["AI_MODE"] = "demo"
from app.main import app, evaluate_readings, init_db, seed

init_db()
seed()
client = TestClient(app)


def test_health_has_no_equipment_control():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["equipment_control"] is False


def test_threshold_boundaries():
    outcomes = []
    for value in (84, 85, 86):
        out = evaluate_readings([{"id":str(value),"sensor_key":"motor_temperature","value":value,"unit":"C"}])
        outcomes.append(out[0]["outcome"])
    assert outcomes == ["within_configured_demo_limit", "within_configured_demo_limit", "threshold_exceeded"]


def test_missing_and_wrong_unit_not_evaluated():
    out = evaluate_readings([
        {"id":"1","sensor_key":"motor_temperature","value":None,"unit":"C"},
        {"id":"2","sensor_key":"motor_temperature","value":90,"unit":"F"},
    ])
    assert all(x["outcome"] == "not_evaluated" for x in out)


def test_future_timestamp_not_evaluated():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    out = evaluate_readings([{
        "id": "future", "sensor_key": "motor_temperature", "value": 100,
        "unit": "C", "observed_at": future
    }])
    assert out[0]["quality"] == "future_timestamp"
    assert out[0]["outcome"] == "not_evaluated"


def test_conflicting_readings_are_preserved_and_not_evaluated():
    out = evaluate_readings([
        {"id":"1","sensor_key":"vibration","value":2,"unit":"mm/s"},
        {"id":"2","sensor_key":"vibration","value":9,"unit":"mm/s"},
    ])
    assert len(out) == 2
    assert all(x["quality"] == "conflicting" for x in out)
    assert all(x["outcome"] == "not_evaluated" for x in out)


def test_issue_to_triage_to_work_order_review():
    assets = client.get("/api/equipment").json()
    motor = next(a for a in assets if a["equipment_type"] == "electric_motor")
    created = client.post("/api/issues", json={
        "equipment_id": motor["id"],
        "description": "Motor has unusual vibration after startup",
        "reporter": "test.technician",
        "events": [{"event_type":"startup","description":"Vibration noticed after startup"}],
        "readings": [{"sensor_key":"vibration","value":8.2,"unit":"mm/s"}]
    })
    assert created.status_code == 200, created.text
    iid = created.json()["id"]
    triage = client.post(f"/api/issues/{iid}/triage", json={})
    assert triage.status_code == 200, triage.text
    result = triage.json()["result"]
    assert result["possible_causes"]
    assert all(x["status"] == "unconfirmed" for x in result["possible_causes"])
    event_observation = next(x for x in result["observations"] if "Vibration noticed after startup" in x["text"])
    assert event_observation["evidence"][0]["type"] == "event"
    assert result["rule_results"][0]["outcome"] == "threshold_exceeded"
    wid = triage.json()["work_order_id"]
    w = client.get(f"/api/work-orders/{wid}").json()
    assert w["status"] == "draft"
    edited = client.put(f"/api/work-orders/{wid}", json={
        "title": w["title"], "priority": w["priority"], "summary": w["summary"],
        "tasks": w["tasks"], "precautions": w["precautions"], "expected_revision": w["revision"]
    })
    assert edited.status_code == 200
    submitted = client.post(f"/api/work-orders/{wid}/submit", json={})
    assert submitted.json()["status"] == "in_review"
    denied = client.post(f"/api/work-orders/{wid}/decision/approve",
                         json={}, headers={"X-Role":"technician","X-Actor":"bad.actor"})
    assert denied.status_code == 403
    rejected_without_reason = client.post(f"/api/work-orders/{wid}/decision/reject",
                         json={}, headers={"X-Role":"approver","X-Actor":"demo.approver"})
    assert rejected_without_reason.status_code == 422
    approved = client.post(f"/api/work-orders/{wid}/decision/approve",
                         json={}, headers={"X-Role":"approver","X-Actor":"demo.approver"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["equipment_control"] is False


def test_finding_and_history():
    issue = client.get("/api/issues").json()[0]
    r = client.post(f"/api/issues/{issue['id']}/findings", json={
        "finding":"Technician confirmed mounting bolt looseness",
        "corrective_action":"Corrected per approved procedure",
        "resolution":"Follow-up measurement recorded",
        "recorded_by":"test.technician"
    })
    assert r.status_code == 200
    hist = client.get(f"/api/equipment/{issue['equipment_id']}/history")
    assert hist.status_code == 200
    assert any("looseness" in x["finding"] for x in hist.json()["maintenance_records"])
