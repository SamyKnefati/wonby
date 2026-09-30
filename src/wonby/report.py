"""CSV exports, the Markdown prospecting report, and `report_runs` logging.

See CLAUDE.md for business rules — especially the "Nouveautés" watermark rule:
a report's new-awards section compares against the watermark (max(first_seen_at)
in stg_awards) of the latest *previous* report whose watermark is strictly
older than the current one. Regenerating a report on unchanged data therefore
always reproduces the same Nouveautés, because a report generated on the same
data state never becomes anyone's baseline (its watermark isn't "older").
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

MARTS = [
    "mart_top_data_suppliers",
    "mart_top_data_agencies",
    "mart_recent_data_awards",
    "mart_supplier_agencies",
    "mart_priority_targets",
]


def run(db_path: Path, output_dir: Path) -> Path:
    """CSV exports + Markdown report + a `report_runs` row. Returns the report path."""
    con = duckdb.connect(str(db_path))
    try:
        exports_dir = output_dir / "exports"
        reports_dir = output_dir / "reports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)

        _export_csvs(con, exports_dir)
        _ensure_report_runs(con)

        params = _radar_params(con)
        anchor_date = con.execute("SELECT max(award_date) FROM stg_awards").fetchone()[0]
        data_watermark = con.execute("SELECT max(first_seen_at) FROM stg_awards").fetchone()[0]
        baseline = _nouveautes_baseline(con, data_watermark)

        generated_at = datetime.now()
        report_path = reports_dir / f"radar_{generated_at.date().isoformat()}.md"

        markdown = _build_markdown(
            con,
            params=params,
            generated_at=generated_at,
            anchor_date=anchor_date,
            baseline=baseline,
        )
        report_path.write_text(markdown, encoding="utf-8")

        con.execute(
            "INSERT INTO report_runs (generated_at, data_watermark, anchor_date, report_path) "
            "VALUES (?, ?, ?, ?)",
            [generated_at, data_watermark, anchor_date, str(report_path)],
        )
        return report_path
    finally:
        con.close()


# --- setup -------------------------------------------------------------------------


def _ensure_report_runs(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SEQUENCE IF NOT EXISTS report_runs_id_seq")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS report_runs (
            run_id BIGINT NOT NULL DEFAULT nextval('report_runs_id_seq'),
            generated_at TIMESTAMP NOT NULL,
            data_watermark TIMESTAMP,
            anchor_date DATE,
            report_path VARCHAR
        )
        """
    )


