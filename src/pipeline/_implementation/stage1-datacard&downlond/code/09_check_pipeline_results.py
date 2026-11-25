#!/usr/bin/env python3
"""Validate final SerpAPI fallback outputs and create a review workbook."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import random
import re
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting



RESOURCE_FIELDS = {
    "supplementary": "supplementary_urls",
    "code": "code_urls",
    "data": "data_urls",
}
VALID_STATUSES = {
    "found", "not_found", "partial_search_error", "search_error",
    "needs_review", "not_applicable",
}
PAPER_AVAILABLE_STATUSES = {
    "local_text_ready", "local_fulltext_available", "downloadable_pdf",
}
STRONG_RESOURCE_EVIDENCE = {
    "doi_in_result_or_indexed_page",
    "paper_url_in_result_or_indexed_page",
    "paper_title_in_result_or_indexed_page",
    "paper_doi_in_resource_page",
    "paper_arxiv_in_resource_page",
    "full_paper_title_in_resource_page",
    "paper_url_linked_from_resource_page",
    "explicit_link_from_accepted_code",
    "explicit_link_from_accepted_data",
    "explicit_link_from_accepted_supplementary",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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


def load_enrichment_module() -> Any:
    script = Path(__file__).with_name("08_serpapi_fallback.py")
    spec = importlib.util.spec_from_file_location("serpapi_enrichment", script)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载 {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_extractor_module() -> Any:
    script = Path(__file__).with_name("05_extract_resource_candidates.py")
    spec = importlib.util.spec_from_file_location("resource_candidate_extractor", script)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载 {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status_value(record: dict[str, Any], resource_type: str) -> str | None:
    value = (record.get("resource_status") or {}).get(resource_type)
    if isinstance(value, dict):
        return value.get("status")
    return value


def paper_content_status(record: dict[str, Any]) -> str:
    paper_content = record.get("paper_content") or {}
    status = str(paper_content.get("status") or "")
    if status:
        return status
    document = record.get("document") or {}
    if document.get("pdf_url") or document.get("pdf_urls"):
        return "downloadable_pdf"
    if document.get("paper_url") or document.get("paper_urls"):
        return "landing_page_only"
    return "not_found"


def task_eligibility(record: dict[str, Any]) -> dict[str, Any]:
    paper_status = paper_content_status(record)
    paper_content = record.get("paper_content") or {}
    paper_available = (
        paper_status in PAPER_AVAILABLE_STATUSES
        and (
            bool(paper_content.get("paper_available"))
            if paper_content else True
        )
    )
    resources = record.get("resources") or {}
    code_urls = resources.get("code_urls") or []
    code_status = status_value(record, "code") or "missing"
    reasons: list[str] = []
    if not paper_available:
        reasons.append(f"paper_{paper_status}")
    if code_status != "found" or not code_urls:
        reasons.append(f"code_{code_status}")
    eligible = not reasons
    return {
        "canonical_id": record.get("canonical_id"),
        "title": record.get("title"),
        "paper_status": paper_status,
        "paper_available": paper_available,
        "paper_text_ready": bool(paper_content.get("text_ready")),
        "valid_local_artifacts": paper_content.get(
            "valid_local_artifact_count", 0
        ),
        "pdf_urls": (record.get("document") or {}).get("pdf_urls") or [],
        "code_status": code_status,
        "code_urls": code_urls,
        "eligible": eligible,
        "eligibility_status": "paper_and_code_candidate" if eligible else "not_eligible",
        "reasons": reasons,
    }


def canonical_url_aliases(
    enrichment: Any,
    urls: Iterable[Any],
    resource_type: str,
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for raw_url in urls:
        url = str(raw_url or "")
        if not enrichment.is_absolute_http_url(url):
            continue
        canonical = enrichment.canonical_resource_url(url, resource_type)
        if canonical:
            grouped[canonical].append(url)
    return {
        canonical: values
        for canonical, values in grouped.items()
        if len(values) > 1
    }


def duplicate_change_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    duplicates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for change in (record.get("serpapi_enrichment") or {}).get("changes") or []:
        marker = json.dumps({
            "field": change.get("field"),
            "new_value": change.get("new_value"),
            "evidence_url": change.get("evidence_url"),
            "search_basis": change.get("search_basis"),
        }, ensure_ascii=False, sort_keys=True)
        if marker in seen:
            duplicates.append(change)
        else:
            seen.add(marker)
    return duplicates


def source_types(record: dict[str, Any]) -> str:
    return "+".join(sorted({
        str(item.get("type") or "")
        for item in record.get("sources") or []
        if item.get("type")
    }))


def safe_cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
        if len(value) > 32760:
            return value[:32730] + " [TRUNCATED_FOR_XLSX]"
    return value


def style_header(sheet: Any) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = fill


def write_xlsx(
    path: Path,
    issues: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    stats: list[dict[str, Any]],
    eligibility_rows: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    issue_sheet = workbook.active
    issue_sheet.title = "结构问题"
    issue_columns = [
        "severity", "canonical_id", "title", "resource_type", "field",
        "value", "message",
    ]
    issue_sheet.append(issue_columns)
    for row in issues:
        issue_sheet.append([safe_cell(row.get(column)) for column in issue_columns])
    style_header(issue_sheet)

    sample_sheet = workbook.create_sheet("分层抽样")
    sample_columns = [
        "canonical_id", "title", "source_types", "resource_type", "status",
        "urls", "status_detail",
    ]
    sample_sheet.append(sample_columns)
    for row in samples:
        sample_sheet.append([safe_cell(row.get(column)) for column in sample_columns])
    style_header(sample_sheet)

    eligibility_sheet = workbook.create_sheet("任务准入")
    eligibility_columns = [
        "canonical_id", "title", "paper_status", "paper_available",
        "paper_text_ready", "valid_local_artifacts", "pdf_urls",
        "code_status", "code_urls", "eligibility_status", "reasons",
    ]
    eligibility_sheet.append(eligibility_columns)
    for row in eligibility_rows:
        eligibility_sheet.append([
            safe_cell(row.get(column)) for column in eligibility_columns
        ])
    style_header(eligibility_sheet)

    stat_sheet = workbook.create_sheet("统计")
    stat_columns = ["category", "name", "count"]
    stat_sheet.append(stat_columns)
    for row in stats:
        stat_sheet.append([safe_cell(row.get(column)) for column in stat_columns])
    style_header(stat_sheet)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def add_issue(
    issues: list[dict[str, Any]],
    record: dict[str, Any] | None,
    message: str,
    *,
    severity: str = "error",
    resource_type: str | None = None,
    field: str | None = None,
    value: Any = None,
) -> None:
    issues.append({
        "severity": severity,
        "canonical_id": record.get("canonical_id") if record else None,
        "title": record.get("title") if record else None,
        "resource_type": resource_type,
        "field": field,
        "value": value,
        "message": message,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check stage-07 SerpAPI fallback outputs")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--expected-records", type=int)
    parser.add_argument("--sample-per-status", type=int, default=setting('quality_samples'))
    parser.add_argument("--seed", type=int, default=setting('seed'))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = (args.output_dir or input_dir / "quality_check").resolve()
    prefix = "08" if (input_dir / "08_serpapi_summary.json").is_file() else "07"
    required = {
        "summary": input_dir / f"{prefix}_serpapi_summary.json",
        "records": input_dir / f"{prefix}_serpapi_enriched.jsonl",
        "queries": input_dir / f"{prefix}_serpapi_query_log.jsonl",
        "reviews": input_dir / f"{prefix}_serpapi_candidates_for_review.jsonl",
        "xlsx": input_dir / f"{prefix}_serpapi_enriched.xlsx",
    }
    missing_files = [str(path) for path in required.values() if not path.is_file()]
    if missing_files:
        raise SystemExit("缺少完成文件:\n" + "\n".join(missing_files))

    enrichment = load_enrichment_module()
    extractor = load_extractor_module()
    summary = read_json(required["summary"])
    records = read_jsonl(required["records"])
    query_logs = read_jsonl(required["queries"])
    reviews = read_jsonl(required["reviews"])
    issues: list[dict[str, Any]] = []

    expected = args.expected_records
    if expected is not None and len(records) != expected:
        add_issue(
            issues, None, f"记录数应为 {expected}，实际为 {len(records)}",
            field="record_count", value=len(records),
        )
    if summary.get("output_records") != len(records):
        add_issue(
            issues, None, "summary.output_records 与 JSONL 行数不一致",
            field="summary.output_records", value=summary.get("output_records"),
        )
    recorded_change_count = sum(
        len((record.get("serpapi_enrichment") or {}).get("changes") or [])
        for record in records
    )
    if summary.get("automatic_changes") != recorded_change_count:
        add_issue(
            issues, None,
            "summary.automatic_changes 与记录内变更条数不一致",
            field="summary.automatic_changes",
            value={
                "summary": summary.get("automatic_changes"),
                "records": recorded_change_count,
            },
        )

    ids = Counter(str(record.get("canonical_id") or "") for record in records)
    for canonical_id, count in ids.items():
        if not canonical_id:
            add_issue(issues, None, "存在空 canonical_id", field="canonical_id")
        elif count > 1:
            record = next(item for item in records if item.get("canonical_id") == canonical_id)
            add_issue(
                issues, record, f"canonical_id 重复 {count} 次",
                field="canonical_id", value=canonical_id,
            )

    status_counts: dict[str, Counter[str]] = {
        resource_type: Counter() for resource_type in RESOURCE_FIELDS
    }
    paper_statuses: Counter[str] = Counter()
    eligibility_rows: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        duplicated_changes = duplicate_change_entries(record)
        if duplicated_changes:
            add_issue(
                issues, record, "serpapi_enrichment.changes 存在重复变更记录",
                field="serpapi_enrichment.changes", value=duplicated_changes,
            )
        paper_content = record.get("paper_content")
        if not isinstance(paper_content, dict):
            add_issue(
                issues, record,
                "缺少阶段04正文合并结果 paper_content，请从阶段05重新运行",
                field="paper_content",
            )
        paper_status = paper_content_status(record)
        paper_statuses[paper_status] += 1
        if paper_content and bool(paper_content.get("paper_available")) != (
            paper_status in PAPER_AVAILABLE_STATUSES
        ):
            add_issue(
                issues, record, "paper_content.paper_available 与正文状态不一致",
                field="paper_content.paper_available",
                value=paper_content.get("paper_available"),
            )
        for artifact in (paper_content or {}).get("local_artifacts") or []:
            if not artifact.get("valid_file"):
                continue
            path = Path(str(artifact.get("path") or ""))
            if not path.is_file():
                add_issue(
                    issues, record, "标记为有效的本地正文文件不存在",
                    field="paper_content.local_artifacts.path",
                    value=str(path),
                )
        eligibility_rows.append(task_eligibility(record))

        resources = record.get("resources") or {}
        provenance_by_url = {
            str(item.get("url")): item
            for item in record.get("resource_provenance") or []
            if item.get("url")
        }
        reused_resource_urls = {
            str(change.get("new_value"))
            for change in (record.get("existing_source_reuse") or {}).get("changes") or []
            if str(change.get("field") or "").startswith("resources.")
            and change.get("new_value")
        }
        changes_by_url = {
            str(change.get("evidence_url")): change
            for change in (record.get("serpapi_enrichment") or {}).get("changes") or []
            if change.get("evidence_url")
        }
        for resource_type, field in RESOURCE_FIELDS.items():
            urls = resources.get(field) or []
            status = status_value(record, resource_type)
            status_counts[resource_type][str(status or "missing")] += 1
            groups[(resource_type, str(status or "missing"))].append(record)
            if status not in VALID_STATUSES:
                add_issue(
                    issues, record, "资源状态缺失或非法",
                    resource_type=resource_type, field="resource_status", value=status,
                )
            if status == "found" and not urls:
                add_issue(
                    issues, record, "状态为 found 但 URL 列表为空",
                    resource_type=resource_type, field=field,
                )
            if urls and status != "found":
                add_issue(
                    issues, record, "存在 URL 但状态不是 found",
                    resource_type=resource_type, field=field, value=urls,
                )
            if len(urls) != len(set(urls)):
                add_issue(
                    issues, record, "同一字段存在重复 URL", severity="warning",
                    resource_type=resource_type, field=field, value=urls,
                )
            aliases = canonical_url_aliases(enrichment, urls, resource_type)
            if aliases:
                add_issue(
                    issues, record,
                    "同一资源以 DOI、落地页或跟踪参数等多个别名重复出现",
                    resource_type=resource_type, field=field, value=aliases,
                )
            for url in urls:
                if not enrichment.is_absolute_http_url(url):
                    add_issue(
                        issues, record, "URL 不是有效的绝对 HTTP(S) 地址",
                        resource_type=resource_type, field=field, value=url,
                    )
                    continue
                actual_kind = enrichment.url_kind({"link": url})
                provenance = provenance_by_url.get(str(url))
                if provenance and provenance.get("origin") == "article_content_extraction":
                    evidence = provenance.get("evidence") or {}
                    contextual_kind = extractor.classify_url(
                        str(url),
                        anchor=str(evidence.get("anchor") or ""),
                        context=str(evidence.get("context") or ""),
                    )
                    if contextual_kind:
                        actual_kind = contextual_kind
                elif provenance and provenance.get("origin") == "open_source_enrichment":
                    evidence = provenance.get("evidence") or {}
                    contextual_kind = extractor.classify_url(
                        str(url),
                        anchor=str(evidence.get("anchor") or ""),
                        context=str(evidence.get("context") or ""),
                    )
                    authoritative_kind = str(
                        evidence.get("authoritative_resource_type") or ""
                    )
                    if authoritative_kind in RESOURCE_FIELDS:
                        actual_kind = authoritative_kind
                    elif contextual_kind:
                        actual_kind = contextual_kind
                    if evidence.get("paper_match") not in {
                        "exact_identifier", "exact_doi_in_related_identifiers",
                        "exact_doi_event", "exact_doi_in_resource_metadata",
                        "exact_pid", "exact_arxiv", "exact_normalized_title",
                        "pdf_processed_directly",
                    }:
                        add_issue(
                            issues, record,
                            "开源来源 URL 缺少论文精确关联证据",
                            resource_type=resource_type, field=field, value=url,
                        )
                if actual_kind != resource_type:
                    add_issue(
                        issues, record, f"URL 被分类为 {actual_kind}，与字段不一致",
                        resource_type=resource_type, field=field, value=url,
                    )
                    continue
                change = changes_by_url.get(str(url))
                if change:
                    matches = set(change.get("evidence_matches") or [])
                    if not matches & STRONG_RESOURCE_EVIDENCE:
                        add_issue(
                            issues, record,
                            "自动写回 URL 没有 DOI、论文链接或论文标题关联证据",
                            resource_type=resource_type, field=field, value=url,
                        )
                elif str(url) in reused_resource_urls:
                    add_issue(
                        issues, record,
                        "旧 Jina 复用 URL 缺少本阶段论文关联证据，需人工复核",
                        severity="warning", resource_type=resource_type,
                        field=field, value=url,
                    )

    rng = random.Random(args.seed)
    samples: list[dict[str, Any]] = []
    for (resource_type, status), group in sorted(groups.items()):
        chosen = group if len(group) <= args.sample_per_status else rng.sample(
            group, args.sample_per_status
        )
        field = RESOURCE_FIELDS[resource_type]
        for record in chosen:
            samples.append({
                "canonical_id": record.get("canonical_id"),
                "title": record.get("title"),
                "source_types": source_types(record),
                "resource_type": resource_type,
                "status": status,
                "urls": (record.get("resources") or {}).get(field) or [],
                "status_detail": (record.get("resource_status") or {}).get(resource_type),
            })

    query_statuses = Counter(str(row.get("status") or "missing") for row in query_logs)
    issue_severities = Counter(row["severity"] for row in issues)
    stats: list[dict[str, Any]] = []
    for resource_type, counts in status_counts.items():
        for status, count in sorted(counts.items()):
            stats.append({
                "category": f"resource_status:{resource_type}",
                "name": status,
                "count": count,
            })
    for name, count in sorted(query_statuses.items()):
        stats.append({"category": "query_status", "name": name, "count": count})
    for name, count in sorted(paper_statuses.items()):
        stats.append({"category": "paper_content_status", "name": name, "count": count})
    eligibility_counts = Counter(
        row["eligibility_status"] for row in eligibility_rows
    )
    for name, count in sorted(eligibility_counts.items()):
        stats.append({"category": "task_eligibility", "name": name, "count": count})
    for name, count in sorted(issue_severities.items()):
        stats.append({"category": "issue_severity", "name": name, "count": count})

    pipeline_integrity_result = (
        "fail" if issue_severities.get("error") else "pass"
    )
    eligible_count = eligibility_counts.get("paper_and_code_candidate", 0)
    if not records or eligible_count == 0:
        eligibility_result = "none"
    elif eligible_count == len(records):
        eligibility_result = "all"
    else:
        eligibility_result = "partial"
    if pipeline_integrity_result == "fail":
        overall_result = "fail"
    elif eligibility_result == "all":
        overall_result = "pass"
    elif eligibility_result == "none":
        overall_result = "fail"
    else:
        overall_result = "partial"

    report = {
        "schema_version": "pipeline-final-quality-check-v2",
        "input_dir": str(input_dir),
        "summary_status": summary.get("status"),
        "records": len(records),
        "queries": len(query_logs),
        "review_candidates": len(reviews),
        "resource_statuses": {
            key: dict(sorted(value.items())) for key, value in status_counts.items()
        },
        "paper_content_statuses": dict(sorted(paper_statuses.items())),
        "task_eligibility": {
            "minimum_gate": "paper_available_and_code_found",
            "status": eligibility_result,
            "paper_and_code_candidates": eligible_count,
            "not_eligible": len(records) - eligible_count,
            "runnable_task_ready": "not_assessed_before_download_and_execution",
        },
        "query_statuses": dict(sorted(query_statuses.items())),
        "issues": dict(sorted(issue_severities.items())),
        "pipeline_integrity_result": pipeline_integrity_result,
        "result": overall_result,
    }
    write_json(output_dir / "09_quality_check.json", report)
    write_xlsx(
        output_dir / "09_quality_review.xlsx", issues, samples, stats,
        eligibility_rows,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
