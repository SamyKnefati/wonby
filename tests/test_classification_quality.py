"""Quality gate for the real config/classification.yaml against the labelled set.

Floors are set a few points below the precision/recall achieved at the time of
the tuning pass (precision=0.862, recall=0.931, f1=0.895, n=445 — see the
classifier-tuner report) so honest future relabelling doesn't make this flaky,
while still enforcing the business floors from CLAUDE.md (recall >= 0.90,
precision >= 0.85 — the recall floor below is exactly that target).
"""

from wonby import settings
from wonby.classify import evaluate, load_classification_config
from wonby.cli import _load_labels

PRECISION_FLOOR = 0.83
RECALL_FLOOR = 0.90


def test_real_config_meets_precision_recall_floors():
    labels = _load_labels(settings.LABELED_DESCRIPTIONS_CSV)
    assert len(labels) > 400, "labelled set shrank unexpectedly"

    config = load_classification_config(settings.CLASSIFICATION_CONFIG_PATH)
    result = evaluate(labels, config)

    assert result.precision >= PRECISION_FLOOR, (
        f"precision {result.precision:.3f} below floor {PRECISION_FLOOR} "
        f"(tp={result.true_positives} fp={result.false_positives})"
    )
    assert result.recall >= RECALL_FLOOR, (
        f"recall {result.recall:.3f} below floor {RECALL_FLOOR} "
        f"(tp={result.true_positives} fn={result.false_negatives})"
    )
