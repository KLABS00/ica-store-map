import asyncio
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CHANGELOG_FILE = DATA_DIR / "changelog.json"
PREVIOUS_ICA_FILE = DATA_DIR / "previous_ica.json"
BV_DISK_CACHE_FILE = DATA_DIR / "bv_cache.json"
MANUAL_MATCHES_FILE = DATA_DIR / "manual_matches.json"


def load_changelog() -> list:
    if CHANGELOG_FILE.exists():
        try:
            return json.loads(CHANGELOG_FILE.read_text())
        except Exception:
            pass
    return []


def save_changelog(entries: list):
    CHANGELOG_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def load_previous_ica() -> dict | None:
    if PREVIOUS_ICA_FILE.exists():
        try:
            return json.loads(PREVIOUS_ICA_FILE.read_text())
        except Exception:
            pass
    return None


def save_previous_ica(stores: list):
    PREVIOUS_ICA_FILE.write_text(json.dumps(stores, ensure_ascii=False))


def load_manual_matches() -> dict:
    if MANUAL_MATCHES_FILE.exists():
        try:
            return json.loads(MANUAL_MATCHES_FILE.read_text())
        except Exception:
            pass
    return {}


def save_manual_matches(matches: dict):
    MANUAL_MATCHES_FILE.write_text(json.dumps(matches, ensure_ascii=False, indent=2))


def load_bv_disk_cache() -> dict:
    if BV_DISK_CACHE_FILE.exists():
        try:
            return json.loads(BV_DISK_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_bv_disk_cache(cache: dict):
    BV_DISK_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def diff_ica_data(old_stores: list, new_stores: list) -> list:
    old_by_name = {s["name"]: s for s in old_stores}
    new_by_name = {s["name"]: s for s in new_stores}
    changes = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    for name, s in new_by_name.items():
        if name not in old_by_name:
            changes.append({
                "date": now,
                "type": "added",
                "store": s["name"],
                "detail": f'{s["type"]} in {s["city"]}' + (f' ({s["address"]})' if s["address"] else ""),
            })
        else:
            old = old_by_name[name]
            diffs = []
            if old.get("address") != s.get("address"):
                diffs.append(f'address: "{old.get("address")}" → "{s.get("address")}"')
            if old.get("city") != s.get("city"):
                diffs.append(f'city: "{old.get("city")}" → "{s.get("city")}"')
            if old.get("type") != s.get("type"):
                diffs.append(f'type: {old.get("type")} → {s.get("type")}')
            if diffs:
                changes.append({
                    "date": now,
                    "type": "changed",
                    "store": s["name"],
                    "detail": ", ".join(diffs),
                })

    for name, s in old_by_name.items():
        if name not in new_by_name:
            changes.append({
                "date": now,
                "type": "removed",
                "store": s["name"],
                "detail": f'{s["type"]} in {s["city"]}',
            })

    return changes


async def check_ica_changes():
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get("https://www.ica.se/butiker/", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            })
            resp.raise_for_status()

        match = re.search(r"window\.__INITIAL_DATA__\s*=\s*(.+?);\s*</script>", resp.text, re.DOTALL)
        if not match:
            return

        raw = match.group(1).replace(":undefined", ":null")
        data = json.loads(raw)
        slim_stores = data.get("SlimStores", {}).get("slimStores", [])

        new_stores = []
        for s in slim_stores:
            addr = s.get("address", {})
            new_stores.append({
                "name": s.get("storeName", ""),
                "type": s.get("profile", "Other"),
                "address": addr.get("street", ""),
                "city": addr.get("city", ""),
            })

        previous = load_previous_ica()
        if previous is not None:
            changes = diff_ica_data(previous, new_stores)
            if changes:
                changelog = load_changelog()
                changelog = changes + changelog
                save_changelog(changelog)
                print(f"[changelog] {len(changes)} changes detected")
            else:
                print("[changelog] No changes detected")
        else:
            print("[changelog] First run, saving baseline")

        save_previous_ica(new_stores)
    except Exception as e:
        print(f"[changelog] Error checking changes: {e}")


async def changelog_loop():
    await asyncio.sleep(5)
    while True:
        await check_ica_changes()
        await asyncio.sleep(86400)


# --- Bolagsverket API ---

