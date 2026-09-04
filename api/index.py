import os
import re
import json
import unicodedata
from io import BytesIO
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from pypdf import PdfReader

VERSION = "1.7.1"
DOE_SEARCH = "https://do-api-web-search.doe.sp.gov.br"
DOE_PDF = "https://do-api-publication-pdf.doe.sp.gov.br"
DOE_WEB = "https://doe.sp.gov.br"
SEARCH_URL = f"{DOE_SEARCH}/v2/advanced-search/publications"
JOURNALS_URL = f"{DOE_SEARCH}/v2/journals"
SECTIONS_URL = f"{DOE_SEARCH}/v2/sections"
EDITION_URL = f"{DOE_PDF}/v1/editions/url"
TIMEOUT = float(os.getenv("DOESP_TIMEOUT_SECONDS", "25"))
PAGE_SIZE = 100
MAX_PAGES = 50
MAX_PDF_BYTES = int(os.getenv("DOESP_MAX_PDF_BYTES", str(40 * 1024 * 1024)))
AUDITOR_CGE_TERMS = (
    "AUDITOR ESTADUAL DE CONTROLE",
    "EDITAL CGE Nº 03/2025",
)

app = FastAPI(title="DOE-SP Bridge", version=VERSION)


def env_list(name: str) -> List[str]:
    return [x.strip() for x in re.split(r"[|,;\r\n]+", os.getenv(name, "")) if x.strip()]


PROFILE_NAME = os.getenv("DOESP_PROFILE_NAME", "").strip()
PROFILE_MATRICULAS = env_list("DOESP_PROFILE_MATRICULAS")
PROFILE_RGS = env_list("DOESP_PROFILE_RGS")
PROFILE_CPFS = env_list("DOESP_PROFILE_CPFS")
PROFILE_OTHER_IDS = env_list("DOESP_PROFILE_OTHER_IDS")


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c))


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(value).casefold()).strip()


def digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def identifier_variants(value: str) -> List[str]:
    d = digits(value)
    variants = [value, d, d.lstrip("0")]
    if re.search(r"[-./]", value or "") and len(d) >= 5:
        variants += [d[:-1], d[:-1].lstrip("0")]
    return unique([v for v in variants if v])


def all_identifiers() -> List[str]:
    out: List[str] = []
    for value in PROFILE_MATRICULAS + PROFILE_RGS + PROFILE_CPFS + PROFILE_OTHER_IDS:
        out += identifier_variants(value)
    return unique(out)


def profile_terms() -> List[str]:
    terms = [PROFILE_NAME, strip_accents(PROFILE_NAME)] if PROFILE_NAME else []
    terms += all_identifiers()
    return unique([x for x in terms if x])


def text_has_identifier(text: str) -> bool:
    td = digits(text)
    return any(digits(v) and digits(v) in td for v in all_identifiers())


def extract_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "results", "hits", "data", "publications", "sections", "journals"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = extract_items(value)
                if nested:
                    return nested
    return []


def item_key(item: Dict[str, Any]) -> str:
    return str(item.get("id") or item.get("publicationId") or item.get("slug") or json.dumps(item, sort_keys=True)[:400])


def item_text(item: Dict[str, Any]) -> str:
    return " ".join(str(item.get(k) or "") for k in ("title", "excerpt", "hierarchy", "content", "description"))


def identity_verified(item: Dict[str, Any]) -> bool:
    text = norm(item_text(item))
    name_ok = bool(PROFILE_NAME and norm(PROFILE_NAME) in text)
    configured_ids = bool(PROFILE_MATRICULAS or PROFILE_RGS or PROFILE_CPFS or PROFILE_OTHER_IDS)
    return name_ok and (text_has_identifier(text) if configured_ids else True)


def auditor_cge_contest_reasons(item: Dict[str, Any]) -> List[str]:
    text = norm(item_text(item))
    if "auditor estadual de controle" not in text:
        return []

    reasons: List[str] = []
    markers = (
        ("edital_cge_03_2025", "edital", "03/2025"),
        ("concurso_publico", "concurso publico"),
        ("comissao_concurso", "comissao especial de concurso"),
        ("chamamento", "chamamento de candidatos"),
        ("anuencia_vaga", "anuencia de vaga"),
        ("candidato", "candidat"),
        ("homologacao", "homologacao"),
        ("classificacao", "classific"),
        ("nomeacao", "nomea"),
        ("posse", "posse"),
        ("exercicio", "exercicio"),
    )
    for marker in markers:
        label, *needles = marker
        if all(needle in text for needle in needles):
            reasons.append(label)
    return unique(reasons)


