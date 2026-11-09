"""Chronologie de l'info : ordre chronologique des événements repérés.

Événements : publication du post, premières apparitions en ligne des médias
(archives Wayback), publication des fact-checks. Les dates Wayback sont des
approximations (date d'archivage de la page, pas de l'image).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import FactCheck, MediaFinding, Post, TimelineEvent


def _aware(dt: datetime | None) -> datetime | None:
    """Date consciente (UTC si naïve) : le tri chronologique mélange des
    dates venues de toutes les sources, naïves et conscientes."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def build_timeline(post: Post, media: list[MediaFinding],
                   factchecks: list[FactCheck]) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    if post.published_at:
        events.append(TimelineEvent(
            kind="post", date=_aware(post.published_at),
            title="Publication du post analysé",
            url=post.url, detail=f"Sur {post.platform}, par {post.author_name or post.author_handle or '?'}",
        ))
    for finding in media:
        earliest = finding.earliest_appearance or None
        if earliest and earliest.first_seen:
            events.append(TimelineEvent(
                kind="media", date=_aware(earliest.first_seen),
                title="Première apparition en ligne du média",
                url=earliest.url,
                detail=f"Média déjà en ligne avant le post (source : {earliest.first_seen_source or 'archive'})",
            ))
    for factcheck in factchecks:
        if factcheck.review_date:
            events.append(TimelineEvent(
                kind="factcheck", date=_aware(factcheck.review_date),
                title=f"Fact-check publié : {factcheck.title or factcheck.claim_text}",
                url=factcheck.url, detail=f"Par {factcheck.publisher} — note : {factcheck.rating}",
            ))
    # Tri chronologique ; dates inconnues à la fin.
    events.sort(key=lambda e: (e.date is None, e.date))
    return events
