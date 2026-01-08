from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="IceValue API")

# Autoriser les sites web à appeler l’API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # autorise tous les sites (ok pour commencer)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {
        "app": "IceValue",
        "status": "online",
        "language": ["fr", "en"]
    }
@app.get("/search")
def search(q: str):
    # Pour l’instant: réponse test (prochaine étape: eBay réel)
    return {
        "query": q,
        "currency": "CAD",
        "estimated_price": 123.45,
        "source": "demo",
        "note_fr": "Prix démo. Prochaine étape: ventes eBay réelles.",
        "note_en": "Demo price. Next step: real eBay sold listings."
    }