def _radar_params(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    keys = ["recent_window_months", "target_window_months", "target_min_data_tenders", "top_n"]
    row = con.execute(f"SELECT {', '.join(keys)} FROM radar_params").fetchone()
    return dict(zip(keys, row))


def _export_csvs(con: duckdb.DuckDBPyConnection, exports_dir: Path) -> None:
    for mart in MARTS:
        target = (exports_dir / f"{mart}.csv").as_posix()
        con.execute(f"COPY (SELECT * FROM {mart}) TO '{target}' (HEADER, DELIMITER ',')")


def _count(con: duckdb.DuckDBPyConnection, mart: str) -> int:
    return con.execute(f"SELECT count(*) FROM {mart}").fetchone()[0]


# --- Nouveautés watermark ------------------------------------------------------------


def _nouveautes_baseline(con: duckdb.DuckDBPyConnection, current_watermark: Any) -> Any:
    """(data_watermark, report_path, generated_at) of the latest previous report
    whose watermark is strictly older than the current one, or None if there
    isn't one (first report ever, or no report yet reflects an older data state).
    """
    if current_watermark is None:
        return None
    return con.execute(
        """
        SELECT data_watermark, report_path, generated_at
        FROM report_runs
        WHERE data_watermark < ?
        ORDER BY generated_at DESC
        LIMIT 1
        """,
        [current_watermark],
    ).fetchone()


# --- formatting helpers --------------------------------------------------------------


def _fmt_amount(x: Any) -> str:
    if x is None:
        return "n/d"
    return f"S${float(x):,.0f}"


def _fmt_date(d: Any) -> str:
    if d is None:
        return "n/d"
    return f"{d:%d/%m/%Y}"


def _fmt_dt(dt: Any) -> str:
    if dt is None:
        return "n/d"
    return f"{dt:%d/%m/%Y %H:%M}"


def _cell(text: Any) -> str:
    """Markdown-table-safe cell: no pipes, no newlines."""
    if text is None:
        return ""
    return str(text).replace("|", "/").replace("\n", " ").replace("\r", " ").strip()


def _truncate(text: str | None, n: int = 100) -> str:
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _flags(is_no_supplier: bool, shared: bool, nominal: bool) -> str:
    flags = []
    if is_no_supplier:
        flags.append("sans fournisseur")
    if shared:
        flags.append("montant partagé")
    if nominal:
        flags.append("montant nominal")
    return f" _({', '.join(flags)})_" if flags else ""


def _source_last_updated_at(con: duckdb.DuckDBPyConnection) -> str | None:
    try:
        row = con.execute(
            """
            SELECT source_last_updated_at
            FROM ingest_runs
            WHERE status = 'success'
            ORDER BY finished_at DESC
            LIMIT 1
            """
        ).fetchone()
    except duckdb.Error:
        return None
    return row[0] if row else None


# --- report assembly -----------------------------------------------------------------


def _build_markdown(
    con: duckdb.DuckDBPyConnection,
    *,
    params: dict[str, Any],
    generated_at: datetime,
    anchor_date: Any,
    baseline: Any,
) -> str:
    lines: list[str] = []
    lines.append("# WonBy - marchés publics data / analytics / BI à Singapour\n")
    lines.append(f"_Généré le {_fmt_dt(generated_at)}._\n")

    date_min, date_max = con.execute("SELECT min(award_date), max(award_date) FROM stg_awards").fetchone()
    n_tenders = con.execute("SELECT count(DISTINCT tender_no) FROM stg_awards").fetchone()[0]
    n_data_tenders = con.execute("SELECT count(*) FROM tender_classification WHERE is_data").fetchone()[0]
    source_last_updated = _source_last_updated_at(con)

    lines.append("## Couverture et fraîcheur des données\n")
    lines.append(f"- Fenêtre de données : {_fmt_date(date_min)} → {_fmt_date(date_max)}")
    lines.append(f"- Date d'ancrage (dernière attribution connue) : **{_fmt_date(anchor_date)}**")
    lines.append(f"- Dernière mise à jour de la source GeBIZ : {source_last_updated or 'n/d'}")
    lines.append(f"- Tenders \"data\" identifiés : **{n_data_tenders}** sur {n_tenders} tenders au total\n")

    lines.extend(_priority_targets_section(con, params))
    lines.extend(_nouveautes_section(con, baseline))
    lines.extend(_top_suppliers_section(con, params))
    lines.extend(_top_agencies_section(con, params))
    lines.extend(_recent_awards_section(con, params))
    lines.extend(_method_section())

    return "\n".join(lines) + "\n"


def _priority_targets_section(con: duckdb.DuckDBPyConnection, params: dict[str, Any]) -> list[str]:
    lines = ["## Cibles prioritaires\n"]
    targets = con.execute(
        """
        SELECT display_name, n_recent_data_tenders, n_all_time_data_tenders, n_recent_quarters,
               last_award_date, agencies_recent, recent_descriptions, recent_amt_excl_shared
        FROM mart_priority_targets
        LIMIT ?
        """,
        [params["top_n"]],
    ).fetchall()
    if not targets:
        lines.append(
            f"Aucun fournisseur n'atteint le seuil de {params['target_min_data_tenders']} tenders data "
            f"distincts sur les {params['target_window_months']} derniers mois avec les données actuelles.\n"
        )
        return lines

    for (
        display_name, n_recent, n_all_time, n_quarters,
        last_award, agencies_recent, recent_desc, amt_excl_shared,
    ) in targets:
        lines.append(f"### {_cell(display_name)}\n")
        lines.append(
            f"- {n_recent} tender(s) data sur les {params['target_window_months']} derniers mois "
            f"({n_all_time} au total depuis le début du jeu de données)"
        )
        lines.append(
            f"- Régularité : {n_quarters} trimestre(s) distinct(s) avec une attribution dans la fenêtre récente"
        )
        lines.append(f"- Dernière attribution : {_fmt_date(last_award)}")
        lines.append(f"- Agences récentes : {_cell(agencies_recent) or 'n/d'}")
        lines.append(f"- Montant récent hors montants partagés/nominaux : {_fmt_amount(amt_excl_shared)}")
        for d in recent_desc or []:
            lines.append(f"  - {_cell(d)}")
        lines.append("")
    return lines


def _nouveautes_section(con: duckdb.DuckDBPyConnection, baseline: Any) -> list[str]:
    lines = ["## Nouveautés\n"]
    if baseline is None:
        lines.append(
            "Premier rapport généré pour ce jeu de données : pas de rapport antérieur pour comparer les "
            "nouvelles attributions. Voir la section **Attributions data récentes** ci-dessous pour "
            "l'ensemble des attributions data des derniers mois.\n"
        )
        return lines

    baseline_watermark, baseline_path, _baseline_generated_at = baseline
    baseline_label = Path(baseline_path).name if baseline_path else "rapport antérieur"
    new_rows = con.execute(
        """
        SELECT s.award_date, s.supplier_group, s.agency, s.tender_description, s.awarded_amt,
               s.is_no_supplier, s.amt_shared_across_suppliers, s.is_nominal_amt
        FROM stg_awards s
        JOIN tender_classification t ON t.tender_no = s.tender_no
        WHERE t.is_data AND s.first_seen_at > ?
        ORDER BY s.first_seen_at DESC, s.award_date DESC
        """,
        [baseline_watermark],
    ).fetchall()

    if not new_rows:
        lines.append(f"Aucune nouvelle attribution data depuis le rapport comparable précédent ({baseline_label}).\n")
        return lines

    lines.append(f"{len(new_rows)} nouvelle(s) attribution(s) data depuis le rapport comparable précédent ({baseline_label}) :\n")
    lines.append("| Date | Fournisseur | Agence | Description | Montant |")
    lines.append("|---|---|---|---|---|")
    for award_date, supplier_group, agency, descr, amt, is_no_supplier, shared, nominal in new_rows:
        flag_str = _flags(is_no_supplier, shared, nominal)
        lines.append(
            f"| {_fmt_date(award_date)} | {_cell(supplier_group)} | {_cell(agency)} | "
            f"{_cell(_truncate(descr))}{flag_str} | {_fmt_amount(amt)} |"
        )
    lines.append("")
    return lines


def _top_suppliers_section(con: duckdb.DuckDBPyConnection, params: dict[str, Any]) -> list[str]:
    total = _count(con, "mart_top_data_suppliers")
    shown = min(params["top_n"], total)
    lines = [f"## Top fournisseurs data ({shown} sur {total})\n"]
    rows = con.execute(
        """
        SELECT display_name, n_data_tenders, n_awards, total_amt, total_amt_excl_shared,
               first_award_date, last_award_date, n_agencies
        FROM mart_top_data_suppliers
        LIMIT ?
        """,
        [params["top_n"]],
    ).fetchall()
    lines.append("| Fournisseur | Tenders | Awards | Montant total | Montant hors partagé | 1re attrib. | Dernière attrib. | Agences |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, n_t, n_a, total_amt, total_excl, first_d, last_d, n_ag in rows:
        lines.append(
            f"| {_cell(name)} | {n_t} | {n_a} | {_fmt_amount(total_amt)} | {_fmt_amount(total_excl)} | "
            f"{_fmt_date(first_d)} | {_fmt_date(last_d)} | {n_ag} |"
        )
    lines.append("")
    return lines


def _top_agencies_section(con: duckdb.DuckDBPyConnection, params: dict[str, Any]) -> list[str]:
    total = _count(con, "mart_top_data_agencies")
    shown = min(params["top_n"], total)
    lines = [f"## Top agences acheteuses ({shown} sur {total})\n"]
    rows = con.execute(
        """
        SELECT agency, n_data_tenders, total_amt, n_distinct_suppliers, last_award_date
        FROM mart_top_data_agencies
        LIMIT ?
        """,
        [params["top_n"]],
    ).fetchall()
    lines.append("| Agence | Tenders | Montant total | Fournisseurs distincts | Dernière attrib. |")
    lines.append("|---|---|---|---|---|")
    for agency, n_t, total_amt, n_sup, last_d in rows:
        lines.append(f"| {_cell(agency)} | {n_t} | {_fmt_amount(total_amt)} | {n_sup} | {_fmt_date(last_d)} |")
    lines.append("")
    return lines


def _recent_awards_section(con: duckdb.DuckDBPyConnection, params: dict[str, Any]) -> list[str]:
    total = _count(con, "mart_recent_data_awards")
    shown = min(params["top_n"], total)
    lines = [f"## Attributions data récentes (derniers {params['recent_window_months']} mois — {shown} sur {total})\n"]
    rows = con.execute(
        """
        SELECT award_date, supplier_group, agency, tender_description, awarded_amt,
               is_no_supplier, amt_shared_across_suppliers, is_nominal_amt
        FROM mart_recent_data_awards
        LIMIT ?
        """,
        [params["top_n"]],
    ).fetchall()
    if not rows:
        lines.append("Aucune attribution data dans cette fenêtre.\n")
        return lines

    lines.append("| Date | Fournisseur | Agence | Description | Montant |")
    lines.append("|---|---|---|---|---|")
    for award_date, supplier_group, agency, descr, amt, is_no_supplier, shared, nominal in rows:
        flag_str = _flags(is_no_supplier, shared, nominal)
        lines.append(
            f"| {_fmt_date(award_date)} | {_cell(supplier_group)} | {_cell(agency)} | "
            f"{_cell(_truncate(descr))}{flag_str} | {_fmt_amount(amt)} |"
        )
    lines.append("")
    return lines


def _method_section() -> list[str]:
    return [
        "## Méthode et limites\n",
        "- **Classification** : un tender est \"data\" s'il matche un mot-clé d'inclusion (analytics, BI, "
        "data platform/warehouse/lake, gouvernance/qualité/intégration des données, data science, ML, "
        "dashboards…) sur sa description, une fois les zones exclues (data centres physiques, SCADA, "
        "données mobiles, équipement de laboratoire) masquées. Classification par mots-clés : des faux "
        "positifs/négatifs restent possibles, voir `config/classification.yaml`.",
        "- **Montants** : les tenders multi-fournisseurs répètent souvent le même montant total sur chaque "
        "ligne fournisseur (« montant partagé ») ; certains lots de panel affichent un montant nominal de "
        "1 $ (« montant nominal »), sans signal financier réel. Les totaux fournisseurs sont donnés avec "
        "et sans ces montants ; les totaux agence ne comptent un montant partagé qu'une seule fois.",
        "- **Fenêtres temporelles** : « récent » est ancré sur la date de la dernière attribution connue "
        "dans les données (jamais sur la date du jour) — la source est publiée par année fiscale et a du retard.",
        "- **Regroupement fournisseurs** : les entités sont regroupées par `supplier_group` (raison sociale "
        "normalisée, suffixe légal et mention de pays retirés, puis alias manuels de "
        "`config/supplier_aliases.yaml`) ; un regroupement imparfait peut sous- ou sur-compter un fournisseur.",
        "",
    ]
