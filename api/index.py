import os
import re
import json
import unicodedata
from io import BytesIO
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from pypdf import PdfReader

VERSION = "1.4.2"
DOE_BASE = "https://do-api-web-search.doe.sp.gov.br"
SEARCH_URL = f"{DOE_BASE}/v2/advanced-search/publications"
JOURNALS_URL = f"{DOE_BASE}/v2/journals"
DOE_PDF_BASE = "https://do-api-publication-pdf.doe.sp.gov.br"
DOE_WEB_BASE = "https://doe.sp.gov.br"
TIMEOUT = float(os.getenv("DOESP_TIMEOUT_SECONDS", "20"))
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
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def profile_terms() -> List[str]:
    terms = []
    if PROFILE_NAME:
        terms += [PROFILE_NAME, strip_accents(PROFILE_NAME)]
    for value in PROFILE_MATRICULAS + PROFILE_RGS + PROFILE_CPFS + PROFILE_OTHER_IDS:
        terms += [value, digits(value), digits(value).lstrip("0")]
    return unique([x for x in terms if x])


def strong_anchors() -> List[str]:
    vals = [PROFILE_NAME]
    vals += PROFILE_MATRICULAS + PROFILE_RGS + PROFILE_CPFS + PROFILE_OTHER_IDS
    return unique([norm(v) for v in vals if v])


def extract_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "results", "hits", "data", "publications"):
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
    parts = []
    for key in ("title", "excerpt", "hierarchy", "content", "description"):
        if item.get(key):
            parts.append(str(item[key]))
    return " ".join(parts)


def identity_verified(item: Dict[str, Any]) -> bool:
    text = norm(item_text(item))
    name_ok = bool(PROFILE_NAME and norm(PROFILE_NAME) in text)
    id_ok = False
    for value in PROFILE_MATRICULAS + PROFILE_RGS + PROFILE_CPFS + PROFILE_OTHER_IDS:
        dv = digits(value)
        if dv and (dv in digits(text) or dv.lstrip("0") in digits(text)):
            id_ok = True
            break
    return name_ok and (id_ok or not (PROFILE_MATRICULAS or PROFILE_RGS or PROFILE_CPFS or PROFILE_OTHER_IDS))


async def get(url: str, params: Optional[dict] = None, accept: str = "application/json,*/*") -> httpx.Response:
    headers = {"Accept": accept, "User-Agent": f"DOE-SP-Bridge/{VERSION}"}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        return await client.get(url, params=params, headers=headers)


async def raw_search(term: str, from_date: date, to_date: date) -> Dict[str, Any]:
    items, seen = [], set()
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
        r = await get(SEARCH_URL, params=params)
        if r.status_code >= 400:
            raise HTTPException(502, detail={"message": "DOE-SP API recusou a consulta", "status": r.status_code})
        batch = extract_items(r.json())
        pages += 1
        if not batch:
            break
        added = 0
        for item in batch:
            k = item_key(item)
            if k not in seen:
                seen.add(k)
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
    weak = 0
    for term in profile_terms():
        result = await raw_search(term, from_date, to_date)
        pages += result["pages"]
        truncated = truncated or result["truncated"]
        for item in result["items"]:
            merged[item_key(item)] = item
    matches = []
    for item in merged.values():
        if identity_verified(item):
            x = dict(item)
            x["matchConfidence"] = "verified"
            x["matchedBy"] = ["name", "identifier"]
            matches.append(x)
        else:
            weak += 1
    matches.sort(key=lambda x: str(x.get("date") or ""))
    return {"matches": matches, "pages": pages, "truncated": truncated, "weak": weak}


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


