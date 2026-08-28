import os
import re
import json
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query

VERSION = "1.6.1"
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


def hierarchy_parts(item: Dict[str, Any]) -> List[str]:
    return [x.strip() for x in str(item.get("hierarchy") or "").split(">") if x.strip()]


async def journals(day: date) -> List[Dict[str, Any]]:
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
    values = await journals(day)
    for journal in values:
        if norm(str(journal.get("name") or journal.get("title") or "")) == wanted:
            return journal
    if str(item.get("slug") or "").startswith("executivo/"):
        for journal in values:
            if norm(str(journal.get("name") or "")) == "executivo":
                return journal
    return None


async def root_sections(journal_id: str) -> List[Dict[str, Any]]:
    response = await http_get(SECTIONS_URL, params={"JournalId": journal_id})
    return extract_items(response.json()) if response.status_code < 400 else []


def ordered_roots(item: Dict[str, Any], roots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parts = hierarchy_parts(item)
    wanted = norm(parts[1]) if len(parts) > 1 else ""
    return sorted(roots, key=lambda r: 0 if norm(str(r.get("name") or r.get("title") or "")) == wanted else 1)


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


async def get_edition_reference(journal_id: str, root_id: str, day: date) -> Dict[str, Any]:
    params = {"JournalId": journal_id, "RootSectionId": root_id, "EditionDate": day.isoformat()}
    response = await http_get(EDITION_URL, params=params)
    result: Dict[str, Any] = {"requestStatus": response.status_code}
    if response.status_code < 400:
        eid, url = edition_reference(response)
        result["edition_id"] = eid
        result["editionUrl"] = url
    return result


PAGE_KEYS = (
    "page", "pageNumber", "page_number", "publicationPage", "publication_page",
    "startPage", "start_page", "initialPage", "initial_page", "firstPage", "first_page"
)


def coerce_page(value: Any) -> Optional[int]:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\b(\d{1,4})\b", value.strip())
        if match:
            page = int(match.group(1))
            return page if page > 0 else None
    return None


def direct_page(node: Dict[str, Any]) -> Optional[int]:
    for key in PAGE_KEYS:
        if key in node:
            page = coerce_page(node.get(key))
            if page:
                return page
    return None


def first_page_in_payload(payload: Any, path: str = "$") -> Tuple[Optional[int], Optional[str]]:
    if isinstance(payload, dict):
        page = direct_page(payload)
        if page:
            return page, path
        # Prefer common wrapper keys before generic traversal.
        for key in ("data", "item", "publication", "result"):
            if key in payload:
                page, found_path = first_page_in_payload(payload[key], f"{path}.{key}")
                if page:
                    return page, found_path
        for key, value in payload.items():
            page, found_path = first_page_in_payload(value, f"{path}.{key}")
            if page:
                return page, found_path
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            page, found_path = first_page_in_payload(value, f"{path}[{index}]")
            if page:
                return page, found_path
    return None, None


async def page_from_publication_detail(item: Dict[str, Any]) -> Dict[str, Any]:
    slug = str(item.get("slug") or "").strip("/")
    iid = str(item.get("id") or "")
    probes: List[Dict[str, Any]] = []

    # v2/publications/{slug} is the exact official detail object used by the public site.
    urls: List[str] = []
    if slug:
        urls.append(f"{DOE_SEARCH}/v2/publications/{slug}")
    if iid:
        urls.append(f"{DOE_SEARCH}/v2/publications/{iid}")

    for url in unique(urls):
        response = await http_get(url)
        probe: Dict[str, Any] = {"url": url, "status": response.status_code}
        if response.status_code < 400:
            payload = response_payload(response)
            page, path = first_page_in_payload(payload)
            if isinstance(payload, dict):
                probe["topLevelKeys"] = list(payload.keys())[:30]
            if page:
                probe["page"] = page
                return {"page": page, "source": "publication_detail", "path": path, "probes": probes + [probe]}
        probes.append(probe)
    return {"page": None, "probes": probes}


async def locate(item: Dict[str, Any]) -> Dict[str, Any]:
    try:
        day = date.fromisoformat(str(item.get("date") or "")[:10])
    except Exception:
        return {"locatorStatus": "edition_not_resolved", "reason": "publication_date_missing", "pdfReadByBridge": False}

    journal = await resolve_journal(item, day)
    if not journal or not journal.get("id"):
        return {"locatorStatus": "edition_not_resolved", "reason": "journal_not_resolved", "pdfReadByBridge": False}
    journal_id = str(journal["id"])

    roots = await root_sections(journal_id)
    if not roots:
        return {"locatorStatus": "edition_not_resolved", "reason": "root_sections_not_resolved", "journal_id": journal_id, "pdfReadByBridge": False}

    detail = await page_from_publication_detail(item)
    page = detail.get("page") or direct_page(item)

    attempts: List[Dict[str, Any]] = []
    for root in ordered_roots(item, roots):
        root_id = str(root.get("id") or "")
        if not root_id:
            continue
        section_name = root.get("name") or root.get("title")
        edition = await get_edition_reference(journal_id, root_id, day)
        attempts.append({"root_section_id": root_id, "section": section_name, **edition})
        if not edition.get("editionUrl"):
            continue

        if page:
            return {
                "locatorStatus": "resolved",
                "edition_id": edition.get("edition_id"),
                "editionUrl": edition.get("editionUrl"),
                "journal_id": journal_id,
                "journal": journal.get("name") or journal.get("title"),
                "root_section_id": root_id,
                "section": section_name,
                "edition_date": day.isoformat(),
                "match_page": page,
                "publication_page_start": page,
                "publication_page_end": page,
                "recommended_read_pages": [p for p in (page - 1, page, page + 1) if p >= 1],
                "pageMetadataSource": detail.get("source") or "search_item",
                "pageMetadataPath": detail.get("path") or "$",
                "locatorEvidence": "official publication detail + journals + sections + editions/url; PDF not read by bridge",
                "pdfReadByBridge": False,
            }

    return {
        "locatorStatus": "edition_resolved_page_not_resolved" if any(a.get("editionUrl") for a in attempts) else "edition_not_resolved",
        "journal_id": journal_id,
        "journal": journal.get("name") or journal.get("title"),
        "publicationDetailProbes": detail.get("probes"),
        "rootSectionAttempts": attempts[:10],
        "pdfReadByBridge": False,
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
        "pdf_scanning": False,
        "role": "document locator only",
    }


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
