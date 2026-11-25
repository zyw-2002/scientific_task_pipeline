#!/usr/bin/env python3
"""Validate locally extracted resource candidates and merge accepted URLs."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting





RESOURCE_FIELDS = {
    "supplementary": "supplementary_urls",
    "code": "code_urls",
    "data": "data_urls",
}
AUTO_THRESHOLDS = {"supplementary": 0.72, "code": 0.72, "data": 0.72}
USER_AGENT = "NatureBenchResourceValidator/1.0"
ILLEGAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return " ".join(ILLEGAL_CONTROL_RE.sub("", str(value or "")).split())


def safe_cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        value = ILLEGAL_CONTROL_RE.sub("", value)
        if len(value) > 32760:
            value = value[:32730] + " [TRUNCATED_FOR_XLSX]"
    return value


def unique(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, "", []):
            continue
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} 不是 JSON 对象")
            rows.append(value)
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_extractor_module() -> Any:
    script = Path(__file__).with_name("05_extract_resource_candidates.py")
    spec = importlib.util.spec_from_file_location("resource_candidate_extractor", script)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载 {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_url(url: str, timeout: float) -> dict[str, Any]:
    result = {"url": url, "status": "error", "http_status": None, "final_url": None, "error": None}
    try:
        response = requests.head(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code in {403, 405} or response.status_code >= 500:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Range": "bytes=0-4095"},
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            )
        result.update({
            "status": "reachable" if response.status_code < 400 else "http_error",
            "http_status": response.status_code,
            "final_url": str(response.url),
        })
        response.close()
    except requests.RequestException as error:
        result["error"] = clean(error)
    return result


def validate_candidate(candidate: dict[str, Any], extractor: Any) -> dict[str, Any]:
    item = deepcopy(candidate)
    resource_type = clean(item.get("resource_type"))
    url = extractor.normalize_url(item.get("url"))
    score = float(item.get("score") or 0)
    signals = set(item.get("signals") or [])
    reasons: list[str] = []
    if resource_type not in RESOURCE_FIELDS:
        decision = "rejected"
        reasons.append("unknown_resource_type")
    elif not url:
        decision = "rejected"
        reasons.append("invalid_absolute_url")
    else:
        actual_type = extractor.classify_url(
            url,
            anchor=(item.get("evidence") or {}).get("anchor") or "",
            context=(item.get("evidence") or {}).get("context") or "",
        )
        if actual_type != resource_type:
            decision = "rejected"
            reasons.append(f"url_type_is_{actual_type or 'unknown'}")
        else:
            strong_type = {
                "supplementary": "explicit_supplement_attachment",
                "code": "concrete_code_repository",
                "data": "concrete_data_record",
            }[resource_type]
            relationship = bool(signals & {
                "availability_language", "code_availability", "data_availability",
                "resource_availability", "supplementary_section",
            })
            if score >= AUTO_THRESHOLDS[resource_type] and strong_type in signals and relationship:
                decision = "accepted"
                reasons.append("strong_url_type_and_article_relationship_evidence")
            elif score >= 0.4 and strong_type in signals:
                decision = "needs_review"
                reasons.append("valid_resource_url_but_article_relationship_not_strong_enough")
            else:
                decision = "rejected"
                reasons.append("insufficient_type_or_relationship_evidence")
    item["url"] = url or item.get("url")
    item["validation"] = {
        "decision": decision,
        "reasons": reasons,
        "validated_at": now_utc(),
    }
    return item


def merge_paper_content(
    record: dict[str, Any], candidate_result: dict[str, Any] | None
) -> None:
    paper_content = deepcopy((candidate_result or {}).get("paper_content") or {})
    document = record.setdefault("document", {})
    document["paper_urls"] = unique([
        document.get("paper_url"), *(document.get("paper_urls") or []),
        *(paper_content.get("candidate_paper_urls") or []),
    ])
    document["pdf_urls"] = unique([
        document.get("pdf_url"), *(document.get("pdf_urls") or []),
        *(paper_content.get("candidate_pdf_urls") or []),
    ])
    if not document.get("paper_url") and document["paper_urls"]:
        document["paper_url"] = document["paper_urls"][0]
    if not document.get("pdf_url") and document["pdf_urls"]:
        document["pdf_url"] = document["pdf_urls"][0]

    if not paper_content:
        if document["pdf_urls"]:
            status = "downloadable_pdf"
        elif document["paper_urls"]:
            status = "landing_page_only"
        else:
            status = "not_found"
        paper_content = {
            "schema_version": "paper-content-status-v1",
            "collection_status": None,
            "status": status,
            "paper_available": status == "downloadable_pdf",
            "text_ready": False,
            "valid_local_artifact_count": 0,
            "text_ready_artifact_count": 0,
            "invalid_artifact_count": 0,
            "coverage": {},
            "candidate_paper_urls": document["paper_urls"],
            "candidate_pdf_urls": document["pdf_urls"],
            "local_artifacts": [],
            "request_problem_count": 0,
        }
    else:
        paper_content["candidate_paper_urls"] = document["paper_urls"]
        paper_content["candidate_pdf_urls"] = document["pdf_urls"]
        if (
            not paper_content.get("paper_available")
            and document["pdf_urls"]
            and paper_content.get("status") in {"landing_page_only", "not_found", None}
        ):
            paper_content["status"] = "downloadable_pdf"
            paper_content["paper_available"] = True
    record["paper_content"] = paper_content


def merge_record(
    paper: dict[str, Any],
    candidate_result: dict[str, Any] | None,
    extractor: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    record = deepcopy(paper)
    merge_paper_content(record, candidate_result)
    decisions = [
        validate_candidate(candidate, extractor)
        for candidate in (candidate_result or {}).get("candidates") or []
    ]

    # PDF/HTML annotations often point to the paper's primary repository, while
    # single text-only URLs in methods or appendices may be third-party tools.
    # When both are otherwise acceptable, keep the repeatedly evidenced code
    # repository automatic and leave single-evidence alternatives for review.
    accepted_code = [
        item for item in decisions
        if item.get("resource_type") == "code"
        and (item.get("validation") or {}).get("decision") == "accepted"
    ]
    repeated_code = [
        item for item in accepted_code if int(item.get("evidence_count") or 0) >= 2
    ]
    if repeated_code:
        for item in accepted_code:
            if int(item.get("evidence_count") or 0) >= 2:
                continue
            validation = item["validation"]
            validation["decision"] = "needs_review"
            validation["reasons"] = [
                "single_evidence_code_candidate_beside_repeated_primary_repository"
            ]

    resources = record.setdefault("resources", {})
    for field in RESOURCE_FIELDS.values():
        resources[field] = unique(resources.get(field) or [])
    provenance = record.setdefault("resource_provenance", [])
    accepted_counts: Counter[str] = Counter()
    review_counts: Counter[str] = Counter()
    for candidate in decisions:
        resource_type = candidate.get("resource_type")
        decision = (candidate.get("validation") or {}).get("decision")
        if decision == "accepted":
            field = RESOURCE_FIELDS[resource_type]
            if candidate["url"] not in resources[field]:
                resources[field].append(candidate["url"])
                provenance.append({
                    "resource_type": resource_type,
                    "url": candidate["url"],
                    "origin": "article_content_extraction",
                    "candidate_id": candidate.get("candidate_id"),
                    "score": candidate.get("score"),
                    "evidence": candidate.get("evidence"),
                })
            accepted_counts[resource_type] += 1
        elif decision == "needs_review":
            review_counts[resource_type] += 1

    resource_status = record.setdefault("resource_status", {})
    local_status: dict[str, Any] = {}
    for resource_type, field in RESOURCE_FIELDS.items():
        urls = resources.get(field) or []
        if urls:
            status = "found"
            reason = "existing_or_locally_extracted_validated_url"
        elif review_counts[resource_type]:
            status = "needs_review"
            reason = "local_article_candidates_require_review"
        else:
            status = "not_found"
            reason = "not_found_in_available_local_txt_html_or_pdf"
        detail = {
            "status": status,
            "reason": reason,
            "url_count": len(urls),
            "accepted_from_article_content": accepted_counts[resource_type],
            "local_review_candidate_count": review_counts[resource_type],
            "serpapi_fallback_required": not bool(urls),
        }
        resource_status[resource_type] = detail
        local_status[resource_type] = detail

    unresolved: list[str] = []
    document = record.get("document") or {}
    identifiers = record.get("identifiers") or {}
    if not record.get("year"):
        unresolved.append("year")
    if not identifiers.get("doi") and not identifiers.get("arxiv"):
        unresolved.append("primary_identifier")
    if not document.get("paper_url") and not document.get("paper_urls"):
        unresolved.append("paper_url")
    if not document.get("pdf_url") and not document.get("pdf_urls"):
        unresolved.append("pdf_url")
    for field in RESOURCE_FIELDS.values():
        if not resources.get(field):
            unresolved.append(field)

    record["local_resource_extraction"] = {
        "generated_at": now_utc(),
        "abstract_required": False,
        "content_status": (candidate_result or {}).get("content_status"),
        "candidate_count": len(decisions),
        "accepted_count": sum(accepted_counts.values()),
        "needs_review_count": sum(review_counts.values()),
        "resource_status": local_status,
        "unresolved_fields": unresolved,
    }
    return record, decisions


def write_xlsx(
    path: Path,
    records: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    papers = workbook.active
    papers.title = "合并后论文"
    paper_columns = [
        "canonical_id", "title", "abstract_present", "paper_content_status",
        "paper_available", "paper_text_ready", "paper_local_artifacts",
        "supplementary_urls",
        "supplementary_status", "code_urls", "code_status", "data_urls",
        "data_status", "candidate_count", "accepted_count", "needs_review_count",
        "unresolved_fields",
    ]
    papers.append(paper_columns)
    for record in records:
        resources = record.get("resources") or {}
        statuses = record.get("resource_status") or {}
        extraction = record.get("local_resource_extraction") or {}
        row = {
            "canonical_id": record.get("canonical_id"),
            "title": record.get("title"),
            "abstract_present": bool(clean(record.get("abstract"))),
            "paper_content_status": (record.get("paper_content") or {}).get("status"),
            "paper_available": bool((record.get("paper_content") or {}).get("paper_available")),
            "paper_text_ready": bool((record.get("paper_content") or {}).get("text_ready")),
            "paper_local_artifacts": (record.get("paper_content") or {}).get(
                "valid_local_artifact_count", 0
            ),
            "supplementary_urls": json.dumps(resources.get("supplementary_urls") or [], ensure_ascii=False),
            "supplementary_status": (statuses.get("supplementary") or {}).get("status"),
            "code_urls": json.dumps(resources.get("code_urls") or [], ensure_ascii=False),
            "code_status": (statuses.get("code") or {}).get("status"),
            "data_urls": json.dumps(resources.get("data_urls") or [], ensure_ascii=False),
            "data_status": (statuses.get("data") or {}).get("status"),
            "candidate_count": extraction.get("candidate_count"),
            "accepted_count": extraction.get("accepted_count"),
            "needs_review_count": extraction.get("needs_review_count"),
            "unresolved_fields": json.dumps(extraction.get("unresolved_fields") or [], ensure_ascii=False),
        }
        papers.append([safe_cell(row[column]) for column in paper_columns])

    candidates = workbook.create_sheet("候选判定")
    candidate_columns = [
        "canonical_id", "title", "resource_type", "url", "score", "decision",
        "reasons", "signals", "method", "source_kind", "section", "source_path",
        "context", "url_check_status", "http_status", "final_url",
    ]
    candidates.append(candidate_columns)
    for item in decisions:
        evidence = item.get("evidence") or {}
        validation = item.get("validation") or {}
        url_check = validation.get("url_check") or {}
        row = {
            "canonical_id": item.get("canonical_id"),
            "title": item.get("title"),
            "resource_type": item.get("resource_type"),
            "url": item.get("url"),
            "score": item.get("score"),
            "decision": validation.get("decision"),
            "reasons": json.dumps(validation.get("reasons") or [], ensure_ascii=False),
            "signals": json.dumps(item.get("signals") or [], ensure_ascii=False),
            "method": evidence.get("method"),
            "source_kind": evidence.get("source_kind"),
            "section": evidence.get("section"),
            "source_path": evidence.get("source_path"),
            "context": evidence.get("context"),
            "url_check_status": url_check.get("status"),
            "http_status": url_check.get("http_status"),
            "final_url": url_check.get("final_url"),
        }
        candidates.append([safe_cell(row[column]) for column in candidate_columns])

    fill = PatternFill("solid", fgColor="1F4E78")
    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = fill
        sheet.column_dimensions["A"].width = 32
        sheet.column_dimensions["B"].width = 60
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate article resource candidates and merge high-confidence URLs"
    )
    parser.add_argument("--papers", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--url-check", choices=("none", "head"), default=setting('url_check'))
    parser.add_argument("--timeout", type=float, default=setting('download_timeout'))
    parser.add_argument("--concurrency", type=int, default=setting('download_workers'))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.papers.is_file():
        raise SystemExit(f"论文输入不存在: {args.papers.resolve()}")
    if not args.candidates.is_file():
        raise SystemExit(f"候选输入不存在: {args.candidates.resolve()}")
    if not 1 <= args.concurrency <= 16:
        raise SystemExit("--concurrency 必须在 1 到 16 之间")
    if args.start_index < 0:
        raise SystemExit("--start-index 不能小于 0")
    if args.max_records is not None and args.max_records < 1:
        raise SystemExit("--max-records 必须大于 0")

    extractor = load_extractor_module()
    all_papers = read_jsonl(args.papers)
    papers = all_papers[args.start_index:]
    if args.max_records is not None:
        papers = papers[: args.max_records]
    candidate_results = {
        clean(item.get("canonical_id")): item for item in read_jsonl(args.candidates)
    }
    records: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for paper in papers:
        record, paper_decisions = merge_record(
            paper, candidate_results.get(clean(paper.get("canonical_id"))), extractor
        )
        records.append(record)
        decisions.extend(paper_decisions)

    if args.url_check == "head":
        accepted = [
            item for item in decisions
            if (item.get("validation") or {}).get("decision") == "accepted"
        ]
        unique_urls = sorted({item["url"] for item in accepted})
        checks: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_url = {
                executor.submit(check_url, url, args.timeout): url for url in unique_urls
            }
            for future in as_completed(future_to_url):
                checks[future_to_url[future]] = future.result()
        for item in decisions:
            if item.get("url") in checks:
                item["validation"]["url_check"] = checks[item["url"]]

    decision_counts = Counter(
        (item.get("validation") or {}).get("decision") for item in decisions
    )
    resource_counts = Counter(
        item.get("resource_type")
        for item in decisions
        if (item.get("validation") or {}).get("decision") == "accepted"
    )
    status_counts = {
        resource_type: dict(sorted(Counter(
            ((record.get("resource_status") or {}).get(resource_type) or {}).get("status")
            for record in records
        ).items()))
        for resource_type in RESOURCE_FIELDS
    }
    summary = {
        "schema_version": "validated-resource-summary-v2",
        "generated_at": now_utc(),
        "papers_input": str(args.papers.resolve()),
        "candidates_input": str(args.candidates.resolve()),
        "input_records": len(all_papers),
        "start_index": args.start_index,
        "records": len(records),
        "candidates": len(decisions),
        "decisions": dict(sorted(decision_counts.items())),
        "accepted_resource_types": dict(sorted(resource_counts.items())),
        "resource_statuses": status_counts,
        "paper_content_statuses": dict(sorted(Counter(
            (record.get("paper_content") or {}).get("status") or "missing"
            for record in records
        ).items())),
        "paper_available": sum(
            bool((record.get("paper_content") or {}).get("paper_available"))
            for record in records
        ),
        "paper_text_ready": sum(
            bool((record.get("paper_content") or {}).get("text_ready"))
            for record in records
        ),
        "url_check": args.url_check,
        "abstract_required": False,
    }
    output_dir = args.output_dir.resolve()
    write_jsonl(output_dir / "06_validated_papers.jsonl", records)
    write_jsonl(output_dir / "06_candidate_decisions.jsonl", decisions)
    write_json(output_dir / "06_validation_summary.json", summary)
    write_xlsx(output_dir / "06_validation_review.xlsx", records, decisions)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
