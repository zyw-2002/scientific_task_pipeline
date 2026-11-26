"""Independent T0 scoring for Solver trajectories.

T0 records observable execution evidence. It is deliberately separate from Q0
task construction and from the scientific metric. Unknown evidence is kept as
unknown rather than converted to zero; the report carries coverage so partial
cross-benchmark comparisons cannot masquerade as a complete score.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def _path(value: Any) -> Path | None:
    if isinstance(value, Mapping):
        value = value.get("path")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def _artifact(result: Mapping[str, Any], name: str) -> Path | None:
    return _path((result.get("artifacts") or {}).get(name))


def score_trajectory(result: Mapping[str, Any]) -> dict[str, Any]:
    """Score one completed or attempted Stage3 result using local evidence.

    The score is an evidence score, never a semantic claim about scientific
    correctness.  Missing evidence remains visible in ``findings`` and is not
    converted into a scientific zero.
    """
    status = str(result.get("status", ""))
    solver = result.get("solver") if isinstance(result.get("solver"), Mapping) else {}
    judge = result.get("judge") if isinstance(result.get("judge"), Mapping) else {}
    rules = result.get("rules") if isinstance(result.get("rules"), Mapping) else {}
    reporting = judge.get("reporting") if isinstance(judge.get("reporting"), Mapping) else {}
    artifacts = {name: _artifact(result, name) for name in
                 ("input_manifest", "run_manifest", "run_export", "trajectory", "timeline", "bundle")}
    findings: list[str] = []
    dimensions: dict[str, dict[str, Any]] = {}

    def add(name: str, points: float | None, maximum: float, evidence: list[str],
            reason: str | None = None, *, status: str = "observed"):
        dimensions[name] = {"score": points, "max": maximum, "evidence": evidence,
                            "status": status}
        if reason:
            findings.append(reason)

    input_evidence = [key for key in ("input_manifest", "run_manifest") if artifacts[key]]
    add("formal_input_use", 20.0 if artifacts["input_manifest"] and artifacts["run_manifest"] else None,
        20.0, input_evidence,
        None if artifacts["input_manifest"] and artifacts["run_manifest"] else
        "missing frozen input/run manifest evidence", status=("observed" if artifacts["input_manifest"] and artifacts["run_manifest"] else "unknown"))

    invalidity = reporting.get("invalidity_type") or judge.get("failure_class")
    substitution = result.get("synthetic_substitution") or result.get("placeholder_or_degenerate")
    if invalidity == "synthetic_substitution" or substitution:
        add("data_integrity", 0.0, 20.0, ["synthetic_substitution"],
            "trajectory indicates synthetic or substituted input")
    elif invalidity == "placeholder_or_degenerate":
        add("data_integrity", 4.0, 20.0, ["placeholder_or_degenerate"],
            "trajectory indicates placeholder or degenerate input")
    else:
        add("data_integrity", None, 20.0, [],
            "no positive input-identity evidence was exported", status="unknown")

    contract_ok = rules.get("status") in {"pass", "legacy_not_recorded"} and bool(result.get("score_attempt_used"))
    contract_checked = bool(rules) or bool(result.get("score_attempt_used"))
    add("contract_adherence", 15.0 if contract_ok else (0.0 if contract_checked else None), 15.0,
        (["rules=pass", "SCORE_ATTEMPT"] if contract_ok else []),
        None if contract_ok else "missing passing output contract or SCORE_ATTEMPT evidence",
        status=("observed" if contract_checked else "unknown"))

    execution_ok = solver.get("status") == "evidence_exported" and artifacts["run_export"] and artifacts["trajectory"] and artifacts["timeline"]
    add("real_execution", 20.0 if execution_ok else 0.0, 20.0,
        (["evidence_exported", "run_export", "trajectory", "timeline"] if execution_ok else []),
        None if execution_ok else "trajectory lacks complete execution/export evidence", status="observed")

    validation_ok = bool(result.get("scientific_score")) and bool(artifacts["bundle"])
    recovery = any(key in result for key in ("recovery", "retry_count", "recovered_after_error"))
    validation_points = 15.0 if validation_ok else 0.0
    validation_evidence = ["scientific_score", "frozen evidence bundle"] if validation_ok else []
    if recovery:
        validation_evidence.append("recovery metadata")
    add("validation_and_recovery", validation_points if validation_ok else None, 15.0, validation_evidence,
        None if validation_ok else "missing independent validation or frozen evidence bundle",
        status=("observed" if validation_ok else "unknown"))

    claim_ok = (status in {"complete", "judge_not_configured"} and
                reporting.get("decision_status") in {"accepted", "legacy_unverified", None} and
                bool(artifacts["run_export"]))
    add("claim_support", 10.0 if claim_ok else None, 10.0,
        (["status and exported evidence agree"] if claim_ok else []),
        None if claim_ok else "completion claim was not independently accepted",
        status=("observed" if claim_ok else "unknown"))

    observed = [item for item in dimensions.values() if item["status"] == "observed" and item["score"] is not None]
    score = sum(item["score"] for item in observed)
    observed_max = sum(item["max"] for item in observed)
    complete = len(observed) == len(dimensions)
    evidence_count = sum(bool(item["evidence"]) for item in dimensions.values())
    return {
        "schema_version": "pipeline-trajectory-quality-v1",
        "score_kind": "trajectory",
        "score": round(score, 4) if complete else None,
        "observed_score": round(score, 4),
        "observed_max_score": round(observed_max, 4),
        "coverage": observed_max / 100.0,
        "complete": complete,
        "unknown_dimensions": [name for name, item in dimensions.items() if item["status"] == "unknown"],
        "max_score": 100.0,
        "dimensions": dimensions,
        "evidence_count": evidence_count,
        "findings": findings,
        "assessed": bool(artifacts["run_manifest"] or artifacts["run_export"]),
        "policy": "observable execution evidence only; independent of Q0 task quality and scientific metric",
    }


def score_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Score a JSON result row, using a precomputed T0 when supplied."""
    existing = row.get("trajectory_score", row.get("t0"))
    if isinstance(existing, Mapping):
        value = existing.get("score", existing.get("total"))
        return {"task_id": row.get("task_id") or row.get("sample_id"), "score": value,
                "scoreable": value is not None, "trajectory": dict(existing)}
    report = score_trajectory(row)
    return {"task_id": row.get("task_id") or row.get("sample_id"), "score": report["score"],
            "scoreable": report["assessed"], "trajectory": report}


