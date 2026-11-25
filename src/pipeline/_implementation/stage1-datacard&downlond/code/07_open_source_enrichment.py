#!/usr/bin/env python3
"""Enrich paper resources from open scholarly infrastructure.

This stage sits between local full-text extraction and generic web search.
It uses exact paper identifiers whenever possible and records every relation
that caused a resource URL to be accepted or queued for review.

Supported sources:

* Europe PMC full-text XML and supplementary-file endpoint
* DataCite DOI metadata and reverse related-identifier lookup
* OpenAIRE ScholeXplorer publication/data/software links
* a local Papers with Code paper-to-repository dump
* optional local Softcite and DataStet HTTP services

The stage never treats a title-only web result as authoritative. SerpAPI is
kept as the next-stage fallback for fields still unresolved here.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import csv
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any, Iterable, Iterator
import urllib.parse

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
EUROPE_PMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DATACITE_BASE = "https://api.datacite.org"
OPENAIRE_BASE = "https://api.scholexplorer.openaire.eu/v3/Links"
USER_AGENT = "NatureBenchOpenSourceEnrichment/1.0"
URL_RE = re.compile(r"https?://[^\s<>\"'\]\[{}]+", re.IGNORECASE)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return " ".join(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).split())


def normalize_title(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", clean(value).casefold()))


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
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def normalize_doi(value: Any, extractor: Any) -> str | None:
    return extractor.extract_doi(value)


def candidate_id(canonical_id: str, resource_type: str, url: str, source: str) -> str:
    raw = f"{canonical_id}|{resource_type}|{url}|{source}"
    return "open-source-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_candidate(
    record: dict[str, Any],
    *,
    resource_type: str,
    url: str,
    source: str,
    decision: str,
    confidence: float,
    evidence: dict[str, Any],
    extractor: Any,
) -> dict[str, Any] | None:
    normalized = extractor.normalize_url(url)
    if not normalized or resource_type not in RESOURCE_FIELDS:
        return None
    context = clean(evidence.get("context"))
    anchor = clean(evidence.get("anchor"))
    actual = extractor.classify_url(normalized, anchor=anchor, context=context)
    authoritative_type = clean(evidence.get("authoritative_resource_type"))
    if actual != resource_type and authoritative_type != resource_type:
        return None
    canonical_id = clean(record.get("canonical_id"))
    return {
        "candidate_id": candidate_id(canonical_id, resource_type, normalized, source),
        "canonical_id": canonical_id,
        "title": record.get("title"),
        "resource_type": resource_type,
        "url": normalized,
        "source": source,
        "decision": decision,
        "confidence": round(confidence, 3),
        "evidence": evidence,
    }


class CachedHttpClient:
    def __init__(self, cache_dir: Path, timeout: float, retries: int) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self._lock = Lock()
        self.cache_hits = 0
        self.network_calls = 0

    def _cache_path(self, method: str, url: str, extra: str = "") -> Path:
        digest = hashlib.sha256(f"{method}|{url}|{extra}".encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = requests.Request("GET", url, params=params).prepare().url or url
        cache_path = self._cache_path("GET", encoded)
        if cache_path.is_file():
            with self._lock:
                self.cache_hits += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                response = requests.get(
                    encoded,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/xml"},
                    timeout=self.timeout,
                )
                with self._lock:
                    self.network_calls += 1
                if response.status_code == 404:
                    payload = {"status": "not_found", "status_code": 404, "url": encoded}
                else:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type:
                        body: Any = response.json()
                        body_type = "json"
                    else:
                        body = response.text
                        body_type = "text"
                    payload = {
                        "status": "ok",
                        "status_code": response.status_code,
                        "url": str(response.url),
                        "body_type": body_type,
                        "body": body,
                    }
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(cache_path, payload)
                return payload
            except (requests.RequestException, ValueError) as error:
                last_error = error
        raise ValueError(f"GET 失败 {encoded}: {clean(last_error)}")

    def post_pdf(self, url: str, pdf_path: Path) -> dict[str, Any]:
        stat = pdf_path.stat()
        extra = f"{pdf_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        cache_path = self._cache_path("POST_PDF", url, extra)
        if cache_path.is_file():
            with self._lock:
                self.cache_hits += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                with pdf_path.open("rb") as handle:
                    response = requests.post(
                        url,
                        files={"input": (pdf_path.name, handle, "application/pdf")},
                        data={"disambiguate": "0"},
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                        timeout=self.timeout,
                    )
                with self._lock:
                    self.network_calls += 1
                if response.status_code == 204:
                    body: Any = {}
                    status = "no_content"
                else:
                    response.raise_for_status()
                    body = response.json()
                    status = "ok"
                payload = {
                    "status": status,
                    "status_code": response.status_code,
                    "url": str(response.url),
                    "body_type": "json",
                    "body": body,
                }
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(cache_path, payload)
                return payload
            except (requests.RequestException, ValueError) as error:
                last_error = error
        raise ValueError(f"POST PDF 失败 {url}: {clean(last_error)}")


def response_body(payload: dict[str, Any]) -> Any:
    return payload.get("body") if payload.get("status") == "ok" else None


def exact_paper_identifiers(record: dict[str, Any], extractor: Any) -> set[str]:
    identifiers = record.get("identifiers") or {}
    values: set[str] = set()
    doi = normalize_doi(identifiers.get("doi"), extractor)
    if doi:
        values.add(doi.casefold())
    arxiv = clean(identifiers.get("arxiv")).casefold().removeprefix("arxiv:")
    if arxiv:
        values.add(arxiv)
    pmcid = clean(identifiers.get("pmcid")).casefold()
    if pmcid:
        values.add(pmcid)
    pmid = clean(identifiers.get("pmid")).casefold()
    if pmid:
        values.add(pmid)
    return values


def europepmc_candidates(
    record: dict[str, Any], client: CachedHttpClient, extractor: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    identifiers = record.get("identifiers") or {}
    doi = normalize_doi(identifiers.get("doi"), extractor)
    pmcid = clean(identifiers.get("pmcid")).upper()
    pmid = clean(identifiers.get("pmid"))
    if doi:
        query = f'DOI:"{doi}"'
    elif pmcid:
        query = f'EXT_ID:"{pmcid}"'
    elif pmid:
        query = f'EXT_ID:"{pmid}" AND SRC:MED'
    else:
        return [], [{"source": "europepmc", "status": "skipped", "reason": "no_supported_identifier"}]

    logs: list[dict[str, Any]] = []
    search = client.get(
        f"{EUROPE_PMC_BASE}/search",
        params={"query": query, "format": "json", "resultType": "core", "pageSize": 5},
    )
    body = response_body(search) or {}
    results = ((body.get("resultList") or {}).get("result") or []) if isinstance(body, dict) else []
    exact: dict[str, Any] | None = None
    for item in results:
        item_doi = normalize_doi(item.get("doi"), extractor)
        if doi and item_doi == doi:
            exact = item
            break
        if pmcid and clean(item.get("pmcid")).upper() == pmcid:
            exact = item
            break
        if pmid and clean(item.get("pmid")) == pmid:
            exact = item
            break
    logs.append({
        "source": "europepmc",
        "status": "ok",
        "query": query,
        "result_count": len(results),
        "exact_match": bool(exact),
    })
    if not exact:
        return [], logs
    pmcid = clean(exact.get("pmcid")).upper()
    if not pmcid:
        return [], logs
    fulltext = client.get(f"{EUROPE_PMC_BASE}/{urllib.parse.quote(pmcid)}/fullTextXML")
    xml = response_body(fulltext)
    if not isinstance(xml, str) or not xml.strip():
        logs.append({"source": "europepmc_fulltext", "status": "not_found", "pmcid": pmcid})
        return [], logs

    manifest = {"canonical_id": record.get("canonical_id"), "title": record.get("title")}
    local = extractor.extract_url_candidates(
        manifest,
        xml,
        source_kind="article_html",
        source_path=None,
        source_url=fulltext.get("url"),
    )
    candidates: list[dict[str, Any]] = []
    for item in local:
        evidence = dict(item.get("evidence") or {})
        evidence.update({
            "paper_match": "exact_identifier",
            "provider": "Europe PMC fullTextXML",
            "pmcid": pmcid,
        })
        candidate = make_candidate(
            record,
            resource_type=item["resource_type"],
            url=item["url"],
            source="europepmc_fulltext",
            decision="accepted" if item.get("decision_hint") == "auto_accept" else "needs_review",
            confidence=max(0.75, float(item.get("score") or 0)),
            evidence=evidence,
            extractor=extractor,
        )
        if candidate:
            candidates.append(candidate)

    if re.search(r"<supplementary-material\b", xml, re.IGNORECASE):
        supplement_url = f"{EUROPE_PMC_BASE}/{urllib.parse.quote(pmcid)}/supplementaryFiles"
        candidate = make_candidate(
            record,
            resource_type="supplementary",
            url=supplement_url,
            source="europepmc_supplementary",
            decision="accepted",
            confidence=0.99,
            evidence={
                "paper_match": "exact_identifier",
                "provider": "Europe PMC supplementaryFiles",
                "pmcid": pmcid,
                "context": "supplementary-material element present in Europe PMC full text XML",
            },
            extractor=extractor,
        )
        if candidate:
            candidates.append(candidate)
    logs.append({
        "source": "europepmc_fulltext",
        "status": "ok",
        "pmcid": pmcid,
        "candidate_count": len(candidates),
    })
    return candidates, logs


def datacite_resource_type(attributes: dict[str, Any], url: str, extractor: Any) -> str | None:
    resource_types = attributes.get("types") or {}
    general = clean(resource_types.get("resourceTypeGeneral")).casefold()
    if general in {"dataset", "collection"}:
        return "data"
    if general == "software":
        return "code"
    return extractor.classify_url(url)


def datacite_candidates(
    record: dict[str, Any],
    client: CachedHttpClient,
    extractor: Any,
    seed_candidates: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paper_doi = normalize_doi((record.get("identifiers") or {}).get("doi"), extractor)
    if not paper_doi:
        return [], [{"source": "datacite", "status": "skipped", "reason": "no_paper_doi"}]
    resource_urls: list[str] = []
    resources = record.get("resources") or {}
    for field in RESOURCE_FIELDS.values():
        resource_urls.extend(clean(url) for url in resources.get(field) or [])
    resource_urls.extend(
        clean(item.get("url")) for item in (seed_candidates or [])
        if item.get("resource_type") in {"code", "data"}
    )
    resource_dois = sorted({
        doi for doi in (normalize_doi(url, extractor) for url in resource_urls)
        if doi and doi != paper_doi
    })
    if not resource_dois:
        return [], [{
            "source": "datacite", "status": "skipped",
            "reason": "no_discovered_resource_doi_to_validate",
        }]
    candidates: list[dict[str, Any]] = []
    metadata_found = 0
    metadata_errors: list[dict[str, str]] = []
    strong_relation_types = {
        "is-supplement-to", "is-source-of", "is-part-of", "is-derived-from",
        "is-identical-to", "compiles", "documents",
    }
    for resource_doi in resource_dois:
        try:
            metadata_payload = client.get(
                f"{DATACITE_BASE}/dois/{urllib.parse.quote(resource_doi, safe='/')}"
            )
        except ValueError as error:
            metadata_errors.append({"resource_doi": resource_doi, "error": clean(error)})
            continue
        metadata_body = response_body(metadata_payload) or {}
        metadata = metadata_body.get("data") if isinstance(metadata_body, dict) else None
        if not metadata:
            continue
        metadata_found += 1
        attributes = metadata.get("attributes") or {}
        url = clean(attributes.get("url")) or f"https://doi.org/{resource_doi}"
        resource_type = datacite_resource_type(attributes, url, extractor)
        if resource_type not in {"code", "data"}:
            continue
        exact_relations = [
            item for item in attributes.get("relatedIdentifiers") or []
            if normalize_doi(item.get("relatedIdentifier"), extractor) == paper_doi
        ]
        for relation in exact_relations:
            relation_type = clean(relation.get("relationType"))
            normalized_relation = re.sub(r"(?<!^)(?=[A-Z])", "-", relation_type).casefold()
            decision = (
                "accepted" if normalized_relation in strong_relation_types else "needs_review"
            )
            candidate = make_candidate(
                record,
                resource_type=resource_type,
                url=url,
                source="datacite",
                decision=decision,
                confidence=0.99 if decision == "accepted" else 0.72,
                evidence={
                    "paper_match": "exact_doi_in_resource_metadata",
                    "provider": "DataCite REST API",
                    "paper_doi": paper_doi,
                    "resource_doi": resource_doi,
                    "relation_type": relation_type,
                    "resource_type_general": (attributes.get("types") or {}).get("resourceTypeGeneral"),
                    "authoritative_resource_type": resource_type,
                },
                extractor=extractor,
            )
            if candidate:
                candidates.append(candidate)
    if metadata_errors and metadata_found:
        status = "partial"
    elif metadata_errors:
        status = "unavailable"
    else:
        status = "ok"
    return candidates, [{
        "source": "datacite",
        "status": status,
        "paper_doi": paper_doi,
        "resource_doi_count": len(resource_dois),
        "resource_metadata_count": metadata_found,
        "candidate_count": len(candidates),
        "metadata_errors": metadata_errors,
    }]


def entity_ids(entity: dict[str, Any], extractor: Any) -> set[str]:
    values: set[str] = set()
    for item in entity.get("Identifier") or []:
        raw = clean(item.get("ID"))
        scheme = clean(item.get("IDScheme")).casefold()
        if scheme == "doi":
            doi = normalize_doi(raw, extractor)
            if doi:
                values.add(doi.casefold())
        elif raw:
            values.add(raw.casefold().removeprefix("arxiv:"))
    return values


def entity_url(entity: dict[str, Any], extractor: Any) -> str | None:
    identifiers = entity.get("Identifier") or []
    for item in identifiers:
        if clean(item.get("IDScheme")).casefold() == "doi":
            doi = normalize_doi(item.get("ID"), extractor)
            if doi:
                return f"https://doi.org/{doi}"
    for item in identifiers:
        url = extractor.normalize_url(item.get("IDURL"))
        if url:
            return url
    return None


def entity_resource_type(entity: dict[str, Any]) -> str | None:
    raw = entity.get("Type")
    if isinstance(raw, dict):
        raw = raw.get("Name") or raw.get("name") or raw.get("value")
    value = clean(raw).casefold()
    if "dataset" in value or value in {"data", "research data"}:
        return "data"
    if "software" in value:
        return "code"
    return None


def openaire_candidates(
    record: dict[str, Any], client: CachedHttpClient, extractor: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paper_ids = exact_paper_identifiers(record, extractor)
    doi = normalize_doi((record.get("identifiers") or {}).get("doi"), extractor)
    arxiv = clean((record.get("identifiers") or {}).get("arxiv")).removeprefix("arXiv:")
    pid = doi or arxiv
    if not pid:
        return [], [{"source": "openaire", "status": "skipped", "reason": "no_doi_or_arxiv"}]
    links: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    for side in ("sourcePid", "targetPid"):
        payload = client.get(OPENAIRE_BASE, params={side: pid})
        body = response_body(payload) or {}
        found = (body.get("result") or []) if isinstance(body, dict) else []
        links.extend(found)
        logs.append({"source": "openaire", "status": "ok", "side": side, "pid": pid, "result_count": len(found)})

    candidates: list[dict[str, Any]] = []
    strong_relations = {
        "issupplementto", "issourceof", "documents", "describes",
        "compiles", "requires", "haspart", "ispartof",
    }
    seen_links: set[str] = set()
    for link in links:
        source = link.get("source") or {}
        target = link.get("target") or {}
        source_match = bool(entity_ids(source, extractor) & paper_ids)
        target_match = bool(entity_ids(target, extractor) & paper_ids)
        if source_match == target_match:
            continue
        resource = target if source_match else source
        resource_type = entity_resource_type(resource)
        url = entity_url(resource, extractor)
        if resource_type not in {"code", "data"} or not url:
            continue
        relationship = link.get("RelationshipType") or {}
        relation = clean(relationship.get("Name") or relationship.get("SubType"))
        subtype = clean(relationship.get("SubType"))
        normalized_relation = re.sub(r"[^a-z]", "", f"{relation} {subtype}".casefold())
        decision = "accepted" if any(item in normalized_relation for item in strong_relations) else "needs_review"
        marker = f"{resource_type}|{url}|{relation}|{subtype}"
        if marker in seen_links:
            continue
        seen_links.add(marker)
        candidate = make_candidate(
            record,
            resource_type=resource_type,
            url=url,
            source="openaire_scholexplorer",
            decision=decision,
            confidence=0.97 if decision == "accepted" else 0.7,
            evidence={
                "paper_match": "exact_pid",
                "provider": "OpenAIRE ScholeXplorer",
                "paper_pid": pid,
                "relation_name": relation,
                "relation_subtype": subtype,
                "resource_title": resource.get("Title"),
                "resource_entity_type": resource.get("Type"),
                "authoritative_resource_type": resource_type,
                "link_providers": link.get("LinkProvider") or [],
            },
            extractor=extractor,
        )
        if candidate:
            candidates.append(candidate)
    return candidates, logs


def iter_pwc_rows(path: Path) -> Iterator[dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix == ".jsonl":
        yield from read_jsonl(path)
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("data") or []
        for row in rows:
            if isinstance(row, dict):
                yield row
    elif suffix == ".csv":
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            yield from csv.DictReader(handle)
    elif suffix in {".parquet", ".pq"}:
        try:
            import pandas as pd
        except ImportError as error:
            raise ValueError("读取 Papers with Code parquet 需要安装 pandas 和 pyarrow") from error
        for row in pd.read_parquet(path).to_dict(orient="records"):
            yield row
    else:
        raise ValueError("--paperswithcode-dump 仅支持 json/jsonl/csv/parquet")


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return clean(value).casefold() in {"1", "true", "yes", "y"}


def build_pwc_index(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    if not path.is_file():
        raise ValueError(f"Papers with Code 数据文件不存在: {path.resolve()}")
    index: dict[str, list[dict[str, Any]]] = {}
    for row in iter_pwc_rows(path):
        arxiv = clean(row.get("paper_arxiv_id")).casefold().removeprefix("arxiv:")
        title = normalize_title(row.get("paper_title"))
        if arxiv:
            index.setdefault(f"arxiv:{arxiv}", []).append(row)
        if title:
            index.setdefault(f"title:{title}", []).append(row)
    return index


def paperswithcode_candidates(
    record: dict[str, Any], index: dict[str, list[dict[str, Any]]], extractor: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not index:
        return [], [{"source": "paperswithcode", "status": "skipped", "reason": "no_local_dump"}]
    identifiers = record.get("identifiers") or {}
    arxiv = clean(identifiers.get("arxiv")).casefold().removeprefix("arxiv:")
    title = normalize_title(record.get("title"))
    rows = index.get(f"arxiv:{arxiv}", []) if arxiv else []
    match_basis = "exact_arxiv"
    if not rows and title:
        rows = index.get(f"title:{title}", [])
        match_basis = "exact_normalized_title"
    candidates: list[dict[str, Any]] = []
    for row in rows:
        url = clean(row.get("repo_url"))
        official = truthy(row.get("is_official"))
        mentioned = truthy(row.get("mentioned_in_paper"))
        decision = "accepted" if official or mentioned else "needs_review"
        candidate = make_candidate(
            record,
            resource_type="code",
            url=url,
            source="paperswithcode_dump",
            decision=decision,
            confidence=0.98 if official else 0.94 if mentioned else 0.65,
            evidence={
                "paper_match": match_basis,
                "provider": "Papers with Code open dump",
                "paper_arxiv_id": row.get("paper_arxiv_id"),
                "paper_title": row.get("paper_title"),
                "is_official": official,
                "mentioned_in_paper": mentioned,
                "mentioned_in_github": truthy(row.get("mentioned_in_github")),
            },
            extractor=extractor,
        )
        if candidate:
            candidates.append(candidate)
    return candidates, [{
        "source": "paperswithcode",
        "status": "ok",
        "match_basis": match_basis if rows else None,
        "result_count": len(rows),
        "candidate_count": len(candidates),
    }]


def recursive_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from recursive_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from recursive_strings(item)


def attribute_value(container: dict[str, Any], name: str) -> bool:
    for key in ("mentionContextAttributes", "documentContextAttributes"):
        value = (container.get(key) or {}).get(name)
        if isinstance(value, dict) and value.get("value") is True:
            return True
        if value is True:
            return True
    return False


def mention_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        strings = list(recursive_strings(value))
        if any(URL_RE.search(item) for item in strings):
            yield value
        for child in value.values():
            yield from mention_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from mention_dicts(child)


def service_candidates(
    record: dict[str, Any],
    *,
    payload: dict[str, Any],
    source: str,
    expected_type: str,
    extractor: Any,
) -> list[dict[str, Any]]:
    body = response_body(payload)
    if not isinstance(body, (dict, list)):
        return []
    candidates_by_url: dict[str, dict[str, Any]] = {}
    for mention in mention_dicts(body):
        shared = attribute_value(mention, "shared")
        created = attribute_value(mention, "created")
        used = attribute_value(mention, "used")
        context = clean(mention.get("context") or " ".join(recursive_strings(mention)))
        for text in recursive_strings(mention):
            for match in URL_RE.finditer(text):
                url = extractor.normalize_url(match.group(0))
                if not url:
                    continue
                actual = extractor.classify_url(url, context=context)
                if actual != expected_type:
                    continue
                decision = "accepted" if shared else "needs_review"
                candidate = make_candidate(
                    record,
                    resource_type=expected_type,
                    url=url,
                    source=source,
                    decision=decision,
                    confidence=0.95 if shared else 0.68,
                    evidence={
                        "paper_match": "pdf_processed_directly",
                        "provider": source,
                        "shared": shared,
                        "created": created,
                        "used": used,
                        "context": context[:1500],
                    },
                    extractor=extractor,
                )
                if candidate:
                    previous = candidates_by_url.get(url)
                    if previous is None or (
                        candidate["decision"] == "accepted"
                        and previous["decision"] != "accepted"
                    ):
                        candidates_by_url[url] = candidate
    return list(candidates_by_url.values())


def choose_pdf(manifest: dict[str, Any] | None) -> Path | None:
    for artifact in (manifest or {}).get("artifacts") or []:
        if artifact.get("kind") != "article_pdf" or artifact.get("status") != "downloaded":
            continue
        path = Path(str(artifact.get("path") or ""))
        try:
            if path.is_file() and path.stat().st_size > 1024:
                with path.open("rb") as handle:
                    if handle.read(5) == b"%PDF-":
                        return path
        except OSError:
            continue
    return None


def local_service_candidates(
    record: dict[str, Any],
    manifest: dict[str, Any] | None,
    client: CachedHttpClient,
    extractor: Any,
    *,
    softcite_url: str | None,
    datastet_url: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pdf_path = choose_pdf(manifest)
    if not pdf_path:
        return [], [{"source": "local_mention_services", "status": "skipped", "reason": "no_valid_pdf"}]
    candidates: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    services = [
        ("softcite", softcite_url, "/service/annotateSoftwarePDF", "code"),
        ("datastet", datastet_url, "/service/annotateDatasetPDF", "data"),
    ]
    for source, base_url, endpoint, expected_type in services:
        if not base_url:
            continue
        url = base_url.rstrip("/") + endpoint
        payload = client.post_pdf(url, pdf_path)
        found = service_candidates(
            record,
            payload=payload,
            source=source,
            expected_type=expected_type,
            extractor=extractor,
        )
        candidates.extend(found)
        logs.append({
            "source": source,
            "status": payload.get("status"),
            "pdf_path": str(pdf_path),
            "candidate_count": len(found),
        })
    return candidates, logs


def merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in candidates:
        grouped.setdefault((item["resource_type"], item["url"]), []).append(item)
    merged: list[dict[str, Any]] = []
    for group in grouped.values():
        best = max(group, key=lambda item: (item["decision"] == "accepted", item["confidence"]))
        item = deepcopy(best)
        item["sources"] = sorted({entry["source"] for entry in group})
        item["all_evidence"] = [entry["evidence"] for entry in group]
        if any(entry["decision"] == "accepted" for entry in group):
            item["decision"] = "accepted"
        item["confidence"] = max(float(entry["confidence"]) for entry in group)
        merged.append(item)
    return sorted(merged, key=lambda item: (item["resource_type"], -item["confidence"], item["url"]))


def apply_candidates(
    original: dict[str, Any], candidates: list[dict[str, Any]], logs: list[dict[str, Any]]
) -> dict[str, Any]:
    record = deepcopy(original)
    resources = record.setdefault("resources", {})
    provenance = record.setdefault("resource_provenance", [])
    accepted_counts: Counter[str] = Counter()
    review_counts: Counter[str] = Counter()
    for resource_type, field in RESOURCE_FIELDS.items():
        resources[field] = unique(resources.get(field) or [])
    for candidate in candidates:
        resource_type = candidate["resource_type"]
        if candidate["decision"] == "accepted":
            field = RESOURCE_FIELDS[resource_type]
            if candidate["url"] not in resources[field]:
                resources[field].append(candidate["url"])
                provenance.append({
                    "resource_type": resource_type,
                    "url": candidate["url"],
                    "origin": "open_source_enrichment",
                    "candidate_id": candidate["candidate_id"],
                    "source": candidate["source"],
                    "confidence": candidate["confidence"],
                    "evidence": candidate["evidence"],
                })
            accepted_counts[resource_type] += 1
        elif candidate["decision"] == "needs_review":
            review_counts[resource_type] += 1

    statuses = record.setdefault("resource_status", {})
    for resource_type, field in RESOURCE_FIELDS.items():
        urls = resources.get(field) or []
        previous = statuses.get(resource_type) or {}
        previous_status = previous.get("status") if isinstance(previous, dict) else previous
        if urls:
            status = "found"
            reason = "validated_local_or_open_source_relation"
        elif previous_status == "not_applicable":
            status = "not_applicable"
            reason = "preserved_manual_or_upstream_status"
        elif review_counts[resource_type]:
            status = "needs_review"
            reason = "open_source_candidates_require_review"
        else:
            status = previous_status or "not_found"
            reason = (previous.get("reason") if isinstance(previous, dict) else None) or "not_found_before_web_search"
        statuses[resource_type] = {
            "status": status,
            "reason": reason,
            "url_count": len(urls),
            "accepted_from_open_sources": accepted_counts[resource_type],
            "open_source_review_candidate_count": review_counts[resource_type],
            "serpapi_fallback_required": not bool(urls) and status != "not_applicable",
        }

    unresolved = [
        field for field in RESOURCE_FIELDS.values() if not resources.get(field)
    ]
    record["open_source_enrichment"] = {
        "generated_at": now_utc(),
        "candidate_count": len(candidates),
        "accepted_count": sum(accepted_counts.values()),
        "needs_review_count": sum(review_counts.values()),
        "providers": sorted({clean(item.get("source")) for item in logs if item.get("source")}),
        "provider_errors": [item for item in logs if item.get("status") == "error"],
        "provider_warnings": [
            item for item in logs if item.get("status") in {"partial", "unavailable"}
        ],
        "unresolved_fields": unresolved,
    }
    return record


def enrich_record(
    record: dict[str, Any],
    *,
    manifest: dict[str, Any] | None,
    sources: set[str],
    client: CachedHttpClient,
    extractor: Any,
    pwc_index: dict[str, list[dict[str, Any]]],
    softcite_url: str | None,
    datastet_url: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    providers = []
    if "europepmc" in sources:
        providers.append(("europepmc", lambda: europepmc_candidates(record, client, extractor)))
    if "openaire" in sources:
        providers.append(("openaire", lambda: openaire_candidates(record, client, extractor)))
    if "paperswithcode" in sources:
        providers.append(("paperswithcode", lambda: paperswithcode_candidates(record, pwc_index, extractor)))
    if {"softcite", "datastet"} & sources:
        providers.append(("local_mention_services", lambda: local_service_candidates(
            record,
            manifest,
            client,
            extractor,
            softcite_url=softcite_url if "softcite" in sources else None,
            datastet_url=datastet_url if "datastet" in sources else None,
        )))
    for provider, callback in providers:
        try:
            found, provider_logs = callback()
            candidates.extend(found)
            logs.extend(provider_logs)
        except (OSError, ValueError, requests.RequestException, json.JSONDecodeError) as error:
            logs.append({"source": provider, "status": "error", "error": clean(error)})
    if "datacite" in sources:
        try:
            found, provider_logs = datacite_candidates(
                record, client, extractor, seed_candidates=candidates
            )
            candidates.extend(found)
            logs.extend(provider_logs)
        except (OSError, ValueError, requests.RequestException, json.JSONDecodeError) as error:
            logs.append({"source": "datacite", "status": "error", "error": clean(error)})
    merged = merge_candidates(candidates)
    return apply_candidates(record, merged, logs), merged, logs


def safe_cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
        if len(value) > 32760:
            return value[:32730] + " [TRUNCATED_FOR_XLSX]"
    return value


def write_xlsx(
    path: Path,
    records: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    logs: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    papers = workbook.active
    papers.title = "论文结果"
    paper_columns = [
        "canonical_id", "title", "supplementary_urls", "supplementary_status",
        "code_urls", "code_status", "data_urls", "data_status",
        "candidate_count", "accepted_count", "needs_review_count", "unresolved_fields",
    ]
    papers.append(paper_columns)
    for record in records:
        resources = record.get("resources") or {}
        statuses = record.get("resource_status") or {}
        info = record.get("open_source_enrichment") or {}
        status = lambda name: (statuses.get(name) or {}).get("status")
        row = {
            "canonical_id": record.get("canonical_id"),
            "title": record.get("title"),
            "supplementary_urls": resources.get("supplementary_urls") or [],
            "supplementary_status": status("supplementary"),
            "code_urls": resources.get("code_urls") or [],
            "code_status": status("code"),
            "data_urls": resources.get("data_urls") or [],
            "data_status": status("data"),
            "candidate_count": info.get("candidate_count"),
            "accepted_count": info.get("accepted_count"),
            "needs_review_count": info.get("needs_review_count"),
            "unresolved_fields": info.get("unresolved_fields") or [],
        }
        papers.append([safe_cell(row[column]) for column in paper_columns])

    candidate_sheet = workbook.create_sheet("候选证据")
    candidate_columns = [
        "canonical_id", "title", "resource_type", "url", "source", "sources",
        "decision", "confidence", "evidence",
    ]
    candidate_sheet.append(candidate_columns)
    for item in candidates:
        candidate_sheet.append([safe_cell(item.get(column)) for column in candidate_columns])

    log_sheet = workbook.create_sheet("来源调用")
    log_columns = ["canonical_id", "title", "source", "status", "result_count", "candidate_count", "reason", "error"]
    log_sheet.append(log_columns)
    for item in logs:
        log_sheet.append([safe_cell(item.get(column)) for column in log_columns])

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


def parse_sources(value: str) -> set[str]:
    allowed = {"europepmc", "datacite", "openaire", "paperswithcode", "softcite", "datastet"}
    sources = {item.strip().casefold() for item in value.split(",") if item.strip()}
    unknown = sources - allowed
    if unknown:
        raise ValueError("未知 --sources: " + ", ".join(sorted(unknown)))
    return sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich resources from open scholarly infrastructure")
    parser.add_argument("--mode", choices=("plan", "run"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--content-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sources", default=setting('enrichment_sources'))
    parser.add_argument("--paperswithcode-dump", type=Path)
    parser.add_argument("--softcite-url")
    parser.add_argument("--datastet-url")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--timeout", type=float, default=setting('download_timeout'))
    parser.add_argument("--retries", type=int, default=setting('download_retries'))
    parser.add_argument("--concurrency", type=int, default=setting('search_workers'))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    publisher = None
    if not args.input.is_file():
        raise SystemExit(f"输入不存在: {args.input.resolve()}")
    if args.start_index < 0:
        raise SystemExit("--start-index 不能小于 0")
    if args.max_records is not None and args.max_records < 1:
        raise SystemExit("--max-records 必须大于 0")
    if not 1 <= args.concurrency <= 16:
        raise SystemExit("--concurrency 必须在 1 到 16 之间")
    try:
        sources = parse_sources(args.sources)
        if "paperswithcode" in sources and args.paperswithcode_dump is None:
            raise ValueError("启用 paperswithcode 时必须提供 --paperswithcode-dump")
        if "softcite" in sources and not args.softcite_url:
            raise ValueError("启用 softcite 时必须提供 --softcite-url")
        if "datastet" in sources and not args.datastet_url:
            raise ValueError("启用 datastet 时必须提供 --datastet-url")

        all_records = read_jsonl(args.input)
        records = all_records[args.start_index:]
        if args.max_records is not None:
            records = records[:args.max_records]
        manifests = {}
        if args.content_manifest.is_file():
            manifests = {
                clean(item.get("canonical_id")): item for item in read_jsonl(args.content_manifest)
            }
        output_dir = args.output_dir.resolve()
        plan: list[dict[str, Any]] = []
        for record in records:
            pdf_path = choose_pdf(manifests.get(clean(record.get("canonical_id"))))
            plan.append({
                "canonical_id": record.get("canonical_id"),
                "title": record.get("title"),
                "identifiers": record.get("identifiers") or {},
                "valid_pdf": str(pdf_path) if pdf_path else None,
                "sources": sorted(sources),
            })
        write_jsonl(output_dir / "07_open_source_plan.jsonl", plan)
        plan_summary = {
            "schema_version": "open-source-enrichment-plan-v1",
            "generated_at": now_utc(),
            "mode": args.mode,
            "input": str(args.input.resolve()),
            "input_records": len(all_records),
            "selected_records": len(records),
            "sources": sorted(sources),
            "records_with_valid_pdf": sum(bool(item["valid_pdf"]) for item in plan),
            "concurrency": args.concurrency,
        }
        write_json(output_dir / "07_open_source_plan_summary.json", plan_summary)
        if args.mode == "plan":
            print(json.dumps({"status": "planned", **plan_summary}, ensure_ascii=False, indent=2))
            return

        extractor = load_extractor_module()
        stream_spec = importlib.util.spec_from_file_location("stage07_record_stream", Path(__file__).with_name("stream_records.py"))
        stream_module = importlib.util.module_from_spec(stream_spec)
        stream_spec.loader.exec_module(stream_module)
        publisher = stream_module.RecordPublisher(output_dir, "07", args.input, records)
        pwc_index = build_pwc_index(args.paperswithcode_dump if "paperswithcode" in sources else None)
        client = CachedHttpClient(output_dir / "cache", args.timeout, args.retries)
        results: dict[int, tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = {}
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_index = {
                executor.submit(
                    enrich_record,
                    record,
                    manifest=manifests.get(clean(record.get("canonical_id"))),
                    sources=sources,
                    client=client,
                    extractor=extractor,
                    pwc_index=pwc_index,
                    softcite_url=args.softcite_url,
                    datastet_url=args.datastet_url,
                ): index
                for index, record in enumerate(records)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                results[index] = future.result()
                publisher.publish(results[index][0])
                if len(results) == 1 or len(results) % 25 == 0:
                    print(json.dumps({"event": "paper_published", "completed": len(results),
                                      "total": len(records)}, ensure_ascii=False), flush=True)

        enriched_records: list[dict[str, Any]] = []
        all_candidates: list[dict[str, Any]] = []
        all_logs: list[dict[str, Any]] = []
        for index in range(len(records)):
            enriched, candidates, logs = results[index]
            enriched_records.append(enriched)
            all_candidates.extend(candidates)
            all_logs.extend({
                "canonical_id": records[index].get("canonical_id"),
                "title": records[index].get("title"),
                **item,
            } for item in logs)

        write_jsonl(output_dir / "07_open_source_enriched.jsonl", enriched_records)
        write_jsonl(output_dir / "07_open_source_candidates.jsonl", all_candidates)
        write_jsonl(output_dir / "07_open_source_lookup_log.jsonl", all_logs)
        status_counts = {
            resource_type: dict(sorted(Counter(
                str(((record.get("resource_status") or {}).get(resource_type) or {}).get("status") or "missing")
                for record in enriched_records
            ).items()))
            for resource_type in RESOURCE_FIELDS
        }
        has_errors = any(item.get("status") == "error" for item in all_logs)
        has_warnings = any(
            item.get("status") in {"partial", "unavailable"} for item in all_logs
        )
        summary = {
            "schema_version": "open-source-enrichment-summary-v1",
            "generated_at": now_utc(),
            "status": (
                "completed_with_errors" if has_errors
                else "completed_with_warnings" if has_warnings
                else "completed"
            ),
            "input": str(args.input.resolve()),
            "output_records": len(enriched_records),
            "sources": sorted(sources),
            "network_calls": client.network_calls,
            "cache_hits": client.cache_hits,
            "candidates": len(all_candidates),
            "candidate_decisions": dict(sorted(Counter(item["decision"] for item in all_candidates).items())),
            "candidate_sources": dict(sorted(Counter(item["source"] for item in all_candidates).items())),
            "resource_statuses": status_counts,
            "provider_errors": sum(item.get("status") == "error" for item in all_logs),
            "provider_unavailable_or_partial": sum(
                item.get("status") in {"partial", "unavailable"} for item in all_logs
            ),
        }
        write_json(output_dir / "07_open_source_summary.json", summary)
        write_xlsx(output_dir / "07_open_source_review.xlsx", enriched_records, all_candidates, all_logs)
        publisher.finish(summary["status"])
    except (OSError, ValueError, json.JSONDecodeError) as error:
        if publisher is not None:
            publisher.finish("failed")
        raise SystemExit(str(error)) from error
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
