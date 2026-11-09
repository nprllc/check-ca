"""Règles de calcul des indices — v0.1.0, NON CALIBRÉES.

⚠️ Ne pas modifier ces règles sans le signaler : elles doivent être
recalibrées sur un jeu de cas déjà tranchés par des fact-checkeurs
avant tout lancement public (voir README).
"""
from __future__ import annotations

from datetime import timedelta

from app.models import FactCheck, MediaFinding, ModuleStatus, Post, Claim


def label(score: int | None) -> str:
    """Étiquette lisible d'un indice 0-100."""
    if score is None:
        return "Non applicable"
    if score <= 30:
        return "Fortement douteux"
    if score <= 50:
        return "Douteux"
    if score <= 70:
        return "Incertain"
    if score <= 85:
        return "Plutôt fiable"
    return "Fiable"


_VERDICTS = {"supported": 100, "mixed": 50, "unverifiable": 50, "contradicted": 0}


def score_claims(claims: list[Claim], factchecks: list[FactCheck]) -> tuple[int | None, str]:
    """Solidité de l'affirmation : moyenne pondérée (principale ×2), plafonnée
    à 15 si l'affirmation principale est démentie par un fact-check."""
    if not claims:
        return None, label(None)
    total = sum(_VERDICTS.get(c.verdict, 50) * (2 if c.importance == "main" else 1) for c in claims)
    weight = sum(2 if c.importance == "main" else 1 for c in claims)
    score = round(total / weight)
    main_refuted = any(fc.related_to_main_claim and fc.rating_class == "false" for fc in factchecks)
    if main_refuted:
        score = min(score, 15)
    return score, label(score)


def score_media_item(finding: MediaFinding, post: Post, already_refuted: bool) -> tuple[int | None, str]:
    """Authenticité d'un média : base 60, bonus/malus cumulés puis plafonds."""
    if finding.error:
        return None, label(None)
    score = 60
    if finding.c2pa_camera_capture and not finding.c2pa_ai_generated:
        score += 25  # Content Credentials « prise de vue » valide
    if finding.ai_probability is not None:
        p = finding.ai_probability
        score += 5 if p < 0.3 else (-20 if p >= 0.7 else 0)
    if (finding.earliest_appearance and finding.earliest_appearance.first_seen
            and post.published_at
            and finding.earliest_appearance.first_seen < post.published_at - timedelta(days=7)):
        score -= 30  # image en ligne plus de 7 jours avant le post : recyclage
    ai_marker = finding.c2pa_ai_generated or bool(finding.exif_ai_software) or finding.iptc_ai_marker
    if ai_marker:
        score = min(score, 10)  # marqueur de génération IA
    if already_refuted:
        score = min(score, 15)  # image déjà démentie par un fact-check
    return score, label(score)


def compute_scores(post: Post, claims: list[Claim], factchecks: list[FactCheck],
                   media: list[MediaFinding], modules: list[ModuleStatus]):
    """Indices finaux + confiance. Le média le plus suspect détermine l'indice."""
    from app.models import Scores

    ok = sum(1 for m in modules if m.ok)
    ratio = (ok / len(modules)) if modules else 0.0
    confidence = "high" if ratio > 0.5 else "low"

    claim_solidity, _ = score_claims(claims, factchecks) if claims else (None, label(None))
    media_scores = [s for s, _ in (score_media_item(f, post, f.refuted_by_factcheck) for f in media) if s is not None]
    media_authenticity = min(media_scores) if media_scores else None

    if confidence == "low":
        # Moins de la moitié des briques ont fonctionné : on n'affiche pas d'étiquette.
        return Scores(claim_solidity=claim_solidity, claim_label="Indéterminé",
                      media_authenticity=media_authenticity, media_label="Indéterminé",
                      confidence=confidence)
    return Scores(claim_solidity=claim_solidity, claim_label=label(claim_solidity),
                  media_authenticity=media_authenticity, media_label=label(media_authenticity),
                  confidence=confidence)
