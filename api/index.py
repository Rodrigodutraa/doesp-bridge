import os
import re
import json
import unicodedata
from io import BytesIO
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, HTTPException, Query
from pypdf import PdfReader

VERSION = "1.5.0"
DOE_BASE = "https://do-api-web-search.doe.sp.gov.br"
SEARCH_URL = f"{DOE_BASE}/v2/advanced-search/publications"
JOURNALS_URL = f"{DOE_BASE}/v2/journals"
SECTIONS_URL = f"{DOE_BASE}/v2/sections"
PDF_BASE = "https://do-api-publication-pdf.doe.sp.gov.br"
EDITION_URL_ENDPOINT = f"{PDF_BASE}/v1/editions/url"
WEB_BASE = "https://doe.sp.gov.br"
TIMEOUT = float(os.getenv("DOESP_TIMEOUT_SECONDS", "30"))
PAGE_SIZE = min(max(int(os.getenv("DOESP_PAGE_SIZE", "100")), 1), 100)
MAX_PAGES = min(max(int(os.getenv("DOESP_MAX_PAGES", "50")), 1), 200)

app = FastAPI(title="DOE-SP Bridge", version=VERSION)


def env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in re.split(r"[|,;\r\n]+", raw) if x.strip()]


PROFILE_NAME = os.getenv("DOESP_PROFILE_NAME", "").strip()
PROFILE_MATRICULAS = env_list("DOESP_PROFILE_MATRICULAS")
PROFILE_RGS = env_list("DOESP_PROFILE_RGS")
PROFILE_CPFS = env_list("DOESP_PROFILE_CPFS")
PROFILE_OTHER_IDS = env_list("DOESP_PROFILE_OTHER_IDS")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s).casefold()).strip()


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def unique(values: List[str]) -> List[str]:
    out, seen = [], set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def profile_terms() -> List[str]:
    terms: List[str] = []
    if PROFILE_NAME:
        terms += [PROFILE_NAME, strip_accents(PROFILE_NAME)]
    for value in PROFILE_MATRICULAS + PROFILE_RGS + PROFILE_CPFS + PROFILE_OTHER_IDS:
        terms += [value, digits(value), digits(value).lstrip("0")]
    return unique([x for x in terms if x])


def extract_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
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
    for key in ("id", "publicationId", "uuid", "slug", "url"):
        if item.get(key):
            return str(item[key])
    return json.dumps(item, sort_keys=True, ensure_ascii=False)[:500]


def item_text(item: Dict[str, Any]) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "excerpt", "hierarchy", "content", "description"))


def identity_verified(item: Dict[str, Any]) -> bool:
    text = norm(item_text(item))
    name_ok = bool(PROFILE_NAME and norm(PROFILE_NAME) in text)
    ids = PROFILE_MATRICULAS + PROFILE_RGS + PROFILE_CPFS + PROFILE_OTHER_IDS
    text_digits = digits(text)
    id_ok = any(
        digits(value) and (digits(value) in text_digits or digits(value).lstrip("0") in text_digits)
        for value in ids
    )
    return name_ok and (id_ok or not ids)


async def http_get(url: str, params: Optional[dict] = None, accept: str = "application/json,*/*") -> httpx.Response:
    headers = {"Accept": accept, "User-Agent": f"DOE-SP-Bridge/{VERSION}"}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        return await client.get(url, params=params, headers=headers)


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

    matches, weak = [], 0
    for item in merged.values():
        if identity_verified(item):
            enriched = dict(item)
            enriched["matchConfidence"] = "verified"
            enriched["matchedBy"] = ["name", "identifier"]
            matches.append(enriched)
        else:
            weak += 1
    matches.sort(key=lambda x: str(x.get("date") or ""))
    return {"matches": matches, "pages": pages, "truncated": truncated, "weak": weak}


def hierarchy_parts(item: Dict[str, Any]) -> List[str]:
    return [part.strip() for part in str(item.get("hierarchy") or "").split(">") if part.strip()]


async def official_journals(day: date) -> List[Dict[str, Any]]:
    # The no-date form is the canonical frontend call. The date form is kept as fallback
    # because the official site may expose a date-specific set of journals.
    for params in (None, {"date": day.isoformat()}):
        response = await http_get(JOURNALS_URL, params=params)
        if response.status_code < 400:
            items = extract_items(response.json())
            if items:
                return items
    return []


async def resolve_journal(item: Dict[str, Any], day: date) -> Optional[Dict[str, Any]]:
    parts = hierarchy_parts(item)
    wanted = norm(parts[0]) if parts else ""
    journals = await official_journals(day)
    if wanted:
        for journal in journals:
            if norm(str(journal.get("name") or journal.get("title") or "")) == wanted:
                return journal
    # Current profile publications are in Executivo; use a semantic fallback only when
    # the hierarchy itself says Executivo or the publication slug starts with executivo/.
    slug = str(item.get("slug") or "")
    if wanted == "executivo" or slug.startswith("executivo/"):
        for journal in journals:
            if norm(str(journal.get("name") or "")) == "executivo":
                return journal
    return None


