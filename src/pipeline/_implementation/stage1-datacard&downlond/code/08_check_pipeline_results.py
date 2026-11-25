#!/usr/bin/env python3
"""Validate final SerpAPI fallback outputs and create a review workbook."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import random
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


DEFAULT_INPUT_DIR = Path(
    "/u01/zhengyawen/pipline/stage1-datacard&downlond/output/"
    "07_serpapi_fallback"
)
RESOURCE_FIELDS = {
    "supplementary": "supplementary_urls",
    "code": "code_urls",
    "data": "data_urls",
}
VALID_STATUSES = {
    "found", "not_found", "partial_search_error", "search_error",
    "needs_review", "not_applicable",
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
    script = Path(__file__).with_name("07_serpapi_fallback.py")
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


def source_types(record: dict[str, Any]) -> str:
    return "+".join(sorted({
        str(item.get("type") or "")
        for item in record.get("sources") or []
        if item.get("type")
    }))


def safe_cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and len(value) > 32760:
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
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--expected-records", type=int)
    parser.add_argument("--sample-per-status", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261025)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = (args.output_dir or input_dir / "quality_check").resolve()
    prefix = "07" if (input_dir / "07_serpapi_summary.json").is_file() else "04"
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
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
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
                if actual_kind != resource_type:
                    add_issue(
                        issues, record, f"URL 被分类为 {actual_kind}，与字段不一致",
                        resource_type=resource_type, field=field, value=url,
                    )
                    continue
                change = changes_by_url.get(str(url))
                if change:
                    matches = set(change.get("evidence_matches") or [])
                    strong_matches = {
                        "doi_in_result_or_indexed_page",
                        "paper_url_in_result_or_indexed_page",
                        "paper_title_in_result_or_indexed_page",
                    }
                    if not matches & strong_matches:
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
    for name, count in sorted(issue_severities.items()):
        stats.append({"category": "issue_severity", "name": name, "count": count})

    report = {
        "schema_version": "pipeline-final-quality-check-v1",
        "input_dir": str(input_dir),
        "summary_status": summary.get("status"),
        "records": len(records),
        "queries": len(query_logs),
        "review_candidates": len(reviews),
        "resource_statuses": {
            key: dict(sorted(value.items())) for key, value in status_counts.items()
        },
        "query_statuses": dict(sorted(query_statuses.items())),
        "issues": dict(sorted(issue_severities.items())),
        "result": "pass" if not issue_severities.get("error") else "fail",
    }
    write_json(output_dir / "08_quality_check.json", report)
    write_xlsx(
        output_dir / "08_quality_review.xlsx", issues, samples, stats
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
