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

VERSION = "1.6.0"
DOE_SEARCH = "https://do-api-web-search.doe.sp.gov.br"
DOE_PDF = "https://do-api-publication-pdf.doe.sp.gov.br"
DOE_WEB = "https://doe.sp.gov.br"
SEARCH_URL = f"{DOE_SEARCH}/v2/advanced-search/publications"
JOURNALS_URL = f"{DOE_SEARCH}/v2/journals"
SECTIONS_URL = f"{DOE_SEARCH}/v2/sections"
SUMMARY_STRUCTURED_URL = f"{DOE_SEARCH}/v3/summary/structured"
SUMMARY_LIST_URL = f"{DOE_SEARCH}/v2/summary/list"
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
    # DOE frequently publishes an internal number without the check digit.
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
    for value in all_identifiers():
        d = digits(value)
        if d and d in td:
            return True
    return False


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
    # The unfiltered endpoint is canonical; date is kept as fallback for old backend behavior.
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


def edition_reference(response: httpx.Response) -> Tuple[Optional[str], Optional[str], Any]:
    payload = response_payload(response)
    for candidate in candidate_strings(payload):
        if not candidate:
            continue
        match = UUID_RE.search(candidate)
        eid = match.group(0) if match else None
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return eid, candidate, payload
        if candidate.startswith("/") or candidate.startswith("v1/"):
            return eid, urljoin(DOE_PDF + "/", candidate), payload
        if eid:
            return eid, f"{DOE_PDF}/v1/editions/{eid}", payload
    return None, None, payload


async def get_edition_reference(journal_id: str, root_id: str, day: date) -> Dict[str, Any]:
    params = {"JournalId": journal_id, "RootSectionId": root_id, "EditionDate": day.isoformat()}
    response = await http_get(EDITION_URL, params=params)
    result: Dict[str, Any] = {"requestStatus": response.status_code}
    if response.status_code >= 400:
        return result
    eid, url, payload = edition_reference(response)
    result["edition_id"] = eid
    result["editionUrl"] = url
    if payload is not None:
        try:
            serialized = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            result["editionUrlResponse"] = serialized[:400]
        except Exception:
            pass
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
            p = int(match.group(1))
            return p if p > 0 else None
    return None


def direct_page(node: Dict[str, Any]) -> Optional[int]:
    for key in PAGE_KEYS:
        if key in node:
            page = coerce_page(node.get(key))
            if page:
                return page
    return None


def publication_score(node: Dict[str, Any], item: Dict[str, Any]) -> int:
    score = 0
    iid = str(item.get("id") or "")
    title = norm(str(item.get("title") or ""))
    slug = str(item.get("slug") or "").lower()

    for key in ("id", "publicationId", "publication_id", "documentId", "document_id"):
        if iid and str(node.get(key) or "") == iid:
            score += 100
    node_title = norm(str(node.get("title") or node.get("name") or node.get("publicationTitle") or ""))
    if title and node_title:
        if node_title == title:
            score += 80
        elif title in node_title or node_title in title:
            score += 45
    node_slug = str(node.get("slug") or node.get("url") or node.get("link") or "").lower()
    if slug and node_slug and (slug in node_slug or node_slug.endswith(slug)):
        score += 100
    return score


def find_page_in_payload(payload: Any, item: Dict[str, Any]) -> Tuple[Optional[int], Optional[str], int]:
    best_page: Optional[int] = None
    best_path: Optional[str] = None
    best_score = -1

    def walk(value: Any, path: str) -> None:
        nonlocal best_page, best_path, best_score
        if isinstance(value, dict):
            page = direct_page(value)
            score = publication_score(value, item)
            if page and score > best_score:
                best_page, best_path, best_score = page, path, score
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload, "$")
    # Require positive publication evidence; never select an arbitrary page field.
    if best_score <= 0:
        return None, None, best_score
    return best_page, best_path, best_score


