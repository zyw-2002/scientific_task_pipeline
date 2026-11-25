"""Offline review of material identity for paper-conditioned task construction."""

from __future__ import annotations

from collections import defaultdict
import hashlib
from importlib import import_module
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote, urlsplit
import zipfile

from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from pipeline.stage1_sources.materials.data_samples import repository_sample_reason


extractor = import_module("05_extract_resource_candidates")
FAMILIES = {
    "code": "code_urls",
    "data": "data_urls",
    "model": "model_urls",
    "supplementary": "supplementary_urls",
}
READY = {"downloaded", "materialized_local", "reused_verified"}
RESOURCE_LANGUAGE = re.compile(
    r"\b(?:availability|available|access(?:ed|ible)?|deposited|released?|archived?|download(?:ed)?|provided?|implementation|code|repository|datasets?)\b",
    re.I,
)
CITATION_HEADING = re.compile(
    r"^\s*(?:references|bibliography|literature cited)\s*$", re.I | re.M
)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def condensed(value):
    return re.sub(
        "[^a-z0-9]", "", unicodedata.normalize("NFKD", value or "").casefold()
    )


def title_in_header(title, header):
    if len(title) < 12:
        return False
    if title in condensed(header):
        return True
    # Line numbers in PDF proofs can separate or touch successive title words.
    unnumbered = re.sub(r"(?<=[a-zA-Z])\d+(?=\s)|(?<!\w)\d+(?=\s|$)", "", header)
    return title in condensed(unnumbered)


def key_for_url(value):
    if not isinstance(value, str):
        return None
    try:
        payload = urlsplit(value)
        if payload.username or payload.password:
            return None
        url = extractor.normalize_url(value)
    except ValueError:
        return None
    if not url:
        return None
    p = urlsplit(url)
    host = (p.hostname or "").removeprefix("www.").lower()
    path = unquote(p.path).strip("/").lower()
    parts = path.split("/")
    if (
        host in {"github.com", "raw.githubusercontent.com", "codeload.github.com"}
        and len(parts) >= 2
    ):
        return "github:" + "/".join(parts[:2]).removesuffix(".git")
    if host in {"zenodo.org", "doi.org", "dx.doi.org"}:
        m = re.search(
            r"(?:^|/)(?:(?:api/)?records?/|10\.5281/zenodo\.)(\d+)(?:/|$)", path
        )
        if m:
            return "zenodo:" + m.group(1)
    if host.endswith(".figshare.com") or host in {"figshare.com", "doi.org"}:
        m = re.search(
            r"(?:articles/(?:[^/]+/)*|10\.6084/m9\.figshare\.)(\d+)(?:\.v\d+|/\d+)?$",
            path,
        )
        if m:
            return "figshare:" + m.group(1)
    if host.endswith("ncbi.nlm.nih.gov") or host == "ebi.ac.uk":
        m = re.search(
            r"\b(?:PXD\d+|GSE\d+|PRJ(?:NA|EB|DB)\d+|E-MTAB-\d+|EMD-\d+)\b",
            unquote(url),
            re.I,
        )
        if m:
            return "accession:" + m.group().upper()
    return host + "/" + path + (("?" + p.query) if p.query else "")


