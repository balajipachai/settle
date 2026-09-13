"""FastAPI backend (PRD §26). The UI never calls vendor APIs directly.

Run: uv run uvicorn app.api.main:create_app --factory --port 8000
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.config import REPO_DIR, Settings, get_settings
from app.domain.policies import load_policy
from app.graph.common import Deps
from app.graph.runner import RunService, jsonable
from app.integrations.factory import build_gateways
from app.integrations.fixtures import FaultSpec, FixtureWorld
from app.integrations.slack import verify_slack_signature
from app.logging import configure_logging, get_logger, log_event
from app.persistence.checkpointing import make_checkpointer
from app.persistence.repository import Repository
from app.services.reasoner import build_reasoner

FRONTEND_DIR = REPO_DIR / "frontend"
_log = get_logger("api")
_RESUME = {"approval": {"approve", "reject", "edit"}, "repair_required": {"retry_repair", "close"}}


class StartRunRequest(BaseModel):
    gmail_message_id: str = Field(min_length=1, max_length=256)
    force_new: bool = False


class ResumeRequest(BaseModel):
    decision: Literal["approve", "reject", "edit", "retry_repair", "close"]
    reviewer_id: str = Field("dashboard.reviewer", min_length=1, max_length=128)
    action_hash: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=1000)
    edits: dict[str, Any] | None = None


class Runtime:
    def __init__(self, settings: Settings, *, run_sync: bool) -> None:
        self.settings = settings
        self.run_sync = run_sync
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="settle-run")
        self.world = FixtureWorld.from_file(settings.fixture_world_path) if settings.mode == "fixture" else None
        self.repo = Repository.from_url(settings.resolved_database_url)
        self.gateways = build_gateways(settings, self.world)
        self.reasoner = build_reasoner(settings)
        self.deps = Deps(settings=settings, repo=self.repo, gateways=self.gateways, reasoner=self.reasoner,
                         policy=load_policy(settings.policy_path))
        url = settings.database_url or ""
        self.checkpointer = make_checkpointer(url.replace("+psycopg", "") if url.startswith("postgres") else settings.checkpoint_path)
        self.service = RunService(self.deps, self.checkpointer)
        self.eval_lock = threading.Lock()

    def submit(self, fn, *args) -> None:
        if self.run_sync:
            fn(*args)
            return

        def safe():
            try:
                fn(*args)
            except Exception as exc:  # already audited by RunService; keep the worker alive
                log_event(_log, "background_task_failed", error=f"{type(exc).__name__}: {exc}")

        self.executor.submit(safe)

    def reset_demo(self) -> None:
        fresh = FixtureWorld.from_file(self.settings.fixture_world_path)
        with self.world.lock:
            self.world.data, self.world.faults = fresh.data, fresh.faults
            self.world.calls.clear()
        self.repo.reset()
        conn = getattr(self.checkpointer, "conn", None)
        if conn is not None:
            for table in ("checkpoints", "writes"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except Exception:  # noqa: BLE001 - table may not exist yet
                    pass
            conn.commit()

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.gateways.close()
        self.repo.engine.dispose()


def create_app(settings: Settings | None = None, *, run_sync: bool = False) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()
    rt = Runtime(settings, run_sync=run_sync)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        rt.close()

    app = FastAPI(title="Settle", version=__version__, lifespan=lifespan)
    app.state.rt = rt

    def require_run(run_id: str) -> dict:
        run = rt.repo.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"Unknown run {run_id}")
        return run

    def require_fixture() -> FixtureWorld:
        if rt.world is None:
            raise HTTPException(400, "Only available in SETTLE_MODE=fixture")
        return rt.world

    # ------------------------------------------------------------------ health / inbox
    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok", "version": __version__, "mode": settings.mode, "reasoner": rt.reasoner.name,
            "policy_version": rt.deps.policy.version,
            "integrations": {s: settings.mode for s in ("gmail", "stripe", "hubspot", "slack")},
            "approval_channel": "slack+dashboard" if settings.slack_signing_secret else "dashboard",
        }

    @app.get("/api/inbox")
    def inbox() -> list[dict]:
        emails = rt.gateways.gmail.list_inbox(limit=25)
        out = []
        for m in emails:
            run = rt.repo.find_original_run_for_source(m.id)
            out.append({"id": m.id, "thread_id": m.thread_id, "from_email": m.from_email, "from_name": m.from_name,
                        "subject": m.subject, "received_at": m.received_at.isoformat(), "snippet": " ".join(m.body.split())[:160],
                        "run_id": run["run_id"] if run else None, "run_status": run["status"] if run else None})
        return out

    # ------------------------------------------------------------------ runs
    @app.post("/api/runs", status_code=202)
    def start_run(req: StartRunRequest) -> dict:
        run, created = rt.service.start_run(req.gmail_message_id, force_new=req.force_new)
        if created:
            rt.submit(rt.service.execute, run["run_id"])
        return {"run": jsonable(rt.repo.get_run(run["run_id"])), "created": created}

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        return jsonable(rt.repo.list_runs(100))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        require_run(run_id)
        return rt.service.view(run_id)

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str) -> list[dict]:
        require_run(run_id)
        return jsonable(rt.repo.list_audit(run_id))

    @app.get("/api/runs/{run_id}/evidence")
    def run_evidence(run_id: str) -> dict:
        require_run(run_id)
        st = rt.service.state(run_id)
        return jsonable({k: st.get(k) for k in ("claim", "identity", "evidence", "reconciliation", "adjudication")})

    @app.get("/api/runs/{run_id}/action")
    def run_action(run_id: str) -> dict:
        require_run(run_id)
        st = rt.service.state(run_id)
        return jsonable({
            "proposed_action": st.get("proposed_action"), "policy_result": st.get("policy_result"),
            "approval": st.get("approval"), "execution": st.get("execution"), "verification": st.get("verification"),
            "ledger": rt.repo.list_financial_actions(run_id), "approvals": rt.repo.list_approvals(run_id),
        })

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, req: ResumeRequest) -> dict:
        require_run(run_id)
        pending = rt.service.pending_interrupt(run_id)
        if not pending:
            raise HTTPException(409, "Run is not waiting for input.")
        if req.decision not in _RESUME.get(pending.get("kind"), set()):
            raise HTTPException(400, f"'{req.decision}' is not valid while the run awaits {pending.get('kind')}.")
        payload = {"decision": req.decision, "reviewer_id": req.reviewer_id, "operator_id": req.reviewer_id,
                   "action_hash": req.action_hash, "note": req.note, "edits": req.edits, "channel": "dashboard"}
        rt.submit(rt.service.resume, run_id, payload)
        return {"accepted": True, "run_id": run_id, "pending_kind": pending.get("kind")}

    @app.post("/api/runs/{run_id}/retry", status_code=202)
    def retry(run_id: str) -> dict:
        run = require_run(run_id)
        if run["status"] != "FAILED":
            raise HTTPException(409, "Only FAILED runs can be retried from their last checkpoint.")
        rt.submit(rt.service.retry, run_id)
        return {"accepted": True, "run_id": run_id}

    # ------------------------------------------------------------------ Slack
    @app.post("/api/slack/interactions")
    async def slack_interactions(request: Request) -> dict:
        secret = settings.slack_signing_secret
        if not secret:
            raise HTTPException(503, "SLACK_SIGNING_SECRET is not configured.")
        body = await request.body()
        if not verify_slack_signature(secret, request.headers.get("X-Slack-Request-Timestamp", ""), body,
                                      request.headers.get("X-Slack-Signature", "")):
            raise HTTPException(401, "Invalid Slack signature.")
        try:
            payload = json.loads(urllib.parse.parse_qs(body.decode())["payload"][0])
            action = (payload.get("actions") or [{}])[0]
            ref = json.loads(action.get("value") or "{}")
        except (KeyError, IndexError, ValueError) as exc:
            raise HTTPException(400, "Malformed Slack payload.") from exc
        decision = {"settle_approve": "approve", "settle_reject": "reject"}.get(action.get("action_id"))
        run_id = ref.get("run_id")
        if not decision or not run_id or rt.repo.get_run(run_id) is None:
            return {"response_type": "ephemeral", "text": "Unrecognised action; nothing was executed."}
        pending = rt.service.pending_interrupt(run_id)
        # Server-side state is the source of truth; the button only names which action it was shown for.
        if not pending or pending.get("kind") != "approval" or pending.get("action_id") != ref.get("action_id"):
            return {"response_type": "ephemeral", "replace_original": False,
                    "text": "This approval card is stale or already handled; nothing was executed."}
        reviewer = f"slack:{(payload.get('user') or {}).get('id', 'unknown')}"
        rt.submit(rt.service.resume, run_id, {"decision": decision, "reviewer_id": reviewer, "channel": "slack",
                                              "action_hash": ref.get("action_hash")})
        return {"response_type": "ephemeral", "text": f"Settle recorded your {decision} for run {run_id}."}

    # ------------------------------------------------------------------ evals
    @app.get("/api/evals/latest")
    def evals_latest() -> dict:
        path = settings.eval_reports_dir / "latest.json"
        if not path.exists():
            raise HTTPException(404, "No evaluation report yet. POST /api/evals/run or `python -m evals.runner`.")
        return json.loads(path.read_text())

    @app.post("/api/evals/run", status_code=202)
    def evals_run() -> dict:
        if not rt.eval_lock.acquire(blocking=False):
            raise HTTPException(409, "An evaluation run is already in progress.")

        def job():
            from evals.runner import run_suite  # local import keeps API startup light

            try:
                run_suite(repo=rt.repo, reports_dir=settings.eval_reports_dir)
            finally:
                rt.eval_lock.release()

        rt.submit(job)
        return {"accepted": True}

    # ------------------------------------------------------------------ demo controls (fixture mode only)
    @app.get("/api/demo/world")
    def demo_world() -> dict:
        snap = require_fixture().snapshot()
        return {"stripe": {"invoices": snap["stripe"]["invoices"], "credit_notes": snap["stripe"]["credit_notes"]},
                "hubspot": {"companies": snap["hubspot"]["companies"]}, "gmail": {"sent": snap["gmail"]["sent"]},
                "slack": snap["slack"], "faults": snap["faults"]}

    @app.post("/api/demo/faults", status_code=201)
    def inject_fault(fault: FaultSpec) -> dict:
        require_fixture().inject(fault)
        return {"faults": [f.model_dump() for f in rt.world.faults]}

    @app.delete("/api/demo/faults")
    def clear_faults() -> dict:
        require_fixture().clear_faults()
        return {"faults": []}

    @app.post("/api/demo/reset")
    def reset() -> dict:
        require_fixture()
        rt.reset_demo()
        return {"reset": True}

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")
    return app