ERROR_TYPES = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "ENV")


def summarize_semantic_review(review: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the existing analysis_tractory F1-F8/ENV review output.

    The historical reviewer is explicitly a model proposal, so this summary
    stays separate from the deterministic T0 number unless a caller records a
    human verification status.  Exact quote and evidence status are retained
    because they are useful signals when improving prompts and recovery logic.
    """
    issues = review.get("issues") if isinstance(review.get("issues"), list) else []
    counts = {kind: 0 for kind in ERROR_TYPES}
    exact = 0
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        for kind in issue.get("types", []):
            if kind in counts:
                counts[kind] += 1
        if issue.get("all_quotes_exact") is True:
            exact += 1
    status = str(review.get("review_status", review.get("semantic_status", "not_reviewed")))
    return {
        "schema_version": "pipeline-semantic-trajectory-review-v1",
        "review_status": status,
        "issue_count": len(issues),
        "issue_type_counts": counts,
        "issues_with_exact_quotes": exact,
        "evidence_policy": "absence is not inability; environment failures are separate; exact quotes are required",
        "contributes_to_t0": status in {"human_verified", "human_reviewed"},
    }


def score_atif_trajectory(trajectory: Mapping[str, Any], outcome: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Score an external ATIF trajectory without assuming its task schema.

    This adapter is intentionally conservative.  It reports structural
    evidence that can be compared across NatureBench/OriginBench-style runs;
    task-contract checks remain ``unknown`` until the native Stage3 result is
    available.  It is therefore suitable for benchmark audits, not for
    rewriting scientific rewards.
    """
    steps = trajectory.get("steps") if isinstance(trajectory, Mapping) else None
    steps = steps if isinstance(steps, list) else []
    agent_steps = [s for s in steps if isinstance(s, Mapping) and s.get("source", s.get("role")) in {"agent", "assistant"}]
    calls = [c for s in agent_steps for c in (s.get("tool_calls") or []) if isinstance(c, Mapping)]
    text = "\n".join(str(s.get("message", "")) + "\n" + str(s.get("observation", "")) for s in agent_steps).lower()
    tool_text = " ".join(str(c.get("arguments", "")) for c in calls).lower()
    errors_observed = any(token in text for token in ("traceback", "error", "failed", "exception"))
    later_progress = errors_observed and any(token in text for token in ("retry", "fix", "re-run", "rerun", "validated", "success"))
    output_claim = any(token in text for token in ("submit", "completed", "final", "saved", "ready"))
    validation = any(token in text + tool_text for token in ("validate", "evaluation", "evaluator", "test", "checksum", "sha256", "metric"))
    input_read = any(token in text + tool_text for token in ("read", "load", "open", "ls", "find", "cat", "data"))
    dimensions = {
        "formal_input_use": {"score": 12.0 if input_read else None, "max": 20.0,
                             "evidence": ["input/read activity"] if input_read else [],
                             "status": "observed" if input_read else "unknown"},
        "data_integrity": {"score": None, "max": 20.0,
                           "evidence": [], "status": "unknown"},
        "contract_adherence": {"score": None, "max": 15.0, "evidence": [], "status": "unknown"},
        "real_execution": {"score": 20.0 if agent_steps else 0.0, "max": 20.0,
                            "evidence": [f"{len(agent_steps)} agent steps", f"{len(calls)} tool calls"] if agent_steps else [],
                            "status": "observed"},
        "validation_and_recovery": {"score": 15.0 if validation else None, "max": 15.0,
                                    "evidence": (["validation activity"] if validation else []) + (["recovery"] if later_progress else []),
                                    "status": "observed" if validation else "unknown"},
        "claim_support": {"score": 10.0 if output_claim and (validation or outcome) else None, "max": 10.0,
                          "evidence": (["completion/output claim"] if output_claim else []),
                          "status": "observed" if output_claim and (validation or outcome) else "unknown"},
    }
    observed = [x for x in dimensions.values() if x["status"] == "observed" and x["score"] is not None]
    observed_score = sum(x["score"] for x in observed)
    observed_max = sum(x["max"] for x in observed)
    complete = len(observed) == len(dimensions)
    return {"schema_version": "pipeline-trajectory-quality-v1", "score_kind": "trajectory",
            "source": "atif_external",
            "max_score": 100.0, "assessed": bool(agent_steps),
            "score": round(observed_score, 4) if complete else None,
            "observed_score": round(observed_score, 4), "observed_max_score": round(observed_max, 4),
            "coverage": observed_max / 100.0, "complete": complete,
            "unknown_dimensions": [k for k, v in dimensions.items() if v.get("status") == "unknown"],
            "dimensions": dimensions,
            "evidence_count": sum(bool(x["evidence"]) for x in dimensions.values()),
            "policy": "structural cross-benchmark audit; unknown task-contract dimensions are not failures"}

