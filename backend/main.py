from fastapi import FastAPI

app = FastAPI(title="IceValue API")

@app.get("/")
def home():
    return {
        "app": "IceValue",
        "status": "online",
        "language": ["fr", "en"]
    }
