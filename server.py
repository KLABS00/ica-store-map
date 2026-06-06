import json
import re
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

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


import math

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


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
