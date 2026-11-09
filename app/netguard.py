"""Protection SSRF : on ne télécharge jamais une URL qui pointe vers un réseau interne."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURL(Exception):
    """URL refusée (schéma non autorisé ou adresse non publique)."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def assert_public_url(url: str) -> None:
    """Vérifie que l'URL est http(s) et que toutes ses adresses IP sont publiques.

    Lève ``UnsafeURL`` sinon. La résolution DNS est faite à chaque appel
    pour éviter les pièges du type DNS rebinding (le domaine est revérifié
    juste avant le téléchargement réel par le pipeline).
    """
    if not url or not isinstance(url, str):
        raise UnsafeURL("URL vide")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"Schéma non autorisé : {parts.scheme or '?'} (http et https seulement)")
    host = parts.hostname
    if not host:
        raise UnsafeURL("URL sans nom d'hôte")
    if host.lower() in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeURL("Hôte local refusé")

    def _resolve():
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
        return {info[4][0] for info in infos}

    try:
        addresses = await asyncio.to_thread(_resolve)
    except socket.gaierror as exc:  # pragma: no cover - dépend du réseau
        raise UnsafeURL(f"Nom d'hôte introuvable : {host}") from exc
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise UnsafeURL(f"Adresse non interprétable : {addr}")
        if not _is_public(ip):
            raise UnsafeURL(f"Adresse non publique refusée : {addr}")
