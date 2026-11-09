from datetime import datetime, timezone

import pytest

from app.analyzers.claims import parse_response
from app.analyzers.factchecks import classify_rating
from app.analyzers.media import ai_probability_from, interpret_c2pa, parse_loose_date
from app.connectors.base import ConnectorError
from app.connectors.x import XConnector, parse_status_url
from app.models import Appearance, Claim, FactCheck, MediaFinding, MediaItem, Post, Source
from app.scoring import compute_scores, label, score_claims, score_media_item
from tests.conftest import CLAUDE_RESPONSE


@pytest.mark.parametrize("url,expected", [
    ("https://x.com/infoflash/status/1987654321098765432", ("infoflash", "1987654321098765432")),
    ("https://twitter.com/a_b/status/123456789?s=20", ("a_b", "123456789")),
    ("https://mobile.twitter.com/0xRicker/status/2035334040216113631", ("0xRicker", "2035334040216113631")),
    ("https://x.com/i/web/status/123456789", ("i", "123456789")),
])
def test_parse_status_url(url, expected):
    assert parse_status_url(url) == expected


def test_parse_status_url_rejects():
    with pytest.raises(ConnectorError):
        parse_status_url("https://x.com/infoflash")


def test_canonical_merges_domains():
    c = XConnector()
    assert c.canonical("https://twitter.com/a/status/42424242") == c.canonical("https://x.com/b/status/42424242?s=1")


@pytest.mark.parametrize("rating,cls", [
    ("Faux", "false"), ("False", "false"), ("Trompeur", "false"), ("Sorti de son contexte", "false"),
    ("Partiellement faux", "mixed"), ("Missing context", "mixed"), ("Vrai", "true"), ("Plausible", "unknown"),
])
def test_classify_rating(rating, cls):
    assert classify_rating(rating) == cls


def test_claims_filter_invented_sources():
    summary, claims, related = parse_response(CLAUDE_RESPONSE)
    assert len(claims) == 3 and claims[0].importance == "main"
    # la 3e affirmation citait une URL jamais consultée : elle redevient « non vérifiable »
    assert claims[2].sources == [] and claims[2].verdict == "unverifiable"
    assert related == ["https://factuel.afp.com/tour-eiffel-crue-2018"]


def test_ai_probability_labels():
    assert ai_probability_from([{"label": "artificial", "score": 0.9}, {"label": "human", "score": 0.1}]) == (0.9, "artificial")
    assert ai_probability_from([[{"label": "real", "score": 0.7}]])[0] == pytest.approx(0.3)
    assert ai_probability_from({"error": "x"}) == (None, None)


def test_interpret_c2pa():
    ai = {"manifests": {"m": {"assertions": [{"data": {"actions": [{"digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}]}}]}}}
    cam = {"manifests": {"m": {"assertions": [{"data": {"actions": [{"digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"}]}}]}}}
    bad = dict(cam, validation_status=[{"code": "assertion.dataHash.mismatch"}])
    assert interpret_c2pa(ai) == (True, False)
    assert interpret_c2pa(cam) == (False, True)
    assert interpret_c2pa(bad) == (False, False)
    assert interpret_c2pa(None) == (False, False)


def test_loose_dates():
    assert parse_loose_date("Mar 3, 2019").year == 2019
    assert parse_loose_date("2019-03-03T10:00:00Z").tzinfo is not None
    assert parse_loose_date("il y a 3 jours") is None


POST = Post(platform="x", url="u", canonical_url="u", published_at=datetime(2026, 11, 7, tzinfo=timezone.utc))


def _finding(**kw):
    return MediaFinding(media=MediaItem(kind="image", url="https://i/x.jpg"), **kw)


def test_media_score_neutral_and_recycling():
    assert score_media_item(_finding(), POST, False)[0] == 60
    old = Appearance(url="https://n/a/b", first_seen=datetime(2018, 1, 1, tzinfo=timezone.utc))
    assert score_media_item(_finding(earliest_appearance=old), POST, False)[0] == 30
    recent = Appearance(url="https://n/a/b", first_seen=datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert score_media_item(_finding(earliest_appearance=recent), POST, False)[0] == 60  # < 7 jours : pas de recyclage


def test_media_score_caps():
    assert score_media_item(_finding(c2pa_camera_capture=True, ai_probability=0.1), POST, False)[0] == 90
    assert score_media_item(_finding(c2pa_camera_capture=True, c2pa_ai_generated=True), POST, False)[0] == 10
    assert score_media_item(_finding(exif_ai_software="Midjourney"), POST, False)[0] == 10
    assert score_media_item(_finding(c2pa_camera_capture=True), POST, True)[0] == 15
    assert score_media_item(_finding(error="x"), POST, False)[0] is None


def test_claim_score_weights_and_cap():
    src = [Source(url="https://s")]
    claims = [Claim(text="a", importance="main", verdict="supported", sources=src),
              Claim(text="b", verdict="contradicted", sources=src)]
    assert score_claims(claims, [])[0] == round((100 * 2 + 0) / 3)  # 67
    fc = FactCheck(rating="Faux", rating_class="false", related_to_main_claim=True)
    assert score_claims(claims, [fc])[0] == 15
    unrelated = FactCheck(rating="Faux", rating_class="false", related_to_main_claim=False)
    assert score_claims(claims, [unrelated])[0] == 67
    assert score_claims([], [])[0] is None


def test_labels():
    assert [label(x) for x in (0, 30, 31, 50, 51, 70, 71, 85, 86, 100, None)] == [
        "Fortement douteux", "Fortement douteux", "Douteux", "Douteux", "Incertain", "Incertain",
        "Plutôt fiable", "Plutôt fiable", "Fiable", "Fiable", "Non applicable"]


def test_low_confidence_hides_numbers_behind_label():
    from app.models import ModuleStatus
    mods = [ModuleStatus(name="fact-checks", ok=False), ModuleStatus(name="affirmations", ok=False),
            ModuleStatus(name="médias", ok=True)]
    s = compute_scores(POST, [], [], [_finding()], mods)
    assert s.confidence == "low" and s.media_label == "Indéterminé"
