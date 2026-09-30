"""Central place for paths and settings.

Project root is derived from this file's location (src/wonby/settings.py
-> project root is two levels up), so the project works regardless of the
current working directory it's invoked from.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
SQL_DIR = PACKAGE_DIR / "sql"
TESTS_DIR = PROJECT_ROOT / "tests"

DB_PATH = Path(os.environ.get("WONBY_DB", str(DATA_DIR / "wonby.duckdb")))

CLASSIFICATION_CONFIG_PATH = CONFIG_DIR / "classification.yaml"
RADAR_PARAMS_PATH = CONFIG_DIR / "radar.yaml"
SUPPLIER_ALIASES_PATH = CONFIG_DIR / "supplier_aliases.yaml"
LABELED_DESCRIPTIONS_CSV = TESTS_DIR / "fixtures" / "labeled_descriptions.csv"

# data.gov.sg API
DATASET_ID = "d_acde1106003906a75c3fa052592f2fcb"
API_KEY = os.environ.get("DATA_GOV_SG_API_KEY")
API_KEY_HEADER = "x-api-key"
DATASTORE_SEARCH_URL = "https://data.gov.sg/api/action/datastore_search"
METADATA_URL_TEMPLATE = (
    "https://api-production.data.gov.sg/v2/public/api/datasets/{dataset_id}/metadata"
)
USER_AGENT = "wonby/0.1 (+https://github.com/SamyKnefati/wonby)"
REQUEST_TIMEOUT_SECONDS = 60.0

# Schema as observed in docs/dataset_profile.md — used to detect drift on the API response.
EXPECTED_FIELD_IDS = (
    "_id",
    "tender_no",
    "tender_description",
    "agency",
    "award_date",
    "tender_detail_status",
    "supplier_name",
    "awarded_amt",
)