async def root_sections(journal_id: str) -> List[Dict[str, Any]]:
    response = await http_get(SECTIONS_URL, params={"JournalId": journal_id})
    if response.status_code >= 400:
        return []
    return extract_items(response.json())


def ordered_root_candidates(item: Dict[str, Any], roots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parts = hierarchy_parts(item)
    wanted = norm(parts[1]) if len(parts) > 1 else ""
    preferred, remaining = [], []
    for root in roots:
        root_name = norm(str(root.get("name") or root.get("title") or root.get("description") or ""))
        if wanted and root_name == wanted:
            preferred.append(root)
        else:
            remaining.append(root)
    # We intentionally try the other official root sections too. The search API hierarchy
    # has shown inconsistent labels in some publications; PDF content validation is the final guard.
    return preferred + remaining


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")


def extract_edition_reference(response: httpx.Response) -> Tuple[Optional[str], Optional[str], Any]:
    content_type = (response.headers.get("content-type") or "").lower()
    payload: Any = None
    try:
        payload = response.json()
    except Exception:
        payload = response.text

    candidates: List[str] = []
    if isinstance(payload, str):
        candidates.append(payload)
    elif isinstance(payload, dict):
        for key in ("url", "editionUrl", "pdfUrl", "id", "editionId", "uuid", "data", "value"):
            value = payload.get(key)
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, dict):
                for nested_key in ("url", "id", "editionId", "uuid"):
                    nested = value.get(nested_key)
                    if isinstance(nested, str):
                        candidates.append(nested)
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, dict):
                for key in ("url", "id", "editionId", "uuid"):
                    if isinstance(value.get(key), str):
                        candidates.append(value[key])

    for candidate in candidates:
        match = UUID_RE.search(candidate)
        if match:
            edition_id = match.group(0)
            if candidate.startswith("http"):
                return edition_id, candidate, payload
            return edition_id, f"{PDF_BASE}/v1/editions/{edition_id}", payload

    # Some deployments can directly return the PDF bytes from /url. Preserve that path.
    if response.content.startswith(b"%PDF") or "application/pdf" in content_type:
        return None, str(response.url), payload
    return None, None, payload


def page_matches_profile(text: str) -> bool:
    normalized = norm(text)
    if PROFILE_NAME and norm(PROFILE_NAME) not in normalized:
        return False
    ids = [digits(x) for x in PROFILE_MATRICULAS + PROFILE_RGS + PROFILE_CPFS + PROFILE_OTHER_IDS if digits(x)]
    if not ids:
        return bool(PROFILE_NAME and norm(PROFILE_NAME) in normalized)
    page_digits = digits(normalized)
    return any(value in page_digits or value.lstrip("0") in page_digits for value in ids)


def locate_profile_page(reader: PdfReader) -> Optional[int]:
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if page_matches_profile(text):
            return index
    return None


async def fetch_edition_pdf(journal_id: str, root_id: str, day: date) -> Dict[str, Any]:
    params = {"JournalId": journal_id, "RootSectionId": root_id, "EditionDate": day.isoformat()}
    response = await http_get(EDITION_URL_ENDPOINT, params=params, accept="application/json,application/pdf,*/*")
    result: Dict[str, Any] = {
        "requestStatus": response.status_code,
        "journal_id": journal_id,
        "root_section_id": root_id,
    }
    if response.status_code >= 400:
        return result

    edition_id, edition_url, payload = extract_edition_reference(response)
    result["edition_id"] = edition_id
    result["editionUrl"] = edition_url
    if isinstance(payload, (dict, list, str)):
        # Keep a bounded diagnostic summary, never the PDF/base64 body.
        serialized = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        result["editionUrlResponse"] = serialized[:800]

    pdf_bytes: Optional[bytes] = None
    if response.content.startswith(b"%PDF"):
        pdf_bytes = response.content
    elif edition_url:
        pdf_response = await http_get(edition_url, accept="application/pdf,application/octet-stream,*/*")
        result["pdfStatus"] = pdf_response.status_code
        result["pdfContentType"] = pdf_response.headers.get("content-type")
        if pdf_response.status_code < 400 and (pdf_response.content.startswith(b"%PDF") or "pdf" in (pdf_response.headers.get("content-type") or "").lower()):
            pdf_bytes = pdf_response.content
    if pdf_bytes:
        result["pdfBytes"] = pdf_bytes
    return result


