#!/usr/bin/env python3
"""Collect local TXT, article HTML, and article PDF content.

This stage does not discover resources with a search engine and does not
download code, data, or supplementary files. It only prepares article content
that can contain authoritative resource links.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Iterable
import urllib.parse

import requests
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from pypdf import PdfReader

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting





USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "NatureBenchArticleCollector/1.0"
)
PDF_META_NAMES = {
    "citation_pdf_url", "eprints.document_url", "wkhealth_pdf_url",
    "bepress_citation_pdf_url", "dc.identifier.pdf",
}
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
SUPPLEMENT_URL_MARKERS = (
    "supplement", "supporting-information", "supporting_information",
    "supp_info", "supp-info", "suppl_file", "additional-file",
    "additional_file", "/esm/", "/mmc", "/suppl/", "suppdata",
    "suppmat", "moesm", "article-supplement",
)
INVALID_PAGE_MARKERS = (
    "enable javascript and cookies to continue",
    "verify you are human",
    "checking your browser",
    "access denied",
    "captcha",
    "just a moment",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


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


def load_resumable_manifests(
    path: Path,
    selected: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    restored: dict[int, dict[str, Any]] = {}
    damaged_lines: list[int] = []
    if not path.is_file():
        return restored, damaged_lines
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                saved = json.loads(line)
                index = int(saved.get("input_index"))
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                damaged_lines.append(line_number)
                continue
            if not 0 <= index < len(selected):
                continue
            expected_id = clean(selected[index].get("canonical_id"))
            if clean(saved.get("canonical_id")) != expected_id:
                continue
            if saved.get("status") not in {"collected", "collected_with_errors"}:
                continue
            manifest = dict(saved)
            manifest.pop("input_index", None)
            restored[index] = manifest
    return restored, damaged_lines


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


def safe_name(value: Any) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", clean(value)).strip("._")
    return name[:120] or "paper"


def is_http_url(value: Any) -> bool:
    parsed = urllib.parse.urlsplit(clean(value))
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def normalize_doi(value: Any) -> str | None:
    text = urllib.parse.unquote(clean(value))
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    return match.group(0).rstrip(".,;)]}").casefold() if match else None


def load_env_optional(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
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
            return result or None
    return None


def is_supplementary_url(value: Any) -> bool:
    url = urllib.parse.unquote(clean(value)).casefold()
    return any(marker in url for marker in SUPPLEMENT_URL_MARKERS)


def html_quality(visible_text: str) -> tuple[bool, str]:
    text = clean(visible_text)
    lower = text.casefold()
    if len(text) < 500:
        return False, f"visible_text_too_short:{len(text)}"
    if any(marker in lower for marker in INVALID_PAGE_MARKERS):
        return False, "browser_verification_or_access_denied_page"
    if lower in {"redirecting", "redirecting...", "redirecting…"}:
        return False, "redirect_page"
    return True, "usable_article_page"


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self.min_interval - (time.monotonic() - self._last_call)
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()


class ArticleHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self.meta: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self._hidden_depth = 0
        self._current_href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        if tag == "base" and is_http_url(values.get("href")):
            self.base_url = values["href"]
        if tag == "a" and values.get("href"):
            self._current_href = urllib.parse.urljoin(self.base_url, values["href"])
            self._anchor_parts = []
        if tag == "meta":
            name = clean(values.get("name") or values.get("property")).casefold()
            content = clean(values.get("content"))
            if name and content:
                self.meta.append({"name": name, "content": content})
        if tag == "link" and values.get("href"):
            href = urllib.parse.urljoin(self.base_url, values["href"])
            self.links.append({
                "url": href,
                "anchor": clean(values.get("title")),
                "rel": clean(values.get("rel")),
                "source": "html_link_tag",
            })

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._current_href:
            self.links.append({
                "url": self._current_href,
                "anchor": clean(" ".join(self._anchor_parts)),
                "rel": "",
                "source": "html_anchor",
            })
            self._current_href = None
            self._anchor_parts = []
        if tag in {"script", "style", "noscript", "svg"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth:
            return
        text = clean(data)
        if text:
            self.text_parts.append(text)
            if self._current_href:
                self._anchor_parts.append(text)


def parse_html(
    html_text: str, base_url: str
) -> tuple[str, list[dict[str, str]], list[str], list[str]]:
    parser = ArticleHTMLParser(base_url)
    parser.feed(html_text)
    parser.close()
    pdf_urls: list[str] = []
    supplementary_urls: list[str] = []
    for item in parser.meta:
        if item["name"] in PDF_META_NAMES:
            url = urllib.parse.urljoin(parser.base_url, item["content"])
            if is_supplementary_url(url):
                supplementary_urls.append(url)
            else:
                pdf_urls.append(url)
    for item in parser.links:
        lower = item["url"].casefold()
        evidence = f"{lower} {item['anchor'].casefold()} {item['rel'].casefold()}"
        if (
            lower.endswith(".pdf")
            or "citation_pdf" in evidence
            or "download pdf" in evidence
            or item["rel"].casefold() == "alternate" and "pdf" in evidence
        ):
            if is_supplementary_url(item["url"]):
                supplementary_urls.append(item["url"])
            else:
                pdf_urls.append(item["url"])
    links = [item for item in parser.links if is_http_url(item.get("url"))]
    return (
        "\n".join(parser.text_parts), links, unique(pdf_urls),
        unique(supplementary_urls),
    )


def request_bytes(
    url: str,
    *,
    timeout: float,
    retries: int,
    max_bytes: int,
) -> tuple[bytes, str, str]:
    last_error: Exception | None = None
    for _ in range(retries + 1):
        try:
            with requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8"},
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            ) as response:
                response.raise_for_status()
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length and content_length > max_bytes:
                    raise ValueError(
                        f"响应大小 {content_length} 超过限制 {max_bytes} bytes"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"下载内容超过限制 {max_bytes} bytes")
                    chunks.append(chunk)
                return (
                    b"".join(chunks),
                    clean(response.headers.get("Content-Type")).casefold(),
                    str(response.url),
                )
        except (requests.RequestException, OSError, ValueError) as error:
            last_error = error
    raise ValueError(f"请求失败: {last_error}")


def request_json(
    url: str,
    *,
    timeout: float,
    retries: int,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    last_error: Exception | None = None
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        **(headers or {}),
    }
    for _ in range(retries + 1):
        try:
            response = requests.get(
                url, headers=request_headers, timeout=timeout, allow_redirects=True
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("响应不是 JSON 对象")
            return payload, str(response.url)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            last_error = error
    raise ValueError(f"JSON 请求失败: {last_error}")


def flatten_jina_links(value: Any, anchor: str = "") -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    if isinstance(value, str) and is_http_url(value):
        links.append({"url": value, "anchor": anchor, "source": "jina_reader"})
    elif isinstance(value, list):
        for item in value:
            links.extend(flatten_jina_links(item, anchor))
    elif isinstance(value, dict):
        direct_url = value.get("url") or value.get("href")
        if isinstance(direct_url, str) and is_http_url(direct_url):
            links.append({
                "url": direct_url,
                "anchor": clean(value.get("text") or value.get("title") or anchor),
                "source": "jina_reader",
            })
        else:
            for key, item in value.items():
                links.extend(flatten_jina_links(item, clean(key)))
    return unique(links)


def request_jina_reader(
    url: str,
    *,
    api_key: str | None,
    limiter: RateLimiter,
    timeout: float,
    retries: int,
) -> tuple[str, list[dict[str, str]], str]:
    limiter.wait()
    headers = {"X-With-Links-Summary": "true"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    endpoint = f"https://r.jina.ai/{url}"
    payload, final_url = request_json(
        endpoint, timeout=timeout, retries=retries, headers=headers
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    text = clean(data.get("content") or data.get("text"))
    if len(text) < 500:
        raise ValueError(f"Jina Reader 正文过短: {len(text)} characters")
    links = flatten_jina_links(data.get("links") or payload.get("links") or [])
    return text, links, final_url


def resolve_doi_locations(
    doi: str,
    *,
    timeout: float,
    retries: int,
    contact_email: str | None,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    paper_urls: list[str] = []
    pdf_urls: list[str] = []
    logs: list[dict[str, Any]] = []

    encoded_id = urllib.parse.quote(f"https://doi.org/{doi}", safe=":/")
    openalex_url = f"https://api.openalex.org/works/{encoded_id}"
    if contact_email:
        openalex_url += "?" + urllib.parse.urlencode({"mailto": contact_email})
    try:
        payload, final_url = request_json(
            openalex_url, timeout=timeout, retries=retries
        )
        locations = [payload.get("primary_location"), payload.get("best_oa_location")]
        locations.extend(payload.get("locations") or [])
        for location in locations:
            if not isinstance(location, dict):
                continue
            for field in ("landing_page_url",):
                if is_http_url(location.get(field)):
                    paper_urls.append(location[field])
            for field in ("pdf_url",):
                if is_http_url(location.get(field)):
                    pdf_urls.append(location[field])
        open_access = payload.get("open_access") or {}
        if is_http_url(open_access.get("oa_url")):
            candidate = open_access["oa_url"]
            if urllib.parse.urlsplit(candidate).path.casefold().endswith(".pdf"):
                pdf_urls.append(candidate)
            else:
                paper_urls.append(candidate)
        logs.append({
            "kind": "doi_resolver", "resolver": "openalex", "status": "ok",
            "url": openalex_url, "final_url": final_url,
            "paper_url_count": len(unique(paper_urls)),
            "pdf_url_count": len(unique(pdf_urls)),
        })
    except Exception as error:
        logs.append({
            "kind": "doi_resolver", "resolver": "openalex", "status": "error",
            "url": openalex_url, "error": clean(error),
        })

    encoded_doi = urllib.parse.quote(doi, safe="")
    crossref_url = f"https://api.crossref.org/works/{encoded_doi}"
    if contact_email:
        crossref_url += "?" + urllib.parse.urlencode({"mailto": contact_email})
    try:
        payload, final_url = request_json(
            crossref_url, timeout=timeout, retries=retries
        )
        message = payload.get("message") or {}
        if is_http_url(message.get("URL")):
            paper_urls.append(message["URL"])
        resource = message.get("resource") or {}
        primary = resource.get("primary") or {}
        if is_http_url(primary.get("URL")):
            paper_urls.append(primary["URL"])
        for item in message.get("link") or []:
            if not isinstance(item, dict) or not is_http_url(item.get("URL")):
                continue
            content_type = clean(item.get("content-type")).casefold()
            if "pdf" in content_type or item["URL"].casefold().endswith(".pdf"):
                pdf_urls.append(item["URL"])
            else:
                paper_urls.append(item["URL"])
        logs.append({
            "kind": "doi_resolver", "resolver": "crossref", "status": "ok",
            "url": crossref_url, "final_url": final_url,
            "paper_url_count": len(unique(paper_urls)),
            "pdf_url_count": len(unique(pdf_urls)),
        })
    except Exception as error:
        logs.append({
            "kind": "doi_resolver", "resolver": "crossref", "status": "error",
            "url": crossref_url, "error": clean(error),
        })
    return unique(paper_urls), unique(pdf_urls), logs


def decode_html(content: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    encodings = [charset_match.group(1).strip("\"'")] if charset_match else []
    encodings.extend(["utf-8", "latin-1"])
    for encoding in unique(encodings):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def looks_like_pdf(content: bytes, content_type: str, url: str) -> bool:
    # URL suffix and Content-Type are not trustworthy: several publishers
    # return an HTML challenge page from a URL ending in .pdf.
    return content[:1024].lstrip().startswith(b"%PDF-")


def extract_pdf(pdf_path: Path, text_path: Path, links_path: Path) -> dict[str, Any]:
    reader = PdfReader(str(pdf_path))
    text_parts: list[str] = []
    links: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, 1):
        try:
            text_parts.append(page.extract_text() or "")
        except Exception as error:  # pypdf handles many malformed PDFs page by page
            text_parts.append(f"\n[PAGE_{page_number}_TEXT_ERROR: {clean(error)}]\n")
        annotations = page.get("/Annots") or []
        for annotation_ref in annotations:
            try:
                annotation = annotation_ref.get_object()
                action = annotation.get("/A") or {}
                uri = clean(action.get("/URI"))
                if is_http_url(uri):
                    links.append({
                        "url": uri,
                        "page": page_number,
                        "source": "pdf_annotation",
                    })
            except Exception:
                continue
    extracted_text = "\n\n".join(text_parts)
    text_path.write_text(extracted_text, encoding="utf-8", errors="replace")
    write_json(links_path, unique(links))
    return {
        "pages": len(reader.pages),
        "link_count": len(unique(links)),
        "text_characters": len(clean(extracted_text)),
        "text_quality": "usable" if len(clean(extracted_text)) >= 500 else "sparse",
    }


def save_pdf_artifact(
    *,
    content: bytes,
    source_url: str,
    final_url: str,
    pdf_path: Path,
    text_path: Path,
    links_path: Path,
    seen_hashes: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    digest = hashlib.sha256(content).hexdigest()
    if digest in seen_hashes:
        return None, {
            "status": "duplicate",
            "sha256": digest,
            "duplicate_of": seen_hashes[digest],
        }
    pdf_path.write_bytes(content)
    artifact = {
        "kind": "article_pdf", "path": str(pdf_path),
        "text_path": str(text_path), "links_path": str(links_path),
        "source_url": source_url, "final_url": final_url,
        "status": "downloaded", "bytes": len(content), "sha256": digest,
    }
    try:
        artifact.update(extract_pdf(pdf_path, text_path, links_path))
        artifact["extraction_status"] = "ok"
    except Exception as error:
        artifact["extraction_status"] = "error"
        artifact["extraction_error"] = clean(error)
    seen_hashes[digest] = str(pdf_path)
    return artifact, {"status": "downloaded", "sha256": digest}


def record_urls(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    document = record.get("document") or {}
    identifiers = record.get("identifiers") or {}
    paper_urls = unique([
        document.get("paper_url"),
        *(document.get("paper_urls") or []),
    ])
    doi = clean(identifiers.get("doi"))
    if doi:
        paper_urls.append(f"https://doi.org/{doi}")
    pdf_urls = unique([
        document.get("pdf_url"),
        *(document.get("pdf_urls") or []),
    ])
    return (
        [url for url in unique(paper_urls) if is_http_url(url)],
        [url for url in unique(pdf_urls) if is_http_url(url)],
    )


def collect_record(record: dict[str, Any], args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    canonical_id = clean(record.get("canonical_id"))
    paper_dir = output_dir / "documents" / safe_name(canonical_id)
    paper_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": "article-content-manifest-v2",
        "canonical_id": canonical_id,
        "title": record.get("title"),
        "paper": deepcopy(record),
        "abstract_policy": "optional_not_collected",
        "artifacts": [],
        "requests": [],
        "discovered_resources": {"supplementary_urls": []},
        "status": "pending",
    }

    local_paths = unique([
        (record.get("document") or {}).get("local_path"),
        *((record.get("document") or {}).get("local_paths") or []),
    ])
    for local_path_value in local_paths:
        local_path = Path(str(local_path_value))
        result["artifacts"].append({
            "kind": "local_txt",
            "path": str(local_path.resolve()),
            "status": "available" if local_path.is_file() else "missing",
            "source_url": None,
        })

    paper_urls, existing_pdf_urls = record_urls(record)
    if args.mode == "plan":
        for url in paper_urls[: args.max_page_urls]:
            result["requests"].append({"kind": "article_page", "url": url, "status": "planned"})
        for url in existing_pdf_urls[: args.max_pdf_urls]:
            result["requests"].append({"kind": "article_pdf", "url": url, "status": "planned"})
        if args.doi_resolver_fallback and normalize_doi((record.get("identifiers") or {}).get("doi")):
            result["requests"].append({"kind": "doi_resolver", "status": "planned"})
        if args.jina_reader_fallback:
            result["requests"].append({"kind": "jina_reader", "status": "planned_on_direct_failure"})
        result["status"] = "planned"
        return result

    seen_pdf_hashes: dict[str, str] = {}
    discovered_pdf_urls: list[str] = []
    resolved_page_urls: list[str] = []
    resolved_pdf_urls: list[str] = []
    jina_targets: list[tuple[str, str]] = []

    def add_supplementary(urls: Iterable[str]) -> None:
        values = result["discovered_resources"]["supplementary_urls"]
        for url in urls:
            if is_http_url(url) and url not in values:
                values.append(url)

    def process_page(url: str, prefix: str, index: int, origin: str) -> bool:
        request_log = {
            "kind": "article_page", "origin": origin, "url": url, "status": "pending"
        }
        usable = False
        try:
            content, content_type, final_url = request_bytes(
                url,
                timeout=args.timeout,
                retries=args.retries,
                max_bytes=args.max_download_mb * 1024 * 1024,
            )
            if looks_like_pdf(content, content_type, final_url):
                if is_supplementary_url(final_url):
                    add_supplementary([final_url])
                    request_log.update({
                        "status": "classified_supplementary_not_downloaded",
                        "final_url": final_url,
                    })
                else:
                    artifact, pdf_result = save_pdf_artifact(
                        content=content,
                        source_url=url,
                        final_url=final_url,
                        pdf_path=paper_dir / f"{prefix}_{index}.pdf",
                        text_path=paper_dir / f"{prefix}_{index}_pdf.txt",
                        links_path=paper_dir / f"{prefix}_{index}_links.json",
                        seen_hashes=seen_pdf_hashes,
                    )
                    if artifact:
                        result["artifacts"].append(artifact)
                        usable = True
                    request_log.update({
                        **pdf_result, "final_url": final_url,
                        "content_type": content_type, "bytes": len(content),
                        "extraction_status": artifact.get("extraction_status") if artifact else None,
                    })
                result["requests"].append(request_log)
                return usable

            html_text = decode_html(content, content_type)
            visible_text, links, pdf_urls, supplementary_urls = parse_html(
                html_text, final_url
            )
            add_supplementary(supplementary_urls)
            discovered_pdf_urls.extend(pdf_urls)
            valid, quality_reason = html_quality(visible_text)
            if not valid:
                rejected_path = paper_dir / f"{prefix}_{index}_rejected.html"
                rejected_path.write_text(html_text, encoding="utf-8", errors="replace")
                request_log.update({
                    "status": "invalid_content", "final_url": final_url,
                    "content_type": content_type, "bytes": len(content),
                    "quality_reason": quality_reason,
                    "diagnostic_path": str(rejected_path),
                })
            else:
                html_path = paper_dir / f"{prefix}_{index}.html"
                text_path = paper_dir / f"{prefix}_{index}_html.txt"
                links_path = paper_dir / f"{prefix}_{index}_links.json"
                html_path.write_text(html_text, encoding="utf-8", errors="replace")
                text_path.write_text(visible_text, encoding="utf-8", errors="replace")
                write_json(links_path, links)
                result["artifacts"].append({
                    "kind": "article_html", "path": str(html_path),
                    "text_path": str(text_path), "links_path": str(links_path),
                    "source_url": url, "final_url": final_url,
                    "origin": origin, "status": "downloaded", "bytes": len(content),
                    "visible_text_characters": len(clean(visible_text)),
                    "quality_status": "usable", "link_count": len(links),
                    "discovered_pdf_urls": pdf_urls,
                    "discovered_supplementary_urls": supplementary_urls,
                })
                request_log.update({
                    "status": "downloaded", "final_url": final_url,
                    "content_type": content_type, "bytes": len(content),
                    "quality_reason": quality_reason,
                    "visible_text_characters": len(clean(visible_text)),
                    "link_count": len(links),
                    "discovered_pdf_count": len(pdf_urls),
                    "discovered_supplementary_count": len(supplementary_urls),
                })
                usable = True
        except Exception as error:
            request_log.update({"status": "error", "error": clean(error)})
        result["requests"].append(request_log)
        if not usable:
            jina_targets.append((url, f"{origin}_page_failed_or_invalid"))
        return usable

    for page_index, url in enumerate(paper_urls[: args.max_page_urls], 1):
        process_page(url, "article", page_index, "input")

    remote_artifact_kinds = {
        item.get("kind") for item in result["artifacts"]
        if item.get("kind") in {"article_html", "article_pdf"}
    }
    doi = normalize_doi((record.get("identifiers") or {}).get("doi"))
    if args.doi_resolver_fallback and doi and (
        "article_html" not in remote_artifact_kinds
        or "article_pdf" not in remote_artifact_kinds
    ):
        resolved_page_urls, resolved_pdf_urls, resolver_logs = resolve_doi_locations(
            doi,
            timeout=args.timeout,
            retries=args.retries,
            contact_email=args.contact_email,
        )
        result["requests"].extend(resolver_logs)
        fallback_pages = [
            url for url in resolved_page_urls if url not in set(paper_urls)
        ][: args.max_fallback_page_urls]
        if "article_html" not in remote_artifact_kinds:
            for fallback_index, url in enumerate(fallback_pages, 1):
                process_page(url, "fallback", fallback_index, "doi_resolver")

    existing_pdf_artifact_urls = {
        clean(item.get("final_url") or item.get("source_url"))
        for item in result["artifacts"]
        if item.get("kind") == "article_pdf"
    }
    pdf_urls: list[str] = []
    for url in unique([*existing_pdf_urls, *discovered_pdf_urls, *resolved_pdf_urls]):
        if is_supplementary_url(url):
            add_supplementary([url])
        elif url not in existing_pdf_artifact_urls:
            pdf_urls.append(url)

    for pdf_index, url in enumerate(pdf_urls[: args.max_pdf_urls], 1):
        request_log = {"kind": "article_pdf", "url": url, "status": "pending"}
        try:
            content, content_type, final_url = request_bytes(
                url,
                timeout=args.timeout,
                retries=args.retries,
                max_bytes=args.max_download_mb * 1024 * 1024,
            )
            if not looks_like_pdf(content, content_type, final_url):
                raise ValueError(f"响应不是 PDF: content-type={content_type}")
            if is_supplementary_url(final_url):
                add_supplementary([final_url])
                request_log.update({
                    "status": "classified_supplementary_not_downloaded",
                    "final_url": final_url,
                })
            else:
                artifact, pdf_result = save_pdf_artifact(
                    content=content,
                    source_url=url,
                    final_url=final_url,
                    pdf_path=paper_dir / f"article_{pdf_index}.pdf",
                    text_path=paper_dir / f"article_{pdf_index}_pdf.txt",
                    links_path=paper_dir / f"article_{pdf_index}_links.json",
                    seen_hashes=seen_pdf_hashes,
                )
                if artifact:
                    result["artifacts"].append(artifact)
                request_log.update({
                    **pdf_result, "final_url": final_url,
                    "content_type": content_type, "bytes": len(content),
                    "extraction_status": artifact.get("extraction_status") if artifact else None,
                })
        except Exception as error:
            request_log.update({"status": "error", "error": clean(error)})
            jina_targets.append((url, "article_pdf_direct_failed"))
        result["requests"].append(request_log)

    if args.jina_reader_fallback:
        attempted_targets: set[str] = set()
        for url, reason in jina_targets:
            if url in attempted_targets:
                continue
            if len(attempted_targets) >= args.max_jina_fallbacks:
                break
            attempted_targets.add(url)
            jina_index = len(attempted_targets)
            request_log = {
                "kind": "jina_reader", "url": url, "reason": reason, "status": "pending"
            }
            try:
                text, links, final_url = request_jina_reader(
                    url,
                    api_key=args.jina_api_key,
                    limiter=args.jina_rate_limiter,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                text_path = paper_dir / f"jina_{jina_index}.txt"
                links_path = paper_dir / f"jina_{jina_index}_links.json"
                text_path.write_text(text, encoding="utf-8", errors="replace")
                write_json(links_path, links)
                result["artifacts"].append({
                    "kind": "jina_reader_text", "path": str(text_path),
                    "text_path": str(text_path), "links_path": str(links_path),
                    "source_url": url, "final_url": final_url,
                    "status": "downloaded", "text_characters": len(text),
                    "link_count": len(links), "quality_status": "usable",
                })
                request_log.update({
                    "status": "downloaded", "final_url": final_url,
                    "text_characters": len(text), "link_count": len(links),
                })
            except Exception as error:
                request_log.update({"status": "error", "error": clean(error)})
            result["requests"].append(request_log)

    useful_artifacts = [
        item for item in result["artifacts"]
        if item.get("status") in {"available", "downloaded"}
        and item.get("kind") in {
            "local_txt", "article_html", "article_pdf", "jina_reader_text",
        }
    ]
    problems = sum(
        item.get("status") in {"error", "invalid_content"}
        for item in result["requests"]
    )
    if useful_artifacts and problems:
        result["status"] = "collected_with_errors"
    elif useful_artifacts:
        result["status"] = "collected"
    elif problems:
        result["status"] = "failed"
    else:
        result["status"] = "no_content_source"
    result["quality"] = {
        "useful_artifact_count": len(useful_artifacts),
        "request_problem_count": problems,
        "duplicate_pdf_count": sum(
            item.get("status") == "duplicate" for item in result["requests"]
        ),
        "supplementary_url_count": len(
            result["discovered_resources"]["supplementary_urls"]
        ),
    }
    return result


def write_review_xlsx(path: Path, manifests: list[dict[str, Any]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "正文采集"
    columns = [
        "canonical_id", "title", "status", "local_txt", "html", "pdf",
        "jina_reader", "duplicates_skipped", "supplementary_urls_discovered",
        "request_problems", "artifact_paths",
    ]
    sheet.append(columns)
    for manifest in manifests:
        kinds = Counter(
            item.get("kind") for item in manifest.get("artifacts") or []
            if item.get("status") in {"available", "downloaded"}
        )
        problems = [
            {
                "kind": item.get("kind"), "status": item.get("status"),
                "url": item.get("url"),
                "reason": item.get("error") or item.get("quality_reason"),
            }
            for item in manifest.get("requests") or []
            if item.get("status") in {"error", "invalid_content"}
        ]
        paths = [item.get("path") for item in manifest.get("artifacts") or [] if item.get("path")]
        row = {
            "canonical_id": manifest.get("canonical_id"),
            "title": manifest.get("title"),
            "status": manifest.get("status"),
            "local_txt": kinds["local_txt"],
            "html": kinds["article_html"],
            "pdf": kinds["article_pdf"],
            "jina_reader": kinds["jina_reader_text"],
            "duplicates_skipped": (manifest.get("quality") or {}).get("duplicate_pdf_count", 0),
            "supplementary_urls_discovered": json.dumps(
                (manifest.get("discovered_resources") or {}).get("supplementary_urls") or [],
                ensure_ascii=False,
            ),
            "request_problems": json.dumps(problems, ensure_ascii=False),
            "artifact_paths": json.dumps(paths, ensure_ascii=False),
        }
        sheet.append([row[column] for column in columns])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = fill
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 65
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect local TXT, publisher HTML, and article PDF content"
    )
    parser.add_argument("--mode", choices=("plan", "download"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=setting('stage1_env_file'))
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--max-page-urls", type=int, default=setting('max_page_urls'))
    parser.add_argument("--max-pdf-urls", type=int, default=setting('max_pdf_urls'))
    parser.add_argument("--max-fallback-page-urls", type=int, default=setting('max_fallback_page_urls'))
    parser.add_argument("--max-jina-fallbacks", type=int, default=setting('max_jina_fallbacks'))
    parser.add_argument("--max-download-mb", type=int, default=setting('article_max_mb'))
    parser.add_argument("--timeout", type=float, default=setting('download_timeout'))
    parser.add_argument("--retries", type=int, default=setting('download_retries'))
    parser.add_argument("--concurrency", type=int, default=setting('download_workers'))
    parser.add_argument("--doi-resolver-fallback", action="store_true")
    parser.add_argument("--jina-reader-fallback", action="store_true")
    parser.add_argument("--jina-min-interval", type=float)
    parser.add_argument("--contact-email")
    parser.add_argument("--allow-large-run", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="从已有 04_content_manifest.partial.jsonl 继续，跳过已完成记录",
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
    if not 1 <= args.concurrency <= 16:
        raise SystemExit("--concurrency 必须在 1 到 16 之间")
    if args.max_page_urls < 0 or args.max_pdf_urls < 0:
        raise SystemExit("--max-page-urls/--max-pdf-urls 不能小于 0")
    if args.max_fallback_page_urls < 0 or args.max_jina_fallbacks < 0:
        raise SystemExit("--max-fallback-page-urls/--max-jina-fallbacks 不能小于 0")

    args.jina_api_key = load_env_optional(args.env_file, "JINA_API_KEY")
    jina_interval = args.jina_min_interval
    if jina_interval is None:
        jina_interval = setting('jina_interval_with_key' if args.jina_api_key else 'jina_interval_without_key')
    args.jina_rate_limiter = RateLimiter(jina_interval)

    all_records = read_jsonl(args.input)
    selected = all_records[args.start_index:]
    if args.max_records is not None:
        selected = selected[: args.max_records]
    if args.mode == "download" and len(selected) > 100 and not args.allow_large_run:
        raise SystemExit(
            "联网采集超过 100 篇，先用 --max-records 试跑；全量确认后加入 --allow-large-run"
        )

    output_dir = args.output_dir.resolve()
    manifests_by_index: dict[int, dict[str, Any]] = {}
    partial_path = output_dir / "04_content_manifest.partial.jsonl"
    if args.mode == "plan":
        for index, record in enumerate(selected):
            manifests_by_index[index] = collect_record(record, args, output_dir)
    else:
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        if args.resume and partial_path.is_file():
            manifests_by_index, damaged_lines = load_resumable_manifests(
                partial_path, selected
            )
            for line_number in damaged_lines:
                print(
                    f"[resume] 忽略损坏的 partial 行 {line_number}",
                    flush=True,
                )
            print(
                f"[resume] 已恢复 {len(manifests_by_index)}/{len(selected)} 条完成记录",
                flush=True,
            )

        pending_indices = [
            index for index in range(len(selected))
            if index not in manifests_by_index
        ]
        partial_mode = "a" if args.resume else "w"
        with partial_path.open(partial_mode, encoding="utf-8") as partial_handle:
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                future_to_index = {
                    executor.submit(collect_record, record, args, output_dir): index
                    for index in pending_indices
                    for record in [selected[index]]
                }
                completed = len(manifests_by_index)
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    try:
                        manifest = future.result()
                    except Exception as error:
                        record = selected[index]
                        manifest = {
                            "schema_version": "article-content-manifest-v2",
                            "canonical_id": record.get("canonical_id"),
                            "title": record.get("title"),
                            "paper": record,
                            "abstract_policy": "optional_not_collected",
                            "artifacts": [],
                            "requests": [],
                            "status": "failed",
                            "fatal_error": clean(error),
                        }
                    manifests_by_index[index] = manifest
                    partial_handle.write(json.dumps({
                        "input_index": index,
                        **manifest,
                    }, ensure_ascii=False, separators=(",", ":")) + "\n")
                    partial_handle.flush()
                    completed += 1
                    print(
                        f"[{completed}/{len(selected)}] "
                        f"{manifest.get('canonical_id')} -> {manifest.get('status')}",
                        flush=True,
                    )

    manifests = [manifests_by_index[index] for index in sorted(manifests_by_index)]
    status_counts = Counter(item.get("status") for item in manifests)
    artifact_counts = Counter(
        artifact.get("kind")
        for manifest in manifests
        for artifact in manifest.get("artifacts") or []
        if artifact.get("status") in {"available", "downloaded"}
    )
    request_counts = Counter(
        request.get("status")
        for manifest in manifests
        for request in manifest.get("requests") or []
    )
    record_coverage = {
        kind: sum(any(
            artifact.get("kind") == kind
            and artifact.get("status") in {"available", "downloaded"}
            for artifact in manifest.get("artifacts") or []
        ) for manifest in manifests)
        for kind in ("local_txt", "article_html", "article_pdf", "jina_reader_text")
    }
    summary = {
        "schema_version": "article-content-summary-v2",
        "generated_at": now_utc(),
        "mode": args.mode,
        "input": str(args.input.resolve()),
        "input_records": len(all_records),
        "selected_records": len(selected),
        "abstract_required": False,
        "status_counts": dict(sorted(status_counts.items())),
        "artifact_counts": dict(sorted(artifact_counts.items())),
        "record_coverage": record_coverage,
        "request_counts": dict(sorted(request_counts.items())),
        "duplicate_pdfs_skipped": sum(
            (manifest.get("quality") or {}).get("duplicate_pdf_count", 0)
            for manifest in manifests
        ),
        "supplementary_urls_discovered": sum(
            (manifest.get("quality") or {}).get("supplementary_url_count", 0)
            for manifest in manifests
        ),
        "fallbacks": {
            "doi_resolver_enabled": args.doi_resolver_fallback,
            "jina_reader_enabled": args.jina_reader_fallback,
            "jina_api_key_present": bool(args.jina_api_key),
        },
        "partial_manifest": str(partial_path) if args.mode == "download" else None,
    }
    write_jsonl(output_dir / "04_content_manifest.jsonl", manifests)
    write_json(output_dir / "04_content_summary.json", summary)
    write_review_xlsx(output_dir / "04_content_review.xlsx", manifests)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
