"""Recherche de propagation : qui a partagé ce contenu en premier,
et ce que le web établit sur chaque image.

Deuxième appel à l'API Mistral Conversations avec l'outil intégré
`web_search`. Rend :
- la première publication connue d'au moins une des images du post
  (jamais le post analysé lui-même, sauf si aucun partage antérieur
  n'est repéré : il est alors le premier partage connu à ce jour) ;
- les autres partages repérés (compte, date, plateforme) ;
- un verdict web par image (authentique, générée par IA, détournée,
  démentie…) avec la source réellement consultée qui le démontre.

Règles éditoriales (décisions produit) :
- le silence des médias n'est jamais un argument : le fait que des
  grands médias aient ou non relayé un contenu n'entre pas en ligne de
  compte ;
- si rien n'est établi, on ne dit rien : pas de formule d'attente
  (« impossible de trancher », « à suivre »…) dans le résumé ;
- on n'invente jamais de source : seules les pages réellement
  consultées pendant la recherche sont citées.

Ce module n'écrit jamais dans les champs utilisés par ``app/scoring.py`` :
les verdicts web sont purement informatifs (affichés, non scorés).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from app.analyzers.media import _excluded, parse_loose_date
from app.analyzers.mistral import call_conversations, extract_json_block, extract_texts_and_refs
from app.config import Settings
from app.models import MediaFinding, Post, TimelineEvent

_JSON_BLOCK = re.compile(r"<json>\s*(\{.*?\})\s*</json>", re.DOTALL)

_VERDICTS = {"authentic", "ai_generated", "misrepresented", "refuted", "unknown"}

# Formules d'attente : on les retire du résumé ; si tout le résumé n'en
# était fait, il devient vide — on ne dit rien plutôt que d'attendre.
_HEDGES = re.compile(
    r"(impossible\s+(de\s+|d')\s*(trancher|conclure|d[eé]terminer|[eé]tablir)"
    r"|pas\s+(assez\s+)?d'[eé]l[eé]ments"
    r"|ne\s+permet\s+(pas|en\s+pas)\s+de\s+conclure"
    r"|sans\s+conclusion|aucune\s+conclusion"
    r"|[àa]\s+suivre|[àa]\s+traiter\s+avec\s+prudence"
    r"|ind[eé]termin[eé])", re.IGNORECASE)

# Le silence médiatique n'est jamais un argument : ces phrases sautent.
_MEDIA_SILENCE = re.compile(
    r"(m[ée]dias?[^.!?]{0,80}(n'ont|n’ont|ne\s+(l')?ont|n'a\s+pas)[^.!?]{0,80}"
    r"(relay|reparl|couvert|mentionn|parl)"
    r"|silence\s+des\s+m[ée]dias)", re.IGNORECASE)

INSTRUCTIONS = """Tu es enquêteur de propagation pour « Check ça », une plateforme d'aide \
à la vérification en français. On te donne un post (texte, auteur, date) et \
la liste de ses images (URLs exactes). Cherche sur le web :

1. QUI A PARTAGÉ CES IMAGES EN PREMIER : le premier compte connu à avoir \
publié au moins une de ces images sur Internet. Le post analysé lui-même ne \
compte pas comme première publication, sauf si ta recherche ne repère aucun \
partage antérieur : dans ce cas, la publication analysée est le premier \
partage connu à ce jour.
2. Les autres partages repérés de ces images (comptes, dates, plateformes).
3. Pour chaque image : ce que le web établit — image authentique, image \
générée par IA, image détournée ou hors contexte, ou image démentie par un \
fact-checkeur — avec la page consultée qui le démontre.

Règles strictes :
- N'utilise JAMAIS le silence des médias comme argument ou indice : le fait \
que des grands médias aient ou non relayé ou couvert le contenu n'entre pas \
en ligne de compte.
- Si tu ne peux rien établir de solide sur un point, n'en parle pas : rends \
une chaîne vide plutôt qu'une formule d'attente (« impossible de trancher », \
« à suivre », « à traiter avec prudence »…). Aucune phrase de remplissage.
- Ne cite que des pages que tu as réellement consultées pendant la recherche.
- Les dates sont au format YYYY-MM-DD quand elles sont connues.