def paper_evidence(path, record, *, include_pdf_body=False):
    """Bind evidence to the article header, before considering any body links."""
    title = condensed(record["title"])
    doi = (record.get("identifiers", {}).get("doi") or "").lower()
    suffix = path.suffix.lower()
    if suffix not in {".html", ".htm", ".pdf", ".txt", ".text"}:
        with path.open("rb") as f:
            if f.read(5) == b"%PDF-":
                suffix = ".pdf"
    header = ""
    html = None
    annotations = []
    body = ""
    ids = set()
    if suffix in {".html", ".htm"}:
        html = BeautifulSoup(path.read_bytes(), "html.parser")
        meta = {
            str(tag.get("name") or tag.get("property") or "").lower(): str(
                tag.get("content") or ""
            )
            for tag in html.find_all("meta")
        }
        h = html.find("h1") or html.title
        header = (
            meta.get("citation_title")
            or meta.get("dc.title")
            or meta.get("og:title")
            or (h.get_text(" ", strip=True) if h else "")
        )
        actual_doi = meta.get("citation_doi") or meta.get("dc.identifier.doi")
        if (
            actual_doi
            and doi
            and actual_doi.lower().removeprefix("https://doi.org/") != doi
            and title not in condensed(header)
        ):
            return None, "publisher DOI and title belong to a different article"
        ids = {
            x.upper()
            for k, v in meta.items()
            if k in {"citation_pmcid", "citation_pmc", "ncbi_pcid"}
            for x in re.findall(r"PMC\d+", v, re.I)
        }
        if (
            "citation_title" in meta
            and title not in condensed(header)
            and doi
            and actual_doi
            and actual_doi.lower() == doi
        ):
            # A DOI alone may describe an incorrectly joined record.
            return None, "citation title disagrees with selected record"
    elif suffix == ".pdf":
        reader = PdfReader(path, strict=False)
        metadata_title = (reader.metadata or {}).get("/Title") or ""
        page = reader.pages[0].extract_text() or ""
        header = (
            metadata_title if title in condensed(str(metadata_title)) else page[:2500]
        )
        body = page
        if title_in_header(title, header):
            if include_pdf_body:
                body = "\n".join(p.extract_text() or "" for p in reader.pages)
            for p in reader.pages:
                page_text = None
                for ref in p.get("/Annots") or []:
                    annot = ref.get_object()
                    action = annot.get("/A")
                    if action:
                        uri = action.get_object().get("/URI")
                        if isinstance(uri, str) and uri.startswith(
                            ("https://", "http://")
                        ):
                            if not extractor.classify_url(uri):
                                continue
                            if page_text is None:
                                page_text = CITATION_HEADING.split(
                                    p.extract_text() or "", maxsplit=1
                                )[0]
                            endpoint = (
                                unquote(urlsplit(uri).path).rstrip("/").split("/")[-1]
                            )
                            for match in (
                                re.finditer(re.escape(endpoint), page_text, re.I)
                                if endpoint
                                else []
                            ):
                                nearby = " ".join(
                                    page_text[
                                        max(0, match.start() - 170) : match.end() + 120
                                    ].split()
                                )
                                if RESOURCE_LANGUAGE.search(nearby):
                                    annotations.append((uri, nearby))
                                    break
    elif suffix in {".txt", ".text"}:
        body = path.read_text(errors="replace")
        header = body[:3200]
    else:
        return None, "unsupported paper representation"
    if not title_in_header(title, header):
        return None, "paper title unconfirmed in publisher metadata or opening text"
    if html is not None:
        # Reference-only occurrences, sidebars and links to related articles cannot
        # establish ownership. Publisher availability sections may follow references.
        for node in html.select(
            'nav, aside, footer, script, style, [role="doc-bibliography"], .ref-list, .references, #references, #Bib1, .c-article-references'
        ):
            if node.parent:
                node.decompose()
        for tag in html.find_all(["h2", "h3"]):
            if (
                tag.get_text(" ", strip=True).lower() in {"references", "bibliography"}
                and tag.parent
                and tag.parent.name == "section"
            ):
                tag.parent.decompose()
        main = html.find("article") or html.find("main") or html.body or html
        links = []
        for a in main.find_all("a", href=True):
            if a.find_parent(
                class_=re.compile(r"bib[-_]?entry|ref[-_]list|related-article", re.I)
            ):
                continue
            href = a["href"]
            if not href.startswith(("http://", "https://")):
                continue
            block = a.find_parent(["p", "dd", "li", "td"]) or a
            context = block.get_text(" ", strip=True)
            if len(context) > 2200:
                anchor = a.get_text(" ", strip=True)
                pos = context.find(anchor)
                context = context[max(0, pos - 300) : pos + len(anchor) + 300]
            links.append((href, context))
        body = main.get_text("\n", strip=True)
    else:
        links = []
    # Follow exact typed resource locators in the body. Reference-only matches in
    # extracted text are deliberately omitted; structured availability comes above.
    body = CITATION_HEADING.split(body, maxsplit=1)[0]
    body = re.sub(r"[\u200b\u00ad]", "", body)
    body = re.sub(r"github\.\s+com", "github.com", body, flags=re.I)
    body = re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "", body)
    body = re.sub(r"(?<=/)\s*\n\s*(?=[\w-])", "", body)
    for m in extractor.URL_RE.finditer(body):
        links.append(
            (m.group(), " ".join(body[max(0, m.start() - 170) : m.end() + 150].split()))
        )
    for m in re.finditer(
        r"\b(?:PXD\d+|GSE\d+|PRJ(?:NA|EB|DB)\d+|E-MTAB-\d+|EMD-\d+)\b", body, re.I
    ):
        links.append(
            (
                "accession:" + m.group().upper(),
                " ".join(body[max(0, m.start() - 140) : m.end() + 120].split()),
            )
        )
    for pat in extractor.DATA_DOI_PATTERNS:
        for m in pat.finditer(body):
            links.append(
                (
                    "https://doi.org/" + m.group(),
                    " ".join(body[max(0, m.start() - 150) : m.end() + 120].split()),
                )
            )
    # Keep the annotation as an exact address. Extraction can otherwise lose
    # characters such as the fl ligature in a repository owner's name.
    links.extend(annotations)
    return dict(
        path=str(path),
        sha256=file_digest(path),
        header=" ".join(str(header).split())[:400],
        pmcids=sorted(ids),
        links=links,
    ), None


