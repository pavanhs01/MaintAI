from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DATABASE_PATH", str(BASE / "data" / "triage.db"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
AI_MODE = os.getenv("AI_MODE", "demo").lower()  # demo | ollama
DEMO_MODE = AI_MODE != "ollama"

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Equipment Maintenance Triage Assistant", version="1.0.0",
              description="Evidence-first maintenance triage. No equipment control.")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS equipment (
          id TEXT PRIMARY KEY, asset_tag TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          equipment_type TEXT NOT NULL, model TEXT, location TEXT, status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS issues (
          id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL REFERENCES equipment(id),
          description TEXT NOT NULL, reporter TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(id),
          event_type TEXT NOT NULL, description TEXT NOT NULL, occurred_at TEXT, source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sensor_readings (
          id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(id),
          sensor_key TEXT NOT NULL, value REAL, unit TEXT NOT NULL, observed_at TEXT,
          source TEXT NOT NULL, quality TEXT NOT NULL, quality_note TEXT
        );
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, revision TEXT NOT NULL,
          equipment_type TEXT NOT NULL, approved INTEGER NOT NULL, content_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document_chunks (
          id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
          section TEXT NOT NULL, page TEXT, text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS triage_runs (
          id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(id),
          status TEXT NOT NULL, provider TEXT NOT NULL, model TEXT, prompt_version TEXT NOT NULL,
          error TEXT, result_json TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hypotheses (
          id TEXT PRIMARY KEY, triage_id TEXT NOT NULL REFERENCES triage_runs(id),
          description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unconfirmed',
          confidence_label TEXT, evidence_json TEXT NOT NULL, contradictions_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_orders (
          id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES issues(id),
          revision INTEGER NOT NULL DEFAULT 1, title TEXT NOT NULL, priority TEXT NOT NULL,
          status TEXT NOT NULL, summary TEXT NOT NULL, tasks_json TEXT NOT NULL,
          precautions_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL REFERENCES work_orders(id),
          reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS maintenance_records (
          id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL REFERENCES equipment(id),
          issue_id TEXT REFERENCES issues(id), work_order_id TEXT REFERENCES work_orders(id),
          finding TEXT NOT NULL, corrective_action TEXT, resolution TEXT, recorded_by TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL, details_json TEXT NOT NULL, correlation_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_issues_equipment ON issues(equipment_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id);
        CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, created_at);
        """)


def audit(c: sqlite3.Connection, actor: str, action: str, entity_type: str,
          entity_id: str, details: dict[str, Any]) -> None:
    c.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?)",
              (str(uuid.uuid4()), actor, action, entity_type, entity_id,
               json.dumps(details), str(uuid.uuid4()), now()))


def seed() -> None:
    with connect() as c:
        if c.execute("SELECT COUNT(*) FROM equipment").fetchone()[0] == 0:
            assets = [
                ("asset-mtr-001", "MTR-001", "North Line Drive Motor", "electric_motor", "DEMO-MTR-7.5", "Line A"),
                ("asset-pmp-002", "PMP-002", "Cooling Loop Pump", "centrifugal_pump", "DEMO-PMP-3", "Utility Room"),
                ("asset-cmp-003", "CMP-003", "Workshop Air Compressor", "air_compressor", "DEMO-CMP-5", "Workshop"),
            ]
            for aid, tag, name, typ, model, loc in assets:
                c.execute("INSERT INTO equipment VALUES(?,?,?,?,?,?,?,?)",
                          (aid, tag, name, typ, model, loc, "active", now()))
        if c.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0:
            docs = [
                ("doc-mtr-demo", "DEMO MANUAL — Electric Motor Inspection", "demo-r1", "electric_motor",
                 "For demonstration only. Verify against approved site and manufacturer procedures. Section 1: Before inspection, follow site isolation and lockout/tagout procedures. Do not inspect moving or energized machinery. Section 2: Unusual vibration may be associated with imbalance, misalignment, looseness, bearing wear, or the installation. These are possible causes, not diagnoses. Record vibration measurement, measurement location, operating state, and instrument details. Section 3: If temperature is above the configured site limit, follow the approved escalation procedure. This demo manual defines no real operating limits."),
                ("doc-pmp-demo", "DEMO MANUAL — Centrifugal Pump Checks", "demo-r1", "centrifugal_pump",
                 "For demonstration only. Verify against approved site and manufacturer procedures. Section 1: Follow site isolation procedures before inspection. Section 2: Low discharge pressure may have several causes, including restricted suction, air ingress, wear, or an instrumentation problem. Verify instrument validity and compare with approved operating specifications. Section 3: Record pressure, flow if available, valve state, abnormal noise, and recent startup or maintenance events."),
                ("doc-cmp-demo", "DEMO MANUAL — Air Compressor Checks", "demo-r1", "air_compressor",
                 "For demonstration only. Verify against approved site and manufacturer procedures. Section 1: Follow site isolation and stored-energy release procedures. Section 2: Unexpected pressure behavior can relate to leakage, controls, restriction, or measurement error. Do not adjust pressure controls based only on an AI suggestion. Record readings and compare with approved specifications."),
            ]
            for did, title, rev, typ, body in docs:
                import hashlib
                h = hashlib.sha256(body.encode()).hexdigest()
                c.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", (did, title, rev, typ, 1, h))
                sections = re.split(r"(?=Section \d+:)", body)
                for i, chunk in enumerate(sections):
                    chunk = chunk.strip()
                    if chunk:
                        section = re.search(r"Section \d+:", chunk)
                        sec = section.group(0).replace(":", "") if section else "Overview"
                        c.execute("INSERT INTO document_chunks VALUES(?,?,?,?,?)",
                                  (f"{did}-chunk-{i}", did, sec, None, chunk))
        c.commit()


class EventIn(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    occurred_at: Optional[str] = None
    source: str = "technician_report"


class ReadingIn(BaseModel):
    sensor_key: str = Field(min_length=1, max_length=80)
    value: Optional[float] = None
    unit: str = Field(min_length=1, max_length=24)
    observed_at: Optional[str] = None
    source: str = "technician_report"


class IssueIn(BaseModel):
    equipment_id: str
    description: str = Field(min_length=5, max_length=5000)
    reporter: str = Field(default="demo.technician", min_length=1, max_length=120)
    events: list[EventIn] = Field(default_factory=list, max_length=30)
    readings: list[ReadingIn] = Field(default_factory=list, max_length=50)


class WorkOrderEdit(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    priority: str
    summary: str = Field(min_length=3, max_length=5000)
    tasks: list[str] = Field(min_length=1, max_length=30)
    precautions: list[str] = Field(default_factory=list, max_length=20)
    expected_revision: int = Field(ge=1)

    @field_validator("priority")
    @classmethod
    def valid_priority(cls, v: str) -> str:
        if v not in {"low", "medium", "high", "critical"}:
            raise ValueError("priority must be low, medium, high, or critical")
        return v


class DecisionIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


class FindingIn(BaseModel):
    finding: str = Field(min_length=3, max_length=3000)
    corrective_action: Optional[str] = Field(default=None, max_length=3000)
    resolution: Optional[str] = Field(default=None, max_length=3000)
    recorded_by: str = Field(default="demo.technician", min_length=1, max_length=120)


def rowdict(row):
    return dict(row) if row else None


def parse_json(value, fallback=None):
    try:
        return json.loads(value) if value else (fallback if fallback is not None else [])
    except (ValueError, TypeError):
        return fallback if fallback is not None else []


# Illustrative values only; not safe operating limits.
RULES = {
    "motor_temperature": {"unit": "C", "upper": 85.0, "severity": "high", "rule_id": "DEMO-MTR-TEMP-01"},
    "vibration": {"unit": "mm/s", "upper": 7.1, "severity": "high", "rule_id": "DEMO-VIB-01"},
    "hydraulic_pressure": {"unit": "bar", "lower": 2.0, "severity": "medium", "rule_id": "DEMO-PRESS-LOW-01"},
    "motor_current": {"unit": "A", "upper": 18.0, "severity": "high", "rule_id": "DEMO-CURRENT-01"},
}
STALE_AFTER_SECONDS = 24 * 3600


def evaluate_readings(readings: list[dict]) -> list[dict]:
    results = []
    for r in readings:
        key = r.get("sensor_key", "").lower().strip()
        rule = RULES.get(key)
        base = {"reading_id": r.get("id"), "sensor_key": key, "value": r.get("value"),
                "unit": r.get("unit"), "source": r.get("source"), "rule_id": None,
                "outcome": "not_evaluated", "quality": "valid", "explanation": ""}
        if r.get("value") is None:
            base.update(quality="missing", explanation="No value supplied; this is not a normal reading.")
        elif not rule:
            base.update(quality="unsupported_sensor", explanation="No configured demo rule exists for this sensor.")
        elif r.get("unit") != rule["unit"]:
            base.update(quality="unit_mismatch", explanation=f"Expected {rule['unit']}; received {r.get('unit')}. No conversion was applied.")
        else:
            base["rule_id"] = rule["rule_id"]
            exceeded = (r["value"] > rule["upper"]) if "upper" in rule else (r["value"] < rule["lower"])
            threshold = rule.get("upper", rule.get("lower"))
            comparator = ">" if "upper" in rule else "<"
            base["threshold"] = threshold
            base["comparator"] = comparator
            if exceeded:
                base.update(outcome="threshold_exceeded", explanation=f"{r['value']} {r['unit']} {comparator} {threshold} {r['unit']}; illustrative rule triggered.")
            else:
                base.update(outcome="within_configured_demo_limit", explanation=f"Reading did not exceed the configured demo threshold ({comparator} {threshold} {r['unit']}).")
        if r.get("observed_at"):
            try:
                dt = datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
                if age < 0:
                    base.update(quality="future_timestamp", outcome="not_evaluated", explanation="Reading timestamp is in the future; threshold result suppressed.")
                elif age > STALE_AFTER_SECONDS:
                    base.update(quality="stale", outcome="not_evaluated", explanation="Reading is older than the demo freshness window; threshold result suppressed.")
            except (ValueError, TypeError):
                base.update(quality="invalid_timestamp", outcome="not_evaluated", explanation="Timestamp could not be parsed.")
        results.append(base)
    # Preserve and flag same-sensor readings that disagree.
    by_sensor: dict[str, list[dict]] = {}
    for item in results:
        if item.get("value") is not None:
            by_sensor.setdefault(item["sensor_key"], []).append(item)
    for sensor, items in by_sensor.items():
        valid = [x for x in items if x["quality"] == "valid"]
        if len(valid) > 1 and len({(x["value"], x["unit"]) for x in valid}) > 1:
            for item in items:
                item["quality"] = "conflicting"
                item["outcome"] = "not_evaluated"
                item["explanation"] = "Conflicting readings exist for this sensor. Values are preserved; resolve the conflict before relying on a threshold result."
    return results


def retrieve_docs(equipment_type: str, query: str) -> list[dict]:
    tokens = set(re.findall(r"[a-z0-9]+", (query + " " + equipment_type).lower()))
    with connect() as c:
        rows = c.execute("""SELECT dc.id chunk_id, dc.document_id, dc.section, dc.page, dc.text,
                            d.title, d.revision, d.equipment_type
                            FROM document_chunks dc JOIN documents d ON d.id=dc.document_id
                            WHERE d.approved=1 AND (d.equipment_type=? OR d.equipment_type='general')""",
                          (equipment_type,)).fetchall()
    scored = []
    for row in rows:
        d = dict(row)
        words = set(re.findall(r"[a-z0-9]+", (d["title"] + " " + d["section"] + " " + d["text"]).lower()))
        score = len(tokens & words) / max(1, len(tokens))
        if score > 0:
            d["score"] = round(score, 3)
            scored.append(d)
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:5]


def deterministic_triage(issue: dict, equipment: dict, events: list[dict],
                         readings: list[dict], rule_results: list[dict], docs: list[dict]) -> dict:
    desc = issue["description"].lower()
    causes = []
    evrefs = [{"type": "issue", "id": issue["id"], "label": "Technician issue report"}]
    for e in events:
        evrefs.append({"type": "event", "id": e["id"], "label": f'{e["event_type"]}: {e["description"]}'})
    for d in docs[:3]:
        evrefs.append({"type": "manual", "id": d["chunk_id"], "document_id": d["document_id"],
                       "label": f'{d["title"]} — {d["section"]}', "revision": d["revision"]})
    evidence_for_causes = [x for x in evrefs if x["type"] in {"issue", "event", "manual"}]
    if equipment["equipment_type"] == "electric_motor":
        if any(w in desc for w in ["vibrat", "shaking", "noise", "rumbl"]):
            causes.append({"description": "Rotor imbalance, misalignment, looseness, or bearing wear may contribute to the reported vibration/noise.",
                           "status": "unconfirmed", "confidence_label": "Hypothesis only",
                           "evidence": evidence_for_causes[:3], "contradictions": [],
                           "note": "These alternatives cannot be distinguished from the report alone."})
        if any(w in desc for w in ["hot", "temperature", "overheat"]):
            causes.append({"description": "Cooling, loading, bearing, or measurement issues may contribute to elevated temperature.",
                           "status": "unconfirmed", "confidence_label": "Hypothesis only",
                           "evidence": evidence_for_causes[:3], "contradictions": []})
    elif equipment["equipment_type"] == "centrifugal_pump":
        causes.append({"description": "A restriction, air ingress, wear, operating condition, or instrument issue may contribute to the reported pump symptom.",
                       "status": "unconfirmed", "confidence_label": "Hypothesis only",
                       "evidence": evidence_for_causes[:3], "contradictions": []})
    else:
        causes.append({"description": "A mechanical, control, operating-condition, or measurement issue may contribute to the reported symptom.",
                       "status": "unconfirmed", "confidence_label": "Hypothesis only",
                       "evidence": evidence_for_causes[:3], "contradictions": []})
    triggered = [r for r in rule_results if r["outcome"] == "threshold_exceeded"]
    data_issues = [r for r in rule_results if r["quality"] not in {"valid"}]
    priority = "high" if triggered else ("medium" if data_issues else "medium")
    priority_reason = ("A configured illustrative threshold was exceeded; verify against approved site policy." if triggered
                       else "Initial triage is incomplete; technician review is required. Demo policy proposes medium priority.")
    tasks = [
        "Review the applicable approved site procedure and confirm equipment identity.",
        "Follow site isolation, lockout/tagout, and stored-energy procedures before any physical inspection.",
        "Record symptom conditions and verify sensor instrument, units, timestamp, and measurement method.",
        "Perform only the applicable inspection steps authorized by the site procedure; document measured results."
    ]
    if any(w in desc for w in ["vibrat", "shaking", "noise", "rumbl"]):
        tasks.append("If authorized and safe, record vibration at the specified measurement point and compare with approved equipment limits.")
    if not docs:
        questions = ["Which operating state was present when the symptom began?",
                     "Has the symptom been repeated and independently observed?",
                     "Are current readings available with units, timestamps, and instrument details?"]
    else:
        questions = ["When did the symptom first appear, and did it follow startup, shutdown, overload, or recent maintenance?",
                     "Can the symptom be reproduced, and under what operating conditions?",
                     "Are sensor readings current, correctly unit-labeled, and from a verified instrument?"]
    observations = [{"text": issue["description"],
                     "evidence": [{"type": "issue", "id": issue["id"], "label": "Technician report"}]}]
    observations.extend({
        "text": f"Operating event ({event['event_type']}): {event['description']}",
        "evidence": [{"type": "event", "id": event["id"],
                      "label": f"{event['event_type']}: {event['description']}"}]
    } for event in events)
    return {
        "mode": "deterministic_demo",
        "disclaimer": "Demo triage output, not an industrial diagnosis. Verify all procedures and limits against approved site documentation.",
        "observations": observations,
        "possible_causes": causes,
        "follow_up_questions": questions,
        "inspection_steps": [{"text": t, "evidence": evidence_for_causes[:2], "safety_note": "Follow approved site safety procedures; do not inspect energized or moving machinery."} for t in tasks],
        "priority": priority,
        "priority_reason": priority_reason,
        "rule_results": rule_results,
        "retrieval": {"status": "succeeded" if docs else "no_relevant_evidence",
                      "mode": "keyword_search", "documents": docs},
        "work_order_draft": {
            "title": f"Inspect {equipment['asset_tag']}: reported equipment issue",
            "priority": priority,
            "summary": issue["description"] + " Initial triage is provisional; confirm findings during authorized inspection.",
            "tasks": tasks,
            "precautions": ["Follow approved site isolation/LOTO and stored-energy procedures.",
                            "Do not rely on demo thresholds as operating limits.",
                            "Do not adjust controls or operate equipment based solely on AI output."],
            "evidence": evrefs
        }
    }


async def ollama_triage(base_result: dict, issue: dict, equipment: dict) -> tuple[dict, str]:
    prompt = f"""You are an assistant for maintenance triage, not a controller or authority.
Use only the supplied JSON as evidence. Do not invent citations, manual facts, sensor readings, or safe limits.
All causes must remain unconfirmed hypotheses. Preserve deterministic rule results and priority; do not override them.
Return JSON with keys observations, possible_causes, follow_up_questions, inspection_steps, priority_reason, work_order_draft.
Do not provide hazardous instructions. Mention approved site procedures and qualified technicians.
Issue: {json.dumps(issue)}
Equipment: {json.dumps(equipment)}
Evidence-backed baseline and deterministic rule results: {json.dumps(base_result)}
"""
    async with httpx.AsyncClient(timeout=35) as client:
        resp = await client.post(f"{OLLAMA_URL.rstrip('/')}/api/generate",
                                 json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                                       "format": "json"})
        resp.raise_for_status()
        body = resp.json()
    generated = json.loads(body.get("response", "{}"))
    # The baseline is authoritative for rule results, citations, retrieval and priority.
    for key in ("observations", "possible_causes", "follow_up_questions", "inspection_steps", "priority_reason", "work_order_draft"):
        if key in generated:
            base_result[key] = generated[key]
    base_result["mode"] = "ollama"
    base_result["disclaimer"] = "Local model-assisted draft. Verify evidence and all procedures; this is not an industrial diagnosis."
    return base_result, body.get("model", OLLAMA_MODEL)


@app.on_event("startup")
def startup():
    init_db()
    seed()


@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/health")
def health():
    try:
        with connect() as c:
            c.execute("SELECT 1")
        return {"status": "ok", "database": "ok", "ai_mode": "demo" if DEMO_MODE else "ollama",
                "equipment_control": False}
    except sqlite3.Error as exc:
        raise HTTPException(503, detail={"status": "error", "component": "database", "message": str(exc)})


@app.get("/api/equipment")
def list_equipment():
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM equipment ORDER BY asset_tag").fetchall()]


@app.post("/api/equipment")
def create_equipment(body: dict, x_actor: str = Header(default="demo.technician")):
    required = ["asset_tag", "name", "equipment_type"]
    if any(not isinstance(body.get(k), str) or not body[k].strip() for k in required):
        raise HTTPException(422, "asset_tag, name and equipment_type are required strings")
    aid = str(uuid.uuid4())
    with connect() as c:
        try:
            c.execute("INSERT INTO equipment VALUES(?,?,?,?,?,?,?,?)",
                      (aid, body["asset_tag"].strip(), body["name"].strip(), body["equipment_type"].strip(),
                       body.get("model"), body.get("location"), "active", now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Asset tag already exists")
        audit(c, x_actor, "equipment.created", "equipment", aid, {"asset_tag": body["asset_tag"]})
    return {"id": aid, "asset_tag": body["asset_tag"]}


@app.get("/api/equipment/{equipment_id}/history")
def equipment_history(equipment_id: str):
    with connect() as c:
        asset = c.execute("SELECT * FROM equipment WHERE id=? OR asset_tag=?", (equipment_id, equipment_id)).fetchone()
        if not asset:
            raise HTTPException(404, "Equipment not found")
        issues = c.execute("SELECT * FROM issues WHERE equipment_id=? ORDER BY created_at DESC", (asset["id"],)).fetchall()
        records = c.execute("SELECT * FROM maintenance_records WHERE equipment_id=? ORDER BY created_at DESC", (asset["id"],)).fetchall()
        return {"equipment": dict(asset), "issues": [dict(x) for x in issues],
                "maintenance_records": [dict(x) for x in records]}


@app.post("/api/issues")
def create_issue(body: IssueIn, x_actor: str = Header(default="demo.technician")):
    iid = str(uuid.uuid4())
    with connect() as c:
        equipment = c.execute("SELECT * FROM equipment WHERE id=?", (body.equipment_id,)).fetchone()
        if not equipment:
            raise HTTPException(404, "Equipment not found")
        timestamp = now()
        c.execute("INSERT INTO issues VALUES(?,?,?,?,?,?,?)",
                  (iid, body.equipment_id, body.description, body.reporter, "reported", timestamp, timestamp))
        for e in body.events:
            c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                      (str(uuid.uuid4()), iid, e.event_type, e.description, e.occurred_at, e.source))
        for r in body.readings:
            quality = "missing" if r.value is None else "submitted"
            c.execute("INSERT INTO sensor_readings VALUES(?,?,?,?,?,?,?,?,?)",
                      (str(uuid.uuid4()), iid, r.sensor_key.strip().lower(), r.value, r.unit,
                       r.observed_at, r.source, quality, None))
        audit(c, x_actor, "issue.created", "issue", iid,
              {"equipment_id": body.equipment_id, "event_count": len(body.events), "reading_count": len(body.readings)})
    return {"id": iid, "status": "reported", "created_at": timestamp}


@app.get("/api/issues")
def list_issues():
    with connect() as c:
        rows = c.execute("""SELECT i.*, e.asset_tag, e.name equipment_name, e.equipment_type
                            FROM issues i JOIN equipment e ON e.id=i.equipment_id
                            ORDER BY i.created_at DESC""").fetchall()
        return [dict(r) for r in rows]


@app.get("/api/issues/{issue_id}")
def issue_detail(issue_id: str):
    with connect() as c:
        issue = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not issue:
            raise HTTPException(404, "Issue not found")
        equipment = c.execute("SELECT * FROM equipment WHERE id=?", (issue["equipment_id"],)).fetchone()
        events = c.execute("SELECT * FROM events WHERE issue_id=?", (issue_id,)).fetchall()
        readings = c.execute("SELECT * FROM sensor_readings WHERE issue_id=?", (issue_id,)).fetchall()
        triages = c.execute("SELECT * FROM triage_runs WHERE issue_id=? ORDER BY created_at DESC", (issue_id,)).fetchall()
        work_orders = c.execute("SELECT * FROM work_orders WHERE issue_id=? ORDER BY created_at DESC", (issue_id,)).fetchall()
        return {"issue": dict(issue), "equipment": dict(equipment), "events": [dict(x) for x in events],
                "readings": [dict(x) for x in readings],
                "triage_runs": [{**dict(t), "result": parse_json(t["result_json"], {})} for t in triages],
                "work_orders": [{**dict(w), "tasks": parse_json(w["tasks_json"]),
                                 "precautions": parse_json(w["precautions_json"]),
                                 "evidence": parse_json(w["evidence_json"])} for w in work_orders]}


@app.post("/api/issues/{issue_id}/triage")
async def run_triage(issue_id: str, x_actor: str = Header(default="demo.technician")):
    tid = str(uuid.uuid4())
    with connect() as c:
        issue = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not issue:
            raise HTTPException(404, "Issue not found")
        equipment = c.execute("SELECT * FROM equipment WHERE id=?", (issue["equipment_id"],)).fetchone()
        events = [dict(x) for x in c.execute("SELECT * FROM events WHERE issue_id=?", (issue_id,)).fetchall()]
        readings = [dict(x) for x in c.execute("SELECT * FROM sensor_readings WHERE issue_id=?", (issue_id,)).fetchall()]
    rule_results = evaluate_readings(readings)
    docs = retrieve_docs(equipment["equipment_type"], issue["description"] + " " + " ".join(e["description"] for e in events))
    baseline = deterministic_triage(dict(issue), dict(equipment), events, readings, rule_results, docs)
    status, provider, model, error = "succeeded", "deterministic_demo", None, None
    result = baseline
    if not DEMO_MODE:
        try:
            result, model = await ollama_triage(baseline, dict(issue), dict(equipment))
            provider = "ollama"
        except Exception as exc:
            status, provider, error, result = "partial", "ollama_failed_demo_baseline", f"{type(exc).__name__}: {str(exc)[:400]}", baseline
            result["ai_failure"] = {"message": "Local model request failed. Showing explicitly labeled deterministic demo baseline.",
                                    "detail": error, "retry_available": True}
            result["mode"] = "deterministic_demo_fallback"
    with connect() as c:
        c.execute("INSERT INTO triage_runs VALUES(?,?,?,?,?,?,?,?,?)",
                  (tid, issue_id, status, provider, model, "triage-v1", error, json.dumps(result), now()))
        for h in result.get("possible_causes", []):
            c.execute("INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?)",
                      (str(uuid.uuid4()), tid, h.get("description", "Unspecified hypothesis"),
                       "unconfirmed", h.get("confidence_label"), json.dumps(h.get("evidence", [])),
                       json.dumps(h.get("contradictions", []))))
        audit(c, x_actor, "triage.completed" if status == "succeeded" else "triage.partial",
              "triage_run", tid, {"issue_id": issue_id, "provider": provider, "status": status})
        # Create an editable draft; this is never auto-approved.
        draft = result["work_order_draft"]
        wid = str(uuid.uuid4())
        c.execute("INSERT INTO work_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (wid, issue_id, 1, draft["title"], draft["priority"], "draft", draft["summary"],
                   json.dumps(draft["tasks"]), json.dumps(draft["precautions"]), json.dumps(draft["evidence"]),
                   now(), now(), x_actor))
        audit(c, x_actor, "work_order.draft_created", "work_order", wid, {"issue_id": issue_id})
        c.execute("UPDATE issues SET status=?, updated_at=? WHERE id=?", ("triaged", now(), issue_id))
    return {"triage_id": tid, "status": status, "provider": provider, "result": result,
            "work_order_id": wid, "work_order_status": "draft"}


@app.get("/api/work-orders")
def list_work_orders():
    with connect() as c:
        rows = c.execute("""SELECT w.*, i.description issue_description, e.asset_tag
                            FROM work_orders w JOIN issues i ON i.id=w.issue_id
                            JOIN equipment e ON e.id=i.equipment_id ORDER BY w.updated_at DESC""").fetchall()
        return [{**dict(r), "tasks": parse_json(r["tasks_json"]), "precautions": parse_json(r["precautions_json"]),
                 "evidence": parse_json(r["evidence_json"])} for r in rows]


@app.get("/api/work-orders/{work_order_id}")
def get_work_order(work_order_id: str):
    with connect() as c:
        row = c.execute("""SELECT w.*, i.description issue_description, e.asset_tag, e.name equipment_name
                           FROM work_orders w JOIN issues i ON i.id=w.issue_id
                           JOIN equipment e ON e.id=i.equipment_id WHERE w.id=?""", (work_order_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Work order not found")
        out = dict(row)
        for field in ("tasks", "precautions", "evidence"):
            out[field] = parse_json(out.pop(field + "_json"))
        out["approvals"] = [dict(a) for a in c.execute("SELECT * FROM approvals WHERE work_order_id=? ORDER BY created_at", (work_order_id,))]
        return out


@app.put("/api/work-orders/{work_order_id}")
def edit_work_order(work_order_id: str, body: WorkOrderEdit, x_actor: str = Header(default="demo.technician"),
                    x_role: str = Header(default="technician")):
    with connect() as c:
        w = c.execute("SELECT * FROM work_orders WHERE id=?", (work_order_id,)).fetchone()
        if not w:
            raise HTTPException(404, "Work order not found")
        if w["status"] not in {"draft", "rejected"}:
            raise HTTPException(409, "Only draft or rejected work orders can be edited")
        if w["revision"] != body.expected_revision:
            raise HTTPException(409, "Stale work-order revision; reload before editing")
        c.execute("""UPDATE work_orders SET revision=revision+1,title=?,priority=?,summary=?,tasks_json=?,
                     precautions_json=?,updated_at=? WHERE id=?""",
                  (body.title, body.priority, body.summary, json.dumps(body.tasks), json.dumps(body.precautions), now(), work_order_id))
        audit(c, x_actor, "work_order.edited", "work_order", work_order_id,
              {"from_revision": w["revision"], "to_revision": w["revision"] + 1})
    return {"id": work_order_id, "status": w["status"], "revision": body.expected_revision + 1}


@app.post("/api/work-orders/{work_order_id}/submit")
def submit_work_order(work_order_id: str, x_actor: str = Header(default="demo.technician")):
    with connect() as c:
        w = c.execute("SELECT * FROM work_orders WHERE id=?", (work_order_id,)).fetchone()
        if not w:
            raise HTTPException(404, "Work order not found")
        if w["status"] not in {"draft", "rejected"}:
            raise HTTPException(409, "Work order cannot be submitted from its current state")
        c.execute("UPDATE work_orders SET status='in_review', updated_at=? WHERE id=?", (now(), work_order_id))
        audit(c, x_actor, "work_order.submitted", "work_order", work_order_id, {})
    return {"id": work_order_id, "status": "in_review"}


@app.post("/api/work-orders/{work_order_id}/decision/{decision}")
def decide_work_order(work_order_id: str, decision: str, body: DecisionIn,
                      x_actor: str = Header(default="demo.approver"), x_role: str = Header(default="approver")):
    if x_role not in {"approver", "admin"}:
        raise HTTPException(403, "Only an approver or admin may decide a work order")
    if decision not in {"approve", "reject"}:
        raise HTTPException(422, "Decision must be approve or reject")
    if decision == "reject" and not (body.reason or "").strip():
        raise HTTPException(422, "A rejection reason is required")
    with connect() as c:
        w = c.execute("SELECT * FROM work_orders WHERE id=?", (work_order_id,)).fetchone()
        if not w:
            raise HTTPException(404, "Work order not found")
        if w["status"] != "in_review":
            raise HTTPException(409, "Only work orders in review can be approved or rejected")
        new_status = "approved" if decision == "approve" else "rejected"
        c.execute("UPDATE work_orders SET status=?, updated_at=? WHERE id=?", (new_status, now(), work_order_id))
        c.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?)",
                  (str(uuid.uuid4()), work_order_id, x_actor, decision, body.reason, now()))
        audit(c, x_actor, f"work_order.{decision}d", "work_order", work_order_id,
              {"reason": body.reason, "status": new_status})
    return {"id": work_order_id, "status": new_status, "equipment_control": False}


@app.post("/api/issues/{issue_id}/findings")
def add_finding(issue_id: str, body: FindingIn, x_actor: str = Header(default="demo.technician")):
    with connect() as c:
        issue = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not issue:
            raise HTTPException(404, "Issue not found")
        equipment = c.execute("SELECT * FROM equipment WHERE id=?", (issue["equipment_id"],)).fetchone()
        rid = str(uuid.uuid4())
        c.execute("INSERT INTO maintenance_records VALUES(?,?,?,?,?,?,?,?,?)",
                  (rid, equipment["id"], issue_id, None, body.finding, body.corrective_action,
                   body.resolution, body.recorded_by, now()))
        audit(c, x_actor, "maintenance.finding_recorded", "maintenance_record", rid, {"issue_id": issue_id})
    return {"id": rid, "status": "recorded"}


@app.get("/api/issues/{issue_id}/audit")
def issue_audit(issue_id: str):
    with connect() as c:
        if not c.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone():
            raise HTTPException(404, "Issue not found")
        rows = c.execute("""SELECT * FROM audit_events WHERE
            (entity_type='issue' AND entity_id=?) OR
            (entity_type='work_order' AND entity_id IN (SELECT id FROM work_orders WHERE issue_id=?)) OR
            (entity_type='triage_run' AND entity_id IN (SELECT id FROM triage_runs WHERE issue_id=?))
            ORDER BY created_at DESC""", (issue_id, issue_id, issue_id)).fetchall()
        return [{**dict(r), "details": parse_json(r["details_json"], {})} for r in rows]


@app.get("/api/documents")
def list_documents():
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT id,title,revision,equipment_type,approved,content_hash FROM documents ORDER BY title")]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
