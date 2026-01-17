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

OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "helloworld")

# ---- FX cache (USD -> CAD) ----
_FX_CACHE = {"rate": None, "ts": 0}
_FX_TTL_SECONDS = 60 * 60  # 1 heure

# eBay category: Sports Trading Cards
EBAY_CATEGORY_SPORTS_TRADING_CARDS = "212"

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
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise RuntimeError("Missing eBay credentials (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")

    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}
    r = requests.post(url, auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET), headers=headers, data=data, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

def fetch_sold_items(query, token, category_id=EBAY_CATEGORY_SPORTS_TRADING_CARDS):
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_CA",
    }

    params = {
        "q": query,
        "limit": 50,
        "category_ids": category_id,  # aide énormément pour les cartes
        "filter": "soldItemsOnly:true,buyingOptions:{FIXED_PRICE|AUCTION}",
    }
    r = requests.get(url, headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json().get("itemSummaries", [])

def simplify_query(q: str, max_words: int = 6) -> str:
    """
    Nettoie une requête OCR:
    - enlève caractères bizarres
    - enlève mots trop courts
    - enlève doublons
    - limite à max_words
    """
    q = (q or "").strip()
    q = q.replace(" OR ", " ").replace("|", " ")
    q = re.sub(r"[^A-Za-z0-9 \-#]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()

    parts = q.split(" ")
    cleaned = []
    for w in parts:
        w2 = w.strip()
        if len(w2) < 3:
            continue
        # retire des mots ultra génériques qui nuisent souvent
        if w2.lower() in {"vintage", "card", "cards", "hockey", "nhl"}:
            continue
        cleaned.append(w2)

    # unique en gardant l'ordre
    seen = set()
    unique = []
    for w in cleaned:
        wl = w.lower()
        if wl in seen:
            continue
        seen.add(wl)
        unique.append(w)

    return " ".join(unique[:max_words]).strip()

def build_suggested_query(ocr_text: str) -> str:
    """
    V2 (beaucoup plus clean):
    - détecte PSA/BGS/SGC + grade si présent
    - prend une requête simplifiée (6 mots)
    """
    t = (ocr_text or "").strip()
    t = re.sub(r"\s+", " ", t)

    grade = ""
    mg = re.search(r"\b(PSA|BGS|SGC)\s*([0-9]{1,2}(\.[0-9])?)\b", t, re.IGNORECASE)
    if mg:
        grade = f"{mg.group(1).upper()} {mg.group(2)}"

    base = simplify_query(t, max_words=6)
    if grade and grade.lower() not in base.lower():
        base = (base + " " + grade).strip()

    return base[:160] if base else ""

def ocr_space_extract_text(image_bytes: bytes, filename: str) -> str:
    url = "https://api.ocr.space/parse/image"
    files = {"filename": (filename or "card.jpg", image_bytes)}
    data = {"apikey": OCR_SPACE_API_KEY, "language": "eng", "isOverlayRequired": "false", "OCREngine": "2"}
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
        "note_fr": "Suggestion auto (nettoyée). Tu peux ajuster avant de lancer le prix.",
        "note_en": "Auto-suggestion (cleaned). You can tweak before pricing.",
    }

@app.get("/search")
def search(q: str):
    token = get_ebay_token()

    # 1) essaie la requête telle quelle
    items = fetch_sold_items(q, token)

    # 2) fallback si 0 (souvent OCR trop bruité)
    q_simple = ""
    if len(items) == 0:
        q_simple = simplify_query(q, max_words=6)
        if q_simple and q_simple.lower() != (q or "").lower():
            items = fetch_sold_items(q_simple, token)

    excluded_titles = [
        "lot", "bundle", "job lot", "mixed lot",
        "x2", "x3", "x4", "x5",
        "2x", "3x", "4x", "5x",
        "set", "complete set", "team set"
    ]

    usd_to_cad = get_usd_cad_rate()

    sales = []
    prices_cad = []

    for item in items:
        title_text = item.get("title") or ""
        title = title_text.lower()
        if any(word in title for word in excluded_titles):
            continue

        p = item.get("price", {})
        val = p.get("value")
        cur = p.get("currency")

        if not val or (cur not in ("USD", "CAD", None)):
            continue

        try:
            price = float(val)
        except:
            continue

        if price <= 5 or price >= 50000:
            continue

        if cur == "USD" or cur is None:
            price_usd = price
            price_cad = price * usd_to_cad
        else:
            price_usd = None
            price_cad = price

        url = item.get("itemWebUrl") or item.get("itemHref") or ""

        sales.append({"title": title_text, "price_cad": price_cad, "price_usd": price_usd, "url": url})
        prices_cad.append(price_cad)

    if len(prices_cad) < 3:
        return {
            "query": q,
            "query_used": (q_simple or q),
            "error": "Not enough data",
            "note_fr": "Pas assez de ventes trouvées. Essaie d'ajouter un mot clé (ex: marque, année, #).",
            "note_en": "Not enough sales found. Try adding a keyword (set, year, card #, etc.).",
            "sales_used": len(prices_cad),
            "top_sales": [],
            "debug": {
                "ebay_items_returned": len(items),
                "after_filters": len(prices_cad),
                "marketplace": "EBAY_CA",
                "category_ids": EBAY_CATEGORY_SPORTS_TRADING_CARDS,
                "q_fallback_used": bool(q_simple),
            }
        }

    prices_cad.sort()
    used_prices_cad = prices_cad[1:-1] if len(prices_cad) > 6 else prices_cad[:]

    remaining = used_prices_cad.copy()
    filtered_sales = []
    for s in sales:
        for rp in remaining:
            if abs(s["price_cad"] - rp) < 0.0001:
                filtered_sales.append(s)
                remaining.remove(rp)
                break

    filtered_sales.sort(key=lambda x: x["price_cad"], reverse=True)
    top5 = filtered_sales[:5]

    return {
        "query": q,
        "query_used": (q_simple or q),
        "currency": "CAD",
        "usd_to_cad": round(usd_to_cad, 6),
        "median_price_cad": round(statistics.median(used_prices_cad), 2),
        "average_price_cad": round((sum(used_prices_cad) / len(used_prices_cad)), 2),
        "sales_used": len(used_prices_cad),
        "source": "eBay Browse API + Bank of Canada FX (CA marketplace, category 212)",
        "top_sales": [
            {
                "title": s["title"],
                "price_cad": round(s["price_cad"], 2),
                "price_usd": (round(s["price_usd"], 2) if isinstance(s["price_usd"], (int, float)) else None),
                "url": s["url"],
            } for s in top5
        ],
    }
