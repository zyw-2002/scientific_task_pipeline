#!/usr/bin/env python3
"""Deduplicate unified NatureBench records and merge all source evidence.

No network requests are made. Automatic merges use exact identifiers or an
exact normalized title when that title does not carry conflicting DOI values.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
import urllib.parse

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill





DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
SOURCE_PRIORITY = {"manual": 0, "jina": 1, "test_data_v2": 2}

XLSX_COLUMNS = [
    "canonical_id",
    "title",
    "abstract",
    "year",
    "doi",
    "arxiv",
    "openalex",
    "pmid",
    "pmcid",
    "local_path",
    "local_paths",
    "paper_url",
    "paper_urls",
    "pdf_url",
    "pdf_urls",
    "supplementary_urls",
    "code_urls",
    "data_urls",
    "domains",
    "difficulties",
    "tasks",
    "source_types",
    "source_count",
    "source_records",
    "merge_reasons",
    "conflicts",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_title(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", clean(value).casefold()))


def normalize_doi(value: Any) -> str | None:
    text = urllib.parse.unquote(clean(value))
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)]}").casefold()
    for suffix in (".full.pdf", ".abstract", ".full", "/pdf", ".pdf"):
        if doi.endswith(suffix):
            doi = doi[: -len(suffix)]
            break
    return doi or None


def normalize_id(field: str, value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    if field == "doi":
        return normalize_doi(text)
    if field == "arxiv":
        text = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^arxiv:\s*", "", text, flags=re.IGNORECASE)
        return re.sub(r"(?:v\d+)?(?:\.pdf)?$", "", text, flags=re.IGNORECASE) or None
    if field == "openalex":
        return text.rstrip("/").rsplit("/", 1)[-1].upper()
    if field == "pmid":
        match = re.search(r"(\d+)", text)
        return match.group(1) if match else text.casefold()
    if field == "pmcid":
        match = re.search(r"PMC\d+", text, flags=re.IGNORECASE)
        return match.group(0).upper() if match else text.upper()
    return text.casefold()


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
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} 不是 JSON 对象")
            records.append(value)
    return records


def parse_year(value: Any) -> int | None:
    match = re.search(r"\b(18\d{2}|19\d{2}|20\d{2}|21\d{2})\b", clean(value))
    return int(match.group(1)) if match else None


def read_local_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            marker = handle.readline().strip()
            payload = handle.readline().strip()
    except OSError:
        return {}
    if marker != "=== PAPER_METADATA_JSON ===" or not payload:
        return {}
    try:
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def parse_extra_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def hydrate_test_identity(record: dict[str, Any]) -> bool:
    source = record.get("source") or {}
    if source.get("type") != "test_data_v2":
        return False
    local_path = clean((record.get("document") or {}).get("local_path"))
    if not local_path:
        return False
    metadata = read_local_metadata(Path(local_path))
    paper = metadata.get("paper") if isinstance(metadata.get("paper"), dict) else {}
    if not paper:
        return False
    extra = parse_extra_metadata(paper.get("extra_metadata"))
    identifiers = record.setdefault("identifiers", {})
    paper_id = clean(paper.get("id"))
    if not identifiers.get("doi"):
        identifiers["doi"] = normalize_doi(paper.get("doi"))
    if not identifiers.get("arxiv"):
        arxiv_value = paper.get("arxiv_id")
        if not arxiv_value and paper_id.casefold().startswith("arxiv:"):
            arxiv_value = paper_id.split(":", 1)[1]
        identifiers["arxiv"] = normalize_id("arxiv", arxiv_value)
    if not identifiers.get("openalex") and "openalex.org/" in paper_id:
        identifiers["openalex"] = paper_id
    if not identifiers.get("pmid"):
        identifiers["pmid"] = clean(paper.get("pmid")) or None
    if not identifiers.get("pmcid"):
        identifiers["pmcid"] = clean(paper.get("pmcid")) or None
    if not record.get("title"):
        record["title"] = clean(paper.get("title")) or None
    if not record.get("year"):
        record["year"] = parse_year(paper.get("pub_date"))
    document = record.setdefault("document", {})
    doi = identifiers.get("doi")
    if not document.get("paper_url"):
        document["paper_url"] = (
            clean(extra.get("abs_url") or paper.get("paper_url"))
            or (f"https://doi.org/{doi}" if doi else None)
        )
    if not document.get("pdf_url"):
        document["pdf_url"] = clean(paper.get("pdf_url")) or None
    return True


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return False
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1
        return True


def record_identifier_keys(record: dict[str, Any]) -> list[str]:
    identifiers = record.get("identifiers") or {}
    keys: list[str] = []
    for field in ("doi", "arxiv", "openalex", "pmid", "pmcid"):
        value = normalize_id(field, identifiers.get(field))
        if value:
            keys.append(f"{field}:{value}")
    return keys


def deduplicate(
    records: list[dict[str, Any]],
) -> tuple[list[list[int]], dict[tuple[int, int], set[str]], list[dict[str, Any]]]:
    union_find = UnionFind(len(records))
    pair_reasons: dict[tuple[int, int], set[str]] = defaultdict(set)
    key_owner: dict[str, int] = {}

    for index, record in enumerate(records):
        for key in record_identifier_keys(record):
            owner = key_owner.setdefault(key, index)
            if owner != index:
                union_find.union(owner, index)
                pair_reasons[tuple(sorted((owner, index)))].add(key)

    title_groups: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        title = normalize_title(record.get("title"))
        if len(title) >= 12:
            title_groups[title].append(index)

    title_conflicts: list[dict[str, Any]] = []
    for title, indexes in title_groups.items():
        if len(indexes) < 2:
            continue
        dois = {
            normalize_doi((records[index].get("identifiers") or {}).get("doi"))
            for index in indexes
        }
        dois.discard(None)
        if len(dois) > 1:
            title_conflicts.append({
                "normalized_title": title,
                "dois": sorted(dois),
                "records": [source_brief(records[index]) for index in indexes],
                "decision": "not_auto_merged",
            })
            continue
        anchor = indexes[0]
        for index in indexes[1:]:
            union_find.union(anchor, index)
            pair_reasons[tuple(sorted((anchor, index)))].add("exact_normalized_title")

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        groups[union_find.find(index)].append(index)
    ordered_groups = sorted(groups.values(), key=lambda values: min(values))
    return ordered_groups, pair_reasons, title_conflicts


def source_brief(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("source") or {}
    return {
        "source_type": source.get("type"),
        "source_record_id": source.get("record_id"),
        "source_record_path": source.get("record_path"),
        "title": record.get("title"),
        "doi": (record.get("identifiers") or {}).get("doi"),
    }


def source_priority(record: dict[str, Any]) -> tuple[int, int]:
    source_type = clean((record.get("source") or {}).get("type"))
    completeness = sum(bool(value) for value in (
        record.get("title"),
        record.get("abstract"),
        record.get("year"),
        (record.get("identifiers") or {}).get("doi"),
        (record.get("document") or {}).get("paper_url"),
        (record.get("document") or {}).get("pdf_url"),
    ))
    return SOURCE_PRIORITY.get(source_type, 99), -completeness


def choose_text(records: list[dict[str, Any]], field: str, longest: bool = False) -> str | None:
    values: list[tuple[tuple[int, int], str]] = []
    for record in records:
        value = clean(record.get(field))
        if value:
            values.append((source_priority(record), value))
    if not values:
        return None
    if longest:
        return max(values, key=lambda item: (len(item[1]), -item[0][0]))[1]
    return sorted(values, key=lambda item: item[0])[0][1]


def choose_year(records: list[dict[str, Any]]) -> int | None:
    years = [record.get("year") for record in records if isinstance(record.get("year"), int)]
    if not years:
        return None
    counts = Counter(years)
    maximum = max(counts.values())
    candidates = {year for year, count in counts.items() if count == maximum}
    for record in sorted(records, key=source_priority):
        if record.get("year") in candidates:
            return record["year"]
    return min(candidates)


def field_values(records: list[dict[str, Any]], section: str, field: str) -> list[Any]:
    return unique((record.get(section) or {}).get(field) for record in records)


def list_field_values(records: list[dict[str, Any]], section: str, field: str) -> list[Any]:
    return unique(
        value
        for record in records
        for value in ((record.get(section) or {}).get(field) or [])
    )


def preferred_value(records: list[dict[str, Any]], section: str, field: str) -> Any:
    for record in sorted(records, key=source_priority):
        value = (record.get(section) or {}).get(field)
        if value not in (None, ""):
            return value
    return None


def canonical_id(identifiers: dict[str, Any], title: str | None) -> str:
    for field in ("doi", "arxiv", "openalex", "pmid", "pmcid"):
        value = normalize_id(field, identifiers.get(field))
        if value:
            digest = hashlib.sha256(f"{field}:{value}".encode()).hexdigest()[:20]
            return f"paper-{field}-{digest}"
    digest = hashlib.sha256(normalize_title(title).encode()).hexdigest()[:20]
    return f"paper-title-{digest}"


def merge_group(
    indexes: list[int],
    all_records: list[dict[str, Any]],
    pair_reasons: dict[tuple[int, int], set[str]],
) -> dict[str, Any]:
    records = [all_records[index] for index in indexes]
    identifiers: dict[str, Any] = {}
    conflicts: dict[str, list[Any]] = {}
    for field in ("doi", "arxiv", "openalex", "pmid", "pmcid"):
        values = field_values(records, "identifiers", field)
        normalized = unique(normalize_id(field, value) for value in values)
        if len(normalized) > 1:
            conflicts[f"identifiers.{field}"] = values
        identifiers[field] = preferred_value(records, "identifiers", field)

    years = unique(record.get("year") for record in records)
    if len(years) > 1:
        conflicts["year"] = years

    title_values = unique(clean(record.get("title")) for record in records)
    if len({normalize_title(value) for value in title_values}) > 1:
        conflicts["title"] = title_values

    local_paths = field_values(records, "document", "local_path")
    paper_urls = field_values(records, "document", "paper_url")
    pdf_urls = field_values(records, "document", "pdf_url")
    domains = unique(
        clean((record.get("selection_context") or {}).get("domain"))
        for record in records
    )
    difficulties = unique(
        clean((record.get("selection_context") or {}).get("difficulty"))
        for record in records
    )
    tasks = unique({
        "task_id": clean((record.get("selection_context") or {}).get("task_id")) or None,
        "task_name": clean((record.get("selection_context") or {}).get("task_name")) or None,
    } for record in records if any((
        clean((record.get("selection_context") or {}).get("task_id")),
        clean((record.get("selection_context") or {}).get("task_name")),
    )))

    sources: list[dict[str, Any]] = []
    for record in sorted(records, key=source_priority):
        source = dict(record.get("source") or {})
        source["selection_context"] = record.get("selection_context") or {}
        sources.append(source)

    reasons: set[str] = set()
    index_set = set(indexes)
    for (left, right), values in pair_reasons.items():
        if left in index_set and right in index_set:
            reasons.update(values)

    title = choose_text(records, "title")
    merged = {
        "schema_version": "paper-entry-merged-v1",
        "canonical_id": None,
        "identifiers": identifiers,
        "title": title,
        "abstract": choose_text(records, "abstract", longest=True),
        "year": choose_year(records),
        "document": {
            "local_path": local_paths[0] if local_paths else None,
            "local_paths": local_paths,
            "paper_url": preferred_value(records, "document", "paper_url"),
            "paper_urls": paper_urls,
            "pdf_url": preferred_value(records, "document", "pdf_url"),
            "pdf_urls": pdf_urls,
        },
        "resources": {
            "supplementary_urls": list_field_values(records, "resources", "supplementary_urls"),
            "code_urls": list_field_values(records, "resources", "code_urls"),
            "data_urls": list_field_values(records, "resources", "data_urls"),
        },
        "selection_context": {
            "domains": domains,
            "difficulties": difficulties,
            "tasks": tasks,
        },
        "merge": {
            "source_record_count": len(records),
            "merge_reasons": sorted(reasons),
            "conflicts": conflicts,
        },
        "sources": sources,
    }
    merged["canonical_id"] = canonical_id(identifiers, title)
    return merged


def flatten(record: dict[str, Any]) -> dict[str, Any]:
    identifiers = record["identifiers"]
    document = record["document"]
    resources = record["resources"]
    context = record["selection_context"]
    sources = record["sources"]
    return {
        "canonical_id": record["canonical_id"],
        "title": record["title"],
        "abstract": record["abstract"],
        "year": record["year"],
        "doi": identifiers["doi"],
        "arxiv": identifiers["arxiv"],
        "openalex": identifiers["openalex"],
        "pmid": identifiers["pmid"],
        "pmcid": identifiers["pmcid"],
        "local_path": document["local_path"],
        "local_paths": json.dumps(document["local_paths"], ensure_ascii=False),
        "paper_url": document["paper_url"],
        "paper_urls": json.dumps(document["paper_urls"], ensure_ascii=False),
        "pdf_url": document["pdf_url"],
        "pdf_urls": json.dumps(document["pdf_urls"], ensure_ascii=False),
        "supplementary_urls": json.dumps(resources["supplementary_urls"], ensure_ascii=False),
        "code_urls": json.dumps(resources["code_urls"], ensure_ascii=False),
        "data_urls": json.dumps(resources["data_urls"], ensure_ascii=False),
        "domains": json.dumps(context["domains"], ensure_ascii=False),
        "difficulties": json.dumps(context["difficulties"], ensure_ascii=False),
        "tasks": json.dumps(context["tasks"], ensure_ascii=False),
        "source_types": json.dumps(unique(source.get("type") for source in sources), ensure_ascii=False),
        "source_count": len(sources),
        "source_records": json.dumps(sources, ensure_ascii=False),
        "merge_reasons": json.dumps(record["merge"]["merge_reasons"], ensure_ascii=False),
        "conflicts": json.dumps(record["merge"]["conflicts"], ensure_ascii=False),
    }


def safe_cell(value: Any) -> Any:
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
    records: list[dict[str, Any]],
    title_conflicts: list[dict[str, Any]],
) -> None:
    workbook = Workbook(write_only=False)
    papers = workbook.active
    papers.title = "去重后论文"
    papers.append(XLSX_COLUMNS)
    for record in records:
        row = flatten(record)
        papers.append([safe_cell(row[column]) for column in XLSX_COLUMNS])
    style_header(papers)
    papers.column_dimensions["A"].width = 32
    papers.column_dimensions["B"].width = 60
    papers.column_dimensions["C"].width = 80

    duplicates = workbook.create_sheet("合并组")
    duplicates.append(["canonical_id", "title", "source_count", "source_types", "merge_reasons", "sources"])
    for record in records:
        if len(record["sources"]) < 2:
            continue
        duplicates.append([
            record["canonical_id"],
            record["title"],
            len(record["sources"]),
            json.dumps(unique(source.get("type") for source in record["sources"]), ensure_ascii=False),
            json.dumps(record["merge"]["merge_reasons"], ensure_ascii=False),
            safe_cell(json.dumps(record["sources"], ensure_ascii=False)),
        ])
    style_header(duplicates)

    conflicts = workbook.create_sheet("冲突待审核")
    conflicts.append(["类型", "标题或字段", "取值", "记录"])
    for item in title_conflicts:
        conflicts.append([
            "same_title_different_doi",
            item["normalized_title"],
            json.dumps(item["dois"], ensure_ascii=False),
            safe_cell(json.dumps(item["records"], ensure_ascii=False)),
        ])
    for record in records:
        for field, values in record["merge"]["conflicts"].items():
            conflicts.append([
                "merged_value_conflict",
                f"{record['canonical_id']}:{field}",
                safe_cell(json.dumps(values, ensure_ascii=False)),
                record["title"],
            ])
    style_header(conflicts)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deduplicate NatureBench records and merge source evidence"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.input.is_file():
        raise SystemExit(f"输入不存在: {args.input.resolve()}")
    try:
        input_records = read_jsonl(args.input)
        hydrated_test_records = sum(hydrate_test_identity(record) for record in input_records)
        groups, pair_reasons, title_conflicts = deduplicate(input_records)
        merged_records = [
            merge_group(group, input_records, pair_reasons)
            for group in groups
        ]
        output_dir = args.output_dir.resolve()
        write_jsonl(output_dir / "02_deduplicated_papers.jsonl", merged_records)
        write_xlsx(
            output_dir / "02_deduplicated_papers.xlsx",
            merged_records,
            title_conflicts,
        )
        audit = {
            "schema_version": "deduplication-audit-v1",
            "generated_at": now_utc(),
            "input": str(args.input.resolve()),
            "network_requests": 0,
            "input_records": len(input_records),
            "test_records_hydrated_from_local_metadata": hydrated_test_records,
            "canonical_papers": len(merged_records),
            "records_removed_as_duplicates": len(input_records) - len(merged_records),
            "merged_groups": sum(1 for record in merged_records if len(record["sources"]) > 1),
            "same_title_different_doi_conflicts": len(title_conflicts),
            "merged_groups_with_field_conflicts": sum(
                1 for record in merged_records if record["merge"]["conflicts"]
            ),
            "source_combinations": dict(sorted(Counter(
                "+".join(sorted(unique(source.get("type") for source in record["sources"])))
                for record in merged_records
            ).items())),
        }
        write_json(output_dir / "02_deduplication_audit.json", audit)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({
        "status": "ok",
        "output_dir": str(output_dir),
        "input_records": audit["input_records"],
        "canonical_papers": audit["canonical_papers"],
        "duplicates_removed": audit["records_removed_as_duplicates"],
        "review_conflicts": audit["same_title_different_doi_conflicts"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
