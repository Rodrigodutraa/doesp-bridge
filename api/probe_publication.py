import re
import httpx
from fastapi import FastAPI, Query

app = FastAPI()
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")
EDITION_RE = re.compile(r"https?://do-api-publication-pdf\.doe\.sp\.gov\.br/v1/editions/([0-9a-fA-F-]{36})")

@app.get("/")
async def probe(slug: str = Query(...)):
    url = f"https://doe.sp.gov.br/{slug.lstrip('/')}"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent":"DOE-SP-Bridge-Probe/1"})
    text = r.text
    uuids = []
    for m in UUID_RE.finditer(text):
        value = m.group(0)
        if value not in uuids:
            uuids.append(value)
    editions = []
    for m in EDITION_RE.finditer(text):
        value = m.group(1)
        if value not in editions:
            editions.append(value)
    hints = []
    for token in ("edition", "editions", "publication-pdf", "sumario", "journal"):
        pos = text.lower().find(token)
        if pos >= 0:
            hints.append(text[max(0,pos-180):pos+360])
    return {"status": r.status_code, "finalUrl": str(r.url), "contentType": r.headers.get("content-type"), "editionIds": editions, "uuidCount": len(uuids), "uuids": uuids[:50], "hints": hints[:10]}
