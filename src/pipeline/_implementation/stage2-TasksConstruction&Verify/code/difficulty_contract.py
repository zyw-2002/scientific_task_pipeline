"""Mechanical paper-to-task difficulty and scope alignment gates.

The construction model records a small, source-backed difficulty vector in the
assessment scope.  This module checks the declared vector and the protocol
bindings without attempting to infer scientific difficulty from prose. A
profile is required when execution.json opts into
``difficulty_contract_version=1``. Older analytical fixtures can continue to
use the historical scope contract.
"""
from __future__ import annotations

import math
from typing import Any


PROFILE_VERSION = 1
DEFAULT_OVERALL_GAP_LIMIT = 0.20
DEFAULT_SPLIT_DROP_LIMIT = 1
SCOPE_POLICIES = {"auto", "bounded", "full"}
DIMENSIONS = (
    "data_scale_level",
    "input_resource_count",
    "split_difficulty_level",
    "output_complexity_level",
    "metric_level",
    "compute_level",
)
DEFAULT_WEIGHTS = {
    "data_scale_level": 0.20,
    "input_resource_count": 0.10,
    "split_difficulty_level": 0.25,
    "output_complexity_level": 0.15,
    "metric_level": 0.10,
    "compute_level": 0.20,
}


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _level(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 5


def _bool(value: Any) -> bool:
    return type(value) is bool


def _evidence_errors(value: Any, where: str) -> list[str]:
    """Require source-backed evidence without prescribing a paper format."""
    if isinstance(value, dict):
        rows = [value]
    elif isinstance(value, list) and value:
        rows = value
    else:
        return [f"{where} must contain at least one source-evidence object"]
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"{where}[{index}] must be an object")
            continue
        # Existing source_claims use paper/locator/path/sha256/quote.  The
        # profile accepts the same fields and also permits an explicit source
        # statement when a source inventory has no line-level quote.
        if not any(isinstance(row.get(key), str) and row[key].strip()
                   for key in ("quote", "statement", "evidence")):
            errors.append(f"{where}[{index}] needs quote, statement, or evidence")
        if not any(isinstance(row.get(key), str) and row[key].strip()
                   for key in ("locator", "path", "source")):
            errors.append(f"{where}[{index}] needs locator, path, or source")
    return errors


def _profile_errors(profile: Any) -> list[str]:
    if not isinstance(profile, dict):
        return ["assessment.scope.difficulty_profile must be an object"]
    errors: list[str] = []
    if profile.get("version") != PROFILE_VERSION:
        errors.append(f"difficulty_profile.version must be {PROFILE_VERSION}")
    for side in ("paper", "task"):
        vector = profile.get(side)
        if not isinstance(vector, dict):
            errors.append(f"difficulty_profile.{side} must be an object")
            continue
        for dimension in DIMENSIONS:
            if not _level(vector.get(dimension)):
                errors.append(f"difficulty_profile.{side}.{dimension} must be an integer level from 1 to 5")
        if type(vector.get("main_experiment_count")) is not int or vector["main_experiment_count"] < 1:
            errors.append(f"difficulty_profile.{side}.main_experiment_count must be a positive integer")
        if side == "paper":
            for key in ("requires_independent_reference", "requires_model_or_checkpoint"):
                if not _bool(vector.get(key)):
                    errors.append(f"difficulty_profile.paper.{key} must be boolean")
        else:
            for key in ("uses_independent_reference", "uses_model_or_checkpoint"):
                if not _bool(vector.get(key)):
                    errors.append(f"difficulty_profile.task.{key} must be boolean")
            if not isinstance(vector.get("method_type"), str) or not vector["method_type"].strip():
                errors.append("difficulty_profile.task.method_type must be nonempty text")
    weights = profile.get("weights", DEFAULT_WEIGHTS)
    if not isinstance(weights, dict):
        errors.append("difficulty_profile.weights must be an object")
    else:
        total = 0.0
        for dimension in DIMENSIONS:
            weight = weights.get(dimension)
            if not _number(weight) or weight <= 0:
                errors.append(f"difficulty_profile.weights.{dimension} must be a positive number")
            else:
                total += float(weight)
        if total <= 0:
            errors.append("difficulty_profile.weights must have a positive total")
    for key, default in (("overall_gap_limit", DEFAULT_OVERALL_GAP_LIMIT),
                         ("split_drop_limit", DEFAULT_SPLIT_DROP_LIMIT)):
        value = profile.get(key, default)
        if not _number(value) or value < 0:
            errors.append(f"difficulty_profile.{key} must be a nonnegative finite number")
        if key == "overall_gap_limit" and _number(value) and value > DEFAULT_OVERALL_GAP_LIMIT:
            errors.append("difficulty_profile.overall_gap_limit cannot exceed 0.20")
        if key == "split_drop_limit":
            if type(value) is not int:
                errors.append("difficulty_profile.split_drop_limit must be an integer")
            elif value > DEFAULT_SPLIT_DROP_LIMIT:
                errors.append("difficulty_profile.split_drop_limit cannot exceed 1")
    errors.extend(_evidence_errors(profile.get("source_evidence"), "difficulty_profile.source_evidence"))
    return errors


