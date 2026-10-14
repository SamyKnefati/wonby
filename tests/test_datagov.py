import httpx
import pytest

from wonby import settings
from wonby.datagov import ApiError, RetryConfig, fetch_all_records, fetch_metadata

FIELDS = [{"id": fid, "type": "text"} for fid in settings.EXPECTED_FIELD_IDS]


def record(i: int, tender_no: str = "T1", supplier: str = "SUP") -> dict:
    return {
        "_id": i,
        "tender_no": tender_no,
        "tender_description": "desc",
        "agency": "Agency",
        "award_date": "1/4/2021",
        "tender_detail_status": "Awarded to Suppliers",
        "supplier_name": supplier,
        "awarded_amt": "100",
    }


def datastore_page(records: list[dict], total: int, fields=None) -> dict:
    return {
        "success": True,
        "result": {"total": total, "fields": fields or FIELDS, "records": records},
    }


def no_sleep(_seconds: float) -> None:
    pass


def fixed_rng() -> float:
    return 0.5


# --- fetch_metadata --------------------------------------------------------------


def test_fetch_metadata_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/metadata")
        return httpx.Response(200, json={"data": {"lastUpdatedAt": "2026-04-26T21:48:27+08:00"}})

    transport = httpx.MockTransport(handler)
    result = fetch_metadata(transport=transport, sleep=no_sleep, rng=fixed_rng)
    assert result == "2026-04-26T21:48:27+08:00"


def test_fetch_metadata_malformed_body_raises():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": True}))
    with pytest.raises(ApiError):
        fetch_metadata(transport=transport, sleep=no_sleep, rng=fixed_rng)


def test_fetch_metadata_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"data": {"lastUpdatedAt": "X"}})

    transport = httpx.MockTransport(handler)
    result = fetch_metadata(
        transport=transport, sleep=no_sleep, rng=fixed_rng, retry=RetryConfig(max_attempts=3)
    )
    assert result == "X"
    assert calls["n"] == 2


def test_fetch_metadata_honours_retry_after():
    calls = {"n": 0}
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"data": {"lastUpdatedAt": "X"}})

    transport = httpx.MockTransport(handler)
    fetch_metadata(
        transport=transport,
        sleep=lambda s: slept.append(s),
        rng=fixed_rng,
        retry=RetryConfig(max_attempts=3),
    )
    assert slept == [7.0]


def test_fetch_metadata_fails_fast_on_other_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    with pytest.raises(ApiError):
        fetch_metadata(transport=transport, sleep=no_sleep, rng=fixed_rng)
    assert calls["n"] == 1  # no retry on 404


def test_fetch_metadata_retries_exhausted():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(ApiError):
        fetch_metadata(
            transport=transport, sleep=no_sleep, rng=fixed_rng, retry=RetryConfig(max_attempts=3)
        )


def test_fetch_metadata_retries_transport_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json={"data": {"lastUpdatedAt": "X"}})

    transport = httpx.MockTransport(handler)
    result = fetch_metadata(
        transport=transport, sleep=no_sleep, rng=fixed_rng, retry=RetryConfig(max_attempts=3)
    )
    assert result == "X"
    assert calls["n"] == 2


# --- fetch_all_records -------------------------------------------------------------


def test_fetch_all_records_single_page():
    records = [record(i) for i in range(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=datastore_page(records, total=3))

    transport = httpx.MockTransport(handler)
    result = fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng, page_size=10000)
    assert len(result) == 3


def test_fetch_all_records_two_pages():
    page1 = [record(i) for i in range(2)]
    page2 = [record(i) for i in range(2, 3)]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        offset = int(request.url.params.get("offset"))
        if offset == 0:
            return httpx.Response(200, json=datastore_page(page1, total=3))
        return httpx.Response(200, json=datastore_page(page2, total=3))

    transport = httpx.MockTransport(handler)
    result = fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng, page_size=2)
    assert len(result) == 3
    assert calls["n"] == 2


def test_fetch_all_records_empty_dataset():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=datastore_page([], total=0))

    transport = httpx.MockTransport(handler)
    result = fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng)
    assert result == []


def test_fetch_all_records_empty_page_before_total_errors_no_infinite_loop():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Always claims total=5 but never returns any records.
        return httpx.Response(200, json=datastore_page([], total=5))

    transport = httpx.MockTransport(handler)
    with pytest.raises(ApiError):
        fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng)
    assert calls["n"] == 1  # fails on the very first empty-before-total page


def test_fetch_all_records_total_changes_between_pages_errors():
    page1 = [record(i) for i in range(2)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset"))
        if offset == 0:
            return httpx.Response(200, json=datastore_page(page1, total=4))
        return httpx.Response(200, json=datastore_page([record(2)], total=999))

    transport = httpx.MockTransport(handler)
    with pytest.raises(ApiError, match="total changed"):
        fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng, page_size=2)


def test_fetch_all_records_success_false_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": "nope"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(ApiError):
        fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng)


def test_fetch_all_records_schema_drift_missing_field_errors():
    bad_fields = [f for f in FIELDS if f["id"] != "awarded_amt"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=datastore_page([record(0)], total=1, fields=bad_fields))

    transport = httpx.MockTransport(handler)
    with pytest.raises(ApiError, match="missing"):
        fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng)


def test_fetch_all_records_schema_drift_extra_field_errors():
    extra_fields = FIELDS + [{"id": "new_field", "type": "text"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=datastore_page([record(0)], total=1, fields=extra_fields))

    transport = httpx.MockTransport(handler)
    with pytest.raises(ApiError, match="extra"):
        fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng)


def test_fetch_all_records_fetched_mismatch_total_errors():
    # total says 5 but only 3 records ever come back across pages that look "done".
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset"))
        if offset == 0:
            return httpx.Response(200, json=datastore_page([record(i) for i in range(3)], total=5))
        return httpx.Response(200, json=datastore_page([], total=5))

    transport = httpx.MockTransport(handler)
    with pytest.raises(ApiError):
        fetch_all_records(transport=transport, sleep=no_sleep, rng=fixed_rng, page_size=3)


def test_fetch_all_records_retries_429_mid_pagination():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        offset = int(request.url.params.get("offset"))
        if offset == 0:
            return httpx.Response(200, json=datastore_page([record(0)], total=2))
        if calls["n"] == 2:
            return httpx.Response(429)
        return httpx.Response(200, json=datastore_page([record(1)], total=2))

    transport = httpx.MockTransport(handler)
    result = fetch_all_records(
        transport=transport,
        sleep=no_sleep,
        rng=fixed_rng,
        page_size=1,
        retry=RetryConfig(max_attempts=3),
    )
    assert len(result) == 2
    assert calls["n"] == 3


def test_user_agent_header_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"data": {"lastUpdatedAt": "X"}})

    transport = httpx.MockTransport(handler)
    fetch_metadata(transport=transport, sleep=no_sleep, rng=fixed_rng)
    assert seen["ua"] == settings.USER_AGENT
