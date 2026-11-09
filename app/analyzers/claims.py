"""Vérification des affirmations : appel à Claude avec l'outil web_search.

Le modèle est chargé de citer ses sources ; toute URL qu'il n'a pas
réellement consultée pendant la recherche est retirée.
"""
from __future__ import annotations

import json
import re

import httpx

from app.config import Settings
from app.models import Claim, Post, Source

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_JSON_BLOCK = re.compile(r"<json>\s*(\{.*?\})\s*</json>", re.DOTALL)

_VERDICTS = {
    "supported": "supported", "confirmed": "supported", "true": "supported",
    "contradicted": "contradicted", "false": "contradicted", "debunked": "contradicted",
    "mixed": "mixed", "partial": "mixed", "partially": "mixed", "misleading": "mixed",
    "unverifiable": "unverifiable", "unknown": "unverifiable",
}

PROMPT = """Tu es vérificateur de faits pour « Check ça », une plateforme d'aide \
à la vérification en français. Analyse le post ci-dessous.

Plateforme : {platform}
Auteur : {author}
Publié le : {published}
Texte : {text}
Médias : {media}

Consignes :
1. Décompose le post en affirmations vérifiables (2 à 4), dont la plus importante.
2. Cherche sur le web de quoi confirmer ou contredire chaque affirmation. \
Utilise uniquement des pages que tu as réellement consultées pendant cette recherche.
3. Réponds en français, en terminant par un bloc <json>…</json> exactement au format :
<json>{{
  "summary": "résumé en 2-3 phrases de ce que la recherche a établi",
  "claims": [
    {{"text": "affirmation", "importance": "main ou secondary", "verdict": "supported, mixed, contradicted ou unverifiable",
      "explanation": "explication brève", "sources": [{{"url": "https://…", "title": "titre de la page"}}]}}
  ],
  "related_factcheck_urls": ["urls de fact-checks existants traitant du même sujet, si tu en as croisé"]
}}</json>
4. Ne cite jamais une URL que tu n'as pas ouverte pendant la recherche.
"""


def _normalize_verdict(value: str) -> str:
    return _VERDICTS.get((value or "").strip().lower(), "unverifiable")


def parse_response(response: dict) -> tuple[str, list[Claim], list[str]]:
    """Extrait (résumé, affirmations, fact-checks liés) d'une réponse Claude.

    Les sources de chaque affirmation sont filtrées : seules celles réellement
    apparues dans les résultats de web_search sont conservées. Une affirmation
    qui n'a plus aucune source devient « non vérifiable ».
    """
    consulted: dict[str, str] = {}
    summary = ""
    for block in response.get("content") or []:
        if block.get("type") == "web_search_tool_result":
            for result in block.get("content") or []:
                if result.get("type") == "web_search_result" and result.get("url"):
                    consulted.setdefault(result["url"], result.get("title") or "")
    raw = None
    for block in response.get("content") or []:
        if block.get("type") == "text":
            m = _JSON_BLOCK.search(block.get("text") or "")
            if m:
                raw = m.group(1)
            elif not summary:
                summary = (block.get("text") or "").strip()
    claims: list[Claim] = []
    related: list[str] = []
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        summary = (data.get("summary") or summary).strip()
        for item in data.get("claims") or []:
            sources = []
            for src in item.get("sources") or []:
                url = (src.get("url") or "").strip()
                if url in consulted and url not in {s.url for s in sources}:
                    sources.append(Source(url=url, title=src.get("title") or consulted[url]))
            claims.append(Claim(
                text=(item.get("text") or "").strip(),
                importance="main" if item.get("importance") == "main" else "secondary",
                verdict=_normalize_verdict(item.get("verdict") or ""),
                explanation=(item.get("explanation") or "").strip(),
                sources=sources,
            ))
        for url in data.get("related_factcheck_urls") or []:
            if isinstance(url, str):
                related.append(url)
    # Garde-fou : une affirmation sans source réellement consultée est non vérifiable.
    for claim in claims:
        if not claim.sources:
            claim.verdict = "unverifiable"
    return summary, claims, related


async def analyze_claims(post: Post, settings: Settings, client: httpx.AsyncClient) -> tuple[str, list[Claim], list[str]]:
    """Appelle l'API Anthropic et renvoie (résumé, affirmations, fact-checks liés)."""
    media_desc = "; ".join(f"{m.kind} : {m.url}" for m in post.media) or "aucun"
    prompt = PROMPT.format(
        platform=post.platform,
        author=post.author_name or post.author_handle or "inconnu",
        published=post.published_at.isoformat() if post.published_at else "inconnue",
        text=post.text or post.title or "(sans texte)",
        media=media_desc,
    )
    body = {
        "model": settings.claude_model,
        "max_tokens": 4096,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"anthropic-version": "2023-06-01", "x-api-key": settings.anthropic_api_key}
    resp = await client.post(_ANTHROPIC_URL, json=body, headers=headers)
    resp.raise_for_status()
    return parse_response(resp.json())
