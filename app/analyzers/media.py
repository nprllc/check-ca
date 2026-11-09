"""Analyse des images et vidéos : métadonnées, C2PA, IPTC, détecteur IA,
recherche inversée (SerpApi) et première apparition en ligne (Wayback)."""
from __future__ import annotations

import base64
import io
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote

import httpx
from PIL import Image

from app.config import Settings
from app.netguard import assert_public_url
from app.models import Appearance, MediaFinding, MediaItem, Post

# Empreinte perceptuelle : optionnelle (imagehash)
try:
    import imagehash
    _IMAGEHASH = True
except ImportError:  # pragma: no cover
    _IMAGEHASH = False

# Content Credentials : optionnelles (c2pa-python)
try:
    import c2pa
    _C2PA = True
except ImportError:  # pragma: no cover
    _C2PA = False

_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                 "%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d")

_IPTC_AI = re.compile(rb"DigitalSourceType[\"'>\s:]*[^<]*trainedAlgorithmicMedia")

_AI_SOFTWARE = ["midjourney", "dall-e", "dall e", "stable diffusion", "firefly", "flux",
                "imagen", "sora", "gpt-4o", "grok", "gemini", "leonardo", "ideogram",
                "adobe firefly", "ai", "artificial intelligence"]


def parse_loose_date(value) -> datetime | None:
    """Date à partir d'un format courant ; None si incompréhensible."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    iso = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def ai_probability_from(response) -> tuple[float | None, str | None]:
    """(probabilité que le média soit généré par IA, étiquette) depuis la
    réponse du détecteur. Accepte une liste plate ou imbriquée, ou un dict d'erreur."""
    if not isinstance(response, (list, tuple)):
        return None, None
    items = [item for entry in response for item in (entry if isinstance(entry, (list, tuple)) else [entry])]
    items = [i for i in items if isinstance(i, dict) and "label" in i and "score" in i]
    if not items:
        return None, None
    for item in items:
        label = (item.get("label") or "").lower()
        if "artificial" in label or "ai" == label:
            return float(item["score"]), "artificial"
    for item in items:
        label = (item.get("label") or "").lower()
        if "human" in label or "real" in label:
            return round(1.0 - float(item["score"]), 4), "real"
    return None, None


def interpret_c2pa(result) -> tuple[bool, bool]:
    """(marqueur de génération IA, prise de vue appareil photo) depuis un
    résultat c2pa-python. Un manifeste dont la validation a échoué n'apporte rien."""
    if not isinstance(result, dict):
        return False, False
    for status in result.get("validation_status") or []:
        code = (status.get("code") or "").lower()
        if "mismatch" in code or "invalid" in code or "failure" in code:
            return False, False
    ai = camera = False
    for manifest in (result.get("manifests") or {}).values():
        for assertion in (manifest.get("assertions") or []):
            for action in ((assertion.get("data") or {}).get("actions") or []):
                source = action.get("digitalSourceType") or ""
                if "trainedAlgorithmicMedia" in source or "algorithmicMedia" in source:
                    ai = True
                if "digitalCapture" in source:
                    camera = True
    return ai, camera


def _detect_ai_software(software: str) -> str:
    low = software.lower()
    for marker in _AI_SOFTWARE:
        if marker == "ai" and "ai" not in low.split():
            continue
        if marker in low:
            return marker.title().replace("Dall-E", "Dall-E")
    return ""


def _excluded(link: str, post: Post) -> bool:
    """Le lien désigne-t-il le post lui-même (à exclure des apparitions) ?"""
    def bare(u: str) -> str:
        return u.split("?")[0].split("#")[0].rstrip("/").lower()
    if bare(link) in (bare(post.url), bare(post.canonical_url)):
        return True
    if post.platform == "x" and "/status/" in link:
        from app.connectors.x import parse_status_url
        from app.connectors.base import ConnectorError
        try:
            _, link_id = parse_status_url(link)
            _, post_id = parse_status_url(post.url or post.canonical_url)
            return link_id == post_id
        except (ConnectorError, ValueError):
            return False
    return False


def parse_cdx(data) -> list[dict]:
    """Lignes d'une réponse Wayback CDX en JSON. Le format réel place
    l'en-tête (nom des colonnes) en première ligne ; certains miroirs non."""
    if not isinstance(data, list) or not data:
        return []
    rows = data[1:] if data and data[0] and str(data[0][0]).lower() == "timestamp" else data
    return [row for row in rows if isinstance(row, list) and row]


