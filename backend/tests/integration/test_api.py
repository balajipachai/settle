import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings

ACME = "msg_acme_dispute_001"
SECRET = "test-signing-secret"


@pytest.fixture
def client(tmp_path):
    s = Settings(mode="fixture", reasoner="rules", data_dir=tmp_path, retry_base_delay_s=0.0,
                 slack_signing_secret=SECRET, eval_reports_dir=tmp_path / "reports")
    with TestClient(create_app(s, run_sync=True)) as c:
        yield c


def slack_post(client, payload, secret=SECRET, ts=None):
    body = urllib.parse.urlencode({"payload": json.dumps(payload)})
    ts = str(int(ts or time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return client.post("/api/slack/interactions", content=body,
                       headers={"Content-Type": "application/x-www-form-urlencoded",
                                "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})


def start(client, msg=ACME):
    r = client.post("/api/runs", json={"gmail_message_id": msg})
    assert r.status_code == 202
    return r.json()


def test_health_and_inbox(client):
    h = client.get("/api/health").json()
    assert h["mode"] == "fixture" and h["reasoner"] == "rules-v1"
    inbox = client.get("/api/inbox").json()
    assert {m["id"] for m in inbox} == {ACME, "msg_initech_inject_001", "msg_umbrella_001"}


def test_dashboard_approval_flow_and_idempotent_intake(client):
    body = start(client)
    run_id = body["run"]["run_id"]
    assert body["created"] and body["run"]["status"] == "AWAITING_APPROVAL"
    view = client.get(f"/api/runs/{run_id}").json()
    assert view["pending"]["kind"] == "approval"
    assert view["state"]["proposed_action"]["amount_minor"] == 600_000
    assert start(client)["created"] is False  # same Gmail event
    r = client.post(f"/api/runs/{run_id}/resume", json={"decision": "approve", "reviewer_id": "alice",
                                                        "action_hash": view["pending"]["action_hash"]})
    assert r.status_code == 202
    assert client.get(f"/api/runs/{run_id}").json()["run"]["status"] == "RESOLVED"
    world = client.get("/api/demo/world").json()
    assert len(world["stripe"]["credit_notes"]) == 1 and len(world["gmail"]["sent"]) == 1
    assert client.post(f"/api/runs/{run_id}/resume", json={"decision": "approve"}).status_code == 409
    action = client.get(f"/api/runs/{run_id}/action").json()
    assert action["ledger"][0]["status"] == "SUCCEEDED"
    assert any(e["event_type"] == "credit_note_created" for e in client.get(f"/api/runs/{run_id}/events").json())


def test_slack_approval_is_signed_and_action_bound(client):
    run_id = start(client)["run"]["run_id"]
    pending = client.get(f"/api/runs/{run_id}").json()["pending"]
    value = json.dumps({"run_id": run_id, "action_id": pending["action_id"], "action_hash": pending["action_hash"]})
    payload = {"type": "block_actions", "user": {"id": "U123"}, "actions": [{"action_id": "settle_approve", "value": value}]}
    assert slack_post(client, payload, secret="wrong").status_code == 401
    assert slack_post(client, payload, ts=time.time() - 3600).status_code == 401  # replayed request
    stale = {**payload, "actions": [{"action_id": "settle_approve",
                                     "value": json.dumps({"run_id": run_id, "action_id": "other", "action_hash": "x"})}]}
    assert "stale" in slack_post(client, stale).json()["text"]
    assert client.get(f"/api/runs/{run_id}").json()["run"]["status"] == "AWAITING_APPROVAL"
    assert slack_post(client, payload).status_code == 200
    view = client.get(f"/api/runs/{run_id}").json()
    assert view["run"]["status"] == "RESOLVED"
    assert any(a["reviewer_id"] == "slack:U123" and a["decision"] == "approve" for a in view["approvals"])


def test_injected_hubspot_failure_is_repaired(client):
    assert client.post("/api/demo/faults", json={"system": "hubspot", "op": "update_dispute", "status": 503,
                                                 "times": 3}).status_code == 201
    run_id = start(client)["run"]["run_id"]
    h = client.get(f"/api/runs/{run_id}").json()["pending"]["action_hash"]
    client.post(f"/api/runs/{run_id}/resume", json={"decision": "approve", "action_hash": h})
    view = client.get(f"/api/runs/{run_id}").json()
    assert view["run"]["status"] == "RESOLVED"
    assert "PARTIALLY_COMPLETED" in [e["metadata"].get("to") for e in view["events"]]
    assert len(client.get("/api/demo/world").json()["stripe"]["credit_notes"]) == 1


def test_branch_cases_and_reset(client):
    injected = start(client, "msg_initech_inject_001")["run"]
    ambiguous = start(client, "msg_umbrella_001")["run"]
    assert injected["status"] == "BLOCKED" and ambiguous["status"] == "AWAITING_HUMAN_INVESTIGATION"
    assert client.post(f"/api/runs/{injected['run_id']}/resume", json={"decision": "approve"}).status_code == 409
    assert client.post("/api/demo/reset").json() == {"reset": True}
    assert client.get("/api/runs").json() == []


def test_eval_endpoints(client):
    assert client.get("/api/evals/latest").status_code == 404
    assert client.post("/api/evals/run").status_code == 202
    latest = client.get("/api/evals/latest").json()
    assert latest["metrics"]["total_cases"] >= 36 and latest["safety"]["unsafe_financial_actions"] == 0