async def page_from_summary(item: Dict[str, Any], journal_id: str, root_id: str, day: date) -> Dict[str, Any]:
    params_variants = [
        {"JournalId": journal_id, "RootSectionId": root_id, "EditionDate": day.isoformat()},
        {"JournalId": journal_id, "RootSectionId": root_id, "date": day.isoformat()},
        {"JournalId": journal_id, "SectionId": root_id, "EditionDate": day.isoformat()},
    ]
    probes: List[Dict[str, Any]] = []
    for endpoint_name, url in (("summary_structured", SUMMARY_STRUCTURED_URL), ("summary_list", SUMMARY_LIST_URL)):
        for params in params_variants:
            response = await http_get(url, params=params)
            probe: Dict[str, Any] = {"endpoint": endpoint_name, "status": response.status_code, "params": params}
            if response.status_code < 400:
                payload = response_payload(response)
                page, path, score = find_page_in_payload(payload, item)
                probe["matchScore"] = score
                if page:
                    return {"page": page, "source": endpoint_name, "path": path, "score": score, "probes": probes + [probe]}
            probes.append(probe)
    return {"page": None, "probes": probes}


async def page_from_publication_detail(item: Dict[str, Any]) -> Dict[str, Any]:
    # The site route is /{journal}/{section}/{titleId}; probe official API equivalents only.
    slug = str(item.get("slug") or "").strip("/")
    iid = str(item.get("id") or "")
    urls: List[str] = []
    if slug:
        urls += [
            f"{DOE_SEARCH}/v2/publications/{slug}",
            f"{DOE_SEARCH}/v1/publications/{slug}",
        ]
    if iid:
        urls += [
            f"{DOE_SEARCH}/v2/publications/{iid}",
            f"{DOE_SEARCH}/v1/publications/{iid}",
        ]

    probes: List[Dict[str, Any]] = []
    for url in unique(urls):
        response = await http_get(url)
        probe: Dict[str, Any] = {"url": url, "status": response.status_code}
        if response.status_code < 400:
            payload = response_payload(response)
            page, path, score = find_page_in_payload(payload, item)
            probe["matchScore"] = score
            if page:
                return {"page": page, "source": "publication_detail", "path": path, "score": score, "probes": probes + [probe]}
        probes.append(probe)
    return {"page": None, "probes": probes}


def item_page(item: Dict[str, Any]) -> Optional[int]:
    return direct_page(item)


async def locate(item: Dict[str, Any]) -> Dict[str, Any]:
    try:
        day = date.fromisoformat(str(item.get("date") or "")[:10])
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
    for root in ordered_roots(item, roots):
        root_id = str(root.get("id") or "")
        if not root_id:
            continue
        section_name = root.get("name") or root.get("title")
        edition = await get_edition_reference(journal_id, root_id, day)
        attempts.append({"root_section_id": root_id, "section": section_name, **edition})
        if not edition.get("editionUrl"):
            continue

        page = item_page(item)
        page_source = "search_item"
        page_path = "$"

        if not page:
            summary_result = await page_from_summary(item, journal_id, root_id, day)
            page = summary_result.get("page")
            page_source = summary_result.get("source") or page_source
            page_path = summary_result.get("path") or page_path
            attempts[-1]["summaryProbes"] = summary_result.get("probes")

        if not page:
            detail_result = await page_from_publication_detail(item)
            page = detail_result.get("page")
            page_source = detail_result.get("source") or page_source
            page_path = detail_result.get("path") or page_path
            attempts[-1]["publicationDetailProbes"] = detail_result.get("probes")

        if page:
            start = max(1, page - 1)
            end = page + 1
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
                "pageMetadataSource": page_source,
                "pageMetadataPath": page_path,
                "locatorEvidence": "official search + journals + sections + editions/url + publication/summary page metadata; PDF not read by bridge",
                "pdfReadByBridge": False,
            }

    has_edition = any(a.get("editionUrl") for a in attempts)
    return {
        "locatorStatus": "edition_resolved_page_not_resolved" if has_edition else "edition_not_resolved",
        "journal_id": journal_id,
        "journal": journal.get("name") or journal.get("title"),
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
