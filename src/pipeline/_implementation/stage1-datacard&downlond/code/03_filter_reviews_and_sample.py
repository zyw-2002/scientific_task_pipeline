#!/usr/bin/env python3
"""Exclude review-like test_data_v2 papers and sample configurable strata.

The script consumes the cross-source deduplicated JSONL. It preserves every
paper coming from Jina or the manual source, while selecting a configurable
number of non-review test_data_v2 papers for each domain/difficulty stratum.
No network requests are made.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import re
from typing import Any, Iterable
import urllib.parse

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting





ALL_DOMAINS = ("化学", "物理", "生物", "天文", "地理")
SUPPORTED_DOMAINS = (*ALL_DOMAINS, "材料")
ALL_DIFFICULTIES = ("基础", "进阶", "复杂")
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)

TITLE_REVIEW_PATTERNS = (
    ("systematic_review", re.compile(r"\bsystematic\s+review\b", re.IGNORECASE)),
    ("scoping_review", re.compile(r"\bscoping\s+review\b", re.IGNORECASE)),
    ("review", re.compile(
        r"\b(?:narrative|integrative|umbrella|literature|critical|comprehensive|mini|rapid)?[- ]*review\b",
        re.IGNORECASE,
    )),
    ("meta_analysis", re.compile(r"\bmeta[- ]analysis\b", re.IGNORECASE)),
    ("survey", re.compile(r"\b(?:a\s+)?survey\s+(?:of|on)\b", re.IGNORECASE)),
    ("overview", re.compile(r"\ban\s+overview\s+of\b", re.IGNORECASE)),
    ("state_of_the_art", re.compile(r"\bstate[- ]of[- ]the[- ]art\b", re.IGNORECASE)),
    ("bibliometric", re.compile(r"\bbibliometric\s+(?:analysis|review)\b", re.IGNORECASE)),
    ("chinese_review", re.compile(r"综述|系统评价|荟萃分析|元分析|研究进展|文献计量")),
)

ABSTRACT_REVIEW_PATTERNS = (
    ("abstract_systematic_review", re.compile(r"\b(?:this|the present)\s+systematic\s+review\b", re.IGNORECASE)),
    ("abstract_this_review", re.compile(r"\bthis\s+(?:narrative\s+|scoping\s+|literature\s+)?review\b", re.IGNORECASE)),
    ("abstract_we_review", re.compile(r"\bwe\s+(?:systematically\s+)?review(?:ed)?\b", re.IGNORECASE)),
    ("abstract_meta_analysis", re.compile(r"\bsystematic\s+review\s+and\s+meta[- ]analysis\b", re.IGNORECASE)),
    ("abstract_review_aim", re.compile(r"\b(?:aim|objective)\s+of\s+this\s+review\b", re.IGNORECASE)),
    ("abstract_chinese_review", re.compile(r"本文综述|本综述|系统评价了|荟萃分析了")),
)

TYPE_REVIEW_TERMS = {
    "review",
    "systematic review",
    "scoping review",
    "meta-analysis",
    "meta analysis",
    "survey",
    "综述",
    "系统评价",
}

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
    "local_paths",
    "paper_urls",
    "pdf_urls",
    "supplementary_urls",
    "code_urls",
    "data_urls",
    "domains",
    "difficulties",
    "source_types",
    "source_count",
    "sample_domain",
    "sample_difficulty",
    "review_decision",
    "review_reasons",
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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def parse_csv_set(value: str, allowed: tuple[str, ...], label: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    invalid = [item for item in items if item not in allowed]
    if invalid:
        raise ValueError(f"{label} 包含无效值: {invalid}; 可选值: {list(allowed)}")
    if not items:
        raise ValueError(f"{label} 不能为空")
    return unique(items)


def parse_difficulty_counts(value: str) -> dict[str, int]:
    counts = {difficulty: 0 for difficulty in ALL_DIFFICULTIES}
    for item in value.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise ValueError("--difficulty-counts 格式应为 基础=50,进阶=50,复杂=50")
        difficulty, count_text = (part.strip() for part in item.split("=", 1))
        if difficulty not in counts:
            raise ValueError(f"未知难度: {difficulty}")
        try:
            count = int(count_text)
        except ValueError as error:
            raise ValueError(f"{difficulty} 的篇数不是整数: {count_text}") from error
        if count < 0:
            raise ValueError(f"{difficulty} 的篇数不能小于 0")
        counts[difficulty] = count
    if not any(counts.values()):
        raise ValueError("三个难度的抽样篇数不能全部为 0")
    return counts


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


def record_metadata(record: dict[str, Any], cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    paths = (record.get("document") or {}).get("local_paths") or []
    for raw_path in paths:
        path_text = clean(raw_path)
        if not path_text:
            continue
        if path_text not in cache:
            cache[path_text] = read_local_metadata(Path(path_text))
        if cache[path_text]:
            return cache[path_text]
    return {}


def metadata_paper(metadata: dict[str, Any]) -> dict[str, Any]:
    paper = metadata.get("paper")
    return paper if isinstance(paper, dict) else {}


def classify_review(
    record: dict[str, Any], metadata: dict[str, Any], policy: str
) -> dict[str, Any]:
    paper = metadata_paper(metadata)
    title = clean(record.get("title") or paper.get("title"))
    abstract = clean(record.get("abstract") or paper.get("abstract"))
    reasons: list[str] = []

    for reason, pattern in TITLE_REVIEW_PATTERNS:
        if pattern.search(title):
            reasons.append(reason)

    for field in ("publication_type", "document_type", "type", "genre"):
        raw_value = paper.get(field)
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in values:
            normalized = clean(value).casefold()
            if normalized in TYPE_REVIEW_TERMS:
                reasons.append(f"metadata_{field}:{normalized}")

    if policy == "balanced" and abstract:
        abstract_prefix = abstract[:2500]
        for reason, pattern in ABSTRACT_REVIEW_PATTERNS:
            if pattern.search(abstract_prefix):
                reasons.append(reason)

    reasons = unique(reasons)
    return {
        "decision": "exclude_review" if reasons else "keep",
        "reasons": reasons,
        "policy": policy,
        "metadata_available": bool(metadata),
    }


def test_stratum(record: dict[str, Any]) -> tuple[str, str] | None:
    pairs: list[tuple[str, str]] = []
    for source in record.get("sources") or []:
        if source.get("type") != "test_data_v2":
            continue
        context = source.get("selection_context") or {}
        domain = clean(context.get("domain"))
        difficulty = clean(context.get("difficulty"))
        if domain in SUPPORTED_DOMAINS and difficulty in ALL_DIFFICULTIES:
            pairs.append((domain, difficulty))
    if not pairs:
        return None
    counts = Counter(pairs)
    return sorted(counts, key=lambda pair: (-counts[pair], pair[0], pair[1]))[0]


def source_types(record: dict[str, Any]) -> list[str]:
    return unique(clean(source.get("type")) for source in record.get("sources") or [])


def deterministic_sample(
    records: list[dict[str, Any]], count: int, seed: int, label: str
) -> list[dict[str, Any]]:
    if count == 0:
        return []
    ordered = sorted(
        records,
        key=lambda record: (
            normalize_title(record.get("title")),
            clean(record.get("canonical_id")),
        ),
    )
    if len(ordered) < count:
        raise ValueError(f"{label} 需要 {count} 篇，排除综述后只有 {len(ordered)} 篇")
    local_seed = int(hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()[:16], 16)
    return random.Random(local_seed).sample(ordered, count)


def append_unique(target: list[Any], values: Iterable[Any]) -> None:
    existing = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in target}
    for value in values:
        if value in (None, "", []):
            continue
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker not in existing:
            target.append(value)
            existing.add(marker)


def split_urls(value: Any) -> list[str]:
    if isinstance(value, list):
        return unique(url for item in value for url in split_urls(item))
    return unique(re.findall(r"https?://[^\s<>\"']+", str(value or "")))


def hydrate_from_local_metadata(record: dict[str, Any], metadata: dict[str, Any]) -> None:
    if not metadata:
        return
    paper = metadata_paper(metadata)
    extra_raw = paper.get("extra_metadata")
    if isinstance(extra_raw, dict):
        extra = extra_raw
    elif isinstance(extra_raw, str) and extra_raw.strip():
        try:
            parsed = json.loads(extra_raw)
            extra = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            extra = {}
    else:
        extra = {}

    if not record.get("title"):
        record["title"] = clean(paper.get("title")) or None
    if not record.get("abstract"):
        record["abstract"] = clean(paper.get("abstract")) or None
    if not record.get("year"):
        match = re.search(r"\b(18\d{2}|19\d{2}|20\d{2}|21\d{2})\b", clean(paper.get("pub_date")))
        record["year"] = int(match.group(1)) if match else None

    identifiers = record.setdefault("identifiers", {})
    if not identifiers.get("doi"):
        identifiers["doi"] = normalize_doi(paper.get("doi"))
    if not identifiers.get("arxiv"):
        identifiers["arxiv"] = clean(paper.get("arxiv_id")) or None
    if not identifiers.get("pmid"):
        identifiers["pmid"] = clean(paper.get("pmid")) or None
    if not identifiers.get("pmcid"):
        identifiers["pmcid"] = clean(paper.get("pmcid")) or None

    document = record.setdefault("document", {})
    doi = identifiers.get("doi")
    paper_url = clean(extra.get("abs_url") or paper.get("paper_url"))
    if not paper_url and doi:
        paper_url = f"https://doi.org/{doi}"
    pdf_url = clean(paper.get("pdf_url"))
    document.setdefault("paper_urls", [])
    document.setdefault("pdf_urls", [])
    append_unique(document["paper_urls"], [paper_url])
    append_unique(document["pdf_urls"], [pdf_url])
    if not document.get("paper_url") and document["paper_urls"]:
        document["paper_url"] = document["paper_urls"][0]
    if not document.get("pdf_url") and document["pdf_urls"]:
        document["pdf_url"] = document["pdf_urls"][0]

    resources = record.setdefault("resources", {})
    resources.setdefault("supplementary_urls", [])
    resources.setdefault("code_urls", [])
    resources.setdefault("data_urls", [])
    for field in ("supplementary_url", "supplementary_urls"):
        append_unique(resources["supplementary_urls"], split_urls(paper.get(field)))
        append_unique(resources["supplementary_urls"], split_urls(extra.get(field)))
    for field in ("code_url", "code_urls", "github_url", "github_urls"):
        append_unique(resources["code_urls"], split_urls(paper.get(field)))
        append_unique(resources["code_urls"], split_urls(extra.get(field)))
    for field in ("data_url", "data_urls", "dataset_url", "dataset_urls"):
        append_unique(resources["data_urls"], split_urls(paper.get(field)))
        append_unique(resources["data_urls"], split_urls(extra.get(field)))


def flatten(record: dict[str, Any], selection: dict[str, Any] | None = None) -> dict[str, Any]:
    identifiers = record.get("identifiers") or {}
    document = record.get("document") or {}
    resources = record.get("resources") or {}
    context = record.get("selection_context") or {}
    review = (selection or {}).get("review") or {}
    stratum = (selection or {}).get("stratum") or (None, None)
    return {
        "canonical_id": record.get("canonical_id"),
        "title": record.get("title"),
        "abstract": record.get("abstract"),
        "year": record.get("year"),
        "doi": identifiers.get("doi"),
        "arxiv": identifiers.get("arxiv"),
        "openalex": identifiers.get("openalex"),
        "pmid": identifiers.get("pmid"),
        "pmcid": identifiers.get("pmcid"),
        "local_paths": json.dumps(document.get("local_paths") or [], ensure_ascii=False),
        "paper_urls": json.dumps(document.get("paper_urls") or [], ensure_ascii=False),
        "pdf_urls": json.dumps(document.get("pdf_urls") or [], ensure_ascii=False),
        "supplementary_urls": json.dumps(resources.get("supplementary_urls") or [], ensure_ascii=False),
        "code_urls": json.dumps(resources.get("code_urls") or [], ensure_ascii=False),
        "data_urls": json.dumps(resources.get("data_urls") or [], ensure_ascii=False),
        "domains": json.dumps(context.get("domains") or [], ensure_ascii=False),
        "difficulties": json.dumps(context.get("difficulties") or [], ensure_ascii=False),
        "source_types": json.dumps(source_types(record), ensure_ascii=False),
        "source_count": len(record.get("sources") or []),
        "sample_domain": stratum[0],
        "sample_difficulty": stratum[1],
        "review_decision": review.get("decision"),
        "review_reasons": json.dumps(review.get("reasons") or [], ensure_ascii=False),
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


def add_record_sheet(
    workbook: Workbook,
    name: str,
    items: list[tuple[dict[str, Any], dict[str, Any] | None]],
) -> None:
    sheet = workbook.create_sheet(name)
    sheet.append(XLSX_COLUMNS)
    for record, selection in items:
        row = flatten(record, selection)
        sheet.append([safe_cell(row[column]) for column in XLSX_COLUMNS])
    style_header(sheet)
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 60
    sheet.column_dimensions["C"].width = 80


def write_xlsx(
    path: Path,
    final_records: list[dict[str, Any]],
    sampled: list[tuple[dict[str, Any], dict[str, Any]]],
    excluded: list[tuple[dict[str, Any], dict[str, Any]]],
    stats: dict[str, Any],
) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    add_record_sheet(workbook, "最终入选", [(record, None) for record in final_records])
    add_record_sheet(workbook, "test_data抽样", sampled)
    add_record_sheet(workbook, "排除的综述", excluded)
    summary = workbook.create_sheet("抽样统计")
    summary.append(["领域/难度", "综述前", "排除综述", "可抽样", "实际抽取"])
    for key in sorted(stats["strata"]):
        item = stats["strata"][key]
        summary.append([
            key,
            item["before_review_filter"],
            item["excluded_reviews"],
            item["eligible"],
            item["sampled"],
        ])
    style_header(summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Filter test_data_v2 reviews and sample configurable difficulty strata"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--domains", default=setting('sample_domains'))
    parser.add_argument(
        "--difficulty-counts",
        default=setting('difficulty_counts'),
        help="Per-domain sample counts, for example 基础=50,进阶=50,复杂=50",
    )
    parser.add_argument("--seed", type=int, default=setting('seed'))
    parser.add_argument(
        "--review-policy",
        choices=("title", "balanced"),
        default=setting('review_policy'),
        help="title uses title/metadata; balanced also checks abstract review statements",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.input.is_file():
        raise SystemExit(f"输入不存在: {args.input.resolve()}")
    try:
        domains = parse_csv_set(args.domains, SUPPORTED_DOMAINS, "--domains")
        counts = parse_difficulty_counts(args.difficulty_counts)
        records = read_jsonl(args.input)
        metadata_cache: dict[str, dict[str, Any]] = {}
        pools: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        excluded: list[tuple[dict[str, Any], dict[str, Any]]] = []
        non_test_records: dict[str, dict[str, Any]] = {}
        test_record_count = 0
        multi_stratum_records = 0

        for record in records:
            types = source_types(record)
            if any(source_type != "test_data_v2" for source_type in types):
                non_test_records[record["canonical_id"]] = record
            stratum = test_stratum(record)
            if not stratum or stratum[0] not in domains:
                continue
            test_record_count += 1
            test_pairs = {
                (
                    clean((source.get("selection_context") or {}).get("domain")),
                    clean((source.get("selection_context") or {}).get("difficulty")),
                )
                for source in record.get("sources") or []
                if source.get("type") == "test_data_v2"
            }
            if len(test_pairs) > 1:
                multi_stratum_records += 1
            metadata = record_metadata(record, metadata_cache)
            review = classify_review(record, metadata, args.review_policy)
            selection = {"stratum": stratum, "review": review}
            if review["decision"] == "exclude_review":
                excluded.append((record, selection))
            else:
                pools[stratum].append((record, selection))

        sampled: list[tuple[dict[str, Any], dict[str, Any]]] = []
        stratum_stats: dict[str, dict[str, int]] = {}
        shortages: list[str] = []
        for domain in domains:
            for difficulty in ALL_DIFFICULTIES:
                key = (domain, difficulty)
                requested = counts[difficulty]
                eligible_items = pools.get(key, [])
                excluded_count = sum(
                    1 for _, selection in excluded if selection["stratum"] == key
                )
                stratum_stats[f"{domain}/{difficulty}"] = {
                    "before_review_filter": len(eligible_items) + excluded_count,
                    "excluded_reviews": excluded_count,
                    "eligible": len(eligible_items),
                    "sampled": requested,
                }
                if len(eligible_items) < requested:
                    shortages.append(
                        f"{domain}/{difficulty}: 需要 {requested}，可用 {len(eligible_items)}"
                    )
                    continue
                selected_records = deterministic_sample(
                    [record for record, _ in eligible_items],
                    requested,
                    args.seed,
                    f"{domain}/{difficulty}",
                )
                selection_by_id = {
                    record["canonical_id"]: selection
                    for record, selection in eligible_items
                }
                sampled.extend(
                    (record, selection_by_id[record["canonical_id"]])
                    for record in selected_records
                )

        output_dir = args.output_dir.resolve()
        if shortages:
            failure = {
                "status": "insufficient_candidates",
                "generated_at": now_utc(),
                "shortages": shortages,
                "strata": stratum_stats,
            }
            write_json(output_dir / "03_sampling_failure.json", failure)
            raise ValueError("抽样数量不足:\n" + "\n".join(shortages))

        sampled_records: list[dict[str, Any]] = []
        for record, selection in sampled:
            metadata = record_metadata(record, metadata_cache)
            hydrate_from_local_metadata(record, metadata)
            record["test_data_selection"] = {
                "selected": True,
                "domain": selection["stratum"][0],
                "difficulty": selection["stratum"][1],
                "review_check": selection["review"],
                "seed": args.seed,
            }
            sampled_records.append(record)

        final_by_id = dict(non_test_records)
        for record in sampled_records:
            final_by_id[record["canonical_id"]] = record
        final_records = sorted(
            final_by_id.values(),
            key=lambda record: (normalize_title(record.get("title")), record["canonical_id"]),
        )

        write_jsonl(output_dir / "03_selected_all_sources.jsonl", final_records)
        write_jsonl(output_dir / "03_selected_test_data.jsonl", sampled_records)
        write_jsonl(
            output_dir / "03_excluded_reviews.jsonl",
            ({"record": record, "selection": selection} for record, selection in excluded),
        )
        audit = {
            "schema_version": "review-filter-sampling-audit-v1",
            "generated_at": now_utc(),
            "input": str(args.input.resolve()),
            "network_requests": 0,
            "parameters": {
                "domains": domains,
                "difficulty_counts_per_domain": counts,
                "seed": args.seed,
                "review_policy": args.review_policy,
            },
            "counts": {
                "input_canonical_papers": len(records),
                "non_test_canonical_papers_retained": len(non_test_records),
                "test_candidates_in_selected_domains": test_record_count,
                "test_candidates_excluded_as_reviews": len(excluded),
                "test_candidates_sampled": len(sampled_records),
                "final_unique_papers": len(final_records),
                "test_records_with_multiple_strata": multi_stratum_records,
                "local_metadata_files_read": len(metadata_cache),
            },
            "strata": stratum_stats,
        }
        write_json(output_dir / "03_filter_sampling_audit.json", audit)
        write_xlsx(
            output_dir / "03_filtered_sampled_papers.xlsx",
            final_records,
            sampled,
            excluded,
            audit,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps({
        "status": "ok",
        "output_dir": str(output_dir),
        "excluded_reviews": audit["counts"]["test_candidates_excluded_as_reviews"],
        "sampled_test_data": audit["counts"]["test_candidates_sampled"],
        "final_unique_papers": audit["counts"]["final_unique_papers"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
