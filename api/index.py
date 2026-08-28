import os, re, json, unicodedata
from io import BytesIO
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, HTTPException, Query
from pypdf import PdfReader

VERSION="1.4.4-debug"
DOE_BASE="https://do-api-web-search.doe.sp.gov.br"
SEARCH_URL=f"{DOE_BASE}/v2/advanced-search/publications"
JOURNALS_URL=f"{DOE_BASE}/v2/journals"
PDF_BASE="https://do-api-publication-pdf.doe.sp.gov.br"
WEB_BASE="https://doe.sp.gov.br"
TIMEOUT=float(os.getenv("DOESP_TIMEOUT_SECONDS","25")); PAGE_SIZE=100; MAX_PAGES=50
app=FastAPI(title="DOE-SP Bridge",version=VERSION)

def env_list(n): return [x.strip() for x in re.split(r"[|,;\r\n]+",os.getenv(n,"")) if x.strip()]
NAME=os.getenv("DOESP_PROFILE_NAME","").strip(); MATS=env_list("DOESP_PROFILE_MATRICULAS"); RGS=env_list("DOESP_PROFILE_RGS"); CPFS=env_list("DOESP_PROFILE_CPFS"); OTHER=env_list("DOESP_PROFILE_OTHER_IDS")
def accents(s): return "".join(c for c in unicodedata.normalize("NFKD",s or "") if not unicodedata.combining(c))
def norm(s): return re.sub(r"\s+"," ",accents(s).casefold()).strip()
def digs(s): return re.sub(r"\D","",s or "")
def uniq(xs):
    out=[]
    for x in xs:
        if x and x not in out: out.append(x)
    return out
def terms():
    xs=[NAME,accents(NAME)] if NAME else []
    for v in MATS+RGS+CPFS+OTHER: xs += [v,digs(v),digs(v).lstrip("0")]
    return uniq(xs)
def item_text(x): return " ".join(str(x.get(k) or "") for k in ("title","excerpt","hierarchy","content","description"))
def verified(x):
    t=norm(item_text(x)); td=digs(t); ids=MATS+RGS+CPFS+OTHER
    return bool(NAME and norm(NAME) in t) and (not ids or any(digs(v) and (digs(v) in td or digs(v).lstrip("0") in td) for v in ids))
def extract(payload):
    if isinstance(payload,list): return [x for x in payload if isinstance(x,dict)]
    if isinstance(payload,dict):
        for k in ("items","results","hits","data","publications"):
            v=payload.get(k)
            if isinstance(v,list): return [x for x in v if isinstance(x,dict)]
            if isinstance(v,dict):
                z=extract(v)
                if z:return z
    return []
def key(x): return str(x.get("id") or x.get("publicationId") or x.get("slug") or json.dumps(x,sort_keys=True)[:300])
async def get(url,params=None,accept="application/json,*/*"):
    async with httpx.AsyncClient(timeout=TIMEOUT,follow_redirects=True) as c:
        return await c.get(url,params=params,headers={"Accept":accept,"User-Agent":f"DOE-SP-Bridge/{VERSION}"})
async def raw(term,fd,td):
    out=[]; seen=set(); pages=0; truncated=False
    for page in range(1,MAX_PAGES+1):
        r=await get(SEARCH_URL,{"PageNumber":page,"PageSize":PAGE_SIZE,"SortField":"Date","periodStartingDate":fd.isoformat(),"FromDate":fd.isoformat(),"ToDate":td.isoformat(),"Terms[0]":term})
        if r.status_code>=400: raise HTTPException(502,"DOE-SP search error")
        batch=extract(r.json()); pages+=1
        if not batch: break
        n=0
        for x in batch:
            k=key(x)
            if k not in seen: seen.add(k); out.append(x); n+=1
        if len(batch)<PAGE_SIZE or n==0: break
    else: truncated=True
    return out,pages,truncated
async def profile(fd,td):
    merged={}; pages=0; trunc=False
    for t in terms():
        xs,p,tr=await raw(t,fd,td); pages+=p; trunc|=tr
        for x in xs: merged[key(x)]=x
    matches=[]; weak=0
    for x in merged.values():
        if verified(x): y=dict(x); y["matchConfidence"]="verified"; y["matchedBy"]=["name","identifier"]; matches.append(y)
        else: weak+=1
    matches.sort(key=lambda x:str(x.get("date") or "")); return matches,pages,trunc,weak

UUID_ANY=re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")
EDITION_URL_RE=re.compile(r"(?:https?:)?//do-api-publication-pdf\.doe\.sp\.gov\.br/v1/editions/([0-9a-fA-F-]{36})",re.I)
def page_match(reader):
    nn=norm(NAME); ids=[digs(x) for x in MATS+RGS+CPFS+OTHER if digs(x)]
    for i,p in enumerate(reader.pages,1):
        text=norm(p.extract_text() or "")
        if nn and nn not in text: continue
        td=digs(text)
        if not ids or any(v in td or v.lstrip("0") in td for v in ids): return i
    return None