def article_relation(kind, url, evidence, record):
    key = key_for_url(url)
    if not key:
        return None
    doi = (record.get("identifiers", {}).get("doi") or "").lower()
    decoded = unquote(url).lower()
    host = (urlsplit(url).hostname or "").lower()
    if kind == "supplementary":
        if (
            host
            in {
                "media.springernature.com",
                "link.springer.com",
                "pubs.acs.org",
                "acs.figshare.com",
                "rsc.org",
                "www.rsc.org",
                "academic.oup.com",
            }
            and doi
            and re.search(re.escape(doi) + r"(?=$|[/?#])", decoded)
        ):
            return dict(
                method="publisher attachment URL contains the article DOI", uri=url
            )
        pmc = re.search(r"/(PMC\d+)/supplementaryFiles/?$", url, re.I)
        if pmc and host in {"ebi.ac.uk", "www.ebi.ac.uk"}:
            article_ids = {i for p in evidence for i in p["pmcids"]}
            known = record.get("identifiers", {}).get("pmcid")
            if known:
                article_ids.add(known.upper())
            if pmc.group(1).upper() in article_ids:
                return dict(
                    method="Europe PMC supplement bound to article PMCID", uri=url
                )
    for article in evidence:
        for linked, context in article["links"]:
            if any(ord(c) < 32 for c in linked):
                continue
            found_key = (
                linked if linked.startswith("accession:") else key_for_url(linked)
            )
            if found_key != key:
                continue
            if not RESOURCE_LANGUAGE.search(context) and kind != "supplementary":
                continue
            if key.startswith("github:"):
                # Judge the clause naming this repository. Availability language
                # for the previous link can describe a different implementation.
                clause = next(
                    (
                        s
                        for s in re.split(r"(?<=[.;])\s+(?=[A-Z])", context)
                        if key.partition(":")[2] in s.lower()
                    ),
                    context,
                )
                if re.search(
                    r"\b(?:implementations? by|adapted from|software package|we use the implementations|was used to convert|per-?\s*formed using|parsed with)\b",
                    clause,
                    re.I,
                ):
                    continue
                if (
                    re.search(
                        r"\b\d+https?://|data files can be found in the pertaining github repository",
                        clause,
                        re.I,
                    )
                    and kind == "code"
                ):
                    continue
                if kind == "data" and not re.search(
                    r"\b(?:data(?:set)?s?|files)\b.{0,110}\b(?:available|access|download|repository|stored|can be found|folder)|\b(?:deposited|provide[sd]?|release[sd]?|contains?|access to)\b.{0,55}\bdata(?:set)?s?\b",
                    clause,
                    re.I,
                ):
                    continue
                if (
                    kind == "code"
                    and "code" not in clause.lower()
                    and re.search(
                        r"datasets?.{0,60}(?:accessed|repository)", clause, re.I
                    )
                ):
                    continue
            if kind == "supplementary" and key.startswith(("figshare:", "zenodo:")):
                # Membership of the overall dataset does not identify a file as
                # publisher supplementary material.
                if urlsplit(linked).path != urlsplit(url).path:
                    continue
            return dict(
                method="identifier in primary article resource context",
                paper_sha256=article["sha256"],
                article_path=article["path"],
                linked_url=linked,
                context=context[:900],
            )
    return None


