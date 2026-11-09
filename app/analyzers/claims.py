"""Vérification des affirmations : recherche web pilotée par un LLM.

Fournisseur par défaut : Mistral (Conversations API + outil intégré `web_search`).
Repli possible : Anthropic (Claude + `web_search`). Dans les deux cas, le modèle
rend la liste des pages réellement consultées ; toute URL qu'il cite sans
l'avoir ouverte pendant la recherche est retirée.
"""
from __future__ import annotations

import json
import re

import httpx

from app.analyzers.mistral import extract_texts_and_refs
from app.config import Settings
from app.models import Claim, Post, Source

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_MISTRAL_URL = "https://api.mistral.ai/v1/conversations"
_JSON_BLOCK = re.compile(r"<json>\s*(\{.*?\})\s*</json>", re.DOTALL)

_VERDICTS = {
    "supported": "supported", "confirmed": "supported", "true": "supported",
    "contradicted": "contradicted", "false": "contradicted", "debunked": "contradicted",
    "mixed": "mixed", "partial": "mixed", "partially": "mixed", "misleading": "mixed",
    "unverifiable": "unverifiable", "unknown": "unverifiable",
}

INSTRUCTIONS = """Tu es vérificateur de faits pour « Check ça », une plateforme d'aide \
à la vérification en français. Analyse le post fourni.

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

# Chemin Anthropic : instructions et contexte dans un seul message.
PROMPT = """Tu es vérificateur de faits pour « Check ça », une plateforme d'aide \
à la vérification en français. Analyse le post ci-dessous.

Plateforme : {platform}
Auteur : {author}
Publié le : {published}
Texte : {text}
Médias : {media}

""" + INSTRUCTIONS.split("\n", 1)[1]


def _post_context(post: Post) -> str:
    media_desc = "; ".join(f"{m.kind} : {m.url}" for m in post.media) or "aucun"
    return (f"Plateforme : {post.platform}\n"
            f"Auteur : {post.author_name or post.author_handle or 'inconnu'}\n"
            f"Publié le : {post.published_at.isoformat() if post.published_at else 'inconnue'}\n"
            f"Texte : {post.text or post.title or '(sans texte)'}\n"
            f"Médias : {media_desc}")


def _normalize_verdict(value: str) -> str:
    return _VERDICTS.get((value or "").strip().lower(), "unverifiable")


def _from_texts_and_consulted(texts: list[str], consulted: dict[str, str]) -> tuple[str, list[Claim], list[str]]:
    """Extraction commune aux deux fournisseurs : bloc <json> + filtre des sources.

    Les sources de chaque affirmation sont filtrées : seules celles réellement
    apparues dans les résultats de recherche sont conservées. Une affirmation
    qui n'a plus aucune source devient « non vérifiable ».
    """
    full_text = "\n".join(texts)
    m = _JSON_BLOCK.search(full_text)
    raw = m.group(1) if m else None
    summary = full_text.replace(raw, "").strip() if raw else full_text.strip()
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


def parse_response(response: dict) -> tuple[str, list[Claim], list[str]]:
    """Extraction depuis une réponse Anthropic (blocs content)."""
    consulted: dict[str, str] = {}
    texts: list[str] = []
    for block in response.get("content") or []:
        if block.get("type") == "web_search_tool_result":
            for result in block.get("content") or []:
                if result.get("type") == "web_search_result" and result.get("url"):
                    consulted.setdefault(result["url"], result.get("title") or "")
    for block in response.get("content") or []:
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
    return _from_texts_and_consulted(texts, consulted)


def parse_mistral_response(data: dict) -> tuple[str, list[Claim], list[str]]:
    """Extraction depuis une réponse Mistral Conversations (outputs → chunks).

    Les chunks « text » portent la réponse du modèle, les chunks
    « tool_reference » les sources consultées (url + titre). Selon les
    réponses, content est une simple chaîne et les pages ouvertes via
    open_url ne figurent que dans les tool.execution : le parcours
    robuste est partagé dans app.analyzers.mistral.
    """
    texts, consulted = extract_texts_and_refs(data)
    return _from_texts_and_consulted(texts, consulted)


async def analyze_claims(post: Post, settings: Settings, client: httpx.AsyncClient) -> tuple[str, list[Claim], list[str]]:
    """Fournisseur par défaut : Mistral ; repli : Anthropic."""
    if settings.mistral_api_key:
        return await analyze_claims_mistral(post, settings, client)
    return await analyze_claims_anthropic(post, settings, client)


async def analyze_claims_mistral(post: Post, settings: Settings, client: httpx.AsyncClient) -> tuple[str, list[Claim], list[str]]:
    """Appelle l'API Conversations Mistral avec l'outil intégré web_search."""
    body = {
        "model": settings.mistral_model,
        "instructions": INSTRUCTIONS,
        "inputs": _post_context(post),
        "tools": [{"type": "web_search"}],
        "store": False,
    }
    resp = await client.post(_MISTRAL_URL, json=body,
                             headers={"Authorization": f"Bearer {settings.mistral_api_key}"})
    resp.raise_for_status()
    return parse_mistral_response(resp.json())


async def analyze_claims_anthropic(post: Post, settings: Settings, client: httpx.AsyncClient) -> tuple[str, list[Claim], list[str]]:
    """Appelle l'API Anthropic avec l'outil web_search (repli)."""
    prompt = PROMPT.format(
        platform=post.platform,
        author=post.author_name or post.author_handle or "inconnu",
        published=post.published_at.isoformat() if post.published_at else "inconnue",
        text=post.text or post.title or "(sans texte)",
        media="; ".join(f"{m.kind} : {m.url}" for m in post.media) or "aucun",
    )
    body = {
        "model": settings.claude_model,
        "max_tokens": 4096,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = await client.post(_ANTHROPIC_URL, json=body,
                             headers={"anthropic-version": "2023-06-01", "x-api-key": settings.anthropic_api_key})
    resp.raise_for_status()
    return parse_response(resp.json())
