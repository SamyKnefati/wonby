"""`radar` CLI entry point."""

from __future__ import annotations

import argparse
import csv
import logging
import random
import sys
from pathlib import Path

import duckdb

from . import build as build_module
from . import classify as classify_module
from . import ingest as ingest_module
from . import settings

logger = logging.getLogger("wonby")


class CliError(RuntimeError):
    """Raised for any failure that should print as a clean one-line error."""


def _connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if not read_only:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(settings.DB_PATH), read_only=read_only)


# --- ingest / build ---------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> None:
    con = _connect()
    try:
        result = ingest_module.run(con, full=args.full, accept_removals=args.accept_removals)
    except ingest_module.IngestError as exc:
        raise CliError(f"ingest failed: {exc}") from exc
    finally:
        con.close()

    if result.status == "skipped":
        print(f"ingest skipped: source unchanged since last run ({result.source_last_updated_at})")
    else:
        print(
            f"ingest {result.status}: fetched={result.fetched} "
            f"inserted={result.inserted} updated={result.updated} removed={result.removed}"
        )


def cmd_build(args: argparse.Namespace) -> None:
    con = _connect()
    try:
        build_module.run(con)
    finally:
        con.close()


def cmd_report(args: argparse.Namespace) -> None:
    try:
        from . import report  # noqa: PLC0415 - deliberately lazy, module may not exist yet
    except ImportError as exc:
        raise CliError(
            "report not implemented yet (wonby.report is missing) — that's the market-analyst's job"
        ) from exc
    run_fn = getattr(report, "run", None)
    if run_fn is None:
        raise CliError("report not implemented yet (wonby.report.run is missing)")
    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = run_fn(settings.DB_PATH, settings.OUTPUT_DIR)
    print(f"report written to {path}")


def cmd_serve(args: argparse.Namespace) -> None:
    # Imported here so the other commands don't pay for loading FastAPI.
    import uvicorn

    from . import api

    uvicorn.run(api.create_app(), host=args.host, port=args.port)


# --- eval ---------------------------------------------------------------------------


def _load_labels(path: Path) -> list[classify_module.LabeledRow]:
    if not path.exists():
        return []
    rows: list[classify_module.LabeledRow] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            is_data_raw = (row.get("is_data") or "").strip().lower()
            rows.append(
                classify_module.LabeledRow(
                    tender_no=row.get("tender_no", ""),
                    tender_description=row.get("tender_description", ""),
                    is_data=is_data_raw in ("true", "1", "yes"),
                    note=row.get("note", "") or "",
                )
            )
    return rows


def _print_eval_result(label: str, result: classify_module.EvalResult) -> None:
    print(
        f"[{label}] n={result.size} precision={result.precision:.3f} "
        f"recall={result.recall:.3f} f1={result.f1:.3f} "
        f"(tp={result.true_positives} fp={result.false_positives} "
        f"fn={result.false_negatives} tn={result.true_negatives})"
    )
    for row in result.false_positive_rows:
        print(
            f"  FP {row.tender_no}: {row.tender_description[:80]!r} matched={row.matched_includes}"
        )
    for row in result.false_negative_rows:
        print(
            f"  FN {row.tender_no}: {row.tender_description[:80]!r} excluded_by={row.matched_excludes}"
        )