def repository_readme(record, row, roots):
    """Only a root README identifying its own paper can establish a code archive."""
    path = verified_local(row.get("local_path"), roots)
    key = key_for_url(row.get("input_url"))
    if (
        not key
        or not key.startswith("github:")
        or key_for_url(row.get("download_url")) != key
        or not path
    ):
        return None
    if path.suffix.lower() != ".zip" or not row.get("sha256"):
        return None
    repo = key.split("/")[-1]
    if re.search(r"awesome|arxiv-radar|paper-list|weekly-|survey", repo):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            readme = next(
                (
                    x
                    for x in z.infolist()
                    if re.fullmatch(
                        r"[^/]+/readme(?:\.md|\.rst|\.txt)?", x.filename, re.I
                    )
                    and x.file_size <= 512000
                ),
                None,
            )
            if not readme:
                return None
            body = z.read(readme).decode("utf-8", errors="replace")
            intro = body[:800]
            candidates = [intro[:300]]
            ids = record.get("identifiers") or {}
            if any(
                v and str(v).lower() in intro.lower()
                for v in [ids.get("arxiv"), ids.get("doi")]
            ):
                candidates.append(intro)
            for matches in re.finditer(
                r"(?:our paper|official implementation|code (?:for|accompanying)|if you use [^\n]{0,65}(?:cite|research)|citation for this (?:package|software))",
                body,
                re.I,
            ):
                candidates.append(
                    body[max(0, matches.start() - 30) : matches.end() + 540]
                )
            claim = next(
                (c for c in candidates if condensed(record["title"]) in condensed(c)),
                None,
            )
            if not claim:
                return None
            if file_digest(path) != row["sha256"]:
                return None
            return dict(
                method="archive root README identifies its paper",
                archive_sha256=row["sha256"],
                member=readme.filename,
                excerpt=claim[:590],
            )
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None


def member_issue(row, path, url_key):
    if row["kind"] == "data" and row.get("data_scope") != "sample":
        return "construction intake accepts development examples only"
    transfer = row.get("download_url") or row.get("input_url") or ""
    transfer_key = key_for_url(transfer)
    if url_key.startswith("github:"):
        if transfer_key != url_key:
            return "download source does not match cited owner/repository"
        if row["kind"] == "data":
            member = unquote(urlsplit(transfer).path).strip("/").split("/", 3)
            if len(member) < 4:
                return "repository member unspecified"
            return repository_sample_reason(member[-1])
    if row["kind"] == "data":
        if url_key.startswith("zenodo:") and transfer_key != url_key:
            return "download points to a different Zenodo record"
        if url_key.startswith("figshare:") and transfer_key != url_key:
            selection = path.parent.parent / "sample-selection.json"
            if not selection.is_file() or not any(
                i.get("url") == transfer
                for i in json.loads(selection.read_bytes()).get("selected", [])
            ):
                return "file absent from original Figshare sampling receipt"
    return None


