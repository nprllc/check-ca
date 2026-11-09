"""Recherche des fact-checks déjà publiés.

Voie principale : Google Fact Check Tools API (si la clé existe).
Repli : recherche web via l'API Mistral Conversations — un fact-check est
toujours trouvé s'il existe en ligne, même sans clé Google.
"""
from __future__ import annotations

import unicodedata

import httpx

from app.analyzers.mistral import call_conversations, extract_json_block, extract_texts_and_refs
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

_MISTRAL_FC_INSTRUCTIONS = """Tu es chercheur de fact-checks pour « Check ça », une plateforme \
d'aide à la vérification en français. On te donne un post (texte, auteur, date) \
et la liste de ses images. Cherche sur le web les vérifications déjà publiées \
par des médias ou des fact-checkeurs (AFP Factuel, Reuters Fact Check, EuvsDisinfo, \
Vishvas News, Snopes, etc.) portant sur ce contenu ou ces images.

Règles strictes :
- Ne cite que des pages que tu as réellement consultées pendant la recherche.
- Si tu ne trouves aucun fact-check, rends une liste vide : n'invente rien, \
ne raconte pas ta recherche.
- Les dates sont au format YYYY-MM-DD quand elles sont connues.

Termine par un bloc <json>…</json> exactement au format :
<json>{
  "factchecks": [
    {"publisher": "nom du média ou du fact-checkeur",
     "url": "page du fact-check consultée",
     "title": "titre du fact-check",
     "rating": "note du fact-check (ex. : Faux, Trompeur, Vrai…)",
     "date": "YYYY-MM-DD",
     "claim_text": "ce que vérifie ce fact-check"}
  ]
}</json>"""


def _mistral_consulted(data: dict) -> set[str]:
    """URLs réellement consultées pendant la recherche Mistral."""
    _, refs = extract_texts_and_refs(data)
    return set(refs)


def _factcheck_context(post: Post) -> str:
    media_desc = "\n".join(f"- {m.url}" for m in post.media) or "aucun"
    return (f"Post : {post.url}\n"
            f"Plateforme : {post.platform}\n"
            f"Auteur : {post.author_name or post.author_handle or 'inconnu'}\n"
            f"Publié le : {post.published_at.isoformat() if post.published_at else 'date inconnue'}\n"
            f"Texte : {post.text or post.title or '(sans texte)'}\n"
            f"Images du post :\n{media_desc}")


def parse_factchecks_mistral(data: dict, post: Post) -> list[FactCheck]:
    """Fact-checks depuis une réponse Mistral ; seules les pages réellement
    consultées sont retenues — on n'invente jamais de source."""
    texts, _ = extract_texts_and_refs(data)
    payload = extract_json_block(texts)
    if not payload:
        return []
    consulted = _mistral_consulted(data)
    checks: list[FactCheck] = []
    seen: set[str] = set()
    for item in payload.get("factchecks") or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url or url not in consulted or url in seen:
            continue
        seen.add(url)
        rating = (item.get("rating") or "").strip()
        claim_text = (item.get("claim_text") or "").strip()
        checks.append(FactCheck(
            publisher=(item.get("publisher") or "").strip(),
            url=url,
            title=(item.get("title") or "").strip(),
            rating=rating,
            rating_class=classify_rating(rating),
            related_to_main_claim=_related(claim_text, (post.text or post.title or "")),
            claim_text=claim_text,
            review_date=parse_loose_date(item.get("date") or ""),
        ))
    return checks


async def search_factchecks_mistral(post: Post, settings: Settings,
                                    client: httpx.AsyncClient) -> list[FactCheck]:
    """Fact-checks déjà publiés, trouvés par recherche web Mistral."""
    data = await call_conversations(client, settings, _MISTRAL_FC_INSTRUCTIONS,
                                    _factcheck_context(post))
    return parse_factchecks_mistral(data, post)