def _stg_tenders(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    return con.execute(
        "SELECT DISTINCT tender_no, tender_description FROM stg_awards ORDER BY tender_no"
    ).fetchall()


def cmd_eval(args: argparse.Namespace) -> None:
    labels_path = Path(args.labels) if args.labels else settings.LABELED_DESCRIPTIONS_CSV
    labels = _load_labels(labels_path)
    if not labels:
        print(f"no labelled rows found at {labels_path}")
        return

    config_path = Path(args.config) if args.config else settings.CLASSIFICATION_CONFIG_PATH
    config = classify_module.load_classification_config(config_path)
    result = classify_module.evaluate(labels, config)
    _print_eval_result(str(config_path), result)

    if args.against:
        against_path = Path(args.against)
        against_config = classify_module.load_classification_config(against_path)
        against_result = classify_module.evaluate(labels, against_config)
        _print_eval_result(str(against_path), against_result)

        try:
            con = _connect(read_only=True)
        except duckdb.Error as exc:
            raise CliError(f"cannot open database for --against flip check: {exc}") from exc
        try:
            tenders = _stg_tenders(con)
        finally:
            con.close()

        flips = []
        for tender_no, description in tenders:
            a = classify_module.classify(description, config).is_data
            b = classify_module.classify(description, against_config).is_data
            if a != b:
                flips.append((tender_no, b, a, description))

        print(f"{len(flips)} tenders flip classification ({against_path} -> {config_path})")
        for tender_no, before, after, description in flips:
            print(f"  {tender_no}: {before} -> {after}  {description[:80]!r}")


# --- sample ---------------------------------------------------------------------------


def cmd_sample(args: argparse.Namespace) -> None:
    labels_path = Path(args.labels) if args.labels else settings.LABELED_DESCRIPTIONS_CSV
    labelled = {row.tender_no for row in _load_labels(labels_path)}

    config = classify_module.load_classification_config(settings.CLASSIFICATION_CONFIG_PATH)

    con = _connect(read_only=True)
    try:
        tenders = _stg_tenders(con)
    finally:
        con.close()

    candidates = [(tn, desc) for tn, desc in tenders if tn not in labelled]

    data_pool = []
    signal_pool = []
    other_pool = []
    results: dict[str, classify_module.ClassifyResult] = {}
    for tender_no, description in candidates:
        result = classify_module.classify(description, config)
        results[tender_no] = result
        if result.is_data:
            data_pool.append((tender_no, description))
        elif classify_module.matches_any_signal(description, config):
            signal_pool.append((tender_no, description))
        else:
            other_pool.append((tender_no, description))

    rng = random.Random(args.seed)
    n = args.n
    n_data = round(n * 0.4)
    n_signal = round(n * 0.4)
    n_other = max(0, n - n_data - n_signal)

    chosen = (
        rng.sample(data_pool, min(n_data, len(data_pool)))
        + rng.sample(signal_pool, min(n_signal, len(signal_pool)))
        + rng.sample(other_pool, min(n_other, len(other_pool)))
    )
    chosen.sort(key=lambda row: row[0])

    out = Path(args.out).open("w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        writer = csv.writer(out)
        writer.writerow(
            [
                "tender_no",
                "predicted_is_data",
                "matched_includes",
                "matched_excludes",
                "tender_description",
            ]
        )
        for tender_no, description in chosen:
            result = results[tender_no]
            writer.writerow(
                [
                    tender_no,
                    result.is_data,
                    "; ".join(result.matched_includes),
                    "; ".join(result.matched_excludes),
                    description,
                ]
            )
    finally:
        if args.out:
            out.close()


# --- argparse wiring -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wonby", description="WonBy: who wins Singapore public tenders"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="incremental load from the API")
    p_ingest.add_argument("--full", action="store_true", help="ignore the metadata skip-check")
    p_ingest.add_argument(
        "--accept-removals", action="store_true", help="bypass the mass-removal guard"
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_build = sub.add_parser("build", help="rebuild stg_awards, tender_classification, marts")
    p_build.set_defaults(func=cmd_build)

    p_report = sub.add_parser("report", help="CSV exports + Markdown report")
    p_report.set_defaults(func=cmd_report)

    p_serve = sub.add_parser("serve", help="read-only HTTP API (docs at /docs)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8001)
    p_serve.set_defaults(func=cmd_serve)

    p_eval = sub.add_parser("eval", help="classifier precision/recall on the labelled set")
    p_eval.add_argument(
        "--config", help="path to a classification.yaml (default: config/classification.yaml)"
    )
    p_eval.add_argument("--against", help="compare against another classification.yaml")
    p_eval.add_argument(
        "--labels",
        help="path to the labelled CSV (default: tests/fixtures/labeled_descriptions.csv)",
    )
    p_eval.set_defaults(func=cmd_eval)

    p_sample = sub.add_parser("sample", help="fresh tenders to label")
    p_sample.add_argument("--n", type=int, default=60)
    p_sample.add_argument("--seed", type=int, default=0)
    p_sample.add_argument(
        "--labels",
        help="path to the labelled CSV to exclude (default: tests/fixtures/labeled_descriptions.csv)",
    )
    p_sample.add_argument("--out", help="output CSV path (default: stdout)")
    p_sample.set_defaults(func=cmd_sample)

    return parser


def main(argv: list[str] | None = None) -> int:
    # WARNING by default: the commands print their own one-line summaries.
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level guard for a clean one-line error
        logger.debug("unhandled exception", exc_info=exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
