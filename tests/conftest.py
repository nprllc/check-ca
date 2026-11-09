import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CHECKCA_DB", ":memory:")


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Les tests n'accèdent jamais au réseau : on neutralise la résolution DNS."""
    async def ok(url):
        return None
    import app.analyzers.media as media
    import app.main as main
    monkeypatch.setattr(media, "assert_public_url", ok)
    monkeypatch.setattr(main, "assert_public_url", ok)


def jpeg_bytes(software: str | None = None, xmp_ai: bool = False) -> bytes:
    img = Image.new("RGB", (64, 48), (120, 140, 160))
    for x in range(64):
        img.putpixel((x, x % 48), (250, 20, 20))
    buf = io.BytesIO()
    exif = Image.Exif()
    if software:
        exif[0x0131] = software  # Software
    img.save(buf, "JPEG", exif=exif.tobytes() if software else b"")
    data = buf.getvalue()
    if xmp_ai:
        data += b'<x:xmpmeta><Iptc4xmpExt:DigitalSourceType>http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia</Iptc4xmpExt:DigitalSourceType></x:xmpmeta>'
    return data


TWEET = {
    "code": 200,
    "message": "OK",
    "tweet": {
        "url": "https://x.com/infoflash/status/1987654321098765432",
        "id": "1987654321098765432",
        "text": "URGENT : la tour Eiffel a été fermée hier soir après l'inondation de Paris. Photo prise ce matin.",
        "author": {"name": "Info Flash", "screen_name": "infoflash", "followers": 48200,
                   "joined": "Tue Mar 02 10:00:00 +0000 2021"},
        "created_at": "Sat Nov 07 08:15:00 +0000 2026",
        "created_timestamp": 1794039300,
        "likes": 15400, "retweets": 6100, "replies": 820, "views": 2100000,
        "lang": "fr",
        "media": {"photos": [{"url": "https://pbs.twimg.com/media/abc.jpg", "width": 64, "height": 48}]},
    },
}

CLAUDE_RESPONSE = {
    "content": [
        {"type": "text", "text": "Je vérifie."},
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "tour Eiffel fermée inondation"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
            {"type": "web_search_result", "url": "https://www.toureiffel.paris/fr/actualites", "title": "Actualités — Tour Eiffel"},
            {"type": "web_search_result", "url": "https://www.vigicrues.gouv.fr/", "title": "Vigicrues"},
        ]},
        {"type": "text", "text": "Analyse terminée.\n<json>{\"summary\": \"Le post affirme une fermeture de la tour Eiffel due à une inondation ; aucune source officielle ne la confirme et la photo circule depuis 2018.\", "
                                 "\"claims\": [{\"text\": \"La tour Eiffel a été fermée hier soir\", \"importance\": \"main\", \"verdict\": \"contradicted\", \"explanation\": \"Le site officiel annonce une ouverture normale.\", \"sources\": [{\"url\": \"https://www.toureiffel.paris/fr/actualites\", \"title\": \"Actualités — Tour Eiffel\"}]}, "
                                 "{\"text\": \"Paris est inondée\", \"importance\": \"secondary\", \"verdict\": \"contradicted\", \"explanation\": \"Vigicrues ne signale aucune crue de la Seine.\", \"sources\": [{\"url\": \"https://www.vigicrues.gouv.fr/\", \"title\": \"Vigicrues\"}]}, "
                                 "{\"text\": \"La photo a été prise ce matin\", \"importance\": \"secondary\", \"verdict\": \"mixed\", \"explanation\": \"Source inventée pour tester le filtre.\", \"sources\": [{\"url\": \"https://inventee.example/fake\", \"title\": \"?\"}]}], "
                                 "\"related_factcheck_urls\": [\"https://factuel.afp.com/tour-eiffel-crue-2018\"]}</json>"},
    ],
}

FACTCHECK_TEXT = {"claims": [{
    "text": "Photo de la tour Eiffel les pieds dans l'eau",
    "claimReview": [{"publisher": {"name": "AFP Factuel", "site": "factuel.afp.com"},
                     "url": "https://factuel.afp.com/tour-eiffel-crue-2018",
                     "title": "Cette photo date de la crue de 2018",
                     "reviewDate": "2024-06-02T00:00:00Z", "textualRating": "Faux"}]}]}

SERP = {"exact_matches": [
    {"title": "Crue de la Seine : les images", "link": "https://www.example-news.fr/2018/01/crue-seine-images", "source": "example-news.fr"},
    {"title": "Repost", "link": "https://x.com/infoflash/status/1987654321098765432", "source": "x.com"},
]}
