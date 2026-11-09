"""Connecteur générique : pages web via les balises Open Graph."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from app.analyzers.media import parse_loose_date
from app.connectors.base import Connector, ConnectorError
from app.models import MediaItem, Post

_TAG = re.compile(r"<meta\s[^>]*>", re.IGNORECASE)
_PROP = re.compile(r"property=[\"']([^\"']+)[\"']", re.IGNORECASE)
_CONT = re.compile(r"content=[\"']([^\"']*)[\"']", re.IGNORECASE)


def _og_tags(html: str) -> dict[str, str]:
    """Extrait les balises Open Graph, quel que soit l'ordre des attributs."""
    tags: dict[str, str] = {}
    for m in _TAG.finditer(html):
        pm = _PROP.search(m.group(0))
        if not pm:
            continue
        prop = pm.group(1).lower()
        if not (prop.startswith("og:") or prop == "article:published_time"):
            continue
        cm = _CONT.search(m.group(0))
        tags.setdefault(prop, cm.group(1) if cm else "")
    return tags


class GenericConnector(Connector):
    hosts = ()  # repli pour toute URL http(s) non gérée par un autre connecteur

    def handles(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    def canonical(self, url: str) -> str:
        parts = urlsplit(url)
        netloc = parts.netloc.lower().removeprefix("www.")
        path = parts.path.rstrip("/")
        return f"{parts.scheme}://{netloc}{path}"

    async def fetch(self, url: str, client: httpx.AsyncClient) -> Post:
        try:
            resp = await client.get(url, headers={"User-Agent": "check-ca/0.1"}, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Page inaccessible : {exc}") from exc
        if resp.status_code >= 400:
            raise ConnectorError(f"La page a répondu {resp.status_code}")
        if "text/html" not in (resp.headers.get("content-type") or ""):
            raise ConnectorError("Seules les pages HTML sont analysées")
        tags = _og_tags(resp.text)
        media = []
        image = tags.get("og:image")
        if image:
            media.append(MediaItem(kind="image", url=image))
        return Post(
            platform="web",
            url=str(resp.url),
            canonical_url=url,
            published_at=parse_loose_date(tags.get("article:published_time") or tags.get("og:article:published_time") or ""),
            title=tags.get("og:title") or re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL).group(1).strip() if re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL) else "",
            text=tags.get("og:description") or "",
            author_name=tags.get("og:site_name") or "",
            site_name=tags.get("og:site_name") or "",
            lang=tags.get("og:locale") or "",
            media=media,
        )
