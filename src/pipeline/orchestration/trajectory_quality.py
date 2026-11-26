"""Independent T0 scoring for Solver trajectories.

T0 records observable execution evidence. It is deliberately separate from Q0
task construction and from the scientific metric. Unknown evidence is kept as
unknown rather than converted to zero; the report carries coverage so partial
cross-benchmark comparisons cannot masquerade as a complete score.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


# T0-v1 has two distinct layers.  Mechanical execution evidence is 60 points;
# semantic process review is 40 points.  F1--F8 are positive process criteria
# here, while the historical labels remain available as issue diagnostics.
T_EVIDENCE_WEIGHTS = {
    "formal_input_use": 12.0,
    "data_integrity": 12.0,
    "contract_adherence": 9.0,
    "real_execution": 12.0,
    "validation_and_recovery": 9.0,
    "claim_support": 6.0,
}
T_PROCESS_WEIGHTS = {
    "goal_constraint_adherence": 5.0,       # F1
    "strategy_choice": 5.0,                # F2
    "tool_interface_use": 5.0,             # F3
    "data_resource_identity": 5.0,         # F4
    "implementation_computation": 5.0,     # F5
    "observation_evidence": 5.0,           # F6
    "context_state_maintenance": 5.0,      # F7
    "process_control_termination": 5.0,    # F8
}
T0_WEIGHTS = {**T_EVIDENCE_WEIGHTS, **T_PROCESS_WEIGHTS}


def _path(value: Any) -> Path | None:
    expected = None
    if isinstance(value, Mapping):
        expected = value.get("sha256")
        value = value.get("path")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_file():
        return None
    if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        return None
    return path


def _artifact(result: Mapping[str, Any], name: str) -> Path | None:
    reference = (result.get("artifacts") or {}).get(name)
    if name == 'bundle' and isinstance(reference, Mapping):
        # Native Stage3 bundles are directories, not files. Their manifest is
        # frozen and hash-bound by the workflow before final judging.
        path = Path(reference.get('path', ''))
        manifest = path/'manifest.json'
        expected = reference.get('manifest_sha256')
        if expected and manifest.is_file() and hashlib.sha256(manifest.read_bytes()).hexdigest() == expected:
            return path
        return None
    return _path(reference)


def _native_input_identity(artifacts, solver, judge):
    """Verify existing framework receipts; do not execute task-specific code."""
    path, run_path = artifacts['input_manifest'], artifacts['run_manifest']
    if path is None or run_path is None or solver.get('status') != 'evidence_exported':
        return False, False
    try:
        manifest, run = json.loads(path.read_bytes()), json.loads(run_path.read_bytes())
        bound = (run.get('effective_input_sha256') == hashlib.sha256(path.read_bytes()).hexdigest()
                 and Path(run.get('effective_input', '')).resolve() == path.resolve()
                 and run.get('task_id') == manifest.get('task_id')
                 and run.get('diagnostic_only') is False)
        if not bound:
            return False, False
        resources = manifest.get('formal_resources')
        if (manifest.get('formal_resources_verified') is not True or not isinstance(resources, list)
                or not resources or judge.get('is_valid') is not True):
            return True, False
        from pipeline.stage2_construction.contracts.resource_contract import digest
        ids = set()
        for row in resources:
            resource = Path(row['path'])
            if (not row.get('id') or row['id'] in ids or not resource.exists()
                    or resource.is_symlink() or digest(resource) != row.get('sha256')):
                return True, False
            ids.add(row['id'])
        return True, True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False, False


def _bound_semantic_scores(review: Mapping[str, Any], trajectory: Path | None) -> dict[str, float]:
    """Require current-trajectory identity and exact step quotes for each score."""
    if trajectory is None:
        return {}
    try:
        raw = trajectory.read_bytes()
        if review.get("trajectory_sha256") != hashlib.sha256(raw).hexdigest():
            return {}
        parsed = json.loads(raw)
        steps = {str(row.get("step_id")): row for row in parsed.get("steps", []) if isinstance(row, Mapping)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for item in value.values(): yield from strings(item)
        elif isinstance(value, list):
            for item in value: yield from strings(item)

    accepted = {}
    criteria = review.get("criteria", {})
    if not isinstance(criteria, Mapping):
        return accepted
    for name in T_PROCESS_WEIGHTS:
        row = criteria.get(name)
        if not isinstance(row, Mapping): continue
        score, evidence = row.get("score"), row.get("evidence")
        if (type(score) not in (int, float) or not 1 <= score <= 5
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()):
            continue
        if not isinstance(evidence, list) or not evidence:
            continue
        exact = True
        for ref in evidence:
            if not isinstance(ref, Mapping): exact = False; break
            step = steps.get(str(ref.get("step_id")))
            quote = ref.get("quote")
            if (step is None or not isinstance(quote, str) or len(quote.strip()) < 10
                    or not any(quote in text for text in strings(step))):
                exact = False; break
        if exact: accepted[name] = float(score)
    return accepted


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

    input_bound, identity_verified = _native_input_identity(artifacts, solver, judge)
    input_evidence = ["matching native input/run identities"] if input_bound else []
    add("formal_input_use", T_EVIDENCE_WEIGHTS["formal_input_use"] if input_bound else None,
        T_EVIDENCE_WEIGHTS["formal_input_use"], input_evidence,
        None if input_bound else
        "missing frozen input/run manifest evidence", status=("observed" if input_bound else "unknown"))

    invalidity = reporting.get("invalidity_type") or judge.get("failure_class")
    substitution = result.get("synthetic_substitution") or result.get("placeholder_or_degenerate")
    if invalidity == "synthetic_substitution" or substitution:
        add("data_integrity", 0.0, T_EVIDENCE_WEIGHTS["data_integrity"], ["synthetic_substitution"],
            "trajectory indicates synthetic or substituted input")
    elif invalidity == "placeholder_or_degenerate":
        add("data_integrity", 1.0, T_EVIDENCE_WEIGHTS["data_integrity"], ["placeholder_or_degenerate"],
            "trajectory indicates placeholder or degenerate input")
    elif identity_verified:
        add("data_integrity", T_EVIDENCE_WEIGHTS["data_integrity"], T_EVIDENCE_WEIGHTS["data_integrity"],
            ["formal resource SHA-256 identities verified", "independent Judge is_valid=true"])
    else:
        add("data_integrity", None, T_EVIDENCE_WEIGHTS["data_integrity"], [],
            "no positive input-identity evidence was exported", status="unknown")

    contract_ok = rules.get("status") in {"pass", "legacy_not_recorded"} and bool(result.get("score_attempt_used"))
    contract_checked = bool(rules) or bool(result.get("score_attempt_used"))
    add("contract_adherence", T_EVIDENCE_WEIGHTS["contract_adherence"] if contract_ok else (0.0 if contract_checked else None), T_EVIDENCE_WEIGHTS["contract_adherence"],
        (["rules=pass", "SCORE_ATTEMPT"] if contract_ok else []),
        None if contract_ok else "missing passing output contract or SCORE_ATTEMPT evidence",
        status=("observed" if contract_checked else "unknown"))

    execution_ok = solver.get("status") == "evidence_exported" and artifacts["run_export"] and artifacts["trajectory"] and artifacts["timeline"]
    add("real_execution", T_EVIDENCE_WEIGHTS["real_execution"] if execution_ok else 0.0, T_EVIDENCE_WEIGHTS["real_execution"],
        (["evidence_exported", "run_export", "trajectory", "timeline"] if execution_ok else []),
        None if execution_ok else "trajectory lacks complete execution/export evidence", status="observed")

    scientific = result.get("scientific_score")
    metrics = list(scientific.values()) if isinstance(scientific, Mapping) else [scientific]
    measured = bool(metrics) and all(type(value) in (int, float) and math.isfinite(value) for value in metrics)
    validation_ok = measured and result.get('score_status') == 'success' and bool(artifacts["bundle"])
    recovery = any(key in result for key in ("recovery", "retry_count", "recovered_after_error"))
    validation_points = T_EVIDENCE_WEIGHTS["validation_and_recovery"] if validation_ok else 0.0
    validation_evidence = ["scientific_score", "frozen evidence bundle"] if validation_ok else []
    if recovery:
        validation_evidence.append("recovery metadata")
    add("validation_and_recovery", validation_points if validation_ok else None, T_EVIDENCE_WEIGHTS["validation_and_recovery"], validation_evidence,
        None if validation_ok else "missing independent validation or frozen evidence bundle",
        status=("observed" if validation_ok else "unknown"))

    claim_ok = (status == "complete" and judge.get('is_valid') is True and
                reporting.get("decision_status") not in {"evidence_insufficient", "rejected"} and
                bool(artifacts["run_export"]) and bool(result.get('score_attempt_used')))
    add("claim_support", T_EVIDENCE_WEIGHTS["claim_support"] if claim_ok else None, T_EVIDENCE_WEIGHTS["claim_support"],
        (["status and exported evidence agree"] if claim_ok else []),
        None if claim_ok else "completion claim was not independently accepted",
        status=("observed" if claim_ok else "unknown"))

    # Semantic process scores are accepted only when an explicit review binds
    # each criterion to trajectory evidence.  Issue counts alone cannot imply
    # a score, and absent observations remain unknown.
    semantic = result.get("semantic_review") if isinstance(result.get("semantic_review"), Mapping) else {}
    semantic_scores = _bound_semantic_scores(semantic, artifacts["trajectory"])
    for name, maximum in T_PROCESS_WEIGHTS.items():
        value = semantic_scores.get(name)
        valid = isinstance(value, (int, float)) and 1 <= value <= 5
        add(name, float(value) if valid else None, maximum,
            [f"semantic_review.criteria.{name}: exact step quotes and trajectory SHA-256"] if valid else [],
            None if valid else f"semantic process dimension {name} has no bound score",
            status=("observed" if valid else "unknown"))

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
        "policy": "T0-v1: 60-point mechanical evidence layer plus 40-point evidence-bound semantic process layer; unknowns are not zero-filled",
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
    # Tool intent and words such as "read", "test" or "completed" are not
    # proof of execution, input identity, validation or supported completion.
    # Only observations linked to actual call IDs establish this limited
    # structural adapter's real-execution dimension.
    matched = []
    for step in agent_steps:
        ids = {c.get("tool_call_id") for c in step.get("tool_calls", [])
               if isinstance(c, Mapping) and c.get("tool_call_id")}
        observation = step.get("observation")
        results = observation.get("results", []) if isinstance(observation, Mapping) else []
        matched.extend(r for r in results if isinstance(r, Mapping)
                       and r.get("source_call_id") in ids and r.get("content"))
    executed = bool(matched)
    dimensions = {name: {"score": None, "max": maximum, "evidence": [], "status": "unknown"}
                  for name, maximum in T0_WEIGHTS.items()}
    if executed:
        dimensions["real_execution"] = {
            "score": T_EVIDENCE_WEIGHTS["real_execution"],
            "max": T_EVIDENCE_WEIGHTS["real_execution"],
            "evidence": [f"{len(matched)} observations linked to actual tool call IDs"],
            "status": "observed"}
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
            "policy": "structural audit of linked tool observations only; input identity, validation and completion claims require independent evidence"}

