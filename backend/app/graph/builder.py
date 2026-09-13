"""The LangGraph state machine (PRD §7.1). Routers are pure functions of state."""

from __future__ import annotations

from collections.abc import Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.domain.policies import PolicyConfig
from app.domain.states import DisputeStatus as S
from app.graph.common import Deps
from app.graph.nodes import approval, execution, investigation, terminal
from app.graph.state import DisputeState

NODES: list[Callable] = [
    investigation.intake_email, investigation.extract_claim, investigation.resolve_entities,
    investigation.retrieve_evidence, investigation.reconcile_financials, investigation.adjudicate_dispute,
    approval.prepare_resolution, approval.validate_policy, approval.request_approval, approval.approval_gate,
    approval.revalidate_action,
    execution.execute_financial_action, execution.update_crm, execution.verify_state, execution.repair_or_escalate,
    execution.escalate_for_repair, execution.await_operator, execution.send_customer_reply,
    terminal.human_investigation, terminal.block, terminal.close_rejected, terminal.close_no_action, terminal.escalate,
]


def route_after_extract(state: dict) -> str:
    return "resolve_entities" if state.get("claim") else "human_investigation"


def route_after_identity(state: dict) -> str:
    return "retrieve_evidence" if state["identity"].status == "RESOLVED" else "human_investigation"


def make_route_after_adjudication(policy: PolicyConfig) -> Callable[[dict], str]:
    def route_after_adjudication(state: dict) -> str:
        adj, recon = state["adjudication"], state["reconciliation"]
        if recon.already_credited:
            return "close_no_action"
        if (adj.resolution == "CREDIT" and adj.validity == "VALID" and adj.classification == "duplicate_charge"
                and recon.outcome == "DUPLICATE_CANDIDATE" and recon.credit_amount_minor
                and adj.model_confidence >= policy.min_model_confidence):
            return "prepare_resolution"
        return "human_investigation"

    return route_after_adjudication


def route_after_policy(state: dict) -> str:
    return "request_approval" if state["policy_result"].passed else "block"


def route_after_approval(state: dict) -> str:
    ad = state.get("approval")
    if ad is None:
        return "approval_gate"
    if not ad.valid:
        # Tampering is an integrity incident; a stale/unauthorized click just waits for a valid approval.
        return "human_investigation" if ad.invalidation_kind == "tampered" else "approval_gate"
    return {"approve": "execute_financial_action", "reject": "close_rejected", "edit": "revalidate_action"}[ad.decision]


def route_after_revalidate(state: dict) -> str:
    if state.get("status") == S.POLICY_REVIEW:
        return "validate_policy"
    return "human_investigation" if state.get("outcome_reason") else "approval_gate"


def route_after_execute(state: dict) -> str:
    ex = state.get("execution")
    return "update_crm" if ex and ex.financial_action_status == "SUCCEEDED" else END


def route_after_crm(state: dict) -> str:
    return "verify_state" if state["execution"].hubspot_updated else "repair_or_escalate"


def make_route_after_verify(max_repair_cycles: int) -> Callable[[dict], str]:
    def route_after_verify(state: dict) -> str:
        v = state["verification"]
        if v.status == "VERIFIED":
            return "send_customer_reply"
        if not v.stripe_ok:
            return "escalate"
        if int(state.get("repair_cycles") or 0) < max_repair_cycles:
            return "repair_or_escalate"
        return "escalate_for_repair"

    return route_after_verify


def route_after_repair(state: dict) -> str:
    return "verify_state" if state["execution"].hubspot_updated and state.get("status") == S.REPAIRING else "escalate_for_repair"


def route_after_operator(state: dict) -> str:
    return "repair_or_escalate" if state.get("outcome_reason") is None else END


def _bind(fn: Callable, deps: Deps) -> Callable[[dict], dict]:
    def run(state: dict) -> dict:
        return fn(state, deps)

    run.__name__ = fn.node_name
    return run


def build_state_graph(deps: Deps) -> StateGraph:
    g = StateGraph(DisputeState)
    for fn in NODES:
        g.add_node(fn.node_name, _bind(fn, deps))

    g.add_edge(START, "intake_email")
    g.add_edge("intake_email", "extract_claim")
    g.add_conditional_edges("extract_claim", route_after_extract, ["resolve_entities", "human_investigation"])
    g.add_conditional_edges("resolve_entities", route_after_identity, ["retrieve_evidence", "human_investigation"])
    g.add_edge("retrieve_evidence", "reconcile_financials")
    g.add_edge("reconcile_financials", "adjudicate_dispute")
    g.add_conditional_edges("adjudicate_dispute", make_route_after_adjudication(deps.policy),
                            ["prepare_resolution", "close_no_action", "human_investigation"])
    g.add_edge("prepare_resolution", "validate_policy")
    g.add_conditional_edges("validate_policy", route_after_policy, ["request_approval", "block"])
    g.add_edge("request_approval", "approval_gate")
    g.add_conditional_edges("approval_gate", route_after_approval,
                            ["execute_financial_action", "close_rejected", "revalidate_action", "approval_gate",
                             "human_investigation"])
    g.add_conditional_edges("revalidate_action", route_after_revalidate,
                            ["validate_policy", "approval_gate", "human_investigation"])
    g.add_conditional_edges("execute_financial_action", route_after_execute, ["update_crm", END])
    g.add_conditional_edges("update_crm", route_after_crm, ["verify_state", "repair_or_escalate"])
    g.add_conditional_edges("verify_state", make_route_after_verify(deps.settings.max_repair_cycles),
                            ["send_customer_reply", "repair_or_escalate", "escalate", "escalate_for_repair"])
    g.add_conditional_edges("repair_or_escalate", route_after_repair, ["verify_state", "escalate_for_repair"])
    g.add_edge("escalate_for_repair", "await_operator")
    g.add_conditional_edges("await_operator", route_after_operator, ["repair_or_escalate", END])
    g.add_edge("send_customer_reply", END)
    for name in ("human_investigation", "block", "close_rejected", "close_no_action", "escalate"):
        g.add_edge(name, END)
    return g


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver):
    return build_state_graph(deps).compile(checkpointer=checkpointer)