async def locate(item: Dict[str, Any]) -> Dict[str, Any]:
    raw_date = str(item.get("date") or "")[:10]
    try:
        day = date.fromisoformat(raw_date)
    except Exception:
        return {"locatorStatus": "edition_not_resolved", "reason": "publication_date_missing"}

    journal = await resolve_journal(item, day)
    if not journal or not journal.get("id"):
        return {"locatorStatus": "edition_not_resolved", "reason": "journal_not_resolved"}
    journal_id = str(journal["id"])
    roots = await root_sections(journal_id)
    if not roots:
        return {"locatorStatus": "edition_not_resolved", "reason": "root_sections_not_resolved", "journal_id": journal_id}

    attempts: List[Dict[str, Any]] = []
    for root in ordered_root_candidates(item, roots):
        root_id = str(root.get("id") or "")
        if not root_id:
            continue
        edition = await fetch_edition_pdf(journal_id, root_id, day)
        attempt = {k: v for k, v in edition.items() if k != "pdfBytes"}
        attempt["root_section_name"] = root.get("name") or root.get("title")
        attempts.append(attempt)
        pdf_bytes = edition.get("pdfBytes")
        if not pdf_bytes:
            continue
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            match_page = locate_profile_page(reader)
        except Exception as exc:
            attempts[-1]["pdfParseError"] = exc.__class__.__name__
            continue
        if not match_page:
            continue

        total_pages = len(reader.pages)
        read_start = max(1, match_page - 1)
        read_end = min(total_pages, match_page + 1)
        return {
            "locatorStatus": "resolved",
            "edition_id": edition.get("edition_id"),
            "editionUrl": edition.get("editionUrl"),
            "journal_id": journal_id,
            "journal": journal.get("name") or journal.get("title"),
            "root_section_id": root_id,
            "section": root.get("name") or root.get("title"),
            "edition_date": day.isoformat(),
            "match_page": match_page,
            "publication_page_start": read_start,
            "publication_page_end": read_end,
            "recommended_read_pages": list(range(read_start, read_end + 1)),
            "total_pages": total_pages,
            "locatorEvidence": "official_v2_journals + official_v2_sections + official_v1_editions_url + PDF identity match",
        }

    return {
        "locatorStatus": "edition_found_match_not_located" if any(a.get("editionUrl") or a.get("pdfStatus") == 200 for a in attempts) else "edition_not_resolved",
        "journal_id": journal_id,
        "journal": journal.get("name") or journal.get("title"),
        "rootSectionAttempts": attempts[:20],
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


def official_url(item: Dict[str, Any]) -> Optional[str]:
    slug = item.get("slug")
    return f"{WEB_BASE}/{str(slug).lstrip('/')}" if slug else None


async def enrich(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for item in matches:
        enriched = dict(item)
        enriched["organization"] = organization(enriched)
        enriched["category"] = category(enriched)
        enriched["relevance"] = "functional"
        enriched["officialUrl"] = official_url(enriched)
        try:
            enriched["documentLocator"] = await locate(enriched)
        except Exception as exc:
            enriched["documentLocator"] = {"locatorStatus": "locator_error", "errorType": exc.__class__.__name__}
        out.append(enriched)
    return out


def summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_category: Dict[str, int] = {}
    by_org: Dict[str, int] = {}
    for item in matches:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
        by_org[item["organization"]] = by_org.get(item["organization"], 0) + 1
    return {"verified_count": len(matches), "probable_count": 0, "by_category": by_category, "by_organization": by_org}


@app.get("/api/health")
async def health():
    response = await http_get(JOURNALS_URL)
    return {"bridge": "ok", "version": VERSION, "doesp_api_reachable": response.status_code < 500, "upstream_status": response.status_code}


@app.get("/api/search")
async def search(term: str = Query(...), from_date: date = Query(...), to_date: date = Query(...)):
    result = await raw_search(term, from_date, to_date)
    return {"source": "DOE-SP API only", "term": term, "from_date": from_date, "to_date": to_date, "pages_fetched": result["pages"], "truncated": result["truncated"], "count": len(result["items"]), "items": result["items"]}


@app.get("/api/me")
async def me(from_date: date = Query(...), to_date: date = Query(...)):
    if not PROFILE_NAME:
        raise HTTPException(503, "Perfil não configurado")
    if to_date < from_date:
        raise HTTPException(400, "to_date deve ser maior ou igual a from_date")
    result = await profile_search(from_date, to_date)
    matches = await enrich(result["matches"])
    return {"source": "DOE-SP API only", "from_date": from_date, "to_date": to_date, "search_variants_count": len(profile_terms()), "pages_fetched": result["pages"], "truncated": result["truncated"], "weak_candidates_discarded": result["weak"], "match_count": len(matches), "summary": summary(matches), "matches": matches}


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
    entries = []
    for item in result["matches"]:
        entries.append({
            "date": str(item.get("date") or "")[:10],
            "organization": item.get("organization"),
            "category": item.get("category"),
            "relevance": item.get("relevance"),
            "matchConfidence": item.get("matchConfidence"),
            "title": item.get("title"),
            "id": item.get("id"),
            "officialUrl": item.get("officialUrl"),
            "documentLocator": item.get("documentLocator"),
        })
    return {k: v for k, v in result.items() if k != "matches"} | {"entries": entries}


@app.get("/api/context")
async def context(slug: str = Query(...)):
    url = f"{WEB_BASE}/{slug.lstrip('/')}"
    response = await http_get(url, accept="text/html,application/xhtml+xml")
    return {"source": "DOE-SP official publication page", "officialUrl": url, "status": response.status_code, "contextComplete": False}
