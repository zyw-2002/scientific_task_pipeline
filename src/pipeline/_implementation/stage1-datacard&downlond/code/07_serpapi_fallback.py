#!/usr/bin/env python3
"""Use SerpAPI only for gaps left after local article-content extraction.

This stage never overwrites stage-3 files. It supports a no-network planning
mode and a cached search mode. High-confidence candidates are added to the
record; lower-confidence candidates are retained for human review.

Abstract is optional and never triggers a search. SerpAPI snippets are evidence
only and are never written into the abstract field.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


DEFAULT_INPUT = Path(
    "/u01/zhengyawen/pipline/stage1-datacard&downlond/output/"
    "06_validated_resources/06_validated_papers.jsonl"
)
DEFAULT_ENV = Path(
    "/u01/zhengyawen/pipline/stage1-datacard&downlond/env/.env"
)
DEFAULT_JINA_RUNS_ROOT = Path(
    "/u01/zhengyawen/bench/NatureBench/citation_harbor_runs"
)
DEFAULT_OUTPUT_DIR = Path(
    "/u01/zhengyawen/pipline/stage1-datacard&downlond/output/07_serpapi_fallback"
)

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
USER_AGENT = "NatureBenchStage1-SerpAPI/1.0"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ARXIV_RE = re.compile(r"(?:arxiv[:./ ]+|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
PMCID_RE = re.compile(r"\bPMC\d+\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(18\d{2}|19\d{2}|20\d{2}|21\d{2})\b")

CODE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")
DATA_HOSTS = (
    "zenodo.org",
    "figshare.com",
    "figshare.org",
    "datadryad.org",
    "dryad.org",
    "osf.io",
    "kaggle.com",
    "huggingface.co/datasets",
    "ncbi.nlm.nih.gov/geo",
    "ncbi.nlm.nih.gov/sra",
    "ncbi.nlm.nih.gov/bioproject",
    "ncbi.nlm.nih.gov/biosample",
    "trace.ncbi.nlm.nih.gov",
    "ebi.ac.uk/ena",
    "ebi.ac.uk/biostudies",
    "ebi.ac.uk/pride",
    "ebi.ac.uk/arrayexpress",
    "ebi.ac.uk/emdb",
    "ebi.ac.uk/empiar",
    "rcsb.org",
    "wwpdb.org",
    "emdataresource.org",
    "proteomexchange.org",
    "massive.ucsd.edu",
    "dataverse",
)
DATA_QUERY_SITES = (
    "zenodo.org", "figshare.com", "dryad.org", "osf.io",
    "huggingface.co/datasets", "ncbi.nlm.nih.gov", "ebi.ac.uk",
    "rcsb.org", "wwpdb.org", "emdataresource.org",
    "proteomexchange.org", "massive.ucsd.edu",
)
SUPPLEMENT_URL_MARKERS = (
    "supplement", "supp_info", "supp-info", "suppinfo", "suppl_file",
    "supporting-information", "supporting_information", "additional-file",
    "additional_file", "/esm/", "/mmc", "article-supplement", "/suppl/",
    "supinfo", "suppdata", "suppmat",
)
SUPPLEMENT_FILE_EXTENSIONS = (
    ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt",
    ".tar", ".gz", ".tgz", ".7z", ".rar", ".ppt", ".pptx",
)
RESOURCE_FIELDS = {
    "supplementary": "supplementary_urls",
    "code": "code_urls",
    "data": "data_urls",
}
RESOURCE_STATUSES = {
    "found", "not_found", "search_error", "needs_review", "not_applicable",
}

PAPER_COLUMNS = [
    "canonical_id",
    "title",
    "year",
    "doi",
    "arxiv",
    "openalex",
    "pmid",
    "pmcid",
    "paper_url",
    "pdf_urls",
    "supplementary_urls",
    "supplementary_status",
    "code_urls",
    "code_status",
    "data_urls",
    "data_status",
    "source_types",
    "changed_fields",
    "unresolved_fields",
    "review_candidate_count",
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


def load_env_value(path: Path, key: str) -> str:
    if not path.is_file():
        raise ValueError(f"环境变量文件不存在: {path.resolve()}")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].strip()
        if "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() == key:
            result = value.strip().strip("\"").strip("'")
            if result:
                return result
    raise ValueError(f"{path.resolve()} 中没有有效的 {key}")


def source_types(record: dict[str, Any]) -> list[str]:
    return unique(clean(source.get("type")) for source in record.get("sources") or [])


def build_legacy_jina_index(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    index: dict[str, dict[str, Any]] = {}
    stats = {"result_files": 0, "discovery_files": 0, "candidates": 0, "errors": 0}
    for result_path in sorted(root.rglob("jina_rerank_result.json")):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stats["errors"] += 1
            continue
        stats["result_files"] += 1
        discovery_by_id: dict[str, dict[str, Any]] = {}
        discovery_path = result_path.parent / "resource_discovery.json"
        if discovery_path.is_file():
            try:
                discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
                discovery_by_id = {
                    clean(item.get("candidate_id")): item
                    for item in discovery.get("papers") or []
                    if isinstance(item, dict) and clean(item.get("candidate_id"))
                }
                stats["discovery_files"] += 1
            except (OSError, json.JSONDecodeError):
                stats["errors"] += 1
        for raw_candidate in payload.get("selected_candidates") or []:
            if not isinstance(raw_candidate, dict):
                continue
            candidate = dict(raw_candidate)
            candidate["__jina_result_path__"] = str(result_path)
            candidate["__resource_discovery_path__"] = (
                str(discovery_path) if discovery_path.is_file() else None
            )
            candidate["__resource_discovery__"] = discovery_by_id.get(
                clean(candidate.get("candidate_id")), {}
            )
            stats["candidates"] += 1
            keys = (
                clean(candidate.get("candidate_id")),
                f"doi:{normalize_doi(candidate.get('doi'))}" if normalize_doi(candidate.get("doi")) else "",
                f"title:{normalize_title(candidate.get('title'))}" if normalize_title(candidate.get("title")) else "",
            )
            for key in keys:
                if key:
                    index.setdefault(key, candidate)
    return index, stats


def legacy_resource_urls(candidate: dict[str, Any]) -> dict[str, list[str]]:
    result = {
        "pdf_urls": [],
        "supplementary_urls": [],
        "code_urls": [],
        "data_urls": [],
    }
    for resource in candidate.get("resource_candidates") or []:
        if not isinstance(resource, dict):
            continue
        url = clean(resource.get("url"))
        kind = clean(resource.get("kind")).casefold()
        if not url:
            continue
        if kind in {"pdf", "article_pdf"}:
            result["pdf_urls"].append(url)
        elif "supp" in kind:
            result["supplementary_urls"].append(url)
        elif kind in {"github", "gitlab", "code", "repository"}:
            result["code_urls"].append(url)
        elif kind in {"dataset", "data", "huggingface_dataset"}:
            result["data_urls"].append(url)
    discovery = candidate.get("__resource_discovery__")
    if isinstance(discovery, dict):
        result["pdf_urls"].extend(
            clean(item.get("url"))
            for item in discovery.get("pdf") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        result["code_urls"].extend(
            clean(item.get("url"))
            for item in discovery.get("github") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        result["data_urls"].extend(
            clean(item.get("url"))
            for bucket in ("dataset", "huggingface")
            for item in discovery.get(bucket) or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
        result["supplementary_urls"].extend(
            clean(item.get("url"))
            for item in discovery.get("supplementary") or []
            if isinstance(item, dict) and clean(item.get("url"))
        )
    return {field: unique(values) for field, values in result.items()}


def reuse_existing_jina(
    original: dict[str, Any], index: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    record = deepcopy(original)
    candidate: dict[str, Any] | None = None
    for source in record.get("sources") or []:
        if source.get("type") != "jina":
            continue
        candidate = index.get(clean(source.get("record_id")))
        if candidate:
            break
    if not candidate:
        doi = normalize_doi((record.get("identifiers") or {}).get("doi"))
        candidate = index.get(f"doi:{doi}") if doi else None
    if not candidate:
        title = normalize_title(record.get("title"))
        candidate = index.get(f"title:{title}") if title else None
    if not candidate:
        return record, []

    changes: list[dict[str, Any]] = []

    def reuse_change(field: str, old_value: Any, new_value: Any) -> None:
        changes.append({
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "origin": "existing_jina",
            "evidence_path": candidate.get("__jina_result_path__"),
            "resource_discovery_path": candidate.get("__resource_discovery_path__"),
        })

    identifiers = record.setdefault("identifiers", {})
    candidate_ids = {
        "doi": normalize_doi(candidate.get("doi")),
        "arxiv": clean(candidate.get("arxiv_id")) or None,
        "openalex": clean(candidate.get("openalex_id")) or None,
        "pmid": clean(candidate.get("pmid")) or None,
        "pmcid": clean(candidate.get("pmcid")) or None,
    }
    for field, value in candidate_ids.items():
        if not identifiers.get(field) and value:
            identifiers[field] = value
            reuse_change(f"identifiers.{field}", None, value)
    if not record.get("title") and clean(candidate.get("title")):
        record["title"] = clean(candidate.get("title"))
        reuse_change("title", None, record["title"])
    if not record.get("abstract") and clean(candidate.get("abstract")):
        record["abstract"] = clean(candidate.get("abstract"))
        reuse_change("abstract", None, record["abstract"])
    if not record.get("year") and candidate.get("year"):
        record["year"] = candidate.get("year")
        reuse_change("year", None, record["year"])

    document = record.setdefault("document", {})
    document.setdefault("paper_urls", unique([document.get("paper_url")]))
    document.setdefault("pdf_urls", unique([document.get("pdf_url")]))
    paper_url = clean(candidate.get("paper_url"))
    if paper_url and paper_url not in document["paper_urls"]:
        document["paper_urls"].append(paper_url)
        reuse_change("document.paper_urls", None, paper_url)
    if not document.get("paper_url") and paper_url:
        document["paper_url"] = paper_url
        reuse_change("document.paper_url", None, paper_url)

    legacy_urls = legacy_resource_urls(candidate)
    direct_pdf = clean(candidate.get("pdf_url"))
    pdf_urls = unique([direct_pdf, *legacy_urls["pdf_urls"]])
    for url in pdf_urls:
        if url not in document["pdf_urls"]:
            document["pdf_urls"].append(url)
            reuse_change("document.pdf_urls", None, url)
    if not document.get("pdf_url") and document["pdf_urls"]:
        document["pdf_url"] = document["pdf_urls"][0]
        reuse_change("document.pdf_url", None, document["pdf_url"])

    resources = record.setdefault("resources", {})
    for field in ("supplementary_urls", "code_urls", "data_urls"):
        values = resources.setdefault(field, [])
        expected_kind = {
            "supplementary_urls": "supplementary",
            "code_urls": "code",
            "data_urls": "data",
        }[field]
        for url in legacy_urls[field]:
            if url_kind({"link": url}) != expected_kind:
                continue
            if url not in values:
                values.append(url)
                reuse_change(f"resources.{field}", None, url)

    record["existing_source_reuse"] = {
        "origin": "jina_rerank_result_and_resource_discovery",
        "changes": changes,
    }
    return record, changes


def metadata_incomplete(record: dict[str, Any]) -> bool:
    identifiers = record.get("identifiers") or {}
    document = record.get("document") or {}
    return any((
        not record.get("year"),
        not identifiers.get("doi") and not identifiers.get("arxiv"),
        not document.get("paper_url"),
    ))


def current_resource_status(record: dict[str, Any], resource_type: str) -> str | None:
    raw = (record.get("resource_status") or {}).get(resource_type)
    if isinstance(raw, dict):
        value = clean(raw.get("status"))
    else:
        value = clean(raw)
    return value if value in RESOURCE_STATUSES else None


def build_search_attempts(
    record: dict[str, Any],
    query_type: str,
    qualifier: str,
    *,
    engine: str = "google",
    profile: str = "balanced",
) -> list[dict[str, str]]:
    title = clean(record.get("title"))
    identifiers = record.get("identifiers") or {}
    doi = normalize_doi(identifiers.get("doi"))
    arxiv = clean(identifiers.get("arxiv"))
    attempts: list[dict[str, str]] = []

    def add_attempt(basis: str, search_value: str, quoted: bool = True) -> None:
        if not search_value:
            return
        value = f'"{search_value}"' if quoted else search_value
        query_text = f"{value} {qualifier}".strip()
        attempt = {
            "query_type": query_type,
            "engine": engine,
            "q": query_text,
            "search_basis": basis,
        }
        if not any(
            item["engine"] == attempt["engine"] and item["q"] == attempt["q"]
            for item in attempts
        ):
            attempts.append(attempt)

    if doi:
        add_attempt("doi", doi)
    elif arxiv:
        add_attempt("arxiv", f"arXiv:{arxiv}")
    add_attempt("title", title)
    if profile == "thorough":
        add_attempt("title_unquoted", title, quoted=False)
    return attempts


def logical_query(
    record: dict[str, Any],
    query_type: str,
    qualifier: str,
    *,
    engine: str = "google",
    profile: str = "balanced",
) -> dict[str, Any] | None:
    attempts = build_search_attempts(
        record, query_type, qualifier, engine=engine, profile=profile
    )
    if not attempts:
        return None
    return {
        "query_type": query_type,
        "engine": attempts[0]["engine"],
        "q": attempts[0]["q"],
        "attempts": attempts,
        "record_context": {
            "title": record.get("title"),
            "identifiers": record.get("identifiers") or {},
            "document": record.get("document") or {},
        },
    }


def build_query_plan(record: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    title = clean(record.get("title"))
    if not title:
        return []
    queries: list[dict[str, Any]] = []

    def add(query: dict[str, Any] | None) -> None:
        if query:
            queries.append(query)

    if metadata_incomplete(record):
        add(logical_query(
            record, "metadata", "", engine="google_scholar", profile=profile
        ))
    document = record.get("document") or {}
    resources = record.get("resources") or {}
    missing_pdf = not document.get("pdf_url") and not document.get("pdf_urls")
    missing_supplementary = (
        not resources.get("supplementary_urls")
        and current_resource_status(record, "supplementary") != "not_applicable"
    )
    missing_code = (
        not resources.get("code_urls")
        and current_resource_status(record, "code") != "not_applicable"
    )
    missing_data = (
        not resources.get("data_urls")
        and current_resource_status(record, "data") != "not_applicable"
    )
    if missing_pdf:
        add(logical_query(record, "pdf", "filetype:pdf", profile=profile))
    if missing_supplementary:
        add(logical_query(
            record,
            "supplementary",
            'supplementary',
            profile=profile,
        ))
    if missing_code:
        add(logical_query(
            record, "code", '(github OR gitlab OR "source code")', profile=profile
        ))
    if missing_data:
        add(logical_query(
            record,
            "data",
            '(dataset OR repository OR accession OR "data availability")',
            profile=profile,
        ))
    return queries


def query_cache_key(query: dict[str, Any]) -> str:
    raw = json.dumps(query, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def redact_secret(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {key: redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "<redacted>")
    return value


class SerpAPISearchError(ValueError):
    def __init__(self, message: str, api_calls: int) -> None:
        super().__init__(message)
        self.api_calls = api_calls


class ApiBudget:
    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.used = 0
        self._lock = Lock()

    def consume(self) -> None:
        with self._lock:
            if self.limit is not None and self.used >= self.limit:
                raise SerpAPISearchError(
                    f"已达到 --max-api-queries={self.limit} 的请求上限", 0
                )
            self.used += 1


def serpapi_search(
    query: dict[str, Any],
    *,
    api_key: str,
    cache_dir: Path,
    timeout: float,
    retries: int,
    delay: float,
    api_budget: ApiBudget,
) -> tuple[dict[str, Any], bool, int]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{query_cache_key(query)}.json"
    if cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8")), True, 0

    params = {
        "engine": query["engine"],
        "q": query["q"],
        "api_key": api_key,
        "hl": "en",
        "num": "10",
        "output": "json",
    }
    url = f"{SERPAPI_ENDPOINT}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None
    api_calls = 0
    for attempt in range(retries + 1):
        try:
            api_budget.consume()
            api_calls += 1
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("SerpAPI 返回的不是 JSON 对象")
            if payload.get("error"):
                error_text = clean(payload["error"])
                if "hasn't returned any results" not in error_text.casefold():
                    raise ValueError(f"SerpAPI error: {error_text}")
            sanitized = redact_secret(payload, api_key)
            write_json(cache_path, sanitized)
            if delay > 0:
                time.sleep(delay)
            return sanitized, False, api_calls
        except SerpAPISearchError:
            raise
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
            last_error = error
            if attempt >= retries:
                break
            time.sleep(min(2 ** attempt, 8))
    raise SerpAPISearchError(f"SerpAPI 查询失败: {last_error}", api_calls)


def is_absolute_http_url(value: Any) -> bool:
    link = clean(value)
    if not link:
        return False
    parsed = urllib.parse.urlsplit(link)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.path.rstrip("/").casefold() == "/goto" and "url=" in parsed.query.casefold():
        return False
    return True


def result_candidates(payload: dict[str, Any], query: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for fallback_position, result in enumerate(payload.get("organic_results") or [], 1):
        if not isinstance(result, dict):
            continue
        base = {
            "query_type": query["query_type"],
            "engine": query["engine"],
            "position": result.get("position") or fallback_position,
            "title": clean(result.get("title")),
            "link": clean(result.get("link")),
            "snippet": clean(result.get("snippet")),
            "publication_info": clean((result.get("publication_info") or {}).get("summary")),
            "file_format": clean(result.get("file_format")),
            "parent_result": True,
            "search_basis": query.get("search_basis"),
        }
        if is_absolute_http_url(base["link"]):
            candidates.append(base)
        for resource in result.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            link = clean(resource.get("link"))
            if not is_absolute_http_url(link):
                continue
            candidates.append({
                **base,
                "title": clean(resource.get("title")) or base["title"],
                "link": link,
                "file_format": clean(resource.get("file_format")),
                "parent_result": False,
            })
    return candidates


def title_score(paper_title: str, candidate: dict[str, Any]) -> float:
    target = normalize_title(paper_title)
    candidate_title = normalize_title(candidate.get("title"))
    if not target or not candidate_title:
        return 0.0
    sequence = SequenceMatcher(None, target, candidate_title).ratio()
    target_tokens = set(target.split())
    evidence_tokens = set(normalize_title(
        f"{candidate.get('title', '')} {candidate.get('snippet', '')}"
    ).split())
    coverage = len(target_tokens & evidence_tokens) / max(1, len(target_tokens))
    return round(max(sequence, coverage), 6)


def candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(clean(candidate.get(field)) for field in (
        "title", "link", "snippet", "publication_info"
    ))


def is_supplementary_url(link: str) -> bool:
    if not is_absolute_http_url(link):
        return False
    parsed = urllib.parse.urlsplit(link)
    host = parsed.netloc.casefold().removeprefix("www.")
    if host in {
        "researchgate.net", "academia.edu", "reddit.com",
        "academia.stackexchange.com",
    }:
        return False
    path_and_query = urllib.parse.unquote(
        f"{parsed.path}?{parsed.query}"
    ).casefold()
    filename = Path(parsed.path).name.casefold()
    numbered_supplement = bool(re.search(
        r"(?:^|[-_.])(?:supp|suppl|s|mmc|moesm)\d*(?:[-_.]|$)", filename
    ))
    has_marker = any(marker in path_and_query for marker in SUPPLEMENT_URL_MARKERS)
    if not (has_marker or numbered_supplement):
        return False
    path = parsed.path.casefold()
    explicit_attachment_path = any(marker in path_and_query for marker in (
        "/doi/suppl/", "/content/suppl/", "/article-supplement/", "/esm/",
        "/mmc", "downloadsupplement", "suppl_file", "type=supplementary",
        "/supplemental/",
    ))
    return bool(
        path.endswith(SUPPLEMENT_FILE_EXTENSIONS) or explicit_attachment_path
    )


def is_code_repository_url(link: str) -> bool:
    if not is_absolute_http_url(link):
        return False
    parsed = urllib.parse.urlsplit(link)
    host = parsed.netloc.casefold().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if host not in CODE_HOSTS or len(parts) < 2:
        return False
    reserved = {
        "about", "blog", "collections", "contact", "enterprise", "events",
        "explore", "features", "issues", "login", "marketplace", "new",
        "organizations", "orgs", "pricing", "search", "security", "shop",
        "skills", "support", "topics", "trending", "users",
    }
    if parts[0].casefold() in reserved or parts[1].casefold() in reserved:
        return False
    if len(parts) >= 4 and parts[2].casefold() in {"blob", "raw"}:
        if Path(parts[-1]).suffix.casefold() in {".bib", ".cff", ".ris"}:
            return False
    return True


def is_data_record_url(link: str) -> bool:
    if not is_absolute_http_url(link):
        return False
    parsed = urllib.parse.urlsplit(link)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = urllib.parse.unquote(parsed.path).casefold()
    parts = [part for part in path.split("/") if part]
    data_doi = normalize_doi(link)
    if data_doi and data_doi.startswith((
        "10.5281/zenodo.", "10.6084/m9.figshare.", "10.5061/dryad.",
    )):
        return True
    if host == "zenodo.org":
        return len(parts) >= 2 and parts[0] in {"record", "records"}
    if "figshare." in host:
        return (
            "/articles/dataset/" in path
            or "/articles/media/" in path
            or "/ndownloader/files/" in path
            or "/collections/" in path
        )
    if host in {"dryad.org", "datadryad.org"}:
        return "/stash/dataset/" in path or "/dataset/doi:" in path
    if host == "osf.io":
        return (
            len(parts) == 1 and bool(re.fullmatch(r"[a-z0-9]{4,8}", parts[0]))
        ) or "/registrations/" in path
    if host == "kaggle.com":
        return len(parts) >= 3 and parts[0] == "datasets"
    if host == "huggingface.co":
        return len(parts) >= 3 and parts[0] == "datasets"
    lower = link.casefold()
    if any(item in lower for item in (
        "ncbi.nlm.nih.gov/geo", "ncbi.nlm.nih.gov/sra",
        "ncbi.nlm.nih.gov/bioproject", "ncbi.nlm.nih.gov/biosample",
        "trace.ncbi.nlm.nih.gov", "ebi.ac.uk/ena", "ebi.ac.uk/biostudies",
        "ebi.ac.uk/pride", "ebi.ac.uk/arrayexpress", "ebi.ac.uk/emdb",
        "ebi.ac.uk/empiar", "proteomexchange.org/project/",
        "massive.ucsd.edu/proteosafe/",
    )):
        return True
    if host in {"rcsb.org", "www.rcsb.org"}:
        return len(parts) >= 2 and parts[0] in {"structure", "sequence", "groups"}
    if host.endswith("wwpdb.org"):
        return len(parts) >= 1 and parts[0] not in {"", "about", "documentation"}
    if host == "emdataresource.org":
        return len(parts) >= 1 and parts[0] not in {"", "about", "news"}
    return "dataverse" in host and len(parts) >= 1


def extracted_identifiers(candidate: dict[str, Any]) -> dict[str, str | None]:
    text = candidate_text(candidate)
    link = clean(candidate.get("link"))
    doi = normalize_doi(text)
    arxiv_match = ARXIV_RE.search(text)
    pmcid_match = PMCID_RE.search(text)
    pmid_match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", link, re.IGNORECASE)
    openalex_match = re.search(r"openalex\.org/(W\d+)", link, re.IGNORECASE)
    return {
        "doi": doi,
        "arxiv": arxiv_match.group(1) if arxiv_match else None,
        "openalex": f"https://openalex.org/{openalex_match.group(1).upper()}" if openalex_match else None,
        "pmid": pmid_match.group(1) if pmid_match else None,
        "pmcid": pmcid_match.group(0).upper() if pmcid_match else None,
    }


def candidate_year(candidate: dict[str, Any]) -> int | None:
    match = YEAR_RE.search(clean(candidate.get("publication_info")))
    return int(match.group(1)) if match else None


def url_kind(candidate: dict[str, Any]) -> str:
    link = clean(candidate.get("link"))
    if not is_absolute_http_url(link):
        return "invalid"
    lower = link.casefold()
    if is_code_repository_url(link):
        return "code"
    if is_data_record_url(link):
        return "data"
    if is_supplementary_url(link):
        return "supplementary"
    if (
        lower.endswith(".pdf")
        or "/pdf/" in lower
        or lower.startswith("https://arxiv.org/pdf/")
        or clean(candidate.get("file_format")).casefold() == "pdf"
    ):
        return "pdf"
    return "paper"


def doi_matches(record: dict[str, Any], candidate: dict[str, Any]) -> bool:
    record_doi = normalize_doi((record.get("identifiers") or {}).get("doi"))
    candidate_doi = extracted_identifiers(candidate).get("doi")
    return bool(record_doi and candidate_doi and record_doi == candidate_doi)


def confidence(record: dict[str, Any], candidate: dict[str, Any]) -> float:
    score = title_score(clean(record.get("title")), candidate)
    if doi_matches(record, candidate):
        score = 1.0
    kind = url_kind(candidate)
    query_type = clean(candidate.get("query_type"))
    if kind in RESOURCE_FIELDS and query_type == kind:
        paper_url = clean((record.get("document") or {}).get("paper_url"))
        if paper_url and paper_url.casefold() in candidate_text(candidate).casefold():
            score = max(score, 0.95)
    return score


def evidence_matches(record: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    matches: list[str] = []
    if doi_matches(record, candidate):
        matches.append("doi_in_result_or_indexed_page")
    paper_url = clean((record.get("document") or {}).get("paper_url"))
    evidence = candidate_text(candidate).casefold()
    if paper_url and paper_url.casefold() in evidence:
        matches.append("paper_url_in_result_or_indexed_page")
    if title_score(clean(record.get("title")), candidate) >= 0.72:
        matches.append("paper_title_in_result_or_indexed_page")
    if "readme" in evidence:
        matches.append("readme_mentioned_in_search_evidence")
    basis = clean(candidate.get("search_basis"))
    if basis in {"doi", "arxiv"}:
        matches.append(f"returned_by_{basis}_targeted_search")
    return unique(matches)


def candidate_allowed_for_query(candidate: dict[str, Any], query_type: str) -> bool:
    kind = url_kind(candidate)
    if query_type == "metadata":
        return kind in {"paper", "pdf"}
    if query_type == "pdf":
        return kind == "pdf"
    if query_type in RESOURCE_FIELDS:
        return kind == query_type
    return False


def candidate_plausible_for_query(
    record: dict[str, Any], candidate: dict[str, Any], query_type: str
) -> bool:
    if not candidate_allowed_for_query(candidate, query_type):
        return False
    if query_type not in RESOURCE_FIELDS:
        return True
    matches = evidence_matches(record, candidate)
    strong_matches = {
        "doi_in_result_or_indexed_page",
        "paper_url_in_result_or_indexed_page",
        "paper_title_in_result_or_indexed_page",
    }
    if strong_matches & set(matches):
        return True
    return title_score(clean(record.get("title")), candidate) >= 0.45


def execute_logical_query(
    query: dict[str, Any],
    *,
    api_key: str,
    cache_dir: Path,
    timeout: float,
    retries: int,
    delay: float,
    api_budget: ApiBudget,
) -> dict[str, Any]:
    query_type = clean(query.get("query_type"))
    record_context = query.get("record_context") or {}
    attempts = query.get("attempts") or [query]
    all_candidates: list[dict[str, Any]] = []
    attempt_logs: list[dict[str, Any]] = []
    total_api_calls = 0
    total_cache_hits = 0
    successful_attempts = 0

    for attempt_number, attempt in enumerate(attempts, 1):
        attempt_log = {
            "attempt": attempt_number,
            "engine": attempt.get("engine"),
            "query": attempt.get("q"),
            "search_basis": attempt.get("search_basis"),
            "status": "pending",
            "cache_hit": False,
            "candidate_count": 0,
            "target_candidate_count": 0,
            "error": None,
        }
        try:
            payload, cache_hit, api_calls = serpapi_search(
                attempt,
                api_key=api_key,
                cache_dir=cache_dir,
                timeout=timeout,
                retries=retries,
                delay=delay,
                api_budget=api_budget,
            )
            total_api_calls += api_calls
            total_cache_hits += int(cache_hit)
            successful_attempts += 1
            candidates = result_candidates(payload, attempt)
            target_count = sum(
                candidate_plausible_for_query(record_context, candidate, query_type)
                for candidate in candidates
            )
            all_candidates.extend(candidates)
            attempt_log.update({
                "status": "ok",
                "cache_hit": cache_hit,
                "candidate_count": len(candidates),
                "target_candidate_count": target_count,
            })
            attempt_logs.append(attempt_log)
            if target_count > 0:
                break
        except SerpAPISearchError as error:
            total_api_calls += error.api_calls
            attempt_log.update({"status": "error", "error": clean(error)})
            attempt_logs.append(attempt_log)

    errors = [item["error"] for item in attempt_logs if item.get("error")]
    if not successful_attempts:
        status = "error"
    elif errors:
        status = "completed_with_errors"
    else:
        status = "ok"
    return {
        "status": status,
        "candidates": all_candidates,
        "attempts": attempt_logs,
        "attempt_count": len(attempt_logs),
        "api_calls": total_api_calls,
        "cache_hits": total_cache_hits,
        "fallback_used": len(attempt_logs) > 1,
        "candidate_count": len(all_candidates),
        "target_candidate_count": sum(
            candidate_plausible_for_query(record_context, candidate, query_type)
            for candidate in all_candidates
        ),
        "error": " | ".join(errors) if errors else None,
        "attempt_errors": len(errors),
    }


def add_change(
    changes: list[dict[str, Any]],
    field: str,
    old_value: Any,
    new_value: Any,
    candidate: dict[str, Any],
    score: float,
) -> None:
    changes.append({
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "confidence": score,
        "query_type": candidate.get("query_type"),
        "search_basis": candidate.get("search_basis"),
        "evidence_matches": candidate.get("evidence_matches") or [],
        "evidence_title": candidate.get("title"),
        "evidence_url": candidate.get("link"),
    })


def append_url_change(
    record: dict[str, Any],
    field: str,
    url: str,
    candidate: dict[str, Any],
    score: float,
    changes: list[dict[str, Any]],
) -> None:
    resources = record.setdefault("resources", {})
    values = resources.setdefault(field, [])
    if url not in values:
        old_value = list(values)
        values.append(url)
        add_change(changes, f"resources.{field}", old_value, list(values), candidate, score)


def apply_candidates(
    original: dict[str, Any],
    candidates: list[dict[str, Any]],
    query_outcomes: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    record = deepcopy(original)
    query_outcomes = query_outcomes or {}
    identifiers = record.setdefault("identifiers", {})
    document = record.setdefault("document", {})
    document.setdefault("paper_urls", unique([document.get("paper_url")]))
    document.setdefault("pdf_urls", unique([document.get("pdf_url")]))
    resources = record.setdefault("resources", {})
    for field in ("supplementary_urls", "code_urls", "data_urls"):
        resources.setdefault(field, [])

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate = dict(candidate)
        candidate["url_kind"] = url_kind(candidate)
        candidate["confidence"] = confidence(record, candidate)
        candidate["extracted_identifiers"] = extracted_identifiers(candidate)
        candidate["evidence_matches"] = evidence_matches(record, candidate)
        ranked.append(candidate)
    ranked.sort(key=lambda item: (-item["confidence"], int(item.get("position") or 999)))

    changes: list[dict[str, Any]] = []
    review_candidates: list[dict[str, Any]] = []
    accepted_resources: Counter[str] = Counter()
    review_resources: Counter[str] = Counter()
    used_urls: set[str] = set()
    for candidate in ranked:
        score = candidate["confidence"]
        link = clean(candidate.get("link"))
        if not link or link in used_urls:
            continue
        kind = candidate["url_kind"]
        query_type = clean(candidate.get("query_type"))
        if not candidate_allowed_for_query(candidate, query_type):
            continue
        used_urls.add(link)
        threshold = {
            "paper": 0.85,
            "pdf": 0.82,
            "supplementary": 0.82,
            "code": 0.72,
            "data": 0.72,
        }[kind]
        accepted = score >= threshold or doi_matches(record, candidate)
        if not accepted:
            review_floor = {
                "paper": 0.45,
                "pdf": 0.45,
                "supplementary": 0.55,
                "code": 0.3,
                "data": 0.4,
            }[kind]
            if score >= review_floor:
                candidate["review_reason"] = (
                    f"recognized_{kind}_url_but_confidence_below_{threshold}"
                )
                review_candidates.append(candidate)
                if kind in RESOURCE_FIELDS:
                    review_resources[kind] += 1
            continue

        ids = candidate["extracted_identifiers"]
        for field in ("doi", "arxiv", "openalex", "pmid", "pmcid"):
            if not identifiers.get(field) and ids.get(field):
                old_value = identifiers.get(field)
                identifiers[field] = ids[field]
                add_change(changes, f"identifiers.{field}", old_value, ids[field], candidate, score)
        if not record.get("year"):
            year = candidate_year(candidate)
            if year:
                record["year"] = year
                add_change(changes, "year", None, year, candidate, score)

        if kind == "paper":
            if not document.get("paper_url"):
                document["paper_url"] = link
                add_change(changes, "document.paper_url", None, link, candidate, score)
            if link not in document["paper_urls"]:
                document["paper_urls"].append(link)
        elif kind == "pdf":
            if not document.get("pdf_url"):
                document["pdf_url"] = link
                add_change(changes, "document.pdf_url", None, link, candidate, score)
            if link not in document["pdf_urls"]:
                document["pdf_urls"].append(link)
        elif kind == "supplementary":
            append_url_change(record, "supplementary_urls", link, candidate, score, changes)
            accepted_resources[kind] += 1
        elif kind == "code":
            append_url_change(record, "code_urls", link, candidate, score, changes)
            accepted_resources[kind] += 1
        elif kind == "data":
            append_url_change(record, "data_urls", link, candidate, score, changes)
            accepted_resources[kind] += 1

    resource_status = record.setdefault("resource_status", {})
    for resource_type, field in RESOURCE_FIELDS.items():
        existing_status = current_resource_status(original, resource_type)
        urls = resources.get(field) or []
        outcome = query_outcomes.get(resource_type)
        if existing_status == "not_applicable" and not urls:
            status = "not_applicable"
            reason = "preserved_manual_or_upstream_status"
        elif urls:
            status = "found"
            reason = "one_or_more_validated_urls"
        elif review_resources[resource_type] or (
            outcome and outcome.get("target_candidate_count", 0) > 0
        ):
            status = "needs_review"
            reason = "target_type_candidates_did_not_meet_auto_accept_threshold"
        elif outcome and outcome.get("status") in {"error", "completed_with_errors"}:
            status = "search_error"
            reason = "one_or_more_required_search_attempts_failed"
        elif outcome:
            status = "not_found"
            reason = "serpapi_search_completed_without_target_type_candidate"
        else:
            status = "needs_review"
            reason = "resource_type_was_not_searched"
        resource_status[resource_type] = {
            "status": status,
            "reason": reason,
            "url_count": len(urls),
            "accepted_in_this_run": accepted_resources[resource_type],
            "review_candidate_count": review_resources[resource_type],
            "searched": bool(outcome),
            "search_attempts": outcome.get("attempt_count", 0) if outcome else 0,
            "search_errors": outcome.get("attempt_errors", 0) if outcome else 0,
        }

    unresolved: list[str] = []
    if not record.get("year"):
        unresolved.append("year")
    if not identifiers.get("doi") and not identifiers.get("arxiv"):
        unresolved.append("primary_identifier")
    if not document.get("paper_url"):
        unresolved.append("paper_url")
    if not document.get("pdf_url") and not document.get("pdf_urls"):
        unresolved.append("pdf_url")
    for field in ("supplementary_urls", "code_urls", "data_urls"):
        if not resources.get(field):
            unresolved.append(field)

    record["serpapi_enrichment"] = {
        "generated_at": now_utc(),
        "changed_fields": unique(change["field"] for change in changes),
        "changes": changes,
        "unresolved_fields": unresolved,
        "review_candidate_count": len(review_candidates),
        "resource_status": resource_status,
    }
    return record, changes, review_candidates, unresolved


def select_records(records: list[dict[str, Any]], start_index: int, max_records: int | None) -> list[dict[str, Any]]:
    selected = records[start_index:]
    if max_records is not None:
        selected = selected[:max_records]
    return selected


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


def paper_row(record: dict[str, Any]) -> dict[str, Any]:
    identifiers = record.get("identifiers") or {}
    document = record.get("document") or {}
    resources = record.get("resources") or {}
    resource_status = record.get("resource_status") or {}
    enrichment = record.get("serpapi_enrichment") or {}

    def status_value(resource_type: str) -> str | None:
        value = resource_status.get(resource_type)
        return value.get("status") if isinstance(value, dict) else value

    return {
        "canonical_id": record.get("canonical_id"),
        "title": record.get("title"),
        "year": record.get("year"),
        "doi": identifiers.get("doi"),
        "arxiv": identifiers.get("arxiv"),
        "openalex": identifiers.get("openalex"),
        "pmid": identifiers.get("pmid"),
        "pmcid": identifiers.get("pmcid"),
        "paper_url": document.get("paper_url"),
        "pdf_urls": json.dumps(document.get("pdf_urls") or [], ensure_ascii=False),
        "supplementary_urls": json.dumps(resources.get("supplementary_urls") or [], ensure_ascii=False),
        "supplementary_status": status_value("supplementary"),
        "code_urls": json.dumps(resources.get("code_urls") or [], ensure_ascii=False),
        "code_status": status_value("code"),
        "data_urls": json.dumps(resources.get("data_urls") or [], ensure_ascii=False),
        "data_status": status_value("data"),
        "source_types": json.dumps(source_types(record), ensure_ascii=False),
        "changed_fields": json.dumps(enrichment.get("changed_fields") or [], ensure_ascii=False),
        "unresolved_fields": json.dumps(enrichment.get("unresolved_fields") or [], ensure_ascii=False),
        "review_candidate_count": enrichment.get("review_candidate_count", 0),
    }


def write_xlsx(
    path: Path,
    records: list[dict[str, Any]],
    existing_changes: list[dict[str, Any]],
    all_changes: list[dict[str, Any]],
    all_reviews: list[dict[str, Any]],
    query_logs: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    papers = workbook.active
    papers.title = "补全后论文"
    papers.append(PAPER_COLUMNS)
    for record in records:
        row = paper_row(record)
        papers.append([safe_cell(row[column]) for column in PAPER_COLUMNS])
    style_header(papers)
    papers.column_dimensions["A"].width = 32
    papers.column_dimensions["B"].width = 65

    reused = workbook.create_sheet("已有Jina复用")
    reuse_columns = [
        "canonical_id", "title", "field", "old_value", "new_value",
        "origin", "evidence_path", "resource_discovery_path",
    ]
    reused.append(reuse_columns)
    for item in existing_changes:
        reused.append([safe_cell(item.get(column)) for column in reuse_columns])
    style_header(reused)

    changes = workbook.create_sheet("自动写回")
    change_columns = [
        "canonical_id", "title", "field", "old_value", "new_value",
        "confidence", "query_type", "search_basis", "evidence_matches",
        "evidence_title", "evidence_url",
    ]
    changes.append(change_columns)
    for item in all_changes:
        changes.append([safe_cell(item.get(column)) for column in change_columns])
    style_header(changes)

    reviews = workbook.create_sheet("候选待审核")
    review_columns = [
        "canonical_id", "paper_title", "query_type", "url_kind", "confidence",
        "search_basis", "evidence_matches", "review_reason", "position",
        "candidate_title", "link", "snippet",
    ]
    reviews.append(review_columns)
    for item in all_reviews:
        reviews.append([safe_cell(item.get(column)) for column in review_columns])
    style_header(reviews)

    queries = workbook.create_sheet("查询记录")
    query_columns = [
        "canonical_id", "title", "query_type", "engine", "query",
        "status", "attempt_count", "api_calls", "cache_hits", "fallback_used",
        "candidate_count", "target_candidate_count", "search_bases", "attempts", "error",
    ]
    queries.append(query_columns)
    for item in query_logs:
        queries.append([safe_cell(item.get(column)) for column in query_columns])
    style_header(queries)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute SerpAPI enrichment for selected NatureBench papers"
    )
    parser.add_argument("--mode", choices=("plan", "search"), required=True)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--jina-runs-root", type=Path, default=DEFAULT_JINA_RUNS_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--profile", choices=("balanced", "thorough"), default="balanced")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--max-api-queries", type=int)
    parser.add_argument("--allow-large-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="并发执行的 SerpAPI 查询数，范围 1-16；默认 1",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.input.is_file():
        raise SystemExit(f"输入不存在: {args.input.resolve()}")
    if args.start_index < 0:
        raise SystemExit("--start-index 不能小于 0")
    if args.max_records is not None and args.max_records < 1:
        raise SystemExit("--max-records 必须大于 0")
    if args.max_api_queries is not None and args.max_api_queries < 1:
        raise SystemExit("--max-api-queries 必须大于 0")
    if not 1 <= args.concurrency <= 16:
        raise SystemExit("--concurrency 必须在 1 到 16 之间")

    try:
        all_input_records = read_jsonl(args.input)
        raw_selected_records = select_records(
            all_input_records, args.start_index, args.max_records
        )
        jina_index, jina_index_stats = build_legacy_jina_index(args.jina_runs_root)
        selected_records: list[dict[str, Any]] = []
        existing_changes: list[dict[str, Any]] = []
        for raw_record in raw_selected_records:
            reused_record, changes = reuse_existing_jina(raw_record, jina_index)
            selected_records.append(reused_record)
            for change in changes:
                existing_changes.append({
                    "canonical_id": raw_record.get("canonical_id"),
                    "title": raw_record.get("title"),
                    **change,
                })
        plans = [
            {
                "canonical_id": record.get("canonical_id"),
                "title": record.get("title"),
                "queries": build_query_plan(record, args.profile),
            }
            for record in selected_records
        ]
        planned_queries = sum(len(item["queries"]) for item in plans)
        planned_search_attempts = sum(
            len(query.get("attempts") or [query])
            for item in plans
            for query in item["queries"]
        )
        output_dir = args.output_dir.resolve()
        plan_summary = {
            "schema_version": "serpapi-query-plan-v1",
            "generated_at": now_utc(),
            "mode": args.mode,
            "profile": args.profile,
            "input": str(args.input.resolve()),
            "input_records": len(all_input_records),
            "start_index": args.start_index,
            "selected_records": len(selected_records),
            "planned_queries": planned_queries,
            "planned_search_attempts_max": planned_search_attempts,
            "concurrency": args.concurrency,
            "existing_jina_reused_values": len(existing_changes),
            "jina_index": jina_index_stats,
            "query_types": dict(sorted(Counter(
                query["query_type"]
                for item in plans
                for query in item["queries"]
            ).items())),
            "note": (
                "Each logical query tries DOI/arXiv first and only falls back to title "
                "when no target-type result is found. Cached attempts do not call SerpAPI."
            ),
        }
        write_jsonl(output_dir / "07_query_plan.jsonl", plans)
        write_jsonl(output_dir / "07_existing_jina_reuse.jsonl", existing_changes)
        write_json(output_dir / "07_query_plan_summary.json", plan_summary)

        if args.mode == "plan":
            print(json.dumps({"status": "planned", **plan_summary}, ensure_ascii=False, indent=2))
            return

        if len(selected_records) > 100 and not args.allow_large_run:
            raise ValueError(
                "本次超过 100 篇。先使用 --max-records 做试验；确认后全量运行时显式加入 --allow-large-run"
            )
        if (
            args.max_api_queries is not None
            and planned_search_attempts > args.max_api_queries
        ):
            raise ValueError(
                f"最多可能执行 {planned_search_attempts} 个搜索尝试，"
                f"超过 --max-api-queries={args.max_api_queries}"
            )

        api_key = load_env_value(args.env_file, "SERPAPI_API_KEY")
        cache_dir = output_dir / "cache"
        api_budget = ApiBudget(args.max_api_queries)
        enriched_records: list[dict[str, Any]] = []
        query_logs: list[dict[str, Any]] = []
        all_changes: list[dict[str, Any]] = []
        all_reviews: list[dict[str, Any]] = []
        api_calls = 0
        cache_hits = 0
        query_errors = 0
        attempt_errors = 0

        unique_queries: dict[str, dict[str, Any]] = {}
        for plan in plans:
            for query in plan["queries"]:
                unique_queries.setdefault(query_cache_key(query), query)

        query_results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_key = {
                executor.submit(
                    execute_logical_query,
                    query,
                    api_key=api_key,
                    cache_dir=cache_dir,
                    timeout=args.timeout,
                    retries=args.retries,
                    delay=args.delay,
                    api_budget=api_budget,
                ): cache_key
                for cache_key, query in unique_queries.items()
            }
            for future in as_completed(future_to_key):
                cache_key = future_to_key[future]
                result = future.result()
                query_results[cache_key] = result
                api_calls += result["api_calls"]
                cache_hits += result["cache_hits"]
                attempt_errors += result["attempt_errors"]
                if result["status"] != "ok":
                    query_errors += 1

        for record, plan in zip(selected_records, plans):
            candidates: list[dict[str, Any]] = []
            record_outcomes: dict[str, dict[str, Any]] = {}
            for query in plan["queries"]:
                result = query_results[query_cache_key(query)]
                candidates.extend(result["candidates"])
                record_outcomes[query["query_type"]] = result
                query_logs.append({
                    "canonical_id": record.get("canonical_id"),
                    "title": record.get("title"),
                    "query_type": query["query_type"],
                    "engine": query["engine"],
                    "query": query["q"],
                    "status": result["status"],
                    "attempt_count": result["attempt_count"],
                    "api_calls": result["api_calls"],
                    "cache_hits": result["cache_hits"],
                    "fallback_used": result["fallback_used"],
                    "candidate_count": result["candidate_count"],
                    "target_candidate_count": result["target_candidate_count"],
                    "search_bases": json.dumps([
                        item.get("search_basis") for item in result["attempts"]
                    ], ensure_ascii=False),
                    "attempts": json.dumps(result["attempts"], ensure_ascii=False),
                    "error": result["error"],
                })

            enriched, changes, reviews, _ = apply_candidates(
                record, candidates, record_outcomes
            )
            enriched_records.append(enriched)
            for change in changes:
                all_changes.append({
                    "canonical_id": record.get("canonical_id"),
                    "title": record.get("title"),
                    **change,
                })
            for candidate in reviews:
                all_reviews.append({
                    "canonical_id": record.get("canonical_id"),
                    "paper_title": record.get("title"),
                    "query_type": candidate.get("query_type"),
                    "url_kind": candidate.get("url_kind"),
                    "confidence": candidate.get("confidence"),
                    "search_basis": candidate.get("search_basis"),
                    "evidence_matches": json.dumps(
                        candidate.get("evidence_matches") or [], ensure_ascii=False
                    ),
                    "review_reason": candidate.get("review_reason"),
                    "position": candidate.get("position"),
                    "candidate_title": candidate.get("title"),
                    "link": candidate.get("link"),
                    "snippet": candidate.get("snippet"),
                })

        write_jsonl(output_dir / "07_serpapi_enriched.jsonl", enriched_records)
        write_jsonl(output_dir / "07_serpapi_candidates_for_review.jsonl", all_reviews)
        write_jsonl(output_dir / "07_serpapi_query_log.jsonl", query_logs)
        summary = {
            "schema_version": "serpapi-enrichment-summary-v1",
            "generated_at": now_utc(),
            "status": "completed" if query_errors == 0 else "completed_with_errors",
            "profile": args.profile,
            "input": str(args.input.resolve()),
            "output_records": len(enriched_records),
            "planned_queries": planned_queries,
            "planned_search_attempts_max": planned_search_attempts,
            "unique_queries": len(unique_queries),
            "concurrency": args.concurrency,
            "api_calls": api_calls,
            "cache_hits": cache_hits,
            "query_errors": query_errors,
            "attempt_errors": attempt_errors,
            "automatic_changes": len(all_changes),
            "existing_jina_reused_values": len(existing_changes),
            "review_candidates": len(all_reviews),
            "changed_fields": dict(sorted(Counter(
                change["field"] for change in all_changes
            ).items())),
            "remaining_unresolved_fields": dict(sorted(Counter(
                field
                for record in enriched_records
                for field in (record.get("serpapi_enrichment") or {}).get("unresolved_fields") or []
            ).items())),
            "resource_statuses": {
                resource_type: dict(sorted(Counter(
                    current_resource_status(record, resource_type) or "missing"
                    for record in enriched_records
                ).items()))
                for resource_type in RESOURCE_FIELDS
            },
            "abstract_required": False,
            "security": "API key was read from env and was not written to outputs.",
        }
        write_json(output_dir / "07_serpapi_summary.json", summary)
        write_xlsx(
            output_dir / "07_serpapi_enriched.xlsx",
            enriched_records,
            existing_changes,
            all_changes,
            all_reviews,
            query_logs,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps({
        "status": summary["status"],
        "output_dir": str(output_dir),
        "records": summary["output_records"],
        "api_calls": summary["api_calls"],
        "cache_hits": summary["cache_hits"],
        "automatic_changes": summary["automatic_changes"],
        "review_candidates": summary["review_candidates"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
