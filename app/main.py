"""API et pages publiques de Check ça.

Endpoints :
- ``POST /api/analyze``      : lance une analyse (URL d'un post ou d'une page)
- ``GET  /api/reports/{id}`` : rapport stocké (JSON)
- ``GET  /r/{id}``           : page publique d'un rapport
- ``GET  /api/health``       : état des briques
- ``GET  /``                 : interface publique
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.config import settings
from app.connectors import ConnectorError, pick_connector
from app.models import report_to_dict
from app.netguard import UnsafeURL, assert_public_url
from app.pipeline import run_analysis
from app.store import Store

INDEX = Path(__file__).parent / "static" / "index.html"

app = FastAPI(title="Check ça", version="0.1.0")

# CORS : autorise l'interface statique hébergée sur GitHub Pages à appeler l'API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

store = Store(settings.db_path)

# Limitation de débit : par connexion et par jour (protection du budget API).
_hits: dict[str, list[float]] = {}
_daily: list[float] = []


def _rate_limited(key: str) -> str | None:
    """Message d'erreur si la limite est atteinte, sinon None (et on compte)."""
    now = time.time()
    today = datetime.fromtimestamp(now).date()
    _daily[:] = [t for t in _daily if datetime.fromtimestamp(t).date() == today]
    if len(_daily) >= settings.max_analyses_per_day:
        return "Limite atteinte : quota quotidien d'analyses épuisé, réessayez demain"
    window = [t for t in _hits.get(key, []) if now - t < 3600]
    _hits[key] = window
    if len(window) >= settings.rate_limit_per_hour:
        return "Trop d'analyses en une heure depuis cette connexion, patientez un peu"
    window.append(now)
    _daily.append(now)
    return None


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "inconnu"


class AnalyzeRequest(BaseModel):
    url: str


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": "0.1.0",
        "affirmations": bool(settings.mistral_api_key or settings.anthropic_api_key),
        "factchecks": bool(settings.google_factcheck_api_key or settings.mistral_api_key),
        "recherche d'images": bool(settings.serpapi_api_key or settings.mistral_api_key),
        "analyse IA des images": bool(settings.ai_detector_url or settings.mistral_api_key),
        "médias (local)": True,
        "chronologie (Wayback)": True,
    }


@app.post("/api/analyze")
async def analyze(payload: AnalyzeRequest, request: Request) -> dict:
    url = payload.url.strip()
    if urlsplit(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Seuls les liens http(s) sont acceptés")
    try:
        await assert_public_url(url)
    except UnsafeURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    limited = _rate_limited(_client_key(request))
    if limited:
        raise HTTPException(status_code=429, detail=limited)

    try:
        canonical = pick_connector(url).canonical(url)
    except ConnectorError:
        canonical = url
    cached = store.find_cached(canonical)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            report = await run_analysis(url, settings, client)
    except UnsafeURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    data = report_to_dict(report)
    store.save(report.id, canonical, data)
    return data


@app.get("/api/reports/{report_id}")
def get_report(report_id: str) -> dict:
    data = store.get(report_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Rapport introuvable")
    return data


@app.get("/", response_class=HTMLResponse)
async def home() -> FileResponse:
    return FileResponse(INDEX)


@app.get("/r/{report_id}", response_class=HTMLResponse)
async def report_page(report_id: str) -> FileResponse:
    return FileResponse(INDEX)
