import os
import time
import requests
import statistics
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="IceValue API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

# ---- FX cache (USD -> CAD) ----
_FX_CACHE = {"rate": None, "ts": 0}
_FX_TTL_SECONDS = 60 * 60  # 1 heure

def get_usd_cad_rate():
    now = time.time()
    if _FX_CACHE["rate"] and (now - _FX_CACHE["ts"] < _FX_TTL_SECONDS):
        return _FX_CACHE["rate"]

    url = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()

    obs = data.get("observations", [])
    rate = float(obs[0]["FXUSDCAD"]["v"])

    _FX_CACHE["rate"] = rate
    _FX_CACHE["ts"] = now
    return rate

def get_ebay_token():
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    r = requests.post(
        url,
        auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET),
        headers=headers,
        data=data,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]

def fetch_sold_items(query, token):
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": query,
        "filter": "soldItemsOnly:true",
        "limit": 20,
    }
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("itemSummaries", [])

@app.get("/")
def home():
    return {"app": "IceValue", "status": "online", "language": ["fr", "en"]}

@app.get("/search")
def search(q: str):
    token = get_ebay_token()
    items = fetch_sold_items(q, token)

    excluded_titles = [
        "lot", "bundle", "job lot", "mixed lot",
        "x2", "x3", "x4", "x5",
        "2x", "3x", "4x", "5x",
        "set", "complete set", "team set"
    ]

    prices_usd = []
    for item in items:
        title = (item.get("title") or "").lower()
        if any(word in title for word in excluded_titles):
            continue

        p = item.get("price", {})
        val = p.get("value")
        cur = p.get("currency")

        if not val or (cur not in ("USD", None)):
            continue

        try:
            price = float(val)
        except:
            continue

        if price <= 5 or price >= 50000:
            continue

        prices_usd.append(price)

    if len(prices_usd) < 3:
        return {
            "query": q,
            "error": "Not enough data",
            "note_fr": "Pas assez de ventes récentes.",
            "note_en": "Not enough recent sales.",
            "sales_used": len(prices_usd),
        }

    prices_usd.sort()
    if len(prices_usd) > 6:
        prices_usd = prices_usd[1:-1]

    usd_to_cad = get_usd_cad_rate()

    return {
        "query": q,
        "currency": "CAD",
        "usd_to_cad": round(usd_to_cad, 6),
        "median_price_cad": round(statistics.median(prices_usd) * usd_to_cad, 2),
        "average_price_cad": round((sum(prices_usd) / len(prices_usd)) * usd_to_cad, 2),
        "sales_used": len(prices_usd),
        "source": "eBay sold listings + Bank of Canada FX",
    }
