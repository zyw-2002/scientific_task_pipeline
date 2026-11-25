"""Historical Stage1 boundary schemas used by the reorganized pipeline."""
from __future__ import annotations
from collections.abc import Iterable, Mapping

HISTORICAL_FILES = {
    "unified": "01_unified_sources.jsonl",
    "deduplicated": "02_deduplicated_papers.jsonl",
    "sampled": "03_selected_all_sources.jsonl",
    "content": "04_content_manifest.jsonl",
    "candidates": "05_resource_candidates.jsonl",
    "validated": "06_validated_papers.jsonl",
    "enriched": "07_open_source_enriched.jsonl",
    "searched": "08_serpapi_enriched.jsonl",
    "materials": "10_download_results.jsonl",
}

REQUIRED_FIELDS = {
    "unified": {"schema_version", "identifiers", "title", "document", "resources", "selection_context"},
    "deduplicated": {"schema_version", "canonical_id", "identifiers", "title", "document", "resources", "sources"},
    "sampled": {"schema_version", "canonical_id", "identifiers", "title", "document", "resources", "sources"},
    "content": {"schema_version", "canonical_id", "title", "paper", "artifacts", "quality", "status"},
    "candidates": {"schema_version", "canonical_id", "title", "candidates", "candidate_count", "paper_content"},
    "validated": {"schema_version", "canonical_id", "title", "resources", "resource_status", "sources"},
    "enriched": {"schema_version", "canonical_id", "title", "resources", "resource_status", "open_source_enrichment"},
    "searched": {"schema_version", "canonical_id", "title", "resources", "resource_status", "serpapi_enrichment"},
    "materials": {"schema_version", "resource_id", "canonical_id", "kind", "status", "local_artifacts", "validation_checks"},
}


def missing_fields(record: Mapping[str, object], boundary: str) -> set[str]:
    return REQUIRED_FIELDS[boundary] - set(record)


def validate_record(record: Mapping[str, object], boundary: str) -> list[str]:
    errors = []
    if not isinstance(record, Mapping):
        return ["record must be an object"]
    errors.extend(f"missing {field}" for field in sorted(missing_fields(record, boundary)))
    if "schema_version" in record and not isinstance(record["schema_version"], str):
        errors.append("schema_version must be text")
    if boundary not in {"unified", "content", "candidates", "materials"}:
        if not isinstance(record.get("canonical_id"), str) or not record["canonical_id"]:
            errors.append("canonical_id must be nonempty text")
    return errors


def validate_records(records: Iterable[Mapping[str, object]], boundary: str) -> list[str]:
    errors=[]
    for index, record in enumerate(records, 1):
        errors.extend(f"row {index}: {error}" for error in validate_record(record, boundary))
    return errors