def verified_local(value, roots):
    if not isinstance(value, str):
        return None
    path = Path(value)
    if ".." in path.parts or any(p.startswith(".") for p in path.parts):
        return None
    candidates = [path] if path.is_absolute() else [root / path for root in roots]
    valid = {
        p
        for p in candidates
        if any(p.is_relative_to(root) for root in roots)
        and not any(part.is_symlink() for part in (p, *p.parents))
        and p.is_file()
    }
    return next(iter(valid)) if len(valid) == 1 else None


def review_materials(record, downloads, *, roots):
    roots = [Path(p).resolve(strict=True) for p in roots]
    decisions = []
    papers = {}
    local = defaultdict(list)
    for a in record.get("paper_content", {}).get("local_artifacts", []):
        if a.get("valid_file") is not False:
            for k, sha in [("path", "sha256"), ("text_path", "text_sha256")]:
                if a.get(k):
                    local[a[k]].append(a.get(sha))
    doc = record.get("document", {})
    paths = doc.get("local_paths") or []
    for p in [doc.get("local_path"), *([paths] if isinstance(paths, str) else paths)]:
        if isinstance(p, str) and p:
            local[p].append(None)
    for row in downloads:
        if row.get("canonical_id") != record["canonical_id"]:
            raise ValueError("download record has a different canonical ID")
        if (
            row.get("kind") == "paper"
            and row.get("local_path")
            and row.get("status") in READY
        ):
            local[row["local_path"]].append(row.get("sha256"))
    for name, expected in local.items():
        path = verified_local(name, roots)
        item = dict(kind="paper", path=name, decision="held")
        evidence = None
        error = "paper missing or outside declared material roots"
        if path:
            try:
                evidence, error = paper_evidence(
                    path,
                    record,
                    include_pdf_body=not any(str(f).endswith(".txt") for f in local),
                )
            except (OSError, PdfReadError, ValueError, IndexError, KeyError) as exc:
                error = "paper parsing failed: " + str(exc)[:120]
        if evidence and any(s and s != evidence["sha256"] for s in expected):
            evidence, error = None, "recorded checksum disagrees with file"
        if evidence:
            item.update(
                decision="kept", sha256=evidence["sha256"], header=evidence["header"]
            )
            papers.setdefault(evidence["sha256"], evidence)
        else:
            item["reason"] = error
        decisions.append(item)
    evidence = list(papers.values())
    archive_evidence = {}
    if evidence:
        for row in downloads:
            if row.get("kind") == "code":
                result = repository_readme(record, row, roots)
                if result:
                    archive_evidence[key_for_url(row["input_url"])] = result
    urls = {
        kind: list((record.get("resources") or {}).get(field) or [])
        for kind, field in FAMILIES.items()
    }
    for row in downloads:
        if row.get("kind") in urls and row.get("input_url"):
            urls[row["kind"]].append(row["input_url"])
    allowed = {}
    published = {}
    for kind, entries in urls.items():
        published[FAMILIES[kind]] = []
        for url in dict.fromkeys(entries):
            proof = article_relation(kind, url, evidence, record) if evidence else None
            if not proof and kind == "code":
                proof = archive_evidence.get(key_for_url(url))
            item = dict(kind=kind, input_url=url, decision="held", scope="url")
            if proof:
                allowed[kind, url] = proof
                published[FAMILIES[kind]].append(url)
                item.update(decision="kept", evidence=proof)
            else:
                item["reason"] = (
                    "no confirmed link between the cited primary article and this resource"
                )
            decisions.append(item)
    # Some PDF extractors concatenate superscript reference numbers directly to
    # a Zenodo identifier. Demand independent structured evidence for the longer ID.
    for kind, url in list(allowed):
        key = key_for_url(url)
        suffix = re.search(r"(?:for|and|from|the)$", key)
        if (
            key.startswith("github:")
            and suffix
            and any(
                key_for_url(u) == key[: suffix.start()] for k, u in allowed if k == kind
            )
        ):
            del allowed[kind, url]
            published[FAMILIES[kind]].remove(url)
            for item in decisions:
                if (
                    item.get("scope") == "url"
                    and item["kind"] == kind
                    and item["input_url"] == url
                ):
                    item.pop("evidence", None)
                    item.update(
                        decision="held",
                        reason="repository name joined to following prose",
                    )
            continue
        if key.startswith("zenodo:") and any(
            key.startswith(key_for_url(other)) and key != key_for_url(other)
            for k, other in allowed
            if k == kind and key_for_url(other).startswith("zenodo:")
        ):
            proof = allowed[kind, url]
            if not any(
                key_for_url(u) == key
                and p["path"].lower().endswith(".html")
                and "zenodo" in u.lower()
                for p in evidence
                for u, _ in p["links"]
                if not u.startswith("accession:")
            ):
                del allowed[kind, url]
                published[FAMILIES[kind]].remove(url)
                for item in decisions:
                    if (
                        item.get("scope") == "url"
                        and item["kind"] == kind
                        and item["input_url"] == url
                    ):
                        item.pop("evidence", None)
                        item.update(
                            decision="held",
                            reason="ambiguous numeric suffix in extracted citation",
                        )
    public = {
        k: record[k]
        for k in (
            "canonical_id",
            "title",
            "identifiers",
            "abstract",
            "year",
            "schema_version",
        )
        if k in record
    }
    ids = public.get("identifiers") or {}
    safe_doc = {}
    if ids.get("doi"):
        safe_doc["paper_url"] = "https://doi.org/" + ids["doi"]
    elif ids.get("arxiv"):
        safe_doc["paper_url"] = "https://arxiv.org/abs/" + ids["arxiv"]
    public.update(
        document=safe_doc,
        resources=published,
        paper_content={
            "local_artifacts": [
                {
                    "path": p["path"],
                    "sha256": p["sha256"],
                    "kind": "article_text" if p["path"].endswith(".txt") else "article",
                    "valid_file": True,
                }
                for p in evidence
            ]
        },
    )
    retained = []
    seen = set()
    for row in downloads:
        if row.get("kind") == "paper":
            continue  # Registered once per unique digest through paper_content.
        kind, url = row.get("kind"), row.get("input_url")
        path = verified_local(row.get("local_path"), roots)
        item = dict(
            kind=kind,
            input_url=url,
            path=row.get("local_path"),
            scope="download",
            decision="held",
        )
        if (kind, url) not in allowed:
            reason = "resource identity unconfirmed"
        elif row.get("status") not in READY or path is None:
            reason = "no local artifact; confirmed remote locator retained separately"
        else:
            reason = member_issue(row, path, key_for_url(url))
            if not reason and (
                not row.get("sha256")
                or file_digest(path) != row["sha256"]
                or path.stat().st_size != row.get("bytes")
            ):
                reason = "local resource integrity unconfirmed"
        if reason:
            item["reason"] = reason
        elif (kind, row["sha256"]) in seen:
            item["reason"] = "duplicate local content"
        else:
            seen.add((kind, row["sha256"]))
            item.update(
                decision="kept", sha256=row["sha256"], evidence=allowed[kind, url]
            )
            accepted = {
                k: row[k]
                for k in (
                    "schema_version",
                    "resource_id",
                    "canonical_id",
                    "kind",
                    "status",
                    "local_path",
                    "bytes",
                    "sha256",
                    "input_url",
                    "download_url",
                    "data_scope",
                    "sampling_method",
                    "sample_rows",
                    "dataset_completeness",
                    "source_expected_checksum",
                    "repository_commit",
                    "source_exhausted",
                    "media_type",
                )
                if k in row
            }
            accepted["local_path"] = str(path)
            retained.append(accepted)
        decisions.append(item)
    return public, retained, decisions