async def http_get(url: str, params: Optional[dict] = None, accept: str = "application/json,*/*") -> httpx.Response:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        return await client.get(url, params=params, headers={"Accept": accept, "User-Agent": f"DOE-SP-Bridge/{VERSION}"})


async def raw_search(term: str, from_date: date, to_date: date) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    seen = set()
    pages = 0
    truncated = False
    for page in range(1, MAX_PAGES + 1):
        params = {
            "PageNumber": page,
            "PageSize": PAGE_SIZE,
            "SortField": "Date",
            "periodStartingDate": from_date.isoformat(),
            "FromDate": from_date.isoformat(),
            "ToDate": to_date.isoformat(),
            "Terms[0]": term,
        }
        response = await http_get(SEARCH_URL, params=params)
        if response.status_code >= 400:
            raise HTTPException(502, detail={"message": "DOE-SP API recusou a consulta", "status": response.status_code})
        batch = extract_items(response.json())
        pages += 1
        if not batch:
            break
        added = 0
        for item in batch:
            key = item_key(item)
            if key not in seen:
                seen.add(key)
                items.append(item)
                added += 1
        if len(batch) < PAGE_SIZE or added == 0:
            break
    else:
        truncated = True
    return {"items": items, "pages": pages, "truncated": truncated}


async def profile_search(from_date: date, to_date: date) -> Dict[str, Any]:
    merged: Dict[str, Dict[str, Any]] = {}
    pages = 0
    truncated = False
    for term in profile_terms():
        result = await raw_search(term, from_date, to_date)
        pages += result["pages"]
        truncated = truncated or result["truncated"]
        for item in result["items"]:
            merged[item_key(item)] = item

    matches: List[Dict[str, Any]] = []
    weak = 0
    for item in merged.values():
        if identity_verified(item):
            row = dict(item)
            row["matchConfidence"] = "verified"
            row["matchedBy"] = ["name", "identifier"]
            matches.append(row)
        else:
            weak += 1
    matches.sort(key=lambda x: str(x.get("date") or ""))
    return {"matches": matches, "pages": pages, "truncated": truncated, "weak": weak}


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")


def response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        try:
            return response.text
        except Exception:
            return None


def candidate_strings(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out += candidate_strings(v)
    elif isinstance(value, list):
        for v in value:
            out += candidate_strings(v)
    return out


def edition_reference(response: httpx.Response) -> Tuple[Optional[str], Optional[str]]:
    payload = response_payload(response)
    for candidate in candidate_strings(payload):
        match = UUID_RE.search(candidate or "")
        eid = match.group(0) if match else None
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return eid, candidate.replace("http://", "https://")
        if candidate.startswith("/") or candidate.startswith("v1/"):
            return eid, urljoin(DOE_PDF + "/", candidate)
        if eid:
            return eid, f"{DOE_PDF}/v1/editions/{eid}"
    return None, None


async def publication_detail(item: Dict[str, Any]) -> Dict[str, Any]:
    slug = str(item.get("slug") or "").strip("/")
    iid = str(item.get("id") or "")
    probes: List[Dict[str, Any]] = []
    urls = []
    if slug:
        urls.append(f"{DOE_SEARCH}/v2/publications/{slug}")
    if iid:
        urls.append(f"{DOE_SEARCH}/v2/publications/{iid}")
    for url in unique(urls):
        response = await http_get(url)
        probe = {"url": url, "status": response.status_code}
        if response.status_code < 400:
            payload = response_payload(response)
            if isinstance(payload, dict):
                return {"payload": payload, "probes": probes + [probe]}
        probes.append(probe)
    return {"payload": None, "probes": probes}


def coerce_page(value: Any) -> Optional[int]:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,4}\s*", value):
        page = int(value.strip())
        return page if page > 0 else None
    return None


def pages_from_edition_pages(value: Any) -> List[int]:
    pages: List[int] = []
    if value is None:
        return pages
    scalar = coerce_page(value)
    if scalar:
        return [scalar]
    if isinstance(value, list):
        for child in value:
            pages += pages_from_edition_pages(child)
    elif isinstance(value, dict):
        # Inside editionPages, number/sequence can legitimately mean the edition page.
        priority_keys = (
            "page", "pageNumber", "page_number", "editionPage", "edition_page",
            "publicationPage", "publication_page", "number", "pageIndex", "page_index"
        )
        for key in priority_keys:
            if key in value:
                page = coerce_page(value.get(key))
                if page:
                    pages.append(page)
        if not pages:
            for key, child in value.items():
                if "page" in norm(str(key)):
                    pages += pages_from_edition_pages(child)
    return sorted(set(pages))