def collect_uuid_paths(value: Any, path: str = "$") -> List[Dict[str, str]]:
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            out += collect_uuid_paths(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += collect_uuid_paths(v, f"{path}[{i}]")
    elif isinstance(value, str) and UUID_RE.match(value):
        out.append({"id": value, "path": path})
    return out


def pdf_page_match(reader: PdfReader) -> Optional[int]:
    anchors = strong_anchors()
    if not anchors:
        return None
    for idx, page in enumerate(reader.pages, start=1):
        text = norm(page.extract_text() or "")
        if PROFILE_NAME and norm(PROFILE_NAME) not in text:
            continue
        if any(a in text for a in anchors[1:]) or len(anchors) == 1:
            return idx
    return None


async def candidate_editions(day: date) -> Dict[str, Any]:
    probes = []
    found: List[Dict[str, str]] = []
    variants = [
        ("journals_date", {"date": day.isoformat()}),
        ("journals_range", {"fromDate": day.isoformat(), "toDate": day.isoformat()}),
        ("journals_period", {"periodStartingDate": day.isoformat(), "FromDate": day.isoformat(), "ToDate": day.isoformat()}),
    ]
    for name, params in variants:
        r = await get(JOURNALS_URL, params=params)
        probe = {"probe": name, "status": r.status_code}
        if r.status_code < 400:
            try:
                uuids = collect_uuid_paths(r.json())
            except Exception:
                uuids = []
            probe["candidateCount"] = len(uuids)
            found += uuids
        probes.append(probe)
    dedup = {}
    for row in found:
        dedup[(row["id"], row["path"])] = row
    return {"candidates": list(dedup.values()), "probes": probes}


async def locate(item: Dict[str, Any]) -> Dict[str, Any]:
    raw_date = str(item.get("date") or "")[:10]
    try:
        day = date.fromisoformat(raw_date)
    except Exception:
        return {"locatorStatus": "edition_not_resolved", "reason": "publication_date_missing"}

    discovery = await candidate_editions(day)
    tested = []
    for cand in discovery["candidates"]:
        eid = cand["id"]
        url = f"{DOE_PDF_BASE}/v1/editions/{eid}"
        r = await get(url, accept="application/pdf,application/octet-stream,*/*")
        tested.append({"id": eid, "path": cand["path"], "status": r.status_code, "contentType": r.headers.get("content-type")})
        if r.status_code >= 400 or not r.content:
            continue
        ctype = (r.headers.get("content-type") or "").lower()
        if "pdf" not in ctype and not r.content.startswith(b"%PDF"):
            continue
        try:
            reader = PdfReader(BytesIO(r.content))
            page = pdf_page_match(reader)
        except Exception:
            continue
        if page:
            total = len(reader.pages)
            start = max(1, page - 1)
            end = min(total, page + 1)
            return {
                "locatorStatus": "resolved",
                "edition_id": eid,
                "editionUrl": url,
                "editionIdSourcePath": cand["path"],
                "match_page": page,
                "publication_page_start": start,
                "publication_page_end": end,
                "recommended_read_pages": list(range(start, end + 1)),
                "total_pages": total,
                "editionDateProbes": discovery["probes"],
            }
    return {
        "locatorStatus": "edition_found_match_not_located" if discovery["candidates"] else "edition_not_resolved",
        "editionDateProbes": discovery["probes"],
        "candidateEditionIds": tested[:30],
    }


def org(item: Dict[str, Any]) -> str:
    t = norm(item_text(item))
    if "ministerio publico" in t:
        return "MPSP"
    if "controladoria geral do estado" in t:
        return "CGE-SP"
    return "DOE-SP"


def category(item: Dict[str, Any]) -> str:
    t = norm(item_text(item))
    if "licenca" in t or "afastamento" in t:
        return "licenca_afastamento"
    if "concurso" in t or "edital" in t:
        return "concurso_processo_seletivo"
    return "vida_funcional"


def official_url(item: Dict[str, Any]) -> Optional[str]:
    slug = item.get("slug")
    return f"{DOE_WEB_BASE}/{str(slug).lstrip('/')}" if slug else None


async def enrich(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for item in matches:
        x = dict(item)
        x["organization"] = org(x)
        x["category"] = category(x)
        x["relevance"] = "functional"
        x["officialUrl"] = official_url(x)
        try:
            x["documentLocator"] = await locate(x)
        except Exception as exc:
            x["documentLocator"] = {"locatorStatus": "locator_error", "errorType": exc.__class__.__name__}
        out.append(x)
    return out


def summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_category, by_org = {}, {}
    for x in matches:
        by_category[x["category"]] = by_category.get(x["category"], 0) + 1
        by_org[x["organization"]] = by_org.get(x["organization"], 0) + 1
    return {"verified_count": len(matches), "probable_count": 0, "by_category": by_category, "by_organization": by_org}


@app.get("/api/health")
async def health():
    r = await get(JOURNALS_URL)
    return {"bridge": "ok", "version": VERSION, "doesp_api_reachable": r.status_code < 500, "upstream_status": r.status_code}


@app.get("/api/search")
async def search(term: str = Query(...), from_date: date = Query(...), to_date: date = Query(...)):
    r = await raw_search(term, from_date, to_date)
    return {"source": "DOE-SP API only", "term": term, "from_date": from_date, "to_date": to_date, "pages_fetched": r["pages"], "truncated": r["truncated"], "count": len(r["items"]), "items": r["items"]}


@app.get("/api/me")
async def me(from_date: date = Query(...), to_date: date = Query(...)):
    if not PROFILE_NAME:
        raise HTTPException(503, "Perfil não configurado")
    if to_date < from_date:
        raise HTTPException(400, "to_date deve ser maior ou igual a from_date")
    r = await profile_search(from_date, to_date)
    matches = await enrich(r["matches"])
    return {"source": "DOE-SP API only", "from_date": from_date, "to_date": to_date, "search_variants_count": len(profile_terms()), "pages_fetched": r["pages"], "truncated": r["truncated"], "weak_candidates_discarded": r["weak"], "match_count": len(matches), "summary": summary(matches), "matches": matches}


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
    for x in result["matches"]:
        entries.append({
            "date": str(x.get("date") or "")[:10],
            "organization": x.get("organization"),
            "category": x.get("category"),
            "relevance": x.get("relevance"),
            "matchConfidence": x.get("matchConfidence"),
            "title": x.get("title"),
            "id": x.get("id"),
            "officialUrl": x.get("officialUrl"),
            "documentLocator": x.get("documentLocator"),
        })
    return {k: v for k, v in result.items() if k != "matches"} | {"entries": entries}


@app.get("/api/context")
async def context(slug: str = Query(...)):
    url = f"{DOE_WEB_BASE}/{slug.lstrip('/')}"
    r = await get(url, accept="text/html,application/xhtml+xml")
    return {"source": "DOE-SP official publication page", "officialUrl": url, "status": r.status_code, "contextComplete": False}
