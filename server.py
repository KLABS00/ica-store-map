import asyncio
import json
import math
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CHANGELOG_FILE = DATA_DIR / "changelog.json"
PREVIOUS_ICA_FILE = DATA_DIR / "previous_ica.json"


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(changelog_loop())
    yield
    task.cancel()


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


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
