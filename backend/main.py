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
_FX_TTL_SECONDS = 60 * 60  # 1 heure de cache

def get_usd_cad_rate():
    """Get latest USD->CAD from Bank of Canada Valet API (cached)."""
    now = time.time()
    if _FX_CACHE["rate"] and (now - _FX_CACHE["ts"] < _FX_TTL_SECONDS):
        return _FX_CACHE["rate"]

    # Bank of Canada Valet API: FXUSDCAD (USD/CAD)
    url = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()

    obs = data.get("observations", [])
    if not obs:
        raise RuntimeError("No FX observations returned")

    # observations[0]["FXUSDCAD"]["v"] is a string number like "1.37"
    rate_str = obs[0].get("FXUSDCAD", {}).get("v")
    if not rate_str:
        raise RuntimeError("FXUSDCAD rate missing")

    rate = float(rate_str)
    _FX_CACHE["rate"] = rate
    _FX_CACHE["ts"] = now
    return rate

# ---- eBay auth + fetch ----
def get_ebay_token():
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise RuntimeError("Missing eBay credentials in environment variables")

    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    response = requests.post(
        url,
        auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET),
        headers=headers,
        data=data,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]

def fetch_sold_items(query, token):
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Optional: force US marketplace results (prices often USD)
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": query,
        "filter": "soldItemsOnly:true",
        "limit": 20,
    }
    res = requests.get(url, headers=headers, params=params, timeout=20)
    res.raise_for_status()
    return res.json().get("itemSummaries", [])

@app.get("/")
def home():
    return {"app": "IceValue", "status": "online", "language": ["fr", "en"]}

@app.get("/search")
def search(q: str):
    token = get_ebay_token()
    items = fetch_sold_items(q, token)

    prices_usd = []
excluded_titles = ["lot", "bundle", "x2", "x3", "x4", "cards", "set"]

for item in items:
    title = item.get("title", "").lower()

    # Exclure les lots / bundles
    if any(word in title for word in excluded_titles):
        continue

    p = item.get("price", {})
    val = p.get("value")
    cur = p.get("currency")

    if val and (cur == "USD" or cur is None):
        try:
            price = float(val)

            # Filtrer prix aberrants
            if price <= 5:
                continue
            if price >= 50000:
                continue

            prices_usd.append(price)
        except:
            pass


    if len(prices_usd) < 3:
        return {
            "query": q,
            "error": "Not enough data",
            "note_fr": "Pas assez de ventes récentes (ou devise non USD). Essaie une recherche plus précise.",
            "note_en": "Not enough recent sales (or non-USD currency). Try a more specific query.",
        }
# Nettoyage des valeurs extrêmes
prices_usd.sort()
if len(prices_usd) > 6:
    prices_usd = prices_usd[1:-1]  # enlève le plus bas et le plus haut

    usd_to_cad = get_usd_cad_rate()
    median_usd = statistics.median(prices_usd)
    avg_usd = sum(prices_usd) / len(prices_usd)

    median_cad = round(median_usd * usd_to_cad, 2)
    avg_cad = round(avg_usd * usd_to_cad, 2)

    return {
        "query": q,
        "currency": "CAD",
        "usd_to_cad": round(usd_to_cad, 6),
        "median_price_cad": median_cad,
        "average_price_cad": avg_cad,
        "sales_used": len(prices_usd),
        "source": "eBay sold listings + Bank of Canada FX",
    }