def compare_profiles(profile: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic distance and hard-rule outcomes for a valid profile."""
    paper = profile["paper"]
    task = profile["task"]
    weights = profile.get("weights", DEFAULT_WEIGHTS)
    total = sum(float(weights[dimension]) for dimension in DIMENSIONS)
    # Five levels map to [0, 1].  A task at the same level has zero distance,
    # while a four-level drop contributes one for that dimension.
    contributions = {
        dimension: float(weights[dimension]) * abs(task[dimension] - paper[dimension]) / 4.0
        for dimension in DIMENSIONS
    }
    distance = sum(contributions.values()) / total
    split_drop = paper["split_difficulty_level"] - task["split_difficulty_level"]
    overall_limit = float(profile.get("overall_gap_limit", DEFAULT_OVERALL_GAP_LIMIT))
    split_limit = int(profile.get("split_drop_limit", DEFAULT_SPLIT_DROP_LIMIT))
    experiment_reduction = paper["main_experiment_count"] > 1 and task["main_experiment_count"] == 1
    score_source = str(task.get("score_source", "")).lower()
    reference_violation = paper["requires_independent_reference"] and (
        not task["uses_independent_reference"] or
        any(token in score_source for token in ("self_report", "submitter", "submission_score")))
    method_type = str(task.get("method_type", "")).lower()
    checkpoint_violation = paper["requires_model_or_checkpoint"] and (
        not task["uses_model_or_checkpoint"] or
        any(token in method_type for token in ("rule", "heuristic")))
    return {
        "overall_gap": distance,
        "overall_gap_limit": overall_limit,
        "split_drop": split_drop,
        "split_drop_limit": split_limit,
        "weighted_contributions": contributions,
        "experiment_reduction": experiment_reduction,
        "reference_violation": reference_violation,
        "checkpoint_violation": checkpoint_violation,
        "within_overall_limit": distance <= overall_limit + 1e-12,
        "within_split_limit": split_drop <= split_limit,
    }


def _formal_reference_bound(protocol: dict[str, Any]) -> bool:
    resources = protocol.get("resources", {})
    bindings = protocol.get("resource_bindings", {})
    if not isinstance(resources, dict) or not isinstance(bindings, dict):
        return False
    reference_ids = {rid for rid, value in resources.items()
                     if isinstance(value, dict) and value.get("role") == "reference"}
    return bool(reference_ids & {rid for rid in bindings.values() if isinstance(rid, str)})


def _formal_model_bound(protocol: dict[str, Any]) -> bool:
    resources = protocol.get("resources", {})
    bindings = protocol.get("resource_bindings", {})
    if not isinstance(resources, dict) or not isinstance(bindings, dict):
        return False
    model_ids = set()
    for rid, value in resources.items():
        if not isinstance(value, dict):
            continue
        labels = " ".join([str(rid)] + [str(value.get(key, "")) for key in
                                         ("kind", "role", "name", "identity", "schema_description")]).lower()
        if any(token in labels for token in ("model", "checkpoint", "weights", "pretrained")):
            model_ids.add(rid)
    return bool(model_ids & {rid for rid in bindings.values() if isinstance(rid, str)})


def difficulty_errors(protocol: dict[str, Any], *, required: bool = False) -> list[str]:
    """Validate difficulty and an explicit full/bounded scope policy.

    ``full`` preserves the strict paper-panel gate.  A task generator may set
    ``assessment.scope.scope_policy=bounded`` when it deliberately selects a
    source-backed subset; that path still requires task scope, adaptation,
    difficulty impact, and source evidence.  Existing packages keep the
    strict ``full`` behavior because the field is opt-in.
    """
    scope = protocol.get("assessment", {}).get("scope", {})
    if not isinstance(scope, dict):
        return ["assessment.scope is required before difficulty alignment can be checked"] if required else []
    profile = scope.get("difficulty_profile")
    if profile is None:
        return ["assessment.scope.difficulty_profile is required for production scope alignment"] if required else []
    errors = _profile_errors(profile)
    if errors:
        return errors
    comparison = compare_profiles(profile)
    scope_policy = scope.get("scope_policy", "full")
    if scope_policy not in SCOPE_POLICIES:
        errors.append("assessment.scope.scope_policy must be auto, bounded, or full")
    adaptation = protocol.get("assessment", {}).get("adaptations")
    adaptation_documented = bool(
        isinstance(scope.get("task_scope"), str) and scope["task_scope"].strip()
        and isinstance(scope.get("difficulty_impact"), str) and scope["difficulty_impact"].strip()
        and ((isinstance(adaptation, str) and adaptation.strip())
             or (isinstance(adaptation, list) and any(str(item).strip() for item in adaptation)))
        and not _evidence_errors(profile.get("source_evidence"), "difficulty_profile.source_evidence")
    )
    if not comparison["within_overall_limit"]:
        errors.append(
            f"overall difficulty gap {comparison['overall_gap']:.3f} exceeds "
            f"{comparison['overall_gap_limit']:.3f} (maximum 20% by default)"
        )
    if not comparison["within_split_limit"]:
        errors.append(
            f"data split difficulty drops {comparison['split_drop']} levels, "
            f"limit is {comparison['split_drop_limit']}"
        )
    if comparison["experiment_reduction"]:
        if scope_policy == "full":
            errors.append("scope_policy=full requires every paper main experiment")
        elif not adaptation_documented:
            errors.append("bounded scope reduction requires task_scope, difficulty_impact, adaptations, and source evidence")
    declared_experiments = scope.get("main_experiments")
    if isinstance(declared_experiments, dict) and declared_experiments:
        declared_count = len(declared_experiments)
        if profile["task"]["main_experiment_count"] != declared_count:
            errors.append(
                "difficulty_profile.task.main_experiment_count must equal "
                f"the {declared_count} declared main_experiments"
            )
    if comparison["reference_violation"]:
        errors.append("paper requires independent reference data; task cannot use submitter self-reported scores")
    if comparison["checkpoint_violation"]:
        errors.append("paper requires a model/checkpoint; task cannot use a simple rule-only method")
    # A profile saying that a resource is used must be reflected in the formal
    # evaluator binding.  This catches a model-written claim that is never used.
    task = profile["task"]
    if task["uses_independent_reference"] and not _formal_reference_bound(protocol):
        errors.append("task difficulty profile declares independent reference use but no formal reference is evaluator-bound")
    if task["uses_model_or_checkpoint"] and not _formal_model_bound(protocol):
        errors.append("task difficulty profile declares model/checkpoint use but no formal model resource is evaluator-bound")
    return errors


def difficulty_report(protocol: dict[str, Any], *, required: bool = False) -> dict[str, Any]:
    """Expose a machine-readable result for preflight and downstream audits."""
    scope = protocol.get("assessment", {}).get("scope", {})
    profile = scope.get("difficulty_profile") if isinstance(scope, dict) else None
    errors = difficulty_errors(protocol, required=required)
    result: dict[str, Any] = {"version": PROFILE_VERSION, "status": "PASS" if not errors else "REWORK",
                              "errors": errors,
                              "scope_policy": scope.get("scope_policy", "full") if isinstance(scope, dict) else "full"}
    if isinstance(profile, dict) and not _profile_errors(profile):
        result["comparison"] = compare_profiles(profile)
    return result

