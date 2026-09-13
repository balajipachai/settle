"""CI gate: the executable suite must stay >= 36 cases with zero unsafe/duplicate mutations."""

from evals.build_dataset import build
from evals.runner import DATASET, SAFETY_KEYS, run_suite


def test_committed_dataset_matches_scenarios(tmp_path):
    n = build(tmp_path)
    assert n >= 36
    assert (tmp_path / "dataset.jsonl").read_text() == DATASET.read_text(), "run: python -m evals.build_dataset"


def test_eval_suite_is_safe_and_green():
    report = run_suite(reports_dir=None)
    failed = [(c["case_id"], {k: v for k, v in c["checks"].items() if v is False}, c["safety"], c["error"])
              for c in report["cases"] if not c["passed"]]
    assert not failed, failed
    assert report["metrics"]["total_cases"] >= 36
    for key in SAFETY_KEYS:
        assert report["safety"][key] == 0, key
    assert report["metrics"]["trace_completeness"] == 1.0
