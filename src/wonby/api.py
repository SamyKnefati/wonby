"""data.gov.sg API client: metadata + paginated datastore fetch, with retries.

I/O only — no DuckDB, no business logic. See CLAUDE.md > Ingestion semantics.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from . import settings

logger = logging.getLogger(__name__)

Sleep = Callable[[float], None]
Rng = Callable[[], float]


class ApiError(RuntimeError):
    """Any unrecoverable failure talking to the data.gov.sg API."""


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 6
    base_delay: float = 2.0
    max_delay: float = 60.0


DEFAULT_RETRY = RetryConfig()


_RETRYABLE_STATUS = {429}


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS or 500 <= status_code < 600


def _backoff_delay(attempt: int, retry: RetryConfig, rng: Rng) -> float:
    """Exponential backoff with full jitter: uniform(0, min(cap, base * 2**(n-1)))."""
    ceiling = min(retry.max_delay, retry.base_delay * (2 ** (attempt - 1)))
    return rng() * ceiling


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _headers() -> dict[str, str]:
    headers = {"User-Agent": settings.USER_AGENT, "Accept": "application/json"}
    if settings.API_KEY:
        headers[settings.API_KEY_HEADER] = settings.API_KEY
    return headers


def _request_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None,
    retry: RetryConfig,
    sleep: Sleep,
    rng: Rng,
) -> httpx.Response:
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            if attempt >= retry.max_attempts:
                raise ApiError(f"request to {url} failed after {attempt} attempts: {exc}") from exc
            delay = _backoff_delay(attempt, retry, rng)
            logger.warning(
                "transport error on %s (attempt %d/%d): %s — retrying in %.1fs",
                url,
                attempt,
                retry.max_attempts,
                exc,
                delay,
            )
            sleep(delay)
            continue

        if _is_retryable_status(response.status_code):
            if attempt >= retry.max_attempts:
                raise ApiError(
                    f"request to {url} failed after {attempt} attempts: HTTP {response.status_code}"
                )
            delay = _retry_after_seconds(response)
            if delay is None:
                delay = _backoff_delay(attempt, retry, rng)
            logger.warning(
                "HTTP %d from %s (attempt %d/%d) — retrying in %.1fs",
                response.status_code,
                url,
                attempt,
                retry.max_attempts,
                delay,
            )
            sleep(delay)
            continue

        if 400 <= response.status_code < 500:
            raise ApiError(
                f"request to {url} failed: HTTP {response.status_code}: {response.text[:300]!r}"
            )

        if response.status_code >= 500:
            # Non-retryable-but-still-error 5xx shouldn't happen given the check
            # above, but guard anyway rather than silently returning it.
            raise ApiError(
                f"request to {url} failed: HTTP {response.status_code}: {response.text[:300]!r}"
            )

        return response


def fetch_metadata(
    *,
    dataset_id: str = settings.DATASET_ID,
    transport: httpx.BaseTransport | None = None,
    sleep: Sleep = time.sleep,
    rng: Rng = random.random,
    retry: RetryConfig = DEFAULT_RETRY,
    timeout: float = settings.REQUEST_TIMEOUT_SECONDS,
) -> str:
    """Return `data.lastUpdatedAt` from the dataset metadata endpoint."""
    url = settings.METADATA_URL_TEMPLATE.format(dataset_id=dataset_id)
    with httpx.Client(transport=transport, timeout=timeout, headers=_headers()) as client:
        response = _request_with_retry(client, url, params=None, retry=retry, sleep=sleep, rng=rng)
    try:
        body = response.json()
        last_updated_at = body["data"]["lastUpdatedAt"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(f"malformed metadata response: {exc}") from exc
    if not isinstance(last_updated_at, str) or not last_updated_at:
        raise ApiError(f"malformed metadata response: bad lastUpdatedAt {last_updated_at!r}")
    return last_updated_at


def _check_fields(fields: list[dict[str, Any]]) -> None:
    actual = {f.get("id") for f in fields}
    expected = set(settings.EXPECTED_FIELD_IDS)
    if actual == expected:
        return
    missing = expected - actual
    extra = actual - expected
    parts = []
    if missing:
        parts.append(f"missing {sorted(missing)}")
    if extra:
        parts.append(f"extra {sorted(extra)}")
    raise ApiError(f"schema drift in datastore fields: {', '.join(parts)}")


def fetch_all_records(
    *,
    dataset_id: str = settings.DATASET_ID,
    page_size: int = 10000,
    transport: httpx.BaseTransport | None = None,
    sleep: Sleep = time.sleep,
    rng: Rng = random.random,
    retry: RetryConfig = DEFAULT_RETRY,
    timeout: float = settings.REQUEST_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Fetch every record from the datastore, paginating until `offset >= total`.

    Raises ApiError on: `success` false, `total` changing between pages, an
    empty page before `total` is reached, a fetched count != `total`, or a
    schema drift in the returned `fields`.
    """
    records: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0

    with httpx.Client(transport=transport, timeout=timeout, headers=_headers()) as client:
        while True:
            params = {
                "resource_id": dataset_id,
                "limit": page_size,
                "offset": offset,
            }
            response = _request_with_retry(
                client,
                settings.DATASTORE_SEARCH_URL,
                params=params,
                retry=retry,
                sleep=sleep,
                rng=rng,
            )
            try:
                body = response.json()
            except ValueError as exc:
                raise ApiError(f"malformed JSON response at offset {offset}: {exc}") from exc

            if not body.get("success"):
                raise ApiError(f"datastore_search reported failure at offset {offset}: {body}")

            try:
                result = body["result"]
                page_total = result["total"]
                fields = result["fields"]
                page_records = result["records"]
            except KeyError as exc:
                raise ApiError(
                    f"malformed datastore_search response at offset {offset}: missing {exc}"
                ) from exc

            _check_fields(fields)

            if total is None:
                total = page_total
            elif page_total != total:
                raise ApiError(
                    f"total changed between pages: was {total}, now {page_total} at offset {offset}"
                )

            if not page_records and offset < total:
                raise ApiError(f"empty page at offset {offset} before reaching total ({total})")

            records.extend(page_records)
            offset += len(page_records)

            if offset >= total:
                break
            if not page_records:
                # Unreachable given the check above, but avoids any possibility
                # of looping forever on a pathological response.
                raise ApiError(f"no progress fetching records at offset {offset}")

    if total is None or len(records) != total:
        raise ApiError(f"fetched {len(records)} records but total was {total}")

    return records
