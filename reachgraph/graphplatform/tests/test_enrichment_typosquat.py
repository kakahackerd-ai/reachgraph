from graphplatform import schema
from graphplatform.enrichment.typosquat import TyposquatService, _best_match, _levenshtein


def test_levenshtein_basic():
    assert _levenshtein("kitten", "sitting") == 3
    assert _levenshtein("lodash", "lodash") == 0
    assert _levenshtein("", "abc") == 3


def test_separator_and_case_match():
    score, method = _best_match("left_pad", "left-pad")
    assert method == "separator_or_case"
    assert score >= 0.9


def test_homoglyph_match():
    score, method = _best_match("1odash", "lodash")  # digit 1, not letter l
    assert method == "homoglyph"
    assert score >= 0.85


def test_keyboard_adjacent_match():
    # 'd' is QWERTY-adjacent to 'e' -- "dxpress" vs "express": e->d substitution
    score, method = _best_match("dxpress", "expres5".replace("5", "s"))
    assert method == "keyboard_adjacent"


def test_edit_distance_scales_with_length_to_avoid_short_name_noise():
    # "six" vs "fix": edit distance 1, but len 3 < the length-4 floor --
    # must NOT be flagged, this is exactly the false-positive case the
    # length-scaled threshold exists to avoid.
    assert _best_match("fix", "six") is None
    # a longer, still edit-distance-1 pair of real-shaped names should fire.
    assert _best_match("reqeusts", "requests") is not None


def test_exact_match_is_never_flagged_as_typosquat_of_itself():
    assert _best_match("lodash", "lodash") is None


def test_run_once_flags_real_ingested_packages(service, cleanup, run_id):
    import datetime as dt

    ts = TyposquatService(service, popular={"npm": [f"realpkg-{run_id}"]})
    now = dt.datetime.now(dt.timezone.utc)
    popular_key = f"npm:realpkg-{run_id}"
    # deliberately one extra doubled character -- close enough to trip the
    # length-scaled edit-distance check, not identical.
    squat_name = f"realpkgg-{run_id}"
    squat_key = f"npm:{squat_name}"

    service.upsert_package(popular_key, "npm", f"realpkg-{run_id}", first_observed_at=now, event_time=now)
    service.upsert_package(squat_key, "npm", squat_name, first_observed_at=now, event_time=now)
    cleanup(schema.PACKAGE, popular_key)
    cleanup(schema.PACKAGE, squat_key)

    flagged = ts.run_once("npm")
    matches = [f for f in flagged if f["candidate"] == squat_key]
    assert matches, f"expected {squat_key} to be flagged, got {flagged}"
    assert matches[0]["popular"] == popular_key

    rows = service._run(
        "MATCH (a:Package {key:$a})-[r:POSSIBLE_TYPOSQUAT_OF]->(b:Package {key:$b}) RETURN r.similarity_score AS score, r.method AS method",
        a=squat_key,
        b=popular_key,
        consistency="strong",
    )
    assert len(rows) == 1
    assert rows[0]["method"] == "edit_distance"
