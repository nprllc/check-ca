"""Connecteurs : chaque plateforme sait transformer une URL en `Post`."""
from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from app.models import Post


class ConnectorError(Exception):
    """Erreur de récupération d'un post (URL invalide, post introuvable…)."""


class Connector(ABC):
    """Interface commune à tous les connecteurs."""

    #: préfixes d'URL gérés (comparés en minuscules)
    hosts: tuple[str, ...] = ()

    def handles(self, url: str) -> bool:
        host = url.split("/", 3)
        return len(host) >= 3 and host[2].lower() in self.hosts

    @abstractmethod
    def canonical(self, url: str) -> str:
        """URL canonique, stable pour le cache (indépendante des paramètres)."""

    @abstractmethod
    async def fetch(self, url: str, client: httpx.AsyncClient) -> Post:
        """Récupère le post ; lève `ConnectorError` en cas d'échec."""