def _timestamp_to_datetime(ts: str) -> datetime | None:
    if not ts or not ts.isdigit() or len(ts) < 8:
        return None
    try:
        return datetime(int(ts[0:4]), int(ts[4:6]), int(ts[6:8]),
                        int(ts[8:10]) if len(ts) >= 10 else 0,
                        int(ts[10:12]) if len(ts) >= 12 else 0,
                        tzinfo=timezone.utc)
    except ValueError:
        return None


async def _first_seen_wayback(url: str, client: httpx.AsyncClient) -> Appearance | None:
    """Première archive Wayback connue d'une page."""
    resp = await client.get("https://web.archive.org/cdx/search/cdx", params={
        "url": url, "output": "json", "fl": "timestamp", "limit": "1",
        "filter": "statuscode:200", "collapse": "digest",
    })
    resp.raise_for_status()
    rows = parse_cdx(resp.json())
    if not rows:
        return None
    first_seen = _timestamp_to_datetime(str(rows[0][0]))
    if not first_seen:
        return None
    return Appearance(url=url, first_seen=first_seen, first_seen_source="wayback")


async def _reverse_search(item: MediaItem, settings: Settings, client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get("https://serpapi.com/search.json", params={
        "engine": "google_lens", "image_url": item.url, "api_key": settings.serpapi_api_key,
    })
    resp.raise_for_status()
    data = resp.json()
    return [m for m in (data.get("exact_matches") or []) if isinstance(m, dict) and m.get("link")]


async def analyze_media(item: MediaItem, post: Post, settings: Settings,
                        client: httpx.AsyncClient) -> MediaFinding:
    finding = MediaFinding(media=item)

    try:
        await assert_public_url(item.url)
        resp = await client.get(item.url)
        resp.raise_for_status()
        data = resp.content
    except Exception as exc:  # noqa: BLE001 - toute défaillance désactive ce média
        finding.error = f"Téléchargement impossible : {exc}"
        return finding
    finding.bytes_downloaded = True

    # --- Métadonnées locales : EXIF, XMP/IPTC, C2PA -------------------------
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        exif = img.getexif()
        software = str(exif.get(0x0131) or "")
        if software:
            finding.exif_software = software
            finding.exif_ai_software = _detect_ai_software(software)
        if _IMAGEHASH:
            finding.perceptual_hash = str(imagehash.phash(img))
    except Exception:  # pas une image (vidéo ?) : on poursuit sur les octets
        pass

    if _IPTC_AI.search(data):
        finding.iptc_ai_marker = True
        finding.c2pa_ai_generated = True

    if _C2PA and not finding.c2pa_ai_generated:
        try:
            import c2pa as c2pa_lib
            result = c2pa_lib.Image.verify_data(data)
            finding.c2pa_ai_generated, finding.c2pa_camera_capture = interpret_c2pa(result)
        except Exception:  # noqa: BLE001 - pas de manifeste C2PA
            pass

    # --- Détecteur IA distant (optionnel) ----------------------------------
    if settings.ai_detector_url:
        try:
            headers = {}
            if settings.ai_detector_token:
                headers["Authorization"] = f"Bearer {settings.ai_detector_token}"
            payload = {"image": base64.b64encode(data).decode(), "media_type": item.kind}
            resp = await client.post(settings.ai_detector_url, json=payload, headers=headers)
            resp.raise_for_status()
            finding.ai_probability, finding.ai_detector_label = ai_probability_from(resp.json())
        except Exception:  # noqa: BLE001
            finding.ai_probability = None

    # --- Recherche inversée : apparitions antérieures ----------------------
    if settings.serpapi_api_key:
        try:
            for match in await _reverse_search(item, settings, client):
                link = match.get("link", "")
                if _excluded(link, post):
                    continue
                appearance = Appearance(url=link, title=match.get("title") or "")
                try:
                    archived = await _first_seen_wayback(link, client)
                    if archived:
                        appearance.first_seen = archived.first_seen
                        appearance.first_seen_source = "wayback"
                except Exception:  # noqa: BLE001 - Wayback indisponible
                    pass
                if appearance.url not in {a.url for a in finding.appearances}:
                    finding.appearances.append(appearance)
        except Exception:  # noqa: BLE001 - SerpApi indisponible
            pass

        dated = [a for a in finding.appearances if a.first_seen]
        if dated:
            finding.earliest_appearance = min(dated, key=lambda a: a.first_seen)

    return finding