def detail_pages(payload: Dict[str, Any]) -> List[int]:
    pages = pages_from_edition_pages(payload.get("editionPages"))
    if pages:
        return pages
    for key in ("page", "pageNumber", "publicationPage", "startPage", "initialPage"):
        page = coerce_page(payload.get(key))
        if page:
            return [page]
    return []


def excerpt_anchors(item: Dict[str, Any], window: int = 8) -> List[str]:
    words = norm(str(item.get("excerpt") or "")).split()
    if len(words) < window:
        return [" ".join(words)] if words else []
    last = len(words) - window
    starts = unique([str(x) for x in (0, last // 3, last // 2, (2 * last) // 3, last)])
    return unique([" ".join(words[int(start):int(start) + window]) for start in starts])


async def locate_page_in_pdf(item: Dict[str, Any], edition_url: str) -> Dict[str, Any]:
    response = await http_get(edition_url, accept="application/pdf")
    if response.status_code >= 400:
        return {"page": None, "reason": "edition_pdf_request_failed", "status": response.status_code}
    if len(response.content) > MAX_PDF_BYTES:
        return {"page": None, "reason": "edition_pdf_too_large", "bytes": len(response.content)}

    title = norm(str(item.get("title") or ""))
    anchors = excerpt_anchors(item)
    reader = PdfReader(BytesIO(response.content))
    best_page: Optional[int] = None
    best_score = 0
    for index, page in enumerate(reader.pages):
        page_text = norm(page.extract_text() or "")
        excerpt_hits = sum(1 for anchor in anchors if anchor and anchor in page_text)
        title_hit = bool(title and title in page_text)
        score = excerpt_hits * 10 + (1 if title_hit else 0)
        if score > best_score:
            best_page = index + 1
            best_score = score

    # Prefer excerpt evidence. Fall back to an exact title only when no excerpt
    # anchor survives PDF text extraction.
    if best_page and best_score > 0:
        return {
            "page": best_page,
            "score": best_score,
            "excerptAnchorHits": best_score // 10,
            "titleMatched": bool(best_score % 10),
        }
    return {"page": None, "reason": "publication_text_not_found_in_edition"}


async def get_edition_reference(journal_id: str, root_id: str, day: date) -> Dict[str, Any]:
    params = {"JournalId": journal_id, "RootSectionId": root_id, "EditionDate": day.isoformat()}
    response = await http_get(EDITION_URL, params=params)
    result: Dict[str, Any] = {"requestStatus": response.status_code}
    if response.status_code < 400:
        eid, url = edition_reference(response)
        result["edition_id"] = eid
        result["editionUrl"] = url
    return result


async def resolve_ids_from_hierarchy(item: Dict[str, Any], day: date) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    parts = [x.strip() for x in str(item.get("hierarchy") or "").split(">") if x.strip()]
    journal_name = parts[0] if parts else None
    section_name = parts[1] if len(parts) > 1 else None
    journals_response = await http_get(JOURNALS_URL)
    journals = extract_items(journals_response.json()) if journals_response.status_code < 400 else []
    journal = next((j for j in journals if norm(str(j.get("name") or "")) == norm(journal_name or "")), None)
    if not journal and str(item.get("slug") or "").startswith("executivo/"):
        journal = next((j for j in journals if norm(str(j.get("name") or "")) == "executivo"), None)
    if not journal:
        return None, None, journal_name, section_name
    journal_id = str(journal.get("id") or "") or None
    if not journal_id:
        return None, None, journal_name, section_name
    sections_response = await http_get(SECTIONS_URL, params={"JournalId": journal_id})
    sections = extract_items(sections_response.json()) if sections_response.status_code < 400 else []
    section = next((s for s in sections if norm(str(s.get("name") or "")) == norm(section_name or "")), None)
    root_id = str(section.get("id") or "") if section else None
    return journal_id, root_id or None, journal.get("name") or journal_name, (section.get("name") if section else section_name)


async def locate(item: Dict[str, Any]) -> Dict[str, Any]:
    try:
        day = date.fromisoformat(str(item.get("date") or "")[:10])
    except Exception:
        return {"locatorStatus": "edition_not_resolved", "reason": "publication_date_missing", "pdfReadByBridge": False}

    detail_result = await publication_detail(item)
    detail = detail_result.get("payload") or {}
    pages = detail_pages(detail) if isinstance(detail, dict) else []

    # Exact official publication detail is authoritative for journal/section linkage.
    journal_id = str(detail.get("journalId") or "") or None
    root_id = str(detail.get("firstLevelSectionId") or detail.get("sectionId") or "") or None
    journal_name = detail.get("journal")
    section_name = detail.get("firstLevelSectionName") or detail.get("section")

    if not journal_id or not root_id:
        journal_id, root_id, fallback_journal, fallback_section = await resolve_ids_from_hierarchy(item, day)
        journal_name = journal_name or fallback_journal
        section_name = section_name or fallback_section

    if not journal_id or not root_id:
        return {
            "locatorStatus": "edition_not_resolved",
            "reason": "journal_or_section_not_resolved",
            "publicationDetailProbes": detail_result.get("probes"),
            "pdfReadByBridge": False,
        }

    edition = await get_edition_reference(journal_id, root_id, day)
    if not edition.get("editionUrl"):
        return {
            "locatorStatus": "edition_not_resolved",
            "journal_id": journal_id,
            "root_section_id": root_id,
            "editionRequest": edition,
            "pdfReadByBridge": False,
        }

    pdf_locator: Optional[Dict[str, Any]] = None
    if not pages:
        pdf_locator = await locate_page_in_pdf(item, str(edition.get("editionUrl")))
        if pdf_locator.get("page"):
            pages = [int(pdf_locator["page"])]

    if not pages:
        return {
            "locatorStatus": "edition_resolved_page_not_resolved",
            "edition_id": edition.get("edition_id"),
            "editionUrl": edition.get("editionUrl"),
            "journal_id": journal_id,
            "journal": journal_name,
            "root_section_id": root_id,
            "section": section_name,
            "edition_date": day.isoformat(),
            "publicationDetailKeys": list(detail.keys())[:30] if isinstance(detail, dict) else [],
            "editionPagesRaw": detail.get("editionPages") if isinstance(detail, dict) else None,
            "pdfReadByBridge": True,
            "pdfLocator": pdf_locator,
        }

    start = min(pages)
    end = max(pages)
    recommended = list(range(max(1, start - 1), end + 2))
    return {
        "locatorStatus": "resolved",
        "edition_id": edition.get("edition_id"),
        "editionUrl": edition.get("editionUrl"),
        "journal_id": journal_id,
        "journal": journal_name,
        "root_section_id": root_id,
        "section": section_name,
        "edition_date": day.isoformat(),
        "publication_pages": pages,
        "match_page": pages[0] if len(pages) == 1 else None,
        "publication_page_start": start,
        "publication_page_end": end,
        "recommended_read_pages": recommended,
        "pageMetadataSource": "official v2/publications detail.editionPages" if detail_pages(detail) else "official edition PDF text match",
        "locatorEvidence": "official publication detail + editions/url" if detail_pages(detail) else "official edition PDF + publication title/excerpt anchors",
        "pdfLocator": pdf_locator,
        "pdfReadByBridge": bool(pdf_locator),
    }


def organization(item: Dict[str, Any]) -> str:
    text = norm(item_text(item))
    if "ministerio publico" in text:
        return "MPSP"
    if "controladoria geral do estado" in text:
        return "CGE-SP"
    return "DOE-SP"


def category(item: Dict[str, Any]) -> str:
    text = norm(item_text(item))
    if "licenca" in text or "afastamento" in text:
        return "licenca_afastamento"
    if "concurso" in text or "edital" in text:
        return "concurso_processo_seletivo"
    return "vida_funcional"


async def enrich(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in matches:
        row = dict(item)
        row["organization"] = organization(row)
        row["category"] = category(row)
        row["relevance"] = "functional"
        row["officialUrl"] = f"{DOE_WEB}/{str(row.get('slug')).lstrip('/')}" if row.get("slug") else None
        try:
            row["documentLocator"] = await locate(row)
        except Exception as exc:
            row["documentLocator"] = {"locatorStatus": "locator_error", "errorType": exc.__class__.__name__, "pdfReadByBridge": False}
        out.append(row)
    return out


def summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_category: Dict[str, int] = {}
    by_org: Dict[str, int] = {}
    for row in matches:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        by_org[row["organization"]] = by_org.get(row["organization"], 0) + 1
    return {"verified_count": len(matches), "probable_count": 0, "by_category": by_category, "by_organization": by_org}


@app.get("/api/health")
async def health():
    response = await http_get(JOURNALS_URL)
    return {
        "bridge": "ok",
        "version": VERSION,
        "doesp_api_reachable": response.status_code < 500,
        "upstream_status": response.status_code,
        "pdf_scanning": True,
        "role": "document locator with official PDF fallback",
    }


@app.get("/api/search")
async def search(term: str = Query(...), from_date: date = Query(...), to_date: date = Query(...)):
    if to_date < from_date:
        raise HTTPException(400, "to_date deve ser maior ou igual a from_date")
    result = await raw_search(term, from_date, to_date)
    return {
        "source": "DOE-SP API only",
        "term": term,
        "from_date": from_date,
        "to_date": to_date,
        "pages_fetched": result["pages"],
        "truncated": result["truncated"],
        "count": len(result["items"]),
        "items": result["items"],
        # Stable aliases shared with /api/me to prevent consumer ambiguity.
        "match_count": len(result["items"]),
        "matches": result["items"],
    }


@app.get("/api/contest/auditor-cge")
async def auditor_cge_contest(from_date: date = Query(...), to_date: date = Query(...)):
    if to_date < from_date:
        raise HTTPException(400, "to_date deve ser maior ou igual a from_date")

    merged: Dict[str, Dict[str, Any]] = {}
    pages = 0
    truncated = False
    for term in AUDITOR_CGE_TERMS:
        result = await raw_search(term, from_date, to_date)
        pages += result["pages"]
        truncated = truncated or result["truncated"]
        for item in result["items"]:
            merged[item_key(item)] = item

    candidates: List[Dict[str, Any]] = []
    discarded = 0
    for item in merged.values():
        reasons = auditor_cge_contest_reasons(item)
        if not reasons:
            discarded += 1
            continue
        row = dict(item)
        row["contestMatchReasons"] = reasons
        candidates.append(row)

    candidates.sort(key=lambda x: str(x.get("date") or ""))
    matches = await enrich(candidates)
    for row in matches:
        row["organization"] = "CGE-SP"
        row["category"] = "concurso_processo_seletivo"
        row["relevance"] = "functional"

    return {
        "source": "DOE-SP API only",
        "contest": "Concurso Público para Provimento de Vagas para o Cargo de Auditor Estadual de Controle",
        "reference_notice": "Edital CGE nº 03/2025",
        "terms": list(AUDITOR_CGE_TERMS),
        "from_date": from_date,
        "to_date": to_date,
        "pages_fetched": pages,
        "truncated": truncated,
        "candidates_discarded": discarded,
        "match_count": len(matches),
        "matches": matches,
    }


@app.get("/api/me")
async def me(from_date: date = Query(...), to_date: date = Query(...)):
    if not PROFILE_NAME:
        raise HTTPException(503, "Perfil não configurado")
    if to_date < from_date:
        raise HTTPException(400, "to_date deve ser maior ou igual a from_date")
    result = await profile_search(from_date, to_date)
    matches = await enrich(result["matches"])
    return {
        "source": "DOE-SP API only",
        "from_date": from_date,
        "to_date": to_date,
        "search_variants_count": len(profile_terms()),
        "pages_fetched": result["pages"],
        "truncated": result["truncated"],
        "weak_candidates_discarded": result["weak"],
        "match_count": len(matches),
        "summary": summary(matches),
        "matches": matches,
    }


@app.get("/api/me/today")
async def me_today():
    today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    result = await me(today, today)
    result["date"] = today
    result["searched"] = True
    return result


@app.get("/api/me/log")
async def me_log(from_date: date = Query(...), to_date: date = Query(...)):
    result = await me(from_date, to_date)
    entries = [
        {
            "date": str(x.get("date") or "")[:10],
            "organization": x.get("organization"),
            "category": x.get("category"),
            "relevance": x.get("relevance"),
            "matchConfidence": x.get("matchConfidence"),
            "title": x.get("title"),
            "id": x.get("id"),
            "officialUrl": x.get("officialUrl"),
            "documentLocator": x.get("documentLocator"),
        }
        for x in result["matches"]
    ]
    return {k: v for k, v in result.items() if k != "matches"} | {"entries": entries}


@app.get("/api/context")
async def context(slug: str = Query(...)):
    url = f"{DOE_WEB}/{slug.lstrip('/')}"
    response = await http_get(url, accept="text/html,application/xhtml+xml")
    return {"source": "DOE-SP official publication page", "officialUrl": url, "status": response.status_code, "contextComplete": False}
