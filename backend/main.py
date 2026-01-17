import os
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

def get_ebay_token():
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }
    response = requests.post(
        url,
        auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET),
        headers=headers,
        data=data
    )
    return response.json()["access_token"]

def fetch_sold_items(query, token):
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {
        "q": query,
        "filter": "soldItemsOnly:true",
        "limit": 20
    }
    res = requests.get(url, headers=headers, params=params)
    return res.json().get("itemSummaries", [])

@app.get("/")
def home():
    return {
        "app": "IceValue",
        "status": "online",
        "language": ["fr", "en"]
    }

@app.get("/search")
def search(q: str):
    token = get_ebay_token()
    items = fetch_sold_items(q, token)

    prices = []
    for item in items:
        price = item.get("price", {}).get("value")
        if price:
            prices.append(float(price))

    if len(prices) < 3:
        return {
            "query": q,
            "error": "Not enough data",
            "note_fr": "Pas assez de ventes récentes.",
            "note_en": "Not enough recent sales."
        }

    median_price = statistics.median(prices)
    average_price = round(sum(prices) / len(prices), 2)

    return {
        "query": q,
        "currency": "USD",
        "median_price": round(median_price, 2),
        "average_price": average_price,
        "sales_used": len(prices),
        "source": "eBay sold listings"
    }