Termine par un bloc <json>…</json> exactement au format :
<json>{
  "summary": "2 à 4 phrases factuelles établies par la recherche, ou chaîne vide",
  "shares": [
    {"platform": "x", "author": "@compte", "url": "https://…",
     "date": "YYYY-MM-DD", "note": "ce que ce partage établit (facultatif)"}
  ],
  "image_assessments": [
    {"url": "URL exacte d'une image du post, reprise telle quelle",
     "verdict": "authentic | ai_generated | misrepresented | refuted | unknown",
     "explanation": "1 à 2 phrases, avec date et acteur si connus",
     "source_url": "page consultée qui le démontre (facultatif)"}
  ]
}</json>"""


def _normalize(url: str) -> str:
    """Clé de comparaison entre URLs (host en minuscules, sans query)."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def clean_summary(summary: str) -> str:
    """Retire les formules d'attente et les arguments de silence médiatique.

    Si le résumé restant est trop court pour apporter quelque chose, il
    devient vide : on préfère ne rien dire plutôt que d'attendre.
    """
    if not summary:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", summary.strip())
    kept = []
    for sentence in sentences:
        if not sentence.strip():
            continue
        if _HEDGES.search(sentence) or _MEDIA_SILENCE.search(sentence):
            continue
        kept.append(sentence.strip())
    result = " ".join(kept).strip()
    if len(result) < 30:
        return ""
    return result


