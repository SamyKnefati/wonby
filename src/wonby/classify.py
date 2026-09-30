"""Pure classification engine — no I/O, no DuckDB.

Config format and masking semantics: CLAUDE.md > Business rules > "Data" tenders.
Config *content* (the keyword lists) is owned by the classifier-tuner; this
module owns the format and the engine that reads it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Pattern:
    raw: str
    why: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class ClassificationConfig:
    include: tuple[Pattern, ...]
    exclude: tuple[Pattern, ...]
    sample_signals: tuple[re.Pattern[str], ...]
    config_hash: str


@dataclass(frozen=True)
class ClassifyResult:
    is_data: bool
    matched_includes: list[str]
    matched_excludes: list[str]


def _compile_patterns(entries: list[dict[str, Any]]) -> tuple[Pattern, ...]:
    return tuple(
        Pattern(
            raw=entry["pattern"],
            why=entry.get("why", ""),
            regex=re.compile(entry["pattern"], re.IGNORECASE),
        )
        for entry in entries
    )


def _config_hash(include: tuple[Pattern, ...], exclude: tuple[Pattern, ...]) -> str:
    """Stable hash over the parts of the config that affect `classify()`'s
    output (not `sample_signals`, not comments/whitespace in the YAML file)."""
    payload = json.dumps(
        {
            "include": [p.raw for p in include],
            "exclude": [p.raw for p in exclude],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_classification_config(path: Path) -> ClassificationConfig:
    data = yaml.safe_load(path.read_text()) or {}
    include = _compile_patterns(data.get("include") or [])
    exclude = _compile_patterns(data.get("exclude") or [])
    sample_signals = tuple(
        re.compile(p, re.IGNORECASE) for p in (data.get("sample_signals") or [])
    )
    return ClassificationConfig(
        include=include,
        exclude=exclude,
        sample_signals=sample_signals,
        config_hash=_config_hash(include, exclude),
    )


def classify(description: str | None, config: ClassificationConfig) -> ClassifyResult:
    """Exclude patterns mask the text they match; include patterns then run on
    what's left. A tender is "data" iff at least one include pattern matches
    the masked text."""
    text = description or ""

    spans: list[tuple[int, int]] = []
    matched_excludes: list[str] = []
    for pattern in config.exclude:
        matched = False
        for m in pattern.regex.finditer(text):
            if m.end() > m.start():
                spans.append((m.start(), m.end()))
            matched = True
        if matched:
            matched_excludes.append(pattern.raw)

    if spans:
        chars = list(text)
        for start, end in spans:
            for i in range(start, end):
                chars[i] = " "
        masked = "".join(chars)
    else:
        masked = text

    matched_includes = [p.raw for p in config.include if p.regex.search(masked)]

    return ClassifyResult(
        is_data=bool(matched_includes),
        matched_includes=matched_includes,
        matched_excludes=matched_excludes,
    )


def matches_any_signal(description: str | None, config: ClassificationConfig) -> bool:
    """Used by `wonby sample` to hunt for false negatives."""
    text = description or ""
    return any(p.search(text) for p in config.sample_signals)


# --- eval metrics ------------------------------------------------------------


@dataclass(frozen=True)
class LabeledRow:
    tender_no: str
    tender_description: str
    is_data: bool
    note: str = ""


@dataclass(frozen=True)
class EvalRow:
    tender_no: str
    tender_description: str
    matched_includes: list[str]
    matched_excludes: list[str]


@dataclass(frozen=True)
class EvalResult:
    size: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1: float
    false_positive_rows: list[EvalRow]
    false_negative_rows: list[EvalRow]


def evaluate(labels: list[LabeledRow], config: ClassificationConfig) -> EvalResult:
    """Precision/recall/F1 of `classify(..., config)` against a labelled set,
    plus the false-positive and false-negative rows for inspection."""
    tp = fp = fn = tn = 0
    fp_rows: list[EvalRow] = []
    fn_rows: list[EvalRow] = []

    for row in labels:
        result = classify(row.tender_description, config)
        if result.is_data and row.is_data:
            tp += 1
        elif result.is_data and not row.is_data:
            fp += 1
            fp_rows.append(
                EvalRow(
                    row.tender_no,
                    row.tender_description,
                    result.matched_includes,
                    result.matched_excludes,
                )
            )
        elif not result.is_data and row.is_data:
            fn += 1
            fn_rows.append(
                EvalRow(
                    row.tender_no,
                    row.tender_description,
                    result.matched_includes,
                    result.matched_excludes,
                )
            )
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return EvalResult(
        size=len(labels),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        precision=precision,
        recall=recall,
        f1=f1,
        false_positive_rows=fp_rows,
        false_negative_rows=fn_rows,
    )