BV_TOKEN_URL = os.environ.get(
    "BV_TOKEN_URL", "https://portal.api.bolagsverket.se/oauth2/token"
)
BV_BASE_URL = os.environ.get(
    "BV_BASE_URL", "https://gw.api.bolagsverket.se/vardefulla-datamangder/v1"
)
BV_CLIENT_ID = os.environ.get("BV_CLIENT_ID", "")
BV_CLIENT_SECRET = os.environ.get("BV_CLIENT_SECRET", "")

bv_token_cache = {"token": None, "expires_at": 0.0}
bv_company_cache: dict = load_bv_disk_cache()

ICA_PARENT_ORGS = [
    "5560482837",  # ICA Gruppen AB
    "5560210261",  # ICA Sverige AB
]

ICA_STORE_ORGS = [
    "5593403594", "5593406357", "5594020009", "5593403644", "5593403651",
    "5593403669", "5593403677", "5594018052", "5594020744", "5591281745",
    "5593403636", "5593403776", "5591279202", "5568994106", "5590860499",
    "5590860457", "5593403685", "5567234421", "5594486069", "5592857600",
    "5592857618", "5594018011", "5594019969", "5593403610", "5593403735",
    "5591280432", "5594020710", "5593403701", "5590270731", "5594486119",
    "5594019951", "5591279939", "5591279673", "5591280549", "5594486135",
    "5594017997", "5591279830", "5590261243", "5590270756", "5590542378",
    "5590542170", "5590860358", "5590860416", "5593403578", "5594020686",
    "5594020728", "5594020736", "5566859004", "5569602609", "5562300193",
    "5591279863", "5591280473", "5592857550", "5592857592", "5594019977",
    "5567682488", "5568994163", "5590860614", "5566456678", "5592857576",
    "5590541982", "5592857642", "5592857691", "5594018045", "5591280507",
    "5594486051", "5594486093", "5594724147", "5594724170", "5594724246",
    "5594486077", "5594724196", "5594019985", "5594486101", "5594724154",
    "5594486044", "5594724238",
]


async def bv_get_token() -> str:
    now = time.time()
    if bv_token_cache["token"] and now < bv_token_cache["expires_at"]:
        return bv_token_cache["token"]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            BV_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": "vardefulla-datamangder:read",
            },
            auth=(BV_CLIENT_ID, BV_CLIENT_SECRET),
        )
        resp.raise_for_status()
        payload = resp.json()
        bv_token_cache["token"] = payload["access_token"]
        bv_token_cache["expires_at"] = now + payload.get("expires_in", 3600) - 60
        return bv_token_cache["token"]


async def bv_lookup_org(org_number: str) -> dict | None:
    digits = "".join(ch for ch in org_number if ch.isdigit())
    if len(digits) == 12 and digits.startswith("16"):
        digits = digits[2:]
    elif len(digits) == 12:
        digits = digits[:10]
    if len(digits) != 10:
        return None

    if digits in bv_company_cache:
        return bv_company_cache[digits]

    token = await bv_get_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BV_BASE_URL}/organisationer",
            json={"identitetsbeteckning": digits},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        data = resp.json()
        orgs = data.get("organisationer", [])
        if not orgs:
            return None
        parsed = parse_bv_company(orgs[0])
        bv_company_cache[digits] = parsed
        return parsed


def parse_bv_company(company: dict) -> dict:
    names_obj = company.get("organisationsnamn") or {}
    names = names_obj.get("organisationsnamnLista") or []
    primary_name = ""
    for n in names:
        name_type = ((n.get("organisationsnamntyp") or {}).get("kod") or "").upper()
        if name_type == "FORETAGSNAMN":
            primary_name = n.get("namn", "")
            break
    if not primary_name and names:
        primary_name = names[0].get("namn", "")

    org_id = company.get("organisationsidentitet") or {}
    org_number = org_id.get("identitetsbeteckning", "")
    org_form = company.get("organisationsform") or {}

    post_addr = company.get("postadressOrganisation") or {}
    addr = post_addr.get("postadress") or {}
    address_parts = [
        addr.get("utdelningsadress", ""),
        addr.get("postnummer", ""),
        addr.get("postort", ""),
    ]
    address = ", ".join(p for p in address_parts if p)

    active_obj = company.get("verksamOrganisation") or {}
    active_code = (active_obj.get("kod") or "").upper()
    dereg = company.get("avregistreradOrganisation")
    liquidation = company.get("pagaendeAvvecklingsEllerOmstruktureringsforfarande")

    if liquidation:
        status = "liquidation"
    elif dereg:
        status = "deregistered"
    elif active_code != "JA":
        status = "inactive"
    else:
        status = "active"

    reg_obj = company.get("organisationsdatum") or {}
    reg_date = reg_obj.get("registreringsdatum", "")

    desc_obj = company.get("verksamhetsbeskrivning") or {}
    description = desc_obj.get("beskrivning", "")

    return {
        "name": primary_name,
        "org_number": org_number,
        "status": status,
        "org_form": org_form.get("klartext", ""),
        "address": address,
        "registered_date": reg_date,
        "description": description,
    }


