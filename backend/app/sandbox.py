"""A fully wired Settle instance over a fixture world. Shared by tests, the eval runner and the demo."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.domain.policies import load_policy
from app.graph.common import Deps
from app.graph.runner import RunService
from app.integrations.factory import build_fixture_gateways
from app.integrations.fixtures import FixtureWorld
from app.persistence.checkpointing import make_checkpointer
from app.persistence.repository import Repository
from app.services.reasoner import Reasoner, RuleBasedReasoner


@dataclass
class Sandbox:
    settings: Settings
    repo: Repository
    world: FixtureWorld
    deps: Deps
    service: RunService

    def run_email(self, email_id: str, *, force_new: bool = False) -> str:
        run, created = self.service.start_run(email_id, force_new=force_new)
        if created:
            self.service.execute(run["run_id"])
        return run["run_id"]

    def status(self, run_id: str) -> str:
        return self.repo.get_run(run_id)["status"]

    def state(self, run_id: str) -> dict:
        return self.service.state(run_id)

    def pending(self, run_id: str) -> dict | None:
        return self.service.pending_interrupt(run_id)

    def decide(self, run_id: str, decision: str, *, reviewer_id: str = "finance.reviewer", action_hash: str | None = None,
               edits: dict[str, Any] | None = None, note: str | None = None, channel: str = "api") -> None:
        pending = self.pending(run_id) or {}
        self.service.resume(run_id, {
            "decision": decision, "reviewer_id": reviewer_id, "channel": channel, "note": note, "edits": edits,
            "action_hash": action_hash if action_hash is not None else pending.get("action_hash"),
        })

    def approve(self, run_id: str, **kw: Any) -> None:
        self.decide(run_id, "approve", **kw)

    def close(self) -> None:
        self.repo.engine.dispose()


def build_sandbox(world: dict | FixtureWorld, data_dir: Path, *, reasoner: Reasoner | None = None,
                  raise_errors: bool = True, **settings_overrides: Any) -> Sandbox:
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(mode="fixture", reasoner="rules", data_dir=data_dir, retry_base_delay_s=0.0,
                        **settings_overrides)
    w = world if isinstance(world, FixtureWorld) else FixtureWorld(world)
    repo = Repository.from_url(settings.resolved_database_url)
    deps = Deps(settings=settings, repo=repo, gateways=build_fixture_gateways(w), reasoner=reasoner or RuleBasedReasoner(),
                policy=load_policy(settings.policy_path), sleep=lambda _s: None)
    service = RunService(deps, make_checkpointer(settings.checkpoint_path), raise_errors=raise_errors)
    return Sandbox(settings=settings, repo=repo, world=w, deps=deps, service=service)
