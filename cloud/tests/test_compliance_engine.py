"""Compliance engine — scope-aware coverage aggregation (app Python 3.14).

Tests ``_derive`` (the heart of the "2.0-scope-aware" scoring): coverage num/den,
unknown/expired never counts as met, no-evidence capabilities are excluded (not
tanked), and a healthy core signal can't conceal a failed integration scope.
Run: ``pytest cloud/tests``.
"""

from cloud.app.compliance.engine import _derive


class _Ev:
    """Minimal evidence stand-in matching exactly what ``_derive`` reads."""

    def __init__(self, status, expected=0, covered=0, failed=0, evidence_level="", entities=None):
        self.status = status
        self.expected = expected
        self.covered = covered
        self.failed = failed
        self.evidence_level = evidence_level
        self.entities = entities or []


def test_binary_met_is_operating():
    state, score, _kept, _cov = _derive(["a"], {"a": [_Ev("met")]})
    assert score == 100
    assert state == "operating"


def test_population_coverage_scores_proportionally():
    state, score, _kept, cov = _derive(["a"], {"a": [_Ev("partial", expected=10, covered=6, failed=4)]})
    assert score == 60
    assert cov["expected"] == 10 and cov["covered"] == 6 and cov["failed"] == 4
    assert state != "operating"


def test_unknown_is_penalized_not_met():
    _state, score, _kept, cov = _derive(["a"], {"a": [_Ev("unknown")]})
    assert score == 0
    assert cov["unknown"] == 1


def test_no_evidence_capability_is_excluded():
    # 'b' has no provider evidence — it must be excluded, not tank the score.
    _state, score, _kept, _cov = _derive(["a", "b"], {"a": [_Ev("met")]})
    assert score == 100


def test_all_caps_without_evidence_is_not_assessed():
    state, _score, _kept, _cov = _derive(["a", "b"], {})
    assert state == "not_assessed"


def test_failed_scope_cannot_be_concealed_by_healthy_core():
    # Same capability: a met binary core signal + a failed M365 population.
    # Coverage = (1 + 0) / (1 + 5) → well below met.
    state, score, _kept, cov = _derive(
        ["a"], {"a": [_Ev("met"), _Ev("unmet", expected=5, covered=0, failed=5)]})
    assert score < 100
    assert state != "operating"
    assert cov["failed"] == 5
