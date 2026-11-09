"""Registre des connecteurs : le premier qui reconnaît l'URL gagne.

Pour ajouter une plateforme (TikTok, Instagram, YouTube…), créer une classe
`Connector`, l'ajouter à `CONNECTORS` avant `GenericConnector` (le repli).
"""
from app.connectors.base import Connector, ConnectorError
from app.connectors.generic import GenericConnector
from app.connectors.x import XConnector

CONNECTORS: list[Connector] = [
    XConnector(),
    # Nouveaux connecteurs ici (TikTok, Instagram, YouTube…)
    GenericConnector(),
]


def pick_connector(url: str) -> Connector:
    for connector in CONNECTORS:
        if connector.handles(url):
            return connector
    raise ConnectorError("Aucun connecteur ne gère cette URL")
