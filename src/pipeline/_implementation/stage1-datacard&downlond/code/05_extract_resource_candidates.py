#!/usr/bin/env python3
"""Extract code, data, and supplementary candidates from article content.

The extraction is offline. It reads the stage-04 manifest, local TXT files,
saved HTML text/links, and PDF text/annotations. Candidates are not written to
the final paper resource fields until stage 06 validates them.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
import unicodedata
import urllib.parse

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill




URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'\[\]{}]+", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
DATA_DOI_PATTERNS = (
    re.compile(r"10\.5281/zenodo\.\d+", re.IGNORECASE),
    re.compile(r"10\.6084/m9\.figshare\.\d+", re.IGNORECASE),
    re.compile(r"10\.5061/dryad\.[-A-Z0-9]+", re.IGNORECASE),
)

CODE_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}
SUPPLEMENT_MARKERS = (
    "supplement", "supporting-information", "supporting_information",
    "supp_info", "supp-info", "suppl_file", "additional-file",
    "additional_file", "/esm/", "/mmc", "/suppl/", "suppdata",
    "suppmat", "moesm", "article-supplement",
)
SUPPLEMENT_EXTENSIONS = (
    ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".txt", ".tar", ".gz", ".tgz", ".7z", ".rar", ".ppt", ".pptx",
)
DATA_HOST_MARKERS = (
    "zenodo.org/record", "zenodo.org/records", "figshare.com/articles/",
    "figshare.org/articles/", "figshare.com/s/", "dryad.org/stash/dataset/",
    "datadryad.org/stash/dataset/", "osf.io/", "kaggle.com/datasets/",
    "huggingface.co/datasets/", "ncbi.nlm.nih.gov/geo/",
    "ncbi.nlm.nih.gov/sra/", "ncbi.nlm.nih.gov/bioproject/",
    "ncbi.nlm.nih.gov/biosample/", "trace.ncbi.nlm.nih.gov/",
    "ebi.ac.uk/ena/", "ebi.ac.uk/biostudies/", "ebi.ac.uk/pride/",
    "ebi.ac.uk/arrayexpress/", "ebi.ac.uk/emdb/", "ebi.ac.uk/empiar/",
    "rcsb.org/structure/", "proteomexchange.org/project/",
    "massive.ucsd.edu/proteosafe/", "dataverse",
)
AVAILABILITY_PATTERNS = {
    "code": re.compile(
        r"\b(code|software|implementation|source code|scripts?)\b.{0,100}"
        r"\b(available|released|deposited|accessible|download|repository|github|gitlab)\b"
        r"|\b(available|released|deposited|accessible)\b.{0,100}\b(code|software|scripts?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    "data": re.compile(
        r"\b(data|dataset|raw data|sequence data|structures?)\b.{0,120}"
        r"\b(available|deposited|accessible|repository|accession|download)\b"
        r"|\b(available|deposited|accessible)\b.{0,120}\b(data|dataset)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    "supplementary": re.compile(
        r"\b(supplementary|supplemental|supporting information|additional file)\b",
        re.IGNORECASE,
    ),
}
SECTION_PATTERNS = [
    ("code_availability", re.compile(r"code\s+availability|availability\s+of\s+code", re.I)),
    ("data_availability", re.compile(r"data\s+availability|availability\s+of\s+data|data\s+and\s+materials", re.I)),
    ("resource_availability", re.compile(r"resource\s+availability|availability\s+statement", re.I)),
    ("supplementary", re.compile(r"supplementary\s+(?:material|information|data)|supporting\s+information", re.I)),
    ("references", re.compile(r"^(?:references|bibliography|literature cited)\s*$", re.I | re.M)),
]
CONTENT_KINDS = {
    "local_txt", "article_html", "article_pdf", "jina_reader_text",
}
MIN_TEXT_CHARACTERS = 500
ILLEGAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    text = ILLEGAL_CONTROL_RE.sub("", str(value or ""))
    return " ".join(text.split())


def safe_cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        value = ILLEGAL_CONTROL_RE.sub("", value)
        if len(value) > 32760:
            value = value[:32730] + " [TRUNCATED_FOR_XLSX]"
    return value


def unique(values: Iterable[Any], key: Any | None = None) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker_value = key(value) if key else value
        marker = json.dumps(marker_value, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


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


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def text_file_ready(value: Any) -> bool:
    path = Path(str(value or ""))
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return len(clean(text)) >= MIN_TEXT_CHARACTERS


def valid_pdf_file(value: Any) -> bool:
    path = Path(str(value or ""))
    try:
        if not path.is_file() or path.stat().st_size <= 1024:
            return False
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def summarize_paper_content(manifest: dict[str, Any]) -> dict[str, Any]:
    """Create a compact, file-validated paper-content status for later stages."""
    local_artifacts: list[dict[str, Any]] = []
    coverage = Counter()
    valid_local_count = 0
    text_ready_count = 0
    invalid_artifact_count = 0
    candidate_pdf_urls: list[str] = []

    paper = manifest.get("paper") or {}
    document = paper.get("document") or {}
    candidate_pdf_urls.extend([
        clean(document.get("pdf_url")),
        *(clean(url) for url in document.get("pdf_urls") or []),
    ])

    for artifact in manifest.get("artifacts") or []:
        kind = clean(artifact.get("kind"))
        if kind not in CONTENT_KINDS:
            continue
        status = clean(artifact.get("status"))
        status_ok = status in {"available", "downloaded"}
        path = Path(str(artifact.get("path") or ""))
        path_exists = path.is_file()
        text_path = artifact.get("text_path") or (
            artifact.get("path") if kind in {"local_txt", "jina_reader_text"} else None
        )
        text_ready = status_ok and text_file_ready(text_path)
        if kind == "article_pdf":
            valid_file = status_ok and valid_pdf_file(path)
        elif kind == "article_html":
            valid_file = (
                status_ok and path_exists
                and clean(artifact.get("quality_status")) == "usable"
            )
        else:
            valid_file = status_ok and path_exists and text_ready

        if valid_file:
            valid_local_count += 1
            coverage[kind] += 1
            if kind == "article_pdf":
                candidate_pdf_urls.extend([
                    clean(artifact.get("final_url")),
                    clean(artifact.get("source_url")),
                ])
        elif status_ok:
            invalid_artifact_count += 1
        if text_ready:
            text_ready_count += 1

        local_artifacts.append({
            "kind": kind,
            "path": str(path) if artifact.get("path") else None,
            "text_path": clean(text_path) or None,
            "source_url": clean(artifact.get("source_url")) or None,
            "final_url": clean(artifact.get("final_url")) or None,
            "status": status or None,
            "valid_file": valid_file,
            "text_ready": text_ready,
            "bytes": artifact.get("bytes"),
            "sha256": artifact.get("sha256"),
            "quality_status": artifact.get("quality_status"),
            "extraction_status": artifact.get("extraction_status"),
        })

    candidate_pdf_urls = unique(
        url for url in candidate_pdf_urls if normalize_url(url)
    )
    paper_urls = unique([
        clean(document.get("paper_url")),
        *(clean(url) for url in document.get("paper_urls") or []),
    ])
    paper_urls = [url for url in paper_urls if normalize_url(url)]

    if text_ready_count:
        status = "local_text_ready"
    elif valid_local_count:
        status = "local_fulltext_available"
    elif candidate_pdf_urls:
        status = "downloadable_pdf"
    elif paper_urls:
        status = "landing_page_only"
    else:
        status = "not_found"

    return {
        "schema_version": "paper-content-status-v1",
        "collection_status": manifest.get("status"),
        "status": status,
        "paper_available": status in {
            "local_text_ready", "local_fulltext_available", "downloadable_pdf",
        },
        "text_ready": bool(text_ready_count),
        "valid_local_artifact_count": valid_local_count,
        "text_ready_artifact_count": text_ready_count,
        "invalid_artifact_count": invalid_artifact_count,
        "coverage": dict(sorted(coverage.items())),
        "candidate_paper_urls": paper_urls,
        "candidate_pdf_urls": candidate_pdf_urls,
        "local_artifacts": local_artifacts,
        "request_problem_count": (manifest.get("quality") or {}).get(
            "request_problem_count", 0
        ),
    }


def normalize_url(value: Any) -> str | None:
    url = unicodedata.normalize("NFKC", clean(value))
    url = url.replace("\u00ad", "").replace("\u200b", "")
    url = url.rstrip(".,;:!?)]}>'\"\\")
    if url.casefold().startswith("www."):
        url = "https://" + url
    if not url.casefold().startswith(("http://", "https://")):
        return None
    parsed = urllib.parse.urlsplit(url)
    if not parsed.netloc:
        return None
    if parsed.path.rstrip("/").casefold() == "/goto" and "url=" in parsed.query.casefold():
        return None
    host = parsed.netloc.casefold().removeprefix("www.")
    path = parsed.path
    query = parsed.query

    # PDF text extraction may attach the next sentence directly to a DOI or
    # repository record. These repositories have identifiers with strict forms,
    # so canonicalize only the identifier portion.
    decoded_path = urllib.parse.unquote(path)
    if host in {"doi.org", "dx.doi.org"}:
        for pattern in DATA_DOI_PATTERNS:
            match = pattern.search(decoded_path.lstrip("/"))
            if match:
                host = "doi.org"
                path = f"/{match.group(0).casefold()}"
                query = ""
                break
    elif host == "zenodo.org":
        match = re.match(r"/(records?)/(\d+)", decoded_path, re.IGNORECASE)
        if match:
            path = f"/records/{match.group(2)}"
            query = ""

    if host in CODE_HOSTS:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            repository = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE)
            repository = re.sub(
                r"\.(?:acknowledgements?|references|methods|results)$",
                "",
                repository,
                flags=re.IGNORECASE,
            )
            parts[1] = repository
            path = "/" + "/".join(parts)
    return urllib.parse.urlunsplit((
        parsed.scheme.casefold(), host, path, query, "",
    ))


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", (
        text.replace("\u00ad", "")
        .replace("\u200b", "")
        .replace("\uff0e", ".")
        .replace("\uff0f", "/")
    ))


def code_repository_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if host not in CODE_HOSTS or len(parts) < 2:
        return False
    reserved = {
        "about", "blog", "collections", "enterprise", "events", "explore",
        "features", "issues", "login", "marketplace", "new", "orgs",
        "organizations", "pricing", "search", "security", "topics",
        "trending", "users",
    }
    return parts[0].casefold() not in reserved and parts[1].casefold() not in reserved


def data_record_url(url: str) -> bool:
    lower = urllib.parse.unquote(url).casefold()
    if any(marker in lower for marker in DATA_HOST_MARKERS):
        parsed = urllib.parse.urlsplit(lower)
        if parsed.netloc.removeprefix("www.") == "osf.io":
            parts = [part for part in parsed.path.split("/") if part]
            return bool(parts and re.fullmatch(r"[a-z0-9]{4,8}", parts[0]))
        return True
    doi = extract_doi(url)
    return bool(doi and doi.startswith(("10.5281/zenodo.", "10.6084/m9.figshare.", "10.5061/dryad.")))


def github_combined_code_data_context(context: str) -> bool:
    evidence = clean(context)
    return bool(re.search(
        r"(?:\b(?:code\s*,\s*data|code\s+and\s+data|data\s+and\s+code)\b"
        r".{0,120}\bavailable\b|"
        r"\b(?:complete|all)\s+(?:data|datasets?)\b.{0,180}"
        r"\b(?:code|scripts?)\b.{0,120}"
        r"\b(?:available|repository|github|gitlab)\b)",
        evidence,
        re.IGNORECASE | re.DOTALL,
    ))


def github_data_context(url: str, anchor: str = "", context: str = "") -> bool:
    """Return true only when a repository link is explicitly presented as data."""
    if not code_repository_url(url):
        return False
    evidence = clean(f"{anchor} {context}")
    if github_combined_code_data_context(evidence):
        return True
    data_signal = re.search(
        r"\b(?:data|dataset|datasets|benchmark dataset|digitized data|raw data)\b",
        evidence,
        re.IGNORECASE,
    )
    availability_signal = re.search(
        r"\b(?:available|deposited|accessible|download|repository|hosted)\b",
        evidence,
        re.IGNORECASE,
    )
    code_signal = re.search(
        r"(?:\bsource code\b|\bcustom code\b|\bcode used\b|"
        r"(?:^|[^A-Za-z])(?:\d{1,2}\s*)?code\b.{0,80}\bavailable\b|"
        r"\bscripts?\b.{0,80}\bavailable\b|\bimplementation\b|"
        r"\bsoftware package\b|\bpackage\b.{0,80}\b(?:developed|available)\b)",
        evidence,
        re.IGNORECASE | re.DOTALL,
    )
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).casefold()
    explicit_data_path = any(marker in path for marker in (
        "/data", "dataset", "digitized", "benchmark",
    ))
    if code_signal and not explicit_data_path:
        return False
    return bool(data_signal and availability_signal and (explicit_data_path or not code_signal))


def supplementary_url(url: str, anchor: str = "") -> bool:
    parsed = urllib.parse.urlsplit(url)
    evidence = urllib.parse.unquote(f"{parsed.path}?{parsed.query} {anchor}").casefold()
    has_marker = any(marker in evidence for marker in SUPPLEMENT_MARKERS)
    filename = Path(parsed.path).name.casefold()
    numbered = bool(re.search(r"(?:^|[-_.])(?:supp|suppl|s|mmc|moesm)\d*(?:[-_.]|$)", filename))
    explicit_path = any(marker in evidence for marker in (
        "/doi/suppl/", "/content/suppl/", "/article-supplement/", "/esm/",
        "/mmc", "downloadsupplement", "type=supplementary", "/supplemental/",
        "/supplementaryfiles",
    ))
    return (has_marker or numbered) and (
        parsed.path.casefold().endswith(SUPPLEMENT_EXTENSIONS) or explicit_path
    )


def extract_doi(value: Any) -> str | None:
    text = urllib.parse.unquote(clean(value))
    for pattern in DATA_DOI_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).casefold()
    match = DOI_RE.search(text)
    return match.group(0).rstrip(".,;)]}").casefold() if match else None


def classify_url(url: str, anchor: str = "", context: str = "") -> str | None:
    if code_repository_url(url):
        if github_combined_code_data_context(f"{anchor} {context}"):
            return "code"
        if github_data_context(url, anchor, context):
            return "data"
        return "code"
    if data_record_url(url):
        return "data"
    if supplementary_url(url, anchor):
        return "supplementary"
    evidence = f"{anchor} {context}".casefold()
    if "github" in url.casefold() and "code" in evidence:
        return "code"
    return None


def section_for(text: str, position: int) -> str:
    prefix = text[max(0, position - 2500):position]
    matches: list[tuple[int, str]] = []
    for name, pattern in SECTION_PATTERNS:
        for match in pattern.finditer(prefix):
            matches.append((match.start(), name))
    return max(matches)[1] if matches else "body"


def context_for(text: str, start: int, end: int, radius: int = 350) -> str:
    return clean(text[max(0, start - radius):min(len(text), end + radius)])


def score_candidate(
    resource_type: str,
    *,
    source_kind: str,
    method: str,
    section: str,
    context: str,
    url: str,
    anchor: str,
) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []
    if method in {"html_link", "pdf_annotation", "jina_link"}:
        score += 0.2
        signals.append(method)
    else:
        score += 0.1
        signals.append("url_in_article_text")
    if source_kind == "local_txt":
        score += 0.08
        signals.append("local_fulltext")
    elif source_kind == "article_html":
        score += 0.1
        signals.append("publisher_html")
    elif source_kind == "article_pdf":
        score += 0.1
        signals.append("article_pdf")
    elif source_kind == "jina_reader_text":
        score += 0.08
        signals.append("jina_reader_article_content")

    if resource_type == "code" and code_repository_url(url):
        score += 0.35
        signals.append("concrete_code_repository")
    elif resource_type == "data" and (
        data_record_url(url) or github_data_context(url, anchor, context)
    ):
        score += 0.35
        signals.append("concrete_data_record")
    elif resource_type == "supplementary" and supplementary_url(url, anchor):
        score += 0.4
        signals.append("explicit_supplement_attachment")

    if section in {"code_availability", "data_availability", "resource_availability"}:
        score += 0.3
        signals.append(section)
    elif section == "supplementary" and resource_type == "supplementary":
        score += 0.25
        signals.append("supplementary_section")
    if AVAILABILITY_PATTERNS[resource_type].search(f"{anchor} {context}"):
        score += 0.22
        signals.append("availability_language")
    if section == "references":
        score -= 0.45
        signals.append("references_penalty")
    if re.search(r"\b(?:cited|reference|software package|we used|using the)\b", context, re.I) and not re.search(
        r"\b(?:our code|code is|data are|data is|we release|we provide|available at|deposited)\b",
        context, re.I,
    ):
        score -= 0.15
        signals.append("third_party_resource_penalty")
    return round(max(0.0, min(1.0, score)), 3), signals


def candidate_id(canonical_id: str, resource_type: str, url: str) -> str:
    digest = hashlib.sha256(f"{canonical_id}|{resource_type}|{url}".encode()).hexdigest()[:16]
    return f"resource-{digest}"


def make_candidate(
    manifest: dict[str, Any],
    *,
    resource_type: str,
    url: str,
    source_kind: str,
    source_path: str | None,
    source_url: str | None,
    method: str,
    context: str,
    section: str,
    anchor: str = "",
    accession: str | None = None,
) -> dict[str, Any]:
    score, signals = score_candidate(
        resource_type,
        source_kind=source_kind,
        method=method,
        section=section,
        context=context,
        url=url,
        anchor=anchor,
    )
    paper = manifest.get("paper") or manifest
    paper_doi = extract_doi((paper.get("identifiers") or {}).get("doi"))
    url_doi = extract_doi(url)
    if (
        resource_type == "supplementary"
        and paper_doi
        and url_doi
        and paper_doi != url_doi
    ):
        score = min(score, 0.39)
        signals.append("different_article_doi_penalty")
    if score >= 0.75:
        hint = "auto_accept"
    elif score >= 0.4:
        hint = "needs_review"
    else:
        hint = "reject"
    canonical_id = clean(manifest.get("canonical_id"))
    return {
        "candidate_id": candidate_id(canonical_id, resource_type, url),
        "canonical_id": canonical_id,
        "title": manifest.get("title"),
        "resource_type": resource_type,
        "url": url,
        "accession": accession,
        "score": score,
        "decision_hint": hint,
        "signals": signals,
        "evidence": {
            "method": method,
            "source_kind": source_kind,
            "source_path": source_path,
            "source_url": source_url,
            "section": section,
            "anchor": clean(anchor),
            "context": context,
        },
    }


def extract_url_candidates(
    manifest: dict[str, Any],
    text: str,
    *,
    source_kind: str,
    source_path: str | None,
    source_url: str | None,
) -> list[dict[str, Any]]:
    normalized = normalize_text(text)
    rows: list[dict[str, Any]] = []
    for match in URL_RE.finditer(normalized):
        url = normalize_url(match.group(0))
        if not url:
            continue
        context = context_for(normalized, match.start(), match.end())
        primary_type = classify_url(url, context=context)
        if not primary_type:
            continue
        resource_types = [primary_type]
        if code_repository_url(url) and github_combined_code_data_context(context):
            resource_types = ["code", "data"]
        for resource_type in resource_types:
            rows.append(make_candidate(
                manifest,
                resource_type=resource_type,
                url=url,
                source_kind=source_kind,
                source_path=source_path,
                source_url=source_url,
                method="text_url",
                context=context,
                section=section_for(normalized, match.start()),
            ))
    return rows


def accession_candidates(
    manifest: dict[str, Any],
    text: str,
    *,
    source_kind: str,
    source_path: str | None,
    source_url: str | None,
) -> list[dict[str, Any]]:
    patterns = [
        (re.compile(r"\b(GSE\d{3,})\b", re.I), lambda value: f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={value.upper()}"),
        (re.compile(r"\b(PRJNA\d+|PRJEB\d+|SRP\d+|ERP\d+|DRP\d+)\b", re.I), lambda value: f"https://www.ncbi.nlm.nih.gov/sra/?term={value.upper()}"),
        (re.compile(r"\b(SRR\d+|ERR\d+|DRR\d+)\b", re.I), lambda value: f"https://www.ncbi.nlm.nih.gov/sra/{value.upper()}"),
        (re.compile(r"\b(PXD\d{6,})\b", re.I), lambda value: f"https://www.ebi.ac.uk/pride/archive/projects/{value.upper()}"),
        (re.compile(r"\b(EMD[- ]?\d{4,})\b", re.I), lambda value: f"https://www.ebi.ac.uk/emdb/{value.upper().replace(' ', '-')}"),
        (re.compile(r"\b(E-(?:MTAB|GEOD|MEXP)-\d+)\b", re.I), lambda value: f"https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{value.upper()}"),
    ]
    rows: list[dict[str, Any]] = []
    normalized = normalize_text(text)
    for pattern, to_url in patterns:
        for match in pattern.finditer(normalized):
            context = context_for(normalized, match.start(), match.end())
            if not re.search(r"\b(?:data|dataset|accession|deposited|available|repository|archive)\b", context, re.I):
                continue
            accession = match.group(1).upper().replace(" ", "-")
            rows.append(make_candidate(
                manifest,
                resource_type="data",
                url=to_url(accession),
                source_kind=source_kind,
                source_path=source_path,
                source_url=source_url,
                method="accession",
                context=context,
                section=section_for(normalized, match.start()),
                accession=accession,
            ))

    for pattern in DATA_DOI_PATTERNS:
        for match in pattern.finditer(normalized):
            doi = match.group(0).casefold()
            context = context_for(normalized, match.start(), match.end())
            rows.append(make_candidate(
                manifest,
                resource_type="data",
                url=f"https://doi.org/{doi}",
                source_kind=source_kind,
                source_path=source_path,
                source_url=source_url,
                method="data_doi",
                context=context,
                section=section_for(normalized, match.start()),
                accession=doi,
            ))

    pdb_pattern = re.compile(
        r"\b(?:PDB|Protein\s+Data\s+Bank)\s*"
        r"(?:IDs?|entries|accessions?)?\s*[:#]?\s*"
        r"([0-9][A-Za-z0-9]{3})\b",
        re.IGNORECASE,
    )
    for match in pdb_pattern.finditer(normalized):
        context = context_for(normalized, match.start(), match.end())
        accession = match.group(1).upper()
        rows.append(make_candidate(
            manifest,
            resource_type="data",
            url=f"https://www.rcsb.org/structure/{accession}",
            source_kind=source_kind,
            source_path=source_path,
            source_url=source_url,
            method="accession",
            context=context,
            section=section_for(normalized, match.start()),
            accession=accession,
        ))
    return rows


def extract_link_candidates(
    manifest: dict[str, Any],
    links: list[dict[str, Any]],
    *,
    source_kind: str,
    source_path: str | None,
    source_url: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in links:
        url = normalize_url(item.get("url"))
        if not url:
            continue
        anchor = clean(item.get("anchor"))
        context = clean(f"{anchor} {item.get('context') or ''}")
        resource_type = classify_url(url, anchor=anchor, context=context)
        if not resource_type:
            continue
        if source_kind == "article_pdf":
            method = "pdf_annotation"
        elif source_kind == "jina_reader_text":
            method = "jina_link"
        else:
            method = "html_link"
        section = "supplementary" if AVAILABILITY_PATTERNS["supplementary"].search(anchor) else "body"
        rows.append(make_candidate(
            manifest,
            resource_type=resource_type,
            url=url,
            source_kind=source_kind,
            source_path=source_path,
            source_url=source_url,
            method=method,
            context=context,
            section=section,
            anchor=anchor,
        ))
    return rows


def merge_duplicate_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["resource_type"], row["url"]), []).append(row)
    merged: list[dict[str, Any]] = []
    for (_, _), group in grouped.items():
        best = max(group, key=lambda item: item["score"])
        item = dict(best)
        item["evidence_count"] = len(group)
        item["all_evidence"] = [entry["evidence"] for entry in group]
        item["signals"] = sorted({signal for entry in group for signal in entry["signals"]})
        if any(entry["decision_hint"] == "auto_accept" for entry in group):
            item["decision_hint"] = "auto_accept"
        elif any(entry["decision_hint"] == "needs_review" for entry in group):
            item["decision_hint"] = "needs_review"
        merged.append(item)
    # A PDF may print a repository name with a visual space (for example
    # ``find orb``), producing a truncated text URL alongside the exact link
    # annotation. Keep the exact annotated repository in that situation.
    strong_code_urls = [
        item["url"] for item in merged
        if item["resource_type"] == "code"
        and any(
            evidence.get("method") in {"html_link", "pdf_annotation"}
            for evidence in item.get("all_evidence") or []
        )
    ]
    pruned: list[dict[str, Any]] = []
    for item in merged:
        if item["resource_type"] == "code":
            parsed = urllib.parse.urlsplit(item["url"])
            prefix = parsed.path.rstrip("/")
            if any(
                other != item["url"]
                and urllib.parse.urlsplit(other).netloc == parsed.netloc
                and urllib.parse.urlsplit(other).path.startswith(prefix + "_")
                for other in strong_code_urls
            ):
                continue
        pruned.append(item)
    return sorted(pruned, key=lambda item: (item["resource_type"], -item["score"], item["url"]))


def extract_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    extraction_errors: list[dict[str, Any]] = []
    for artifact in manifest.get("artifacts") or []:
        if artifact.get("status") not in {"available", "downloaded"}:
            continue
        source_kind = clean(artifact.get("kind"))
        source_path = clean(artifact.get("path")) or None
        source_url = clean(artifact.get("final_url") or artifact.get("source_url")) or None
        text_path_value = artifact.get("text_path") or (
            source_path if source_kind == "local_txt" else None
        )
        if text_path_value:
            text_path = Path(str(text_path_value))
            if not text_path.is_file() and artifact.get("extraction_status") == "error":
                text_path = None
            if text_path is not None:
                try:
                    text = text_path.read_text(encoding="utf-8", errors="replace")
                    candidates.extend(extract_url_candidates(
                        manifest, text, source_kind=source_kind,
                        source_path=str(text_path), source_url=source_url,
                    ))
                    candidates.extend(accession_candidates(
                        manifest, text, source_kind=source_kind,
                        source_path=str(text_path), source_url=source_url,
                    ))
                except OSError as error:
                    extraction_errors.append({"path": str(text_path), "error": clean(error)})
        links_path_value = artifact.get("links_path")
        if links_path_value:
            links_path = Path(str(links_path_value))
            if not links_path.is_file() and artifact.get("extraction_status") == "error":
                continue
            try:
                links = read_json(links_path)
                if isinstance(links, list):
                    candidates.extend(extract_link_candidates(
                        manifest, links, source_kind=source_kind,
                        source_path=str(links_path), source_url=source_url,
                    ))
            except (OSError, json.JSONDecodeError) as error:
                extraction_errors.append({"path": str(links_path), "error": clean(error)})
    merged = merge_duplicate_candidates(candidates)
    return {
        "schema_version": "resource-candidates-v3",
        "canonical_id": manifest.get("canonical_id"),
        "title": manifest.get("title"),
        "abstract_used": False,
        "content_status": manifest.get("status"),
        "paper_content": summarize_paper_content(manifest),
        "candidate_count": len(merged),
        "candidates": merged,
        "extraction_errors": extraction_errors,
    }


def write_review_xlsx(path: Path, results: list[dict[str, Any]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "资源候选"
    columns = [
        "canonical_id", "title", "resource_type", "url", "accession",
        "score", "decision_hint", "signals", "evidence_count", "method",
        "source_kind", "section", "source_path", "context",
    ]
    sheet.append(columns)
    for result in results:
        for candidate in result.get("candidates") or []:
            evidence = candidate.get("evidence") or {}
            row = {
                "canonical_id": result.get("canonical_id"),
                "title": result.get("title"),
                "resource_type": candidate.get("resource_type"),
                "url": candidate.get("url"),
                "accession": candidate.get("accession"),
                "score": candidate.get("score"),
                "decision_hint": candidate.get("decision_hint"),
                "signals": json.dumps(candidate.get("signals") or [], ensure_ascii=False),
                "evidence_count": candidate.get("evidence_count"),
                "method": evidence.get("method"),
                "source_kind": evidence.get("source_kind"),
                "section": evidence.get("section"),
                "source_path": evidence.get("source_path"),
                "context": evidence.get("context"),
            }
            sheet.append([safe_cell(row[column]) for column in columns])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = fill
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 60
    sheet.column_dimensions["D"].width = 65
    sheet.column_dimensions["N"].width = 100
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract resource URL and accession candidates from saved article content"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-records", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.input.is_file():
        raise SystemExit(f"输入不存在: {args.input.resolve()}")
    manifests = read_jsonl(args.input)
    if args.max_records is not None:
        if args.max_records < 1:
            raise SystemExit("--max-records 必须大于 0")
        manifests = manifests[: args.max_records]
    results = [extract_manifest(manifest) for manifest in manifests]
    all_candidates = [
        candidate for result in results for candidate in result.get("candidates") or []
    ]
    summary = {
        "schema_version": "resource-candidate-summary-v3",
        "generated_at": now_utc(),
        "input": str(args.input.resolve()),
        "records": len(results),
        "records_with_candidates": sum(bool(result["candidate_count"]) for result in results),
        "candidates": len(all_candidates),
        "resource_types": dict(sorted(Counter(
            item["resource_type"] for item in all_candidates
        ).items())),
        "decision_hints": dict(sorted(Counter(
            item["decision_hint"] for item in all_candidates
        ).items())),
        "extraction_errors": sum(len(result["extraction_errors"]) for result in results),
        "paper_content_statuses": dict(sorted(Counter(
            (result.get("paper_content") or {}).get("status") or "missing"
            for result in results
        ).items())),
        "paper_available": sum(
            bool((result.get("paper_content") or {}).get("paper_available"))
            for result in results
        ),
        "paper_text_ready": sum(
            bool((result.get("paper_content") or {}).get("text_ready"))
            for result in results
        ),
        "abstract_required": False,
    }
    output_dir = args.output_dir.resolve()
    write_jsonl(output_dir / "05_resource_candidates.jsonl", results)
    write_json(output_dir / "05_resource_candidates_summary.json", summary)
    write_review_xlsx(output_dir / "05_resource_candidates_review.xlsx", results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