def _parse_date(value) -> datetime | None:
    """Date consciente (UTC si naïve), ou None — la chronologie trie des
    dates conscientes et naïves mélangées, ce qui planterait sinon."""
    parsed = parse_loose_date(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _share_events(shares: list[dict], post: Post, consulted: dict[str, str]) -> tuple[list[TimelineEvent], bool]:
    """Événements « partage » depuis la réponse du modèle.

    Le post analysé lui-même n'apparaît jamais comme un partage repéré :
    soit un partage antérieur existe (premier partageur), soit le post est
    le premier partage connu à ce jour — et c'est dit comme tel.
    Seuls les partages réellement apparus pendant la recherche sont retenus.
    """
    events: list[TimelineEvent] = []
    seen: set[str] = set()
    consulted_keys = {_normalize(u) for u in consulted}
    for share in shares:
        if not isinstance(share, dict):
            continue
        url = (share.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or _excluded(url, post):
            continue
        if _normalize(url) not in consulted_keys:
            continue  # URL jamais consultée : on ne présente pas ce partage
        key = _normalize(url)
        if key in seen:
            continue
        seen.add(key)
        events.append(TimelineEvent(
            kind="share",
            date=_parse_date(share.get("date")),
            title="Partage repéré",
            url=url,
            detail=" ".join(filter(None, [
                f"{(share.get('author') or '').strip()} sur {(share.get('platform') or '?').strip()}",
                (share.get("note") or "").strip(),
            ])),
        ))
    dated = [e for e in events if e.date is not None]
    post_date = post.published_at
    post_is_first = not any(e.date and post_date and e.date < post_date for e in dated)
    if post_date is None and dated:
        post_is_first = False  # dates de post inconnue : on ne peut pas trancher
    for event in events:
        event.title = "Autre partage repéré"
    if dated:
        dated.sort(key=lambda e: e.date)
        events = dated + [e for e in events if e.date is None]
    if post_is_first:
        # Aucun partage antérieur au post analysé n'a été repéré : c'est lui,
        # le premier partage connu à ce jour de ce contenu.
        events.insert(0, TimelineEvent(
            kind="share",
            date=post_date,
            title="Premier partage connu à ce jour : la publication analysée",
            url=post.url or post.canonical_url,
            detail="Aucun partage antérieur de ces images repéré sur le web à ce jour",
        ))
    elif dated:
        dated[0].title = "Premier partage repéré"
    return events, post_is_first


def parse_mistral_response(data: dict, post: Post) -> tuple[str, list[TimelineEvent], list[dict]]:
    """Extraction depuis une réponse Mistral Conversations.

    Rend (résumé nettoyé, événements de partage, verdicts web par image).
    Seuls les partages et les sources réellement consultés sont retenus ;
    les URLs non consultées sont rejetées — on n'invente pas de source.
    Le parcours (content en liste de chunks ou en chaîne brute, URLs
    ouvertes via open_url) est géré par app.analyzers.mistral.
    """
    texts, consulted = extract_texts_and_refs(data)
    full = "\n".join(texts)
    block = extract_json_block(texts)
    json_text = ""
    m = _JSON_BLOCK.search(full)
    if m:
        json_text = m.group(0)
    summary = clean_summary((block.get("summary") or "") or full.replace(json_text, " "))
    events, _ = _share_events(block.get("shares") or [], post, consulted)
    consulted_keys = {_normalize(u): u for u in consulted}
    assessments: list[dict] = []
    for item in block.get("image_assessments") or []:
        if not isinstance(item, dict):
            continue
        verdict = (item.get("verdict") or "").strip().lower()
        if verdict not in _VERDICTS or verdict == "unknown":
            continue
        source = (item.get("source_url") or "").strip()
        verified_source = consulted_keys.get(_normalize(source), "")
        assessments.append({
            "url": (item.get("url") or "").strip(),
            "verdict": verdict,
            "explanation": (item.get("explanation") or "").strip(),
            "source_url": verified_source,
            "source_title": consulted.get(verified_source, "") if verified_source else "",
        })
    return summary, events, assessments


def apply_image_assessments(media: list[MediaFinding], assessments: list[dict]) -> None:
    """Rattache chaque verdict web à son image : par URL exacte, puis par
    chemin, puis par nom de fichier. Les verdicts sans image restante
    sont ignorés. Aucun champ scorisé n'est modifié."""
    if not assessments:
        return
    remaining = list(assessments)
    # 1) URL exacte, 2) chemin, 3) nom de fichier.
    for finding in media:
        url = finding.media.url
        exact = next((a for a in remaining if a["url"] == url), None)
        if exact is None:
            path = urlsplit(url).path
            near = next((a for a in remaining
                         if urlsplit(a["url"]).path == path), None)
            if near is None:
                name = path.rsplit("/", 1)[-1]
                near = next((a for a in remaining
                             if urlsplit(a["url"]).path.rsplit("/", 1)[-1] == name), None)
            exact = near
        if exact is not None:
            finding.web_verdict = exact["verdict"]
            finding.web_explanation = exact["explanation"]
            finding.web_source_url = exact["source_url"]
            finding.web_source_title = exact["source_title"]
            remaining.remove(exact)


def _post_context(post: Post) -> str:
    """Description du post et de ses images pour la recherche."""
    media_desc = "\n".join(f"- {m.url}" for m in post.media) or "aucun"
    return (f"Post analysé : {post.url}\n"
            f"Plateforme : {post.platform}\n"
            f"Auteur : {post.author_name or post.author_handle or 'inconnu'}\n"
            f"Publié le : {post.published_at.isoformat() if post.published_at else 'date inconnue'}\n"
            f"Texte : {post.text or post.title or '(sans texte)'}\n"
            f"Images du post (URLs exactes, à reprendre telles quelles dans "
            f"image_assessments) :\n{media_desc}")


async def analyze_propagation(post: Post, settings: Settings,
                              client: httpx.AsyncClient) -> tuple[str, list[TimelineEvent], list[dict]]:
    """Recherche de propagation via l'API Conversations Mistral."""
    data = await call_conversations(client, settings, INSTRUCTIONS,
                                    _post_context(post))
    return parse_mistral_response(data, post)
