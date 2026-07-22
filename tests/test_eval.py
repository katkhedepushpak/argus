from eval import score

GROUND_TRUTH = {
    "key_terms": ["v2.4.0", "eviction", "OOMKill", "cache", "rollback"]
}

def test_all_terms_found():
    report = "v2.4.0 introduced a cache with no eviction policy, causing OOMKill. Rollback recommended."
    hits, misses = score(report, GROUND_TRUTH)
    assert len(hits) == 5
    assert len(misses) == 0

def test_partial_terms_found():
    report = "OOMKill events detected. Rollback to previous version."
    hits, misses = score(report, GROUND_TRUTH)
    assert "OOMKill" in hits
    assert "rollback" in hits
    assert "v2.4.0" in misses
    assert "eviction" in misses

def test_case_insensitive():
    report = "OOMKILL events and EVICTION policy missing in V2.4.0."
    hits, misses = score(report, GROUND_TRUTH)
    assert "OOMKill" in hits
    assert "eviction" in hits
    assert "v2.4.0" in hits

def test_empty_report_scores_zero():
    hits, misses = score("", GROUND_TRUTH)
    assert len(hits) == 0
    assert len(misses) == 5
