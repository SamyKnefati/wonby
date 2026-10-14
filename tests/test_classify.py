import pytest

from wonby import settings
from wonby.classify import (
    ClassificationConfig,
    LabeledRow,
    Pattern,
    classify,
    evaluate,
    load_classification_config,
    matches_any_signal,
)


def make_config(include, exclude, sample_signals=()):
    import re

    return ClassificationConfig(
        include=tuple(Pattern(p, p, re.compile(p, re.IGNORECASE)) for p in include),
        exclude=tuple(Pattern(p, p, re.compile(p, re.IGNORECASE)) for p in exclude),
        sample_signals=tuple(re.compile(p, re.IGNORECASE) for p in sample_signals),
        config_hash="test",
    )


# --- masking semantics ---------------------------------------------------------


def test_masking_excludes_without_includes_is_not_data():
    config = make_config(include=[r"\bdata\s+analytics?\b"], exclude=[r"\bdata\s*cent(re|er)s?\b"])
    result = classify("Comprehensive maintenance of the data centre", config)
    assert result.is_data is False
    assert result.matched_excludes == [r"\bdata\s*cent(re|er)s?\b"]
    assert result.matched_includes == []


def test_masking_leaves_remainder_for_includes():
    config = make_config(include=[r"\bdata\s+analytics?\b"], exclude=[r"\bdata\s*cent(re|er)s?\b"])
    result = classify("data centre and data analytics platform", config)
    assert result.is_data is True
    assert result.matched_includes == [r"\bdata\s+analytics?\b"]
    assert result.matched_excludes == [r"\bdata\s*cent(re|er)s?\b"]


def test_no_match_at_all():
    config = make_config(include=[r"\bdata\s+analytics?\b"], exclude=[])
    result = classify("Supply of stationery for the office", config)
    assert result.is_data is False
    assert result.matched_includes == []
    assert result.matched_excludes == []


def test_empty_description():
    config = make_config(include=[r"\bdata\b"], exclude=[])
    result = classify("", config)
    assert result.is_data is False
    result_none = classify(None, config)
    assert result_none.is_data is False


def test_multiple_excludes_each_reported():
    config = make_config(
        include=[r"\bdata\s+analytics?\b"],
        exclude=[r"\bscada\b", r"\bmobile\s+data\b"],
    )
    result = classify("SCADA system with mobile data telemetry", config)
    assert result.is_data is False
    assert set(result.matched_excludes) == {r"\bscada\b", r"\bmobile\s+data\b"}


# --- loading the real config ---------------------------------------------------


def test_load_real_classification_config():
    config = load_classification_config(settings.CLASSIFICATION_CONFIG_PATH)
    assert len(config.include) > 0
    assert len(config.exclude) > 0
    assert len(config.config_hash) == 16


def test_config_hash_stable_for_same_content(tmp_path):
    body = "include:\n  - pattern: 'foo'\n    why: test\nexclude: []\n"
    p1 = tmp_path / "a.yaml"
    p1.write_text(body)
    p2 = tmp_path / "b.yaml"
    p2.write_text(body + "\n# a trailing comment\n")
    c1 = load_classification_config(p1)
    c2 = load_classification_config(p2)
    assert c1.config_hash == c2.config_hash


def test_config_hash_changes_with_patterns(tmp_path):
    p1 = tmp_path / "a.yaml"
    p1.write_text("include:\n  - pattern: 'foo'\n    why: x\nexclude: []\n")
    p2 = tmp_path / "b.yaml"
    p2.write_text("include:\n  - pattern: 'bar'\n    why: x\nexclude: []\n")
    c1 = load_classification_config(p1)
    c2 = load_classification_config(p2)
    assert c1.config_hash != c2.config_hash


def test_load_config_sample_signals():
    config = load_classification_config(settings.CLASSIFICATION_CONFIG_PATH)
    assert len(config.sample_signals) > 0
    assert matches_any_signal("A new dashboard for reporting", config) is True
    assert matches_any_signal("Supply of office chairs", config) is False


# --- eval metrics ----------------------------------------------------------------


def test_evaluate_precision_recall_f1():
    config = make_config(include=[r"\bdata\s+analytics?\b"], exclude=[])
    labels = [
        LabeledRow("T1", "data analytics platform", True, ""),
        LabeledRow("T2", "data analytics for HR", True, ""),
        LabeledRow("T3", "generic IT support", False, ""),
        LabeledRow("T4", "data analytics but mislabeled as not data", False, ""),
        # Genuinely data work, but doesn't literally contain "data analytics"
        # adjacent — the regex misses it, hence a false negative.
        LabeledRow("T5", "predictive analytics using structured data warehouses", True, ""),
    ]
    result = evaluate(labels, config)
    assert result.size == 5
    assert result.true_positives == 2
    assert result.false_positives == 1
    assert result.false_negatives == 1
    assert result.true_negatives == 1
    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(2 / 3)
    assert result.f1 == pytest.approx(2 / 3)
    assert [r.tender_no for r in result.false_positive_rows] == ["T4"]
    assert [r.tender_no for r in result.false_negative_rows] == ["T5"]


def test_evaluate_empty_labels():
    config = make_config(include=[r"\bdata\b"], exclude=[])
    result = evaluate([], config)
    assert result.size == 0
    assert result.precision == 0.0
    assert result.recall == 0.0
    assert result.f1 == 0.0
