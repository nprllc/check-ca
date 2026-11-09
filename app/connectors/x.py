"""Connecteur X (Twitter) via l'API publique FxTwitter, non officielle."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from app.analyzers.media import parse_loose_date
from app.connectors.base import Connector, ConnectorError
from app.models import MediaItem, Post

_STATUS = re.compile(
    r"^/(?:i/)?(?:web/)?(?:[^/]+/)?status/(\d+)"  # /user/status/123 ou /i/web/status/123
)
_API = "https://api.fxtwitter.com"


def parse_status_url(url: str) -> tuple[str, str]:
    """Extrait (screen_name, status_id) d'une URL de post X, ou lève `ConnectorError`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConnectorError("URL invalide")
    host = parts.netloc.lower().removeprefix("www.")
    if not (host == "x.com" or host in ("twitter.com", "mobile.twitter.com")):
        raise ConnectorError(f"Domaine non géré : {parts.netloc}")
    m = _STATUS.match(parts.path)
    if not m:
        raise ConnectorError("Ce n'est pas un lien de post (il manque /status/<id>)")
    status_id = m.group(1)
    # /i/web/status/123 n'a pas de nom d'utilisateur : FxTwitter accepte i/web
    screen = parts.path.strip("/").split("/")
    if screen[0] in ("i",) or (len(screen) > 1 and screen[1] == "web"):
        user = "i"
    else:
        user = screen[0]
    return user, status_id


class XConnector(Connector):
    hosts = ("x.com", "www.x.com", "twitter.com", "mobile.twitter.com")

    def canonical(self, url: str) -> str:
        _, status_id = parse_status_url(url)
        return f"https://x.com/i/web/status/{status_id}"

    async def fetch(self, url: str, client: httpx.AsyncClient) -> Post:
        screen, status_id = parse_status_url(url)
        api_url = f"{_API}/{screen}/status/{status_id}"
        try:
            resp = await client.get(api_url, headers={"User-Agent": "check-ca/0.1 (+https://github.com/nprllc/check-ca)"})
        except httpx.HTTPError as exc:
            raise ConnectorError(f"X indisponible : {exc}") from exc
        if resp.status_code == 404:
            raise ConnectorError("Post X introuvable (supprimé ou privé)")
        if resp.status_code >= 400:
            raise ConnectorError(f"X a répondu {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ConnectorError("Réponse illisible de X") from exc
        if data.get("code") not in (None, 200) or "tweet" not in data:
            raise ConnectorError(f"Post X introuvable ({data.get('message', 'réponse inattendue')})")
        return _to_post(data["tweet"])


def _to_post(tweet: dict) -> Post:
    author = tweet.get("author") or {}
    published = None
    if tweet.get("created_timestamp"):
        published = datetime.fromtimestamp(int(tweet["created_timestamp"]), tz=timezone.utc)
    elif tweet.get("created_at"):
        published = parse_loose_date(tweet["created_at"]) or parse_twitter_date(tweet["created_at"])
    media = []
    photos = ((tweet.get("media") or {}).get("photos") or [])
    for photo in photos:
        media.append(MediaItem(kind="image", url=photo.get("url", ""),
                               width=photo.get("width"), height=photo.get("height")))
    videos = ((tweet.get("media") or {}).get("videos") or [])
    for video in videos:
        media.append(MediaItem(kind="video", url=video.get("url", ""),
                               width=video.get("width"), height=video.get("height")))
    return Post(
        platform="x",
        url=tweet.get("url") or "",
        canonical_url=tweet.get("url") or "",
        published_at=published,
        text=tweet.get("text") or "",
        author_name=author.get("name") or "",
        author_handle=author.get("screen_name") or "",
        author_followers=author.get("followers"),
        author_joined=parse_loose_date(author.get("joined") or ""),
        lang=tweet.get("lang") or "",
        likes=tweet.get("likes"),
        retweets=tweet.get("retweets"),
        replies=tweet.get("replies"),
        views=tweet.get("views"),
        media=media,
    )


def parse_twitter_date(value: str) -> datetime | None:
    """Format X : « Sat Nov 07 08:15:00 +0000 2026 »."""
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return None
