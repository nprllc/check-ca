"""Tests de bout en bout, toutes les API externes simulées (aucun réseau)."""
import asyncio
import re

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.pipeline import run_analysis
from tests.conftest import CLAUDE_RESPONSE, FACTCHECK_TEXT, SERP, TWEET, jpeg_bytes

URL = "https://x.com/infoflash/status/1987654321098765432"
FULL = Settings(anthropic_api_key="k", google_factcheck_api_key="g", serpapi_api_key="s",
                ai_detector_url="https://detector.example/models/m", db_path=":memory:")
NONE = Settings(anthropic_api_key="", google_factcheck_api_key="", serpapi_api_key="", ai_detector_url="", db_path=":memory:")


def mock_all(router, image: bytes):
    router.get("https://api.fxtwitter.com/infoflash/status/1987654321098765432").respond(json=TWEET)
    router.get(url__regex=r"https://factchecktools\.googleapis\.com/v1alpha1/claims:search.*").respond(json=FACTCHECK_TEXT)
    router.get(url__regex=r"https://factchecktools\.googleapis\.com/v1alpha1/claims:imageSearch.*").respond(json={})
    router.post("https://api.anthropic.com/v1/messages").respond(json=CLAUDE_RESPONSE)
    router.get("https://pbs.twimg.com/media/abc.jpg").respond(content=image, headers={"content-type": "image/jpeg"})
    router.post("https://detector.example/models/m").respond(json=[{"label": "artificial", "score": 0.12}, {"label": "human", "score": 0.88}])
    router.get(url__regex=r"https://serpapi\.com/search\.json.*").respond(json=SERP)

    def cdx(request):
        target = dict(httpx.QueryParams(request.url.query)).get("url", "")
        if "example-news.fr" in target:
            return httpx.Response(200, json=[["timestamp"], ["20180124093000"]])
        if "x.com" in target:
            return httpx.Response(200, json=[["timestamp"], ["20261107120000"]])
        return httpx.Response(200, json=[])
    router.get(url__regex=r"https://web\.archive\.org/cdx/search/cdx.*").mock(side_effect=cdx)


async def _run(settings, image):
    with respx.mock(assert_all_called=False) as router:
        mock_all(router, image)
        async with httpx.AsyncClient() as client:
            report = await run_analysis(URL, settings, client)
        return report, list(router.calls)


def test_full_pipeline_recycled_photo_and_false_claim():
    report, router = asyncio.run(_run(FULL, jpeg_bytes()))
    s = report.scores
    # Affirmations : principale contredite (0×2) + secondaire contredite (0) + non vérifiable (50) → 50/4 = 12,5 → 12
    # (le plafond de 15 lié au fact-check AFP « Faux » ne change rien ici, il est déjà dessous)
    assert [c.verdict for c in report.claims] == ["contradicted", "contradicted", "unverifiable"]
    assert report.factchecks[0].related_to_main_claim and report.factchecks[0].rating_class == "false"
    assert s.claim_solidity == 12 and s.claim_label == "Fortement douteux"
    # Image : 60 + 5 (détecteur 12 %) − 30 (en ligne depuis 2018) = 35
    m = report.media[0]
    assert m.earliest_appearance.url.startswith("https://www.example-news.fr/2018")
    assert m.earliest_appearance.first_seen.year == 2018 and m.earliest_appearance.first_seen_source == "wayback"
    assert all("1987654321098765432" not in a.url for a in m.appearances)  # le post lui-même est exclu
    assert s.media_authenticity == 35 and s.media_label == "Douteux"
    assert s.confidence == "high"
    kinds = [e.kind for e in report.timeline]
    assert kinds.index("media") < kinds.index("factcheck") < kinds.index("post")
    # La clé d'API Anthropic part bien dans l'en-tête, jamais dans l'URL
    call = next(c for c in router if c.request.url.host == "api.anthropic.com")
    assert call.request.headers["x-api-key"] == "k" and "k" not in str(call.request.url.query)


def test_ai_generated_file_is_capped():
    report, _ = asyncio.run(_run(FULL, jpeg_bytes(software="Midjourney v7")))
    assert report.media[0].exif_ai_software.lower() == "midjourney"
    assert report.scores.media_authenticity == 10


def test_iptc_marker_detected():
    report, _ = asyncio.run(_run(FULL, jpeg_bytes(xmp_ai=True)))
    assert report.media[0].c2pa_ai_generated and report.scores.media_authenticity == 10


def test_degraded_mode_without_keys():
    report, router = asyncio.run(_run(NONE, jpeg_bytes()))
    hosts = {c.request.url.host for c in router}
    assert "api.anthropic.com" not in hosts and "serpapi.com" not in hosts
    assert report.claims == [] and report.scores.claim_solidity is None
    assert report.scores.confidence == "low"
    assert any("non configuré" in l or "désactivée" in l for l in report.limits)


def test_api_endpoint_cache_and_permalink(monkeypatch):
    import app.main as main
    from app.store import Store
    monkeypatch.setattr(main, "settings", FULL)
    monkeypatch.setattr(main, "store", Store(":memory:"))
    with respx.mock(assert_all_called=False) as router:
        mock_all(router, jpeg_bytes())
        router.route(host="testserver").pass_through()
        c = TestClient(main.app)
        r1 = c.post("/api/analyze", json={"url": URL})
        assert r1.status_code == 200, r1.text
        rid = r1.json()["id"]
        n_calls = len(router.calls)
        r2 = c.post("/api/analyze", json={"url": "https://twitter.com/infoflash/status/1987654321098765432"})
        assert r2.json()["id"] == rid and len(router.calls) == n_calls  # servi depuis le cache
        assert c.get(f"/api/reports/{rid}").json()["id"] == rid
        assert c.get(f"/r/{rid}").status_code == 200
        assert c.get("/api/reports/inconnu").status_code == 404
        assert c.post("/api/analyze", json={"url": "ftp://x"}).status_code == 400


def test_deleted_post_returns_clear_error(monkeypatch):
    import app.main as main
    from app.store import Store
    monkeypatch.setattr(main, "settings", NONE)
    monkeypatch.setattr(main, "store", Store(":memory:"))
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.fxtwitter\.com/.*").respond(404, json={"code": 404, "message": "NOT_FOUND"})
        r = TestClient(main.app).post("/api/analyze", json={"url": URL})
    assert r.status_code == 422 and "introuvable" in r.json()["detail"]


def test_netguard_blocks_private_addresses():
    from app.netguard import UnsafeURL, assert_public_url
    import pytest
    for u in ("http://127.0.0.1/admin", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data", "http://[::1]/"):
        with pytest.raises(UnsafeURL):
            asyncio.run(assert_public_url(u))


def test_daily_cap(monkeypatch):
    import app.main as main
    from dataclasses import replace
    monkeypatch.setattr(main, "settings", replace(NONE, max_analyses_per_day=2, rate_limit_per_hour=99))
    main._daily.clear(); main._hits.clear()
    assert main._rate_limited("a") is None and main._rate_limited("b") is None
    assert "quota" in main._rate_limited("c")
    main._daily.clear()
