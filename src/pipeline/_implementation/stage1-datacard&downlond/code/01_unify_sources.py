#!/usr/bin/env python3
"""Unify the three NatureBench paper sources without network requests.

The script only reuses information already present in the source workbooks,
the referenced Jina result JSON files, and test_data_v2 local metadata headers.
Missing fields remain empty for later deduplication, sampling, and API enrichment.

The full run intentionally does not sample test_data_v2. It emits every row
already assigned to 基础/进阶/复杂 so review filtering and configurable sampling
can happen after cross-source deduplication.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import random
import re
from typing import Any, Iterable
import urllib.parse

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting


SCHEMA_VERSION = "paper-entry-v1"
TARGET_DOMAINS = ("化学", "物理", "生物", "天文", "地理")
TARGET_DIFFICULTIES = ("基础", "进阶", "复杂")






DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ARXIV_RE = re.compile(r"(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)

XLSX_COLUMNS = [
    "schema_version",
    "title",
    "abstract",
    "year",
    "doi",
    "arxiv",
    "openalex",
    "pmid",
    "pmcid",
    "local_path",
    "paper_url",
    "pdf_url",
    "supplementary_urls",
    "code_urls",
    "data_urls",
    "domain",
    "difficulty",
    "task_id",
    "task_name",
    "source_rank",
    "source_score",
    "source_type",
    "source_record_id",
    "source_file",
    "source_sheet",
    "source_row",
    "source_record_path",
]

FIELD_DESCRIPTIONS = {
    "schema_version": "统一论文记录格式版本",
    "title": "论文标题",
    "abstract": "论文摘要（可选）；来源中没有时留空，不因缺失单独调用搜索 API",
    "year": "发表年份",
    "doi": "规范化 DOI，不带 https://doi.org/",
    "arxiv": "arXiv 标识符，不带版本号",
    "openalex": "OpenAlex work URL 或 ID",
    "pmid": "PubMed ID",
    "pmcid": "PubMed Central ID",
    "local_path": "已有本地论文文本路径",
    "paper_url": "论文落地页",
    "pdf_url": "论文 PDF 地址",
    "supplementary_urls": "补充材料地址数组，Excel 中存 JSON",
    "code_urls": "代码地址数组，Excel 中存 JSON",
    "data_urls": "数据地址数组，Excel 中存 JSON",
    "domain": "领域",
    "difficulty": "基础/进阶/复杂；来源没有时留空",
    "task_id": "关联的 NatureBench 种子任务 ID",
    "task_name": "关联的 NatureBench 任务名称",
    "source_rank": "来源内部排名，例如 Jina 排名",
    "source_score": "来源内部得分，例如 Jina 分数",
    "source_type": "jina、test_data_v2 或 manual",
    "source_record_id": "来源记录的稳定标识",
    "source_file": "原始来源文件",
    "source_sheet": "原始工作表名称",
    "source_row": "原始行号",
    "source_record_path": "可回溯到原始记录的路径描述",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(html.unescape(str(value)).split())


def normalize_title(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", clean(value).casefold()))


def normalize_doi(value: Any) -> str | None:
    text = urllib.parse.unquote(clean(value))
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)]}").casefold()
    for suffix in (".full.pdf", ".abstract", ".full", "/pdf", ".pdf"):
        if doi.endswith(suffix):
            doi = doi[: -len(suffix)]
            break
    return doi or None


def normalize_arxiv(value: Any) -> str | None:
    match = ARXIV_RE.search(clean(value))
    return match.group(1) if match else None


def parse_year(value: Any) -> int | None:
    match = re.search(r"\b(18\d{2}|19\d{2}|20\d{2}|21\d{2})\b", clean(value))
    return int(match.group(1)) if match else None


def unique_urls(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, list):
            candidates = value
        else:
            candidates = re.findall(r"https?://[^\s<>\"']+", str(value or ""))
        for candidate in candidates:
            url = clean(candidate).rstrip(".,;)]}")
            if url and url not in seen:
                result.append(url)
                seen.add(url)
    return result


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(clean(part).casefold() for part in parts if clean(part))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


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


def read_test_metadata(path: Path) -> dict[str, Any]:
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


def empty_entry() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "identifiers": {
            "doi": None,
            "arxiv": None,
            "openalex": None,
            "pmid": None,
            "pmcid": None,
        },
        "title": None,
        "abstract": None,
        "year": None,
        "document": {
            "local_path": None,
            "paper_url": None,
            "pdf_url": None,
        },
        "resources": {
            "supplementary_urls": [],
            "code_urls": [],
            "data_urls": [],
        },
        "selection_context": {
            "domain": None,
            "difficulty": None,
            "task_id": None,
            "task_name": None,
            "source_rank": None,
            "source_score": None,
        },
        "source": {
            "type": None,
            "record_id": None,
            "file": None,
            "sheet": None,
            "row": None,
            "record_path": None,
        },
    }


def classify_resource_urls(
    candidate: dict[str, Any],
) -> tuple[list[str], list[str], list[str], list[str]]:
    supplementary: list[str] = []
    code: list[str] = []
    data: list[str] = []
    pdf: list[str] = []
    for resource in candidate.get("resource_candidates") or []:
        if not isinstance(resource, dict):
            continue
        url = clean(resource.get("url"))
        kind = clean(resource.get("kind")).casefold()
        if not url:
            continue
        if "supp" in kind:
            supplementary.append(url)
        elif kind in {"github", "gitlab", "code", "repository"}:
            code.append(url)
        elif kind in {"dataset", "data", "huggingface_dataset"}:
            data.append(url)
        elif kind in {"pdf", "article_pdf"}:
            pdf.append(url)
    discovery = candidate.get("__resource_discovery__")
    if isinstance(discovery, dict):
        pdf.extend(
            clean(item.get("url"))
            for item in discovery.get("pdf") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        code.extend(
            clean(item.get("url"))
            for item in discovery.get("github") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        data.extend(
            clean(item.get("url"))
            for bucket in ("dataset", "huggingface")
            for item in discovery.get(bucket) or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        supplementary.extend(
            clean(item.get("url"))
            for item in discovery.get("supplementary") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
    return (
        unique_urls(supplementary),
        unique_urls(code),
        unique_urls(data),
        unique_urls(pdf),
    )


def load_jina_result(path: Path, cache: dict[Path, dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    if path in cache:
        return cache[path]
    indexes: dict[str, dict[str, Any]] = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache[path] = indexes
        return indexes
    discovery_by_id: dict[str, dict[str, Any]] = {}
    discovery_path = path.parent / "resource_discovery.json"
    if discovery_path.is_file():
        try:
            discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
            discovery_by_id = {
                clean(item.get("candidate_id")): item
                for item in discovery.get("papers") or []
                if isinstance(item, dict) and clean(item.get("candidate_id"))
            }
        except (OSError, json.JSONDecodeError):
            discovery_by_id = {}
    for candidate in payload.get("selected_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        candidate = dict(candidate)
        candidate["__resource_discovery__"] = discovery_by_id.get(
            clean(candidate.get("candidate_id")), {}
        )
        candidate["__resource_discovery_path__"] = (
            str(discovery_path) if discovery_path.is_file() else None
        )
        keys = (
            clean(candidate.get("candidate_id")),
            normalize_doi(candidate.get("doi")) or "",
            normalize_title(candidate.get("title")),
        )
        for key in keys:
            if key:
                indexes.setdefault(key, candidate)
    cache[path] = indexes
    return indexes


def load_jina_entries(path: Path, demo: bool) -> list[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    headers = [clean(value) for value in next(sheet.iter_rows(min_row=3, max_row=3, values_only=True))]
    expected = {"候选论文标题", "候选ID", "原始Jina结果路径"}
    if not expected.issubset(set(headers)):
        raise ValueError(f"Jina 工作簿第 3 行不是预期表头: {headers}")

    cache: dict[Path, dict[str, dict[str, Any]]] = {}
    forwarded = [None] * 6
    entries: list[dict[str, Any]] = []
    for row_number, values in enumerate(
        sheet.iter_rows(min_row=4, values_only=True), start=4
    ):
        row = dict(zip(headers, values))
        for index, header in enumerate(headers[:6]):
            if values[index] not in (None, ""):
                forwarded[index] = values[index]
            row[header] = forwarded[index]
        title = clean(row.get("候选论文标题"))
        candidate_id = clean(row.get("候选ID"))
        if not title and not candidate_id:
            continue

        result_path = Path(clean(row.get("原始Jina结果路径")))
        indexes = load_jina_result(result_path, cache) if result_path.is_file() else {}
        doi = normalize_doi(row.get("候选DOI"))
        candidate = (
            indexes.get(candidate_id)
            or indexes.get(doi or "")
            or indexes.get(normalize_title(title))
            or {}
        )
        supplementary, code, data, discovered_pdf = classify_resource_urls(candidate)

        entry = empty_entry()
        entry["identifiers"].update({
            "doi": normalize_doi(candidate.get("doi")) or doi,
            "arxiv": normalize_arxiv(candidate.get("arxiv_id")),
            "openalex": clean(candidate.get("openalex_id")) or None,
            "pmid": clean(candidate.get("pmid")) or None,
            "pmcid": clean(candidate.get("pmcid")) or None,
        })
        entry["title"] = clean(candidate.get("title")) or title or None
        entry["abstract"] = clean(candidate.get("abstract")) or None
        entry["year"] = parse_year(candidate.get("year") or row.get("年份"))
        entry["document"].update({
            "paper_url": clean(candidate.get("paper_url") or row.get("候选文章URL")) or None,
            "pdf_url": clean(
                candidate.get("pdf_url")
                or row.get("候选PDF_URL")
                or (discovered_pdf[0] if discovered_pdf else None)
            ) or None,
        })
        entry["resources"].update({
            "supplementary_urls": supplementary,
            "code_urls": code,
            "data_urls": data,
        })
        entry["selection_context"].update({
            "domain": clean(row.get("领域 / 子领域")) or None,
            "task_id": clean(row.get("种子任务ID")) or None,
            "task_name": clean(row.get("NatureBench任务名称")) or None,
            "source_rank": row.get("候选排名"),
            "source_score": row.get("Jina分数"),
        })
        entry["source"].update({
            "type": "jina",
            "record_id": candidate_id or stable_id("jina", doi, title),
            "file": str(path.resolve()),
            "sheet": sheet.title,
            "row": row_number,
            "record_path": f"{path.resolve()}:sheet={sheet.title},row={row_number}",
            "upstream_result_path": str(result_path) if result_path.is_file() else None,
            "resource_discovery_path": candidate.get("__resource_discovery_path__"),
        })
        entries.append(entry)
        if demo:
            break
    return entries


def deterministic_sample(
    rows: list[dict[str, Any]], count: int, seed: int, label: str
) -> list[dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda row: (
            normalize_title(row.get("title")),
            clean(row.get("local_path")),
            int(row.get("source_row") or 0),
        ),
    )
    if len(ordered) < count:
        raise ValueError(f"{label} 需要 {count} 篇，但只有 {len(ordered)} 篇")
    label_seed = int(hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()[:16], 16)
    return random.Random(label_seed).sample(ordered, count)


def test_row_to_entry(
    source: dict[str, Any], source_file: Path, *, read_local_metadata: bool
) -> dict[str, Any]:
    local_path_text = clean(source.get("local_path"))
    local_path = Path(local_path_text) if local_path_text else None
    metadata = (
        read_test_metadata(local_path)
        if read_local_metadata and local_path
        else {}
    )
    paper = metadata.get("paper") if isinstance(metadata.get("paper"), dict) else {}
    extra = parse_extra_metadata(paper.get("extra_metadata"))
    paper_id = clean(paper.get("id"))
    doi = normalize_doi(paper.get("doi")) or normalize_doi(source.get("doi"))
    arxiv = normalize_arxiv(paper.get("arxiv_id") or paper_id)
    paper_url = clean(
        extra.get("abs_url")
        or paper.get("paper_url")
        or (f"https://doi.org/{doi}" if doi else "")
    )

    supplementary: list[str] = []
    code: list[str] = []
    data: list[str] = []
    for key in ("supplementary_url", "supplementary_urls"):
        supplementary.extend(unique_urls([paper.get(key), extra.get(key)]))
    for key in ("code_url", "code_urls", "github_url", "github_urls"):
        code.extend(unique_urls([paper.get(key), extra.get(key)]))
    for key in ("data_url", "data_urls", "dataset_url", "dataset_urls"):
        data.extend(unique_urls([paper.get(key), extra.get(key)]))

    record_id = clean(metadata.get("file_id") or metadata.get("es_id"))
    if not record_id:
        record_id = stable_id("test", local_path_text, doi, source.get("title"))
    entry = empty_entry()
    entry["identifiers"].update({
        "doi": doi,
        "arxiv": arxiv,
        "openalex": paper_id if "openalex.org/" in paper_id else None,
        "pmid": clean(paper.get("pmid")) or None,
        "pmcid": clean(paper.get("pmcid")) or None,
    })
    entry["title"] = clean(paper.get("title") or source.get("title")) or None
    entry["abstract"] = clean(paper.get("abstract")) or None
    entry["year"] = parse_year(paper.get("pub_date"))
    entry["document"].update({
        "local_path": str(local_path.resolve()) if local_path else None,
        "paper_url": paper_url or None,
        "pdf_url": clean(paper.get("pdf_url")) or None,
    })
    entry["resources"].update({
        "supplementary_urls": unique_urls(supplementary),
        "code_urls": unique_urls(code),
        "data_urls": unique_urls(data),
    })
    entry["selection_context"].update({
        "domain": source["domain"],
        "difficulty": source["difficulty"],
    })
    entry["source"].update({
        "type": "test_data_v2",
        "record_id": record_id,
        "file": str(source_file.resolve()),
        "sheet": source["domain"],
        "row": source["source_row"],
        "record_path": (
            f"{source_file.resolve()}:sheet={source['domain']},row={source['source_row']}"
        ),
    })
    return entry


def load_test_entries(path: Path, seed: int, demo: bool) -> list[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    strata: dict[tuple[str, str], list[dict[str, Any]]] = {
        (domain, difficulty): []
        for domain in TARGET_DOMAINS
        for difficulty in TARGET_DIFFICULTIES
    }
    for domain in TARGET_DOMAINS:
        sheet = workbook[domain]
        headers = [clean(value) for value in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
        for row_number, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            row = dict(zip(headers, values))
            difficulty = clean(row.get("难度建议（待审核）"))
            if difficulty not in TARGET_DIFFICULTIES:
                continue
            strata[(domain, difficulty)].append({
                "domain": domain,
                "difficulty": difficulty,
                "title": row.get("文章标题"),
                "doi": row.get("DOI"),
                "local_path": row.get("原文路径"),
                "source_row": row_number,
            })

    if demo:
        pool = [row for rows in strata.values() for row in rows]
        selected = deterministic_sample(pool, 1, seed, "demo-test_data_v2")
    else:
        selected = [
            row
            for domain in TARGET_DOMAINS
            for difficulty in TARGET_DIFFICULTIES
            for row in strata[(domain, difficulty)]
        ]
    return [
        test_row_to_entry(row, path, read_local_metadata=demo)
        for row in selected
    ]


def load_manual_entries(path: Path, demo: bool) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            title = clean(row.get("论文标题"))
            doi = normalize_doi(row.get("DOI"))
            if not title and not doi:
                continue
            entry = empty_entry()
            entry["identifiers"]["doi"] = doi
            entry["title"] = title or None
            entry["abstract"] = clean(row.get("摘要(源数据)")) or None
            entry["year"] = parse_year(row.get("论文发表日") or row.get("种子年份"))
            entry["document"]["paper_url"] = (
                clean(row.get("DOI链接"))
                or (f"https://doi.org/{doi}" if doi else "")
                or None
            )
            entry["selection_context"].update({
                "domain": clean(row.get("学科")) or None,
                "task_id": clean(row.get("任务ID")) or None,
                "task_name": clean(row.get("任务名称")) or None,
            })
            entry["source"].update({
                "type": "manual",
                "record_id": stable_id("manual", doi, title, row_number),
                "file": str(path.resolve()),
                "sheet": None,
                "row": row_number,
                "record_path": f"{path.resolve()}:row={row_number}",
            })
            entries.append(entry)
            if demo:
                break
    return entries


def flatten_entry(entry: dict[str, Any]) -> dict[str, Any]:
    identifiers = entry["identifiers"]
    document = entry["document"]
    resources = entry["resources"]
    context = entry["selection_context"]
    source = entry["source"]
    return {
        "schema_version": entry["schema_version"],
        "title": entry["title"],
        "abstract": entry["abstract"],
        "year": entry["year"],
        "doi": identifiers["doi"],
        "arxiv": identifiers["arxiv"],
        "openalex": identifiers["openalex"],
        "pmid": identifiers["pmid"],
        "pmcid": identifiers["pmcid"],
        "local_path": document["local_path"],
        "paper_url": document["paper_url"],
        "pdf_url": document["pdf_url"],
        "supplementary_urls": json.dumps(resources["supplementary_urls"], ensure_ascii=False),
        "code_urls": json.dumps(resources["code_urls"], ensure_ascii=False),
        "data_urls": json.dumps(resources["data_urls"], ensure_ascii=False),
        "domain": context["domain"],
        "difficulty": context["difficulty"],
        "task_id": context["task_id"],
        "task_name": context["task_name"],
        "source_rank": context["source_rank"],
        "source_score": context["source_score"],
        "source_type": source["type"],
        "source_record_id": source["record_id"],
        "source_file": source["file"],
        "source_sheet": source["sheet"],
        "source_row": source["row"],
        "source_record_path": source["record_path"],
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def safe_excel_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 32760:
        return value[:32740] + " [TRUNCATED_FOR_XLSX]"
    return value


def style_sheet(sheet: Any, widths: dict[str, int] | None = None) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = fill
    if widths:
        for name, width in widths.items():
            if name in XLSX_COLUMNS:
                index = XLSX_COLUMNS.index(name) + 1
                sheet.column_dimensions[get_column_letter(index)].width = width


def write_xlsx(path: Path, entries: list[dict[str, Any]], audit: dict[str, Any]) -> None:
    workbook = Workbook()
    papers = workbook.active
    papers.title = "统一论文"
    papers.append(XLSX_COLUMNS)
    for entry in entries:
        flat = flatten_entry(entry)
        papers.append([safe_excel_value(flat.get(column)) for column in XLSX_COLUMNS])
    style_sheet(papers, {
        "title": 50,
        "abstract": 80,
        "doi": 32,
        "local_path": 55,
        "paper_url": 55,
        "pdf_url": 55,
        "source_file": 45,
        "source_record_path": 55,
    })

    fields = workbook.create_sheet("字段说明")
    fields.append(["字段", "说明"])
    for column in XLSX_COLUMNS:
        fields.append([column, FIELD_DESCRIPTIONS[column]])
    fields.column_dimensions["A"].width = 28
    fields.column_dimensions["B"].width = 85
    style_sheet(fields)

    stats = workbook.create_sheet("来源统计")
    stats.append(["来源", "记录数"])
    for source_type, count in audit["counts_by_source"].items():
        stats.append([source_type, count])
    stats.append(["总计", audit["total_entries"]])
    stats.column_dimensions["A"].width = 24
    stats.column_dimensions["B"].width = 16
    style_sheet(stats)

    sampling = workbook.create_sheet("test_data统计")
    sampling.append(["领域", "难度", "记录数"])
    for key, count in audit["test_stratum_counts"].items():
        domain, difficulty = key.split("/", 1)
        sampling.append([domain, difficulty, count])
    style_sheet(sampling)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_audit(entries: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    counts_by_source = {source: 0 for source in ("jina", "test_data_v2", "manual")}
    test_stratum_counts: dict[str, int] = {}
    field_coverage = {
        field: 0
        for field in (
            "title", "abstract", "year", "doi", "arxiv", "openalex", "pmid", "pmcid",
            "local_path", "paper_url", "pdf_url", "supplementary_urls", "code_urls", "data_urls",
        )
    }
    for entry in entries:
        source_type = entry["source"]["type"]
        counts_by_source[source_type] += 1
        if source_type == "test_data_v2":
            context = entry["selection_context"]
            key = f"{context['domain']}/{context['difficulty']}"
            test_stratum_counts[key] = test_stratum_counts.get(key, 0) + 1
        values = {
            "title": entry["title"],
            "abstract": entry["abstract"],
            "year": entry["year"],
            **entry["identifiers"],
            **entry["document"],
            **entry["resources"],
        }
        for field in field_coverage:
            if values.get(field):
                field_coverage[field] += 1
    return {
        "schema_version": "unified-source-audit-v1",
        "generated_at": now_utc(),
        "mode": "demo_one_per_source" if args.demo_one_per_source else "full",
        "network_requests": 0,
        "total_entries": len(entries),
        "counts_by_source": counts_by_source,
        "test_stratum_counts": dict(sorted(test_stratum_counts.items())),
        "field_coverage": field_coverage,
        "inputs": {
            "jina_xlsx": str(args.jina_xlsx.resolve()),
            "test_xlsx": str(args.test_xlsx.resolve()),
            "manual_csv": str(args.manual_csv.resolve()),
        },
        "sampling": {
            "domains": list(TARGET_DOMAINS),
            "difficulties": list(TARGET_DIFFICULTIES),
            "seed": args.seed,
            "policy": (
                "one test_data_v2 row for demo"
                if args.demo_one_per_source
                else "no sampling; include all rows already assigned to the three difficulty levels"
            ),
        },
        "note": "Missing fields are intentionally left empty for later API enrichment.",
    }


def validate_inputs(args: argparse.Namespace) -> None:
    inputs = {
        "Jina workbook": args.jina_xlsx,
        "test_data_v2 workbook": args.test_xlsx,
        "manual CSV": args.manual_csv,
    }
    missing = [f"- {label}: {path.resolve()}" for label, path in inputs.items() if not path.is_file()]
    if missing:
        raise ValueError("缺少输入文件:\n" + "\n".join(missing))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unify Jina, test_data_v2, and manual NatureBench paper sources"
    )
    parser.add_argument("--jina-xlsx", type=Path, default=setting('jina_xlsx'))
    parser.add_argument("--test-xlsx", type=Path, default=setting('test_xlsx'))
    parser.add_argument("--manual-csv", type=Path, default=setting('manual_csv'))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=setting('seed'))
    parser.add_argument("--demo-one-per-source", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        validate_inputs(args)
        jina_entries = load_jina_entries(args.jina_xlsx, args.demo_one_per_source)
        test_entries = load_test_entries(
            args.test_xlsx,
            args.seed,
            args.demo_one_per_source,
        )
        manual_entries = load_manual_entries(args.manual_csv, args.demo_one_per_source)
        entries = [*jina_entries, *test_entries, *manual_entries]
        audit = build_audit(entries, args)
        output_dir = args.output_dir.resolve()
        write_jsonl(output_dir / "01_unified_sources.jsonl", entries)
        write_xlsx(output_dir / "01_unified_sources.xlsx", entries, audit)
        write_json(output_dir / "01_unified_sources_audit.json", audit)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({
        "status": "ok",
        "output_dir": str(output_dir),
        "counts": audit["counts_by_source"],
        "total": audit["total_entries"],
        "network_requests": 0,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