# --- Company-to-store matching ---

def extract_location(company_name: str) -> tuple[str, str]:
    name = re.sub(r"\s*(AB|Aktiebolag)\s*$", "", company_name).strip()
    type_hint = ""
    lower = name.lower()
    if "stormarknad" in lower:
        type_hint = "Maxi"
    elif "matmarknad" in lower or "matdestination" in lower:
        type_hint = "Kvantum"
    elif "matbutik" in lower or "storbutik" in lower:
        type_hint = "Supermarket"
    elif "närbutik" in lower:
        type_hint = "Nära"

    for prefix in [
        "Stormarknaden i ", "Stormarknaden ", "Matmarknaden i ", "Matmarknaden på ",
        "Matmarknaden ", "Matbutiken i ", "Matbutiken på ", "Matbutiken ",
        "Närbutiken i ", "Närbutiken ", "Storbutiken i ", "Storbutiken ",
        "Matdestinationen i ", "Matdestinationen ",
    ]:
        if name.startswith(prefix):
            return name[len(prefix):].strip(), type_hint

    for suffix in [" Stormarknad", " Livs"]:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip(), type_hint

    return name, type_hint


def match_company_to_stores(company: dict, stores: list) -> dict | None:
    location, type_hint = extract_location(company["name"])
    if not location:
        return None

    loc_lower = location.lower()
    best = None
    best_score = 0

    for store in stores:
        sname = store["name"].lower()
        scity = (store.get("city") or "").lower()
        score = 0

        if loc_lower == scity:
            score = 3
        elif loc_lower in scity:
            score = 2
        elif loc_lower in sname:
            score = 1
        else:
            continue

        if type_hint and store.get("type") == type_hint:
            score += 2

        if score > best_score:
            best_score = score
            best = store

    return best


enrichment_ready = False


async def prefetch_store_companies():
    global enrichment_ready
    if not BV_CLIENT_ID:
        enrichment_ready = True
        return

    semaphore = asyncio.Semaphore(5)
    to_fetch = [org for org in ICA_STORE_ORGS if org not in bv_company_cache]

    if not to_fetch:
        print(f"[enrichment] All {len(ICA_STORE_ORGS)} companies cached")
        enrichment_ready = True
        return

    print(f"[enrichment] Fetching {len(to_fetch)} companies from Bolagsverket...")

    async def lookup(org):
        async with semaphore:
            try:
                await bv_lookup_org(org)
            except Exception as e:
                print(f"[enrichment] Failed {org}: {e}")

    await asyncio.gather(*[lookup(org) for org in to_fetch])
    save_bv_disk_cache(bv_company_cache)
    found = sum(1 for org in ICA_STORE_ORGS if org in bv_company_cache)
    print(f"[enrichment] Done: {found}/{len(ICA_STORE_ORGS)} companies found")
    enrichment_ready = True


async def enrichment_startup():
    await asyncio.sleep(8)
    await prefetch_store_companies()


# --- App setup ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    changelog_task = asyncio.create_task(changelog_loop())
    enrichment_task = asyncio.create_task(enrichment_startup())
    yield
    changelog_task.cancel()
    enrichment_task.cancel()


app = FastAPI(lifespan=lifespan)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = '[out:json][timeout:60];area["ISO3166-1"="SE"]->.sweden;(nwr["brand"~"ICA",i](area.sweden);nwr["name"~"^ICA "](area.sweden););out center body;'
ICA_URL = "https://www.ica.se/butiker/"

