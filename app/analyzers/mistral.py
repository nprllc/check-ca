"""Parcours des réponses de l'API Mistral Conversations.

La réponse réelle a piègé le premier parseur : selon les cas,
``message.output.content`` est une liste de chunks (text / tool_reference)
ou une simple chaîne de caractères, et les pages réellement ouvertes par
le modèle apparaissent dans les entrées ``tool.execution`` (fonction
``open_url``), pas seulement dans les chunks ``tool_reference``.

Ce module fournit le parcours robuste commun aux modules claims,
fact-checks et propagation :
- textes : chunks « text » ou content brut (chaîne) ;
- références : chunks « tool_reference » + URLs ouvertes par ``open_url``.
"""
from __future__ import annotations

import json
import re

_JSON_BLOCK = re.compile(r"<json>\s*(\{.*?\})\s*</json>", re.DOTALL)


def _url_from_arguments(args) -> str:
    """URL ouverte par un open_url, depuis ses arguments (JSON en chaîne)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return ""
    if isinstance(args, dict):
        url = args.get("url") or ""
        return url if isinstance(url, str) else ""
    return ""


def extract_texts_and_refs(data: dict) -> tuple[list[str], dict[str, str]]:
    """(textes du modèle, pages réellement consultées : url → titre).

    Les pages consultées regroupent les chunks ``tool_reference`` (citations
    posées par le modèle) et les URLs ouvertes via ``open_url`` pendant la
    recherche — dans les deux cas, le modèle les a réellement vues.
    """
    texts: list[str] = []
    refs: dict[str, str] = {}
    for output in data.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        if output.get("type") == "tool.execution" and output.get("function") == "open_url":
            url = _url_from_arguments(output.get("arguments"))
            if url:
                refs.setdefault(url, "")
        if output.get("type") != "message.output":
            continue
        content = output.get("content")
        if isinstance(content, str):
            texts.append(content)  # réponse en une seule chaîne, pas de chunks
        elif isinstance(content, list):
            for chunk in content:
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("type") == "text" and chunk.get("text"):
                    texts.append(chunk["text"])
                elif chunk.get("type") == "tool_reference" and chunk.get("url"):
                    refs.setdefault(chunk["url"], chunk.get("title") or "")
    return texts, refs


def extract_json_block(texts: list[str]) -> dict:
    """Premier bloc <json>…</json> valide des textes, ou dict vide."""
    m = _JSON_BLOCK.search("\n".join(texts))
    if not m:
        return {}
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