async def candidate_from_publication(item):
    slug=item.get("slug")
    if not slug:return {"url":None,"status":None,"editionIds":[],"uuids":[],"hints":[]}
    url=f"{WEB_BASE}/{str(slug).lstrip('/')}"; r=await get(url,accept="text/html,application/xhtml+xml,*/*"); text=r.text
    editions=uniq([m.group(1) for m in EDITION_URL_RE.finditer(text)])
    uuids=uniq(UUID_ANY.findall(text))
    hints=[]; low=text.lower()
    for token in ("publication-pdf","/v1/editions/","editionid","edition-id","sumario"):
        pos=low.find(token)
        if pos>=0:hints.append(text[max(0,pos-250):pos+500])
    return {"url":url,"status":r.status_code,"finalUrl":str(r.url),"editionIds":editions,"uuidCount":len(uuids),"uuids":uuids[:40],"hints":hints[:8]}
async def locate(item):
    discovery=await candidate_from_publication(item); tested=[]
    for eid in discovery["editionIds"]:
        url=f"{PDF_BASE}/v1/editions/{eid}"; r=await get(url,accept="application/pdf,application/octet-stream,*/*"); tested.append({"id":eid,"status":r.status_code})
        if r.status_code>=400 or not (r.content.startswith(b"%PDF") or "pdf" in (r.headers.get("content-type") or "").lower()): continue
        try: reader=PdfReader(BytesIO(r.content)); pg=page_match(reader)
        except Exception: continue
        if pg:
            total=len(reader.pages); a=max(1,pg-1); b=min(total,pg+1)
            return {"locatorStatus":"resolved","edition_id":eid,"editionUrl":url,"match_page":pg,"publication_page_start":a,"publication_page_end":b,"recommended_read_pages":list(range(a,b+1)),"total_pages":total,"editionDiscovery":"publication_page"}
    return {"locatorStatus":"edition_found_match_not_located" if discovery["editionIds"] else "edition_not_resolved","publicationPageProbe":{"status":discovery["status"],"finalUrl":discovery.get("finalUrl"),"editionIds":discovery["editionIds"],"uuidCount":discovery["uuidCount"],"uuids":discovery["uuids"],"hints":discovery["hints"]},"testedEditions":tested}
def org(x):
    t=norm(item_text(x)); return "MPSP" if "ministerio publico" in t else ("CGE-SP" if "controladoria geral do estado" in t else "DOE-SP")
def cat(x):
    t=norm(item_text(x)); return "licenca_afastamento" if ("licenca" in t or "afastamento" in t) else ("concurso_processo_seletivo" if ("concurso" in t or "edital" in t) else "vida_funcional")
async def enrich(xs):
    out=[]
    for x in xs:
        y=dict(x); y["organization"]=org(y); y["category"]=cat(y); y["relevance"]="functional"; y["officialUrl"]=f"{WEB_BASE}/{str(y.get('slug')).lstrip('/')}" if y.get("slug") else None
        try:y["documentLocator"]=await locate(y)
        except Exception as e:y["documentLocator"]={"locatorStatus":"locator_error","errorType":type(e).__name__}
        out.append(y)
    return out
def summary(xs):
    bc={};bo={}
    for x in xs:bc[x["category"]]=bc.get(x["category"],0)+1;bo[x["organization"]]=bo.get(x["organization"],0)+1
    return {"verified_count":len(xs),"probable_count":0,"by_category":bc,"by_organization":bo}
@app.get("/api/health")
async def health():
    r=await get(JOURNALS_URL);return {"bridge":"ok","version":VERSION,"doesp_api_reachable":r.status_code<500,"upstream_status":r.status_code}
@app.get("/api/debug/publication")
async def debug_publication(slug:str=Query(...)): return await candidate_from_publication({"slug":slug})
@app.get("/api/search")
async def search(term:str=Query(...),from_date:date=Query(...),to_date:date=Query(...)):
    xs,p,tr=await raw(term,from_date,to_date);return {"source":"DOE-SP API only","term":term,"from_date":from_date,"to_date":to_date,"pages_fetched":p,"truncated":tr,"count":len(xs),"items":xs}
@app.get("/api/me")
async def me(from_date:date=Query(...),to_date:date=Query(...)):
    if not NAME:raise HTTPException(503,"Perfil não configurado")
    xs,p,tr,w=await profile(from_date,to_date);xs=await enrich(xs);return {"source":"DOE-SP API only","from_date":from_date,"to_date":to_date,"search_variants_count":len(terms()),"pages_fetched":p,"truncated":tr,"weak_candidates_discarded":w,"match_count":len(xs),"summary":summary(xs),"matches":xs}
@app.get("/api/me/today")
async def today():
    d=datetime.now(ZoneInfo("America/Sao_Paulo")).date();z=await me(d,d);z["date"]=d;z["searched"]=True;return z
@app.get("/api/me/log")
async def log(from_date:date=Query(...),to_date:date=Query(...)):
    z=await me(from_date,to_date);return {k:v for k,v in z.items() if k!="matches"}|{"entries":[{"date":str(x.get("date") or "")[:10],"organization":x.get("organization"),"category":x.get("category"),"title":x.get("title"),"id":x.get("id"),"officialUrl":x.get("officialUrl"),"documentLocator":x.get("documentLocator")} for x in z["matches"]]}
@app.get("/api/context")
async def context(slug:str=Query(...)):
    z=await candidate_from_publication({"slug":slug});return {"source":"DOE-SP official publication page",**z,"contextComplete":False}
