"""Recherche des fact-checks déjà publiés (Google Fact Check Tools API)."""
from __future__ import annotations

import unicodedata

import httpx

from app.config import Settings
from app.models import FactCheck, Post
from app.analyzers.media import parse_loose_date

_BASE = "https://factchecktools.googleapis.com/v1alpha1/claims"

_STOPWORDS = {"les", "des", "une", "aux", "avec", "pour", "dans", "sur", "par", "que",
              "qui", "quoi", "cette", "après", "avant", "plus", "moins", "être", "avoir"}


def _norm(text: str) -> set[str]:
    """Mots signifiants, en minuscules et sans accents."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return {w for w in text.lower().split()
            if len(w) >= 4 and w.strip(".,;:!?()'\"") not in _STOPWORDS}


def _related(claim_text: str, post_text: str) -> bool:
    return len(_norm(claim_text) & _norm(post_text)) >= 2


def classify_rating(rating: str) -> str:
    """Classe une note textuelle de fact-check : false | mixed | true | unknown."""
    norm = (rating or "").lower()
    norm = unicodedata.normalize("NFKD", norm).encode("ascii", "ignore").decode()
    if any(k in norm for k in ("partiel", "partial", "missing context", "contexte manquant", "exagere", "exaggerated")):
        return "mixed"
    if any(k in norm for k in ("faux", "false", "trompeur", "misleading", "sorti de son contexte",
                                "out of context", "manipule", "manipulated", "debunk")):
        return "false"
    if any(k in norm for k in ("vrai", "true", "exact", "correct", "verifie", "verified")):
        return "true"
    return "unknown"


def _to_factchecks(data: dict, post: Post) -> list[FactCheck]:
    checks: list[FactCheck] = []
    for claim in data.get("claims") or []:
        text = claim.get("text") or ""
        for review in claim.get("claimReview") or []:
            publisher = (review.get("publisher") or {})
            rating = review.get("textualRating") or ""
            checks.append(FactCheck(
                publisher=publisher.get("name") or publisher.get("site") or "",
                url=review.get("url") or "",
                title=review.get("title") or "",
                rating=rating,
                rating_class=classify_rating(rating),
                related_to_main_claim=_related(text, (post.text or post.title or "")),
                claim_text=text,
                review_date=parse_loose_date(review.get("reviewDate") or ""),
            ))
    return checks


async def search_factchecks(post: Post, settings: Settings, client: httpx.AsyncClient) -> list[FactCheck]:
    """Fact-checks textuels liés au contenu du post."""
    query = (post.text or post.title or post.url)[:300]
    resp = await client.get(f"{_BASE}:search", params={
        "query": query, "key": settings.google_factcheck_api_key,
        "languageCode": "fr",
    })
    resp.raise_for_status()
    return _to_factchecks(resp.json(), post)


async def image_search_factchecks(image_url: str, post: Post, settings: Settings,
                                  client: httpx.AsyncClient) -> list[FactCheck]:
    """Fact-checks portant sur une image (Google Fact Check, recherche par image)."""
    resp = await client.get(f"{_BASE}:imageSearch", params={
        "imageUri": image_url, "key": settings.google_factcheck_api_key,
    })
    resp.raise_for_status()
    return _to_factchecks(resp.json(), post)
