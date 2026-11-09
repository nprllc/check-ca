"""Modèles de données de Check ça (v0.1)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Source:
    url: str
    title: str = ""


@dataclass
class Claim:
    text: str
    importance: str = "secondary"  # main | secondary
    verdict: str = "unverifiable"  # supported | mixed | contradicted | unverifiable
    explanation: str = ""
    sources: list[Source] = field(default_factory=list)


@dataclass
class FactCheck:
    publisher: str = ""
    url: str = ""
    title: str = ""
    rating: str = ""
    rating_class: str = "unknown"  # false | mixed | true | unknown
    related_to_main_claim: bool = False
    claim_text: str = ""
    review_date: Optional[datetime] = None


@dataclass
class MediaItem:
    kind: str = "image"  # image | video
    url: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    alt: str = ""


@dataclass
class Appearance:
    url: str = ""
    first_seen: Optional[datetime] = None
    first_seen_source: str = ""  # wayback | post
    title: str = ""


@dataclass
class MediaFinding:
    media: MediaItem
    bytes_downloaded: bool = False
    exif_software: str = ""
    exif_ai_software: str = ""
    c2pa_ai_generated: bool = False
    c2pa_camera_capture: bool = False
    iptc_ai_marker: bool = False
    ai_probability: Optional[float] = None
    ai_detector_label: Optional[str] = None
    perceptual_hash: str = ""
    earliest_appearance: Optional[Appearance] = None
    appearances: list[Appearance] = field(default_factory=list)
    refuted_by_factcheck: bool = False
    error: str = ""
    # Verdict trouvé sur le web par le module propagation (recherche Mistral)
    web_verdict: str = ""  # authentic | ai_generated | misrepresented | refuted | unknown | ""
    web_explanation: str = ""
    web_source_url: str = ""
    web_source_title: str = ""


@dataclass
class Post:
    platform: str  # x | web
    url: str
    canonical_url: str
    published_at: Optional[datetime] = None
    text: str = ""
    title: str = ""
    author_name: str = ""
    author_handle: str = ""
    author_followers: Optional[int] = None
    author_joined: Optional[datetime] = None
    lang: str = ""
    likes: Optional[int] = None
    retweets: Optional[int] = None
    replies: Optional[int] = None
    views: Optional[int] = None
    site_name: str = ""
    media: list[MediaItem] = field(default_factory=list)


@dataclass
class TimelineEvent:
    kind: str  # post | media | factcheck | share
    date: Optional[datetime]
    title: str = ""
    url: str = ""
    detail: str = ""


@dataclass
class ModuleStatus:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Scores:
    claim_solidity: Optional[int] = None
    claim_label: str = "Indéterminé"
    media_authenticity: Optional[int] = None
    media_label: str = "Indéterminé"
    confidence: str = "low"  # low | high
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    id: str
    url: str
    created_at: Optional[datetime] = None
    post: Optional[Post] = None
    claims: list[Claim] = field(default_factory=list)
    factchecks: list[FactCheck] = field(default_factory=list)
    media: list[MediaFinding] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    scores: Optional[Scores] = None
    summary: str = ""
    propagation_summary: str = ""
    limits: list[str] = field(default_factory=list)


def _clean(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _clean(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def report_to_dict(report: Report) -> dict:
    """Sérialisation JSON d'un rapport (aucune donnée binaire n'est incluse)."""
    return _clean(report)
