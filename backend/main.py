import os
import time
import re
import requests
import statistics
from fastapi import FastAPI, UploadFile, File
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

# OCR (V1) via OCR.Space (tu peux mettre ta clé dans Render -> Environment)
# Si tu ne mets pas de clé, on tente le mode démo (limité).
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "helloworld")

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
    data = {"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}
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
    params = {"q": query, "filter": "soldItemsOnly:true", "limit": 20}
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("itemSummaries", [])

def build_suggested_query(ocr_text: str) -> str:
    """
    Heuristique simple pour V1:
    - Nettoie le texte
    - Garde mots utiles (joueur, marque, année, PSA/BGS/SGC, numéro, etc.)
    - Construit une requête courte.
    """
    t = (ocr_text or "").strip()
    t = re.sub(r"\s+", " ", t)

    # mots clés souvent utiles sur cartes
    keywords = ["upper deck", "opc", "o-pee-chee", "young guns", "mvp", "spx", "spa",
                "psa", "bgs", "sgc", "rookie", "rc", "autograph", "auto", "canvas"]

    low = t.lower()

    picked = []
    for k in keywords:
        if k in low:
            picked.append(k)

    # cherche une année 19xx ou 20xx
    m = re.search(r"\b(19|20)\d{2}\b", t)
    year = m.group(0) if m else ""

    # cherche un grade PSA/BGS/SGC + chiffre (ex: PSA 10)
    grade = ""
    mg = re.search(r"\b(PSA|BGS|SGC)\s*([0-9]{1,2}(\.[0-9])?)\b", t, re.IGNORECASE)
    if mg:
        grade = f"{mg.group(1).upper()} {mg.group(2)}"

    # garde seulement une partie du texte pour éviter des requêtes trop longues
    base = t[:120]

    parts = []
    if year:
        parts.append(year)
    # base (souvent joueur + marque)
    parts.append(base)

    if grade:
        parts.append(grade)
    if picked:
        parts.append(" ".join(dict.fromkeys(picked)))  # unique

    # nettoie final
    q = " ".join(parts)
    q = re.sub(r"[^A-Za-z0-9 \-\.#]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:160] if q else ""

def ocr_space_extract_text(image_bytes: bytes, filename: str) -> str:
    url = "https://api.ocr.space/parse/image"
    files = {"filename": (filename or "card.jpg", image_bytes)}
    data = {
        "apikey": OCR_SPACE_API_KEY,
        "language": "eng",          # cartes souvent EN; on peut changer plus tard
        "isOverlayRequired": "false",
        "OCREngine": "2"
    }
    r = requests.post(url, files=files, data=data, timeout=60)
    r.raise_for_status()
    j = r.json()
    parsed = j.get("ParsedResults") or []
    if not parsed:
        return ""
    return (parsed[0].get("ParsedText") or "").strip()

@app.get("/")
def home():
    return {"app": "IceValue", "status": "online", "language": ["fr", "en"]}

@app.post("/photo")
async def photo_to_query(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        return {"error": "Empty file"}

    text = ocr_space_extract_text(content, file.filename or "card.jpg")
    suggested = build_suggested_query(text)

    return {
        "ocr_text": text,
        "suggested_query": suggested,
        "note_fr": "Suggestion automatique. Tu peux modifier la recherche avant de lancer le prix.",
        "note_en": "Auto-suggestion. You can edit the search before pricing.",
    }

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

    sales = []
    prices_usd = []

    for item in items:
        title_text = item.get("title") or ""
        title = title_text.lower()

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

        url = item.get("itemWebUrl") or item.get("itemHref") or ""

        sales.append({"title": title_text, "price_usd": price, "url": url})
        prices_usd.append(price)

    if len(prices_usd) < 3:
        return {
            "query": q,
            "error": "Not enough data",
            "note_fr": "Pas assez de ventes récentes (ou elles ont été filtrées).",
            "note_en": "Not enough recent sales (or they were filtered out).",
            "sales_used": len(prices_usd),
            "top_sales": []
        }

    prices_usd.sort()
    used_prices = prices_usd[1:-1] if len(prices_usd) > 6 else prices_usd[:]

    remaining = used_prices.copy()
    filtered_sales = []
    for s in sales:
        if s["price_usd"] in remaining:
            filtered_sales.append(s)
            remaining.remove(s["price_usd"])

    filtered_sales.sort(key=lambda x: x["price_usd"], reverse=True)
    top5 = filtered_sales[:5]

    usd_to_cad = get_usd_cad_rate()

    return {
        "query": q,
        "currency": "CAD",
        "usd_to_cad": round(usd_to_cad, 6),
        "median_price_cad": round(statistics.median(used_prices) * usd_to_cad, 2),
        "average_price_cad": round((sum(used_prices) / len(used_prices)) * usd_to_cad, 2),
        "sales_used": len(used_prices),
        "source": "eBay sold listings + Bank of Canada FX",
        "top_sales": [
            {
                "title": s["title"],
                "price_cad": round(s["price_usd"] * usd_to_cad, 2),
                "price_usd": round(s["price_usd"], 2),
                "url": s["url"],
            } for s in top5
        ],
    }
