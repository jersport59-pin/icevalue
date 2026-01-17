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

EBAY_CATEGORY_SPORTS_TRADING_CARDS = "212"

# --- Heuristiques "carte" ---
SET_KEYWORDS = [
    "upper deck", "o-pee-chee", "opc", "platinum", "allure", "mvp",
    "series 1", "series 2", "extended series",
    "spx", "spa", "sp authentic", "stature", "the cup",
    "tim hortons", "credentials", "artifacts"
]

# NOTE: Young Guns est géré avec une détection robuste (regex) plus bas,
# mais on garde aussi des variantes ici pour compléter.
INSERT_KEYWORDS = [
    "young guns", "younggun", "youngguns", "yg",
    "canvas", "clear cut", "clearcut",
    "autograph", "auto",
    "rookie", "rc",
    "patch", "jersey",
    "numbered"
]


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


def fetch_sold_items(query, token, marketplace_id="EBAY_CA", category_id=None):
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
    }
    params = {
        "q": query,
        "limit": 50,
        "filter": "soldItemsOnly:true,buyingOptions:{FIXED_PRICE|AUCTION}",
    }
    if category_id:
        params["category_ids"] = category_id

    r = requests.get(url, headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json().get("itemSummaries", [])


def extract_grade(text: str) -> str:
    mg = re.search(r"\b(PSA|BGS|SGC)\s*([0-9]{1,2}(\.[0-9])?)\b", text or "", re.IGNORECASE)
    if not mg:
        return ""
    return f"{mg.group(1).upper()} {mg.group(2)}"


def extract_year(text: str) -> str:
    """
    Supporte: 2020-21, 2020/21, 2023-24, 2016, etc.
    """
    t = text or ""
    m = re.search(r"\b(19|20)\d{2}\s*[-/]\s*\d{2}\b", t)
    if m:
        return re.sub(r"\s+", "", m.group(0)).replace("/", "-")
    m2 = re.search(r"\b(19|20)\d{2}\b", t)
    return m2.group(0) if m2 else ""


def extract_card_number(text: str) -> str:
    """
    Supporte: #208, No 208, No.208, Card 208
    """
    t = text or ""
    m = re.search(r"(#|No\.?|Card)\s*([0-9]{1,4})\b", t, re.IGNORECASE)
    if m:
        return f"#{m.group(2)}"
    return ""


def extract_keywords(text: str, keywords: list[str]) -> list[str]:
    """
    Détection keywords "tolérante"
    - Détection robuste pour Young Guns (OCR imparfait)
    - Détection simple pour les autres mots clés
    """
    low = (text or "").lower()
    low = re.sub(r"\s+", " ", low).strip()

    picked = []

    # --- Détection robuste "Young Guns" ---
    # Match:
    # - "young guns" / "youngguns"
    # - "y0ung guns" (OCR 0 vs o)
    # - "yg" / "y g" / "y-g"
    if (
        "young guns" in low
        or "youngguns" in low
        or re.search(r"y[o0]ung\s*g[u v]\s*n[s5]\b", low)  # tolère OCR sur "guns"
        or re.search(r"\by\s*[-]?\s*g\b", low)             # "yg", "y g", "y-g"
    ):
        picked.append("young guns")

    # --- Autres keywords (détection simple) ---
    for k in keywords:
        k_norm = re.sub(r"\s+", " ", (k or "").strip().lower())
        if not k_norm:
            continue
        if k_norm in {"young guns", "yg", "younggun", "youngguns"}:
            continue  # déjà géré
        if k_norm in low:
            picked.append(k_norm)

    # unique en gardant l'ordre
    seen = set()
    out = []
    for k in picked:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def simplify_query(q: str, max_words: int = 7) -> str:
    """
    Nettoyage OCR -> requête eBay simple et robuste
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
        wl = w2.lower()
        if wl in {"vintage", "card", "cards", "hockey", "nhl"}:
            continue
        if wl in {"ier", "1er", "lerr", "l", "le"}:
            continue
        cleaned.append(w2)

    # Correction simple: ORONTO -> TORONTO si TORONTO absent
    joined = " ".join(cleaned).upper()
    if "ORONTO" in joined and "TORONTO" not in joined:
        cleaned = [("TORONTO" if w.upper() == "ORONTO" else w) for w in cleaned]

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


def build_suggested_query_clean(ocr_text: str) -> tuple[str, dict]:
    """
    Suggestion PROPRE améliorée:
    base (joueur/équipe) + année + set + insert + # + grade
    """
    t = re.sub(r"\s+", " ", (ocr_text or "").strip())

    year = extract_year(t)
    card_no = extract_card_number(t)
    grade = extract_grade(t)

    sets = extract_keywords(t, SET_KEYWORDS)
    inserts = extract_keywords(t, INSERT_KEYWORDS)

    # base courte (souvent joueur + équipe)
    base = simplify_query(t, max_words=7)

    parts = []
    if year:
        parts.append(year)

    # set: 1-2 max
    if sets:
        parts.append(sets[0])
        if len(sets) > 1 and sets[1] not in sets[0]:
            parts.append(sets[1])

    # insert: 1-2 max
    if inserts:
        parts.append(inserts[0])
        if len(inserts) > 1 and inserts[1] not in inserts[0]:
            parts.append(inserts[1])

    if card_no:
        parts.append(card_no)

    if base:
        parts.append(base)

    if grade and grade.lower() not in " ".join(parts).lower():
        parts.append(grade)

    q = " ".join(parts)
    q = re.sub(r"\s+", " ", q).strip()
    q = q[:160] if q else ""

    meta = {
        "year": year,
        "card_number": card_no,
        "set_hits": sets[:3],
        "insert_hits": inserts[:3],
        "grade": grade
    }
    return q, meta


def build_suggested_query_full(ocr_text: str) -> str:
    t = re.sub(r"\s+", " ", (ocr_text or "").strip())
    grade = extract_grade(t)

    t2 = re.sub(r"[^A-Za-z0-9 \-#]", " ", t)
    t2 = re.sub(r"\s+", " ", t2).strip()
    full = t2[:140].strip()

    if grade and grade.lower() not in full.lower():
        full = (full + " " + grade).strip()
    return full[:160] if full else ""


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

    clean, meta = build_suggested_query_clean(text)
    full = build_suggested_query_full(text)

    return {
        "ocr_text": text,
        "suggested_query": clean,  # compat
        "suggested_query_clean": clean,
        "suggested_query_full": full,
        "detected": meta,
        "note_fr": "Suggestions améliorées (année/set/insert/#/grade). Choisis PROPRE pour le meilleur résultat.",
        "note_en": "Improved suggestions (year/set/insert/#/grade). Choose CLEAN for best results.",
    }


@app.get("/search")
def search(q: str):
    token = get_ebay_token()
    usd_to_cad = get_usd_cad_rate()

    # 1) CA + catégorie 212
    items = fetch_sold_items(q, token, marketplace_id="EBAY_CA", category_id=EBAY_CATEGORY_SPORTS_TRADING_CARDS)
    query_used = q
    tried = [{"marketplace": "EBAY_CA", "category": "212", "q": q, "items": len(items)}]

    # 2) fallback: simplifier q
    if len(items) == 0:
        q_simple = simplify_query(q, max_words=7)
        if q_simple and q_simple.lower() != (q or "").lower():
            items = fetch_sold_items(q_simple, token, marketplace_id="EBAY_CA", category_id=EBAY_CATEGORY_SPORTS_TRADING_CARDS)
            query_used = q_simple
            tried.append({"marketplace": "EBAY_CA", "category": "212", "q": q_simple, "items": len(items)})

    # 3) fallback: enlever catégorie
    if len(items) == 0:
        items = fetch_sold_items(query_used, token, marketplace_id="EBAY_CA", category_id=None)
        tried.append({"marketplace": "EBAY_CA", "category": None, "q": query_used, "items": len(items)})

    # 4) fallback: marketplace US
    if len(items) == 0:
        items = fetch_sold_items(query_used, token, marketplace_id="EBAY_US", category_id=None)
        tried.append({"marketplace": "EBAY_US", "category": None, "q": query_used, "items": len(items)})

    excluded_titles = [
        "lot", "bundle", "job lot", "mixed lot",
        "x2", "x3", "x4", "x5",
        "2x", "3x", "4x", "5x",
        "set", "complete set", "team set"
    ]

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
            "query_used": query_used,
            "error": "Not enough data",
            "note_fr": "Pas assez de ventes trouvées. Ajoute année / set / # / Young Guns / PSA si possible.",
            "note_en": "Not enough sales found. Add year / set / # / Young Guns / PSA if possible.",
            "sales_used": len(prices_cad),
            "top_sales": [],
            "debug": {"tried": tried, "ebay_items_returned": len(items), "after_filters": len(prices_cad)},
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
        "query_used": query_used,
        "currency": "CAD",
        "usd_to_cad": round(usd_to_cad, 6),
        "median_price_cad": round(statistics.median(used_prices_cad), 2),
        "average_price_cad": round((sum(used_prices_cad) / len(used_prices_cad)), 2),
        "sales_used": len(used_prices_cad),
        "source": "eBay Browse API + Bank of Canada FX (fallback CA->US, category/no-category)",
        "top_sales": [
            {
                "title": s["title"],
                "price_cad": round(s["price_cad"], 2),
                "price_usd": (round(s["price_usd"], 2) if isinstance(s["price_usd"], (int, float)) else None),
                "url": s["url"],
            } for s in top5
        ],
        "debug": {"tried": tried},
    }
