"""Repair ranking must measure error with the judge's electrical convention."""

from boardmodeler.authoring.harness import ProbeOutcome, _judge
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.authoring.validation_cache import progress_score


def _current_spec(statement: str = "Supply current") -> SpecSet:
    char = Characteristic(
        char_id="IQ",
        statement=statement,
        unit="A",
        min_value=None,
        max_value=0.02,
        typ_value=None,
        target=None,
        source_page=0,
        excerpt="Supply current, maximum 20 mA",
        req_class="numeric_limit",
        probe="circuit_measurement",
        probe_params={},
        not_testable_reason=None,
    )
    return SpecSet("PART", "PART", "document", (char,))


def _report(value: float):
    from types import SimpleNamespace

    outcome = ProbeOutcome(
        probe_id="circuit_measurement",
        status="FAIL",
        measured={"recipe_value": value},
        detail="",
        unknown_reason=None,
        run_dir="",
        char_ids=("IQ",),
    )
    return SimpleNamespace(outcomes=(outcome,))


def test_unsigned_current_repair_ranking_uses_the_judged_magnitude():
    spec = _current_spec()
    char = spec.by_id("IQ")
    assert _judge(char, "recipe_value", -0.01)[0] == "PASS"
    assert _judge(char, "recipe_value", -0.1)[0] == "FAIL"
    assert progress_score(_report(-0.01), spec) == (0, 1, 0)
    assert progress_score(_report(-0.1), spec) == (0, 1, 4)


def test_cited_current_direction_keeps_signed_comparison():
    spec = _current_spec("Current flows into the pin")
    char = spec.by_id("IQ")
    assert _judge(char, "recipe_value", -0.1)[0] == "PASS"
    assert progress_score(_report(-0.1), spec) == (0, 1, 0)
