"""Orchestration d'une analyse :

connecteur → fact-checks → (affirmations, médias, archives en parallèle)
→ chronologie → scores.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx

from app.analyzers.claims import analyze_claims
from app.analyzers.factchecks import image_search_factchecks, search_factchecks
from app.analyzers.media import analyze_media
from app.analyzers.timeline import build_timeline
from app.config import Settings
from app.connectors import pick_connector
from app.models import Claim, FactCheck, MediaFinding, ModuleStatus, Report, Scores
from app.scoring import compute_scores


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


async def run_analysis(url: str, settings: Settings, client: httpx.AsyncClient) -> Report:
    report = Report(id=_new_id(), url=url, created_at=datetime.now(timezone.utc))

    # 1. Connecteur : transformer l'URL en Post.
    connector = pick_connector(url)
    post = await connector.fetch(url, client)
    report.post = post

    # 2. Fact-checks déjà publiés (indépendant du reste).
    factchecks: list[FactCheck] = []
    modules = [ModuleStatus(name="affirmations", ok=False),
               ModuleStatus(name="fact-checks", ok=False),
               ModuleStatus(name="médias", ok=True)]
    if settings.google_factcheck_api_key:
        try:
            factchecks.extend(await search_factchecks(post, settings, client))
            modules[1] = ModuleStatus(name="fact-checks", ok=True)
        except Exception as exc:  # noqa: BLE001
            report.limits.append(f"Recherche de fact-checks en échec : {exc}")
    else:
        report.limits.append("Recherche de fact-checks désactivée (clé GOOGLE_FACTCHECK_API_KEY absente)")

    # 3. Affirmations et médias, en parallèle.
    claims: list[Claim] = []
    media: list[MediaFinding] = []
    claim_task = None
    if settings.mistral_api_key or settings.anthropic_api_key:
        claim_task = asyncio.ensure_future(analyze_claims(post, settings, client))
    else:
        report.limits.append("Vérification des affirmations non configurée (clé MISTRAL_API_KEY ou ANTHROPIC_API_KEY absente)")
    media_tasks = [asyncio.ensure_future(analyze_media(item, post, settings, client))
                   for item in post.media]

    summary = ""
    related_urls: list[str] = []
    if claim_task is not None:
        try:
            summary, claims, related_urls = await claim_task
            modules[0] = ModuleStatus(name="affirmations", ok=True)
        except Exception as exc:  # noqa: BLE001
            report.limits.append(f"Vérification des affirmations en échec : {exc}")

    if media_tasks:
        media = await asyncio.gather(*media_tasks)
        media = list(media)
        modules[2] = ModuleStatus(name="médias", ok=not any(f.error for f in media))
        # Fact-checks par image (Google Fact Check imageSearch), puis lien
        # avec les fact-checks déjà trouvés.
        for finding in media:
            if finding.error or not settings.google_factcheck_api_key:
                continue
            try:
                image_checks = await image_search_factchecks(finding.media.url, post, settings, client)
            except Exception:  # noqa: BLE001
                continue
            factchecks.extend(image_checks)
            finding.refuted_by_factcheck = any(c.rating_class == "false" for c in image_checks)
    else:
        modules[2] = ModuleStatus(name="médias", ok=True)
        report.limits.append("Aucun média à analyser")

    if not settings.serpapi_api_key:
        report.limits.append("Recherche inversée désactivée (clé SERPAPI_API_KEY absente) : origine des images inconnue")
    if not settings.ai_detector_url:
        report.limits.append("Détecteur IA non configuré (AI_DETECTOR_URL absente) : pas de probabilité IA")

    # Un fact-check déjà lié à l'affirmation principale via la recherche Claude compte aussi.
    for factcheck in factchecks:
        if factcheck.url and factcheck.url in related_urls:
            factcheck.related_to_main_claim = True

    # 4. Chronologie, scores, résumé.
    report.claims = claims
    report.factchecks = factchecks
    report.media = media
    report.timeline = build_timeline(post, media, factchecks)
    report.scores = compute_scores(post, claims, factchecks, media, modules)
    report.summary = summary or _fallback_summary(post, claims, factchecks, media)
    report.limits = [l for l in report.limits if l]
    return report


def _fallback_summary(post, claims, factchecks, media) -> str:
    if claims:
        main = next((c for c in claims if c.importance == "main"), claims[0])
        return f"Affirmation principale : « {main.text} » — verdict : {main.verdict}."
    parts = []
    if factchecks:
        parts.append(f"{len(factchecks)} fact-check(s) déjà publié(s) sur ce sujet")
    if media:
        parts.append(f"{len(media)} média(s) analysé(s)")
    return "Post analysé" + (" : " + ", ".join(parts) if parts else " (aucune brique externe activée).")