osm_cache = {"data": None, "ts": 0}
ica_cache = {"data": None, "ts": 0}
CACHE_TTL = 3600


def classify_store_type(name: str) -> str:
    lower = name.lower()
    if "maxi" in lower:
        return "Maxi"
    if "kvantum" in lower:
        return "Kvantum"
    if "supermarket" in lower:
        return "Supermarket"
    if "nära" in lower or "nara" in lower:
        return "Nära"
    return "Other"


@app.get("/api/stores/osm")
async def get_osm_stores():
    now = time.time()
    if osm_cache["data"] and now - osm_cache["ts"] < CACHE_TTL:
        return osm_cache["data"]

    async with httpx.AsyncClient(timeout=90, headers={"User-Agent": "ICAStoreMap/1.0", "Accept": "*/*"}) as client:
        resp = await client.post(OVERPASS_URL, data={"data": OVERPASS_QUERY})
        resp.raise_for_status()

    elements = resp.json().get("elements", [])
    stores = []
    for el in elements:
        lat = el.get("lat") or (el.get("center", {}).get("lat"))
        lon = el.get("lon") or (el.get("center", {}).get("lon"))
        if not lat or not lon:
            continue
        tags = el.get("tags", {})
        name = tags.get("name", "ICA")
        address_parts = []
        if tags.get("addr:street"):
            street = tags["addr:street"]
            if tags.get("addr:housenumber"):
                street += " " + tags["addr:housenumber"]
            address_parts.append(street)
        if tags.get("addr:postcode"):
            address_parts.append(tags["addr:postcode"])
        city = tags.get("addr:city", "")

        stores.append({
            "name": name,
            "type": classify_store_type(name),
            "address": ", ".join(address_parts) if address_parts else "",
            "city": city,
            "lat": lat,
            "lon": lon,
        })

    result = {"source": "OpenStreetMap", "count": len(stores), "stores": stores}
    osm_cache["data"] = result
    osm_cache["ts"] = now
    return result


@app.get("/api/stores/ica")
async def get_ica_stores():
    now = time.time()
    if ica_cache["data"] and now - ica_cache["ts"] < CACHE_TTL:
        return ica_cache["data"]

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(ICA_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })
        resp.raise_for_status()

    match = re.search(r"window\.__INITIAL_DATA__\s*=\s*(.+?);\s*</script>", resp.text, re.DOTALL)
    if not match:
        return {"source": "ICA.se", "count": 0, "stores": []}

    raw = match.group(1).replace(":undefined", ":null")
    data = json.loads(raw)
    slim_stores = data.get("SlimStores", {}).get("slimStores", [])

    stores = []
    for s in slim_stores:
        addr = s.get("address", {})
        lat = s.get("lat")
        lng = s.get("lng")
        stores.append({
            "name": s.get("storeName", ""),
            "type": s.get("profile", "Other"),
            "address": addr.get("street", ""),
            "city": addr.get("city", ""),
            "postalCode": addr.get("postalCode", ""),
            "lat": float(lat) if lat else None,
            "lon": float(lng) if lng else None,
        })

    result = {"source": "ICA.se", "count": len(stores), "stores": stores}
    ica_cache["data"] = result
    ica_cache["ts"] = now
    return result


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-zåäö0-9]", "", name.lower().replace("nära", "nara").replace("ica ", ""))


@app.get("/api/stores/validate")
async def validate_stores():
    osm_data = await get_osm_stores()
    ica_data = await get_ica_stores()

    osm_stores = [s for s in osm_data["stores"] if s["lat"] and s["lon"]]
    ica_stores = [s for s in ica_data["stores"] if s["lat"] and s["lon"]]

    matched = []
    ica_only = []
    osm_matched_indices = set()

    for ica_s in ica_stores:
        best_match = None
        best_dist = float("inf")
        best_idx = -1

        for idx, osm_s in enumerate(osm_stores):
            if idx in osm_matched_indices:
                continue
            dist = haversine(ica_s["lat"], ica_s["lon"], osm_s["lat"], osm_s["lon"])
            if dist < 500 and dist < best_dist:
                name_sim = normalize_name(ica_s["name"]) == normalize_name(osm_s["name"])
                if dist < 200 or name_sim:
                    best_dist = dist
                    best_match = osm_s
                    best_idx = idx

        if best_match:
            osm_matched_indices.add(best_idx)
            matched.append({
                "ica": ica_s,
                "osm": best_match,
                "distance_m": round(best_dist),
            })
        else:
            ica_only.append(ica_s)

    osm_only = [s for i, s in enumerate(osm_stores) if i not in osm_matched_indices]

    return {
        "summary": {
            "matched": len(matched),
            "ica_only": len(ica_only),
            "osm_only": len(osm_only),
        },
        "matched": matched,
        "ica_only": ica_only,
        "osm_only": osm_only,
    }


@app.get("/api/changelog")
async def get_changelog():
    return load_changelog()


@app.get("/api/company/ica-group")
async def get_ica_group():
    if not BV_CLIENT_ID:
        return {"error": "Bolagsverket API not configured", "companies": []}
    results = []
    for org_nr in ICA_PARENT_ORGS:
        try:
            parsed = await bv_lookup_org(org_nr)
            if parsed:
                results.append(parsed)
        except Exception as e:
            print(f"[bv] Failed to look up {org_nr}: {e}")
    return {"companies": results}


@app.get("/api/stores/enriched")
async def get_enriched_stores():
    if not BV_CLIENT_ID:
        return {"error": "Bolagsverket API not configured", "ready": False}

    ica_data = await get_ica_stores()
    stores = ica_data["stores"]
    manual_matches = load_manual_matches()

    companies = {org: bv_company_cache[org] for org in ICA_STORE_ORGS if org in bv_company_cache}

    matched = []
    unmatched = []
    used_orgs = set()

    for store in stores:
        if store["name"] in manual_matches:
            org = manual_matches[store["name"]]
            company = bv_company_cache.get(org)
            if not company:
                company = await bv_lookup_org(org)
            if company:
                matched.append({"store": store, "company": company, "match_type": "manual"})
                used_orgs.add(org)
                continue

        best_company = None
        best_org = None
        best_score = 0
        for org, company in companies.items():
            if org in used_orgs:
                continue
            result = match_company_to_stores(company, [store])
            if result:
                location, type_hint = extract_location(company["name"])
                loc_lower = location.lower()
                scity = (store.get("city") or "").lower()
                sname = store["name"].lower()
                score = 0
                if loc_lower == scity:
                    score = 3
                elif loc_lower in scity:
                    score = 2
                elif loc_lower in sname:
                    score = 1
                if type_hint and store.get("type") == type_hint:
                    score += 2
                if score > best_score:
                    best_score = score
                    best_company = company
                    best_org = org

        if best_company and best_score >= 3:
            matched.append({"store": store, "company": best_company, "match_type": "auto"})
            used_orgs.add(best_org)
        else:
            unmatched.append(store)

    return {
        "ready": enrichment_ready,
        "summary": {
            "total": len(stores),
            "matched": len(matched),
            "unmatched": len(unmatched),
            "companies_loaded": len(companies),
        },
        "matched": matched,
        "unmatched": unmatched,
    }


@app.post("/api/stores/match")
async def save_match(request: Request):
    body = await request.json()
    store_name = body.get("store_name", "")
    org_number = body.get("org_number", "")
    if not store_name or not org_number:
        return {"error": "Missing store_name or org_number"}

    company = await bv_lookup_org(org_number)
    if not company:
        return {"error": "Company not found at Bolagsverket"}

    save_bv_disk_cache(bv_company_cache)

    matches = load_manual_matches()
    digits = "".join(ch for ch in org_number if ch.isdigit())
    if len(digits) == 12 and digits.startswith("16"):
        digits = digits[2:]
    elif len(digits) == 12:
        digits = digits[:10]
    matches[store_name] = digits
    save_manual_matches(matches)

    return {"ok": True, "company": company}


@app.delete("/api/stores/match/{store_name}")
async def delete_match(store_name: str):
    matches = load_manual_matches()
    if store_name in matches:
        del matches[store_name]
        save_manual_matches(matches)
    return {"ok": True}


@app.get("/api/company/{org_number}")
async def get_company(org_number: str):
    if not BV_CLIENT_ID:
        return {"error": "Bolagsverket API not configured"}
    try:
        parsed = await bv_lookup_org(org_number)
        if not parsed:
            return {"error": "Not found"}
        return parsed
    except Exception as e:
        return {"error": str(e)}


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
