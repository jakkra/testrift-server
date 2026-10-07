"""Tests for versioned, run-bound KPI ingest and read APIs."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from starlette.datastructures import QueryParams

from testrift_server import database
from testrift_server.database import TestCaseData, TestRunData


class Request:
    def __init__(self, method, run_id=None, body=b"", query=None):
        self.method = method
        self.match_info = {"run_id": run_id} if run_id else {}
        self.headers = {"content-length": str(len(body))} if body else {}
        self.query = query or {}
        self._body = body

    async def read(self, max_bytes=None):
        return self._body


def _payload(value=940000, file_size=1000000):
    return {
        "_schemaVersion": 1,
        "_samples": [
            {
                "metric_key": "file_download.rx_throughput",
                "value": value,
                "unit": "bps",
                "test_name": "NUnitTest.FileDownload.Download_1M",
                "dimensions": {
                    "file_size_bytes": file_size,
                    "tls": False,
                    "transfer_mode": "Buffered",
                    "protocol": "TCP",
                },
                "timestamp_utc": "2026-09-23T10:00:00Z",
            },
            {
                "metric_key": "file_download.rx_throughput",
                "value": value + 100,
                "unit": "bps",
                "test_name": "NUnitTest.FileDownload.NotInRun",
                "dimensions": {
                    "file_size_bytes": file_size * 5,
                    "tls": True,
                    "transfer_mode": "Transparent",
                    "protocol": "TCP",
                },
                "timestamp_utc": "2026-09-23T10:01:00Z",
            },
        ],
        "Throughput": [{"TestName": "NUnitTest.FileDownload.Download_1M", "RxSpeed": value}],
    }


def _request(payload, run_id="pilot-run-1"):
    body = json.dumps(payload, separators=(",", ":"), allow_nan=True).encode("utf-8")
    return Request("POST", run_id=run_id, body=body)


@pytest_asyncio.fixture
async def kpi_db(tmp_path, monkeypatch):
    database.initialize_database(tmp_path)
    await database.db.initialize()
    run = TestRunData(
        run_id="pilot-run-1",
        status="running",
        start_time=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        end_time=None,
        retention_days=None,
        local_run=True,
        dut="Device A",
        target_key="device-a",
        run_name="KPI pilot",
    )
    assert await database.db.insert_test_run(run, {}, {
        "test-system": {"branch": "main", "revision": "abc123"},
    })
    test_case = TestCaseData(
        id=0,
        run_id=run.run_id,
        tc_full_name="NUnitTest.FileDownload.Download_1M",
        tc_id="tc-1",
        status="passed",
        start_time=run.start_time,
        end_time=run.start_time,
    )
    assert await database.db.insert_test_case(test_case)
    yield database.db


@pytest.mark.asyncio
async def test_upload_retry_and_corrected_source_replace_samples_atomically(kpi_db):
    from testrift_server.api_handlers import api_run_kpis_handler

    payload = _payload()
    first = await api_run_kpis_handler(_request(payload))
    assert first.status == 201
    first_json = json.loads(first.text)
    assert first_json["sample_count"] == 2
    assert first_json["matched_count"] == 1
    assert first_json["unmatched_count"] == 1

    retry = await api_run_kpis_handler(_request(payload))
    assert retry.status == 200
    assert json.loads(retry.text)["idempotent"] is True

    corrected = await api_run_kpis_handler(_request(_payload(value=960000, file_size=1000000)))
    assert corrected.status == 201
    rows, total = await kpi_db.get_kpi_samples({"run_id": "pilot-run-1"})
    assert total == 2
    assert {row["value"] for row in rows} == {960000.0, 960100.0}

    async with kpi_db.get_connection() as db:
        batches = await (await db.execute("SELECT COUNT(*) FROM kpi_batches")).fetchone()
        samples = await (await db.execute("SELECT COUNT(*) FROM kpi_samples")).fetchone()
    assert batches[0] == 1
    assert samples[0] == 2


@pytest.mark.asyncio
async def test_unknown_run_schema_nan_and_size_are_rejected(kpi_db):
    from testrift_server.api_handlers import MAX_KPI_UPLOAD_BYTES, api_run_kpis_handler

    unknown = await api_run_kpis_handler(_request(_payload(), run_id="missing-run"))
    assert unknown.status == 404

    invalid_schema = _payload()
    invalid_schema["_schemaVersion"] = 2
    assert (await api_run_kpis_handler(_request(invalid_schema))).status == 400

    non_finite = _payload()
    non_finite["_samples"][0]["value"] = float("nan")
    assert (await api_run_kpis_handler(_request(non_finite))).status == 400

    oversized = Request("POST", run_id="pilot-run-1", body=b"{}")
    oversized.headers = {"content-length": str(MAX_KPI_UPLOAD_BYTES + 1)}
    assert (await api_run_kpis_handler(oversized)).status == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", [
    pytest.param(10 ** 400, id="float-conversion-overflow"),
    pytest.param(100000000000000000000, id="sqlite-bind-overflow"),
    pytest.param(-(2 ** 63) - 1, id="below-int64"),
    pytest.param(2 ** 63, id="above-int64"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="infinity"),
    pytest.param(None, id="null"),
    pytest.param([], id="array"),
    pytest.param({}, id="object"),
    pytest.param("x" * 257, id="long-string"),
])
async def test_kpi_upload_rejects_invalid_dimensions(kpi_db, dimension):
    from testrift_server.api_handlers import api_run_kpis_handler

    payload = _payload()
    payload["_samples"][0]["dimensions"] = {"boundary": dimension}
    response = await api_run_kpis_handler(_request(payload))

    assert response.status == 400
    assert json.loads(response.text)["success"] is False
    _, count = await kpi_db.get_kpi_samples({})
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "1protocol", "_protocol", "protocol.name", "\u00e9protocol", "protocol\n"])
async def test_kpi_upload_rejects_invalid_dimension_names(kpi_db, key):
    from testrift_server.api_handlers import api_run_kpis_handler

    payload = _payload()
    payload["_samples"][0]["dimensions"] = {key: "TCP"}
    response = await api_run_kpis_handler(_request(payload))

    assert response.status == 400
    assert json.loads(response.text)["success"] is False


@pytest.mark.asyncio
async def test_kpi_upload_rejects_too_many_dimensions(kpi_db):
    from testrift_server.api_handlers import api_run_kpis_handler

    payload = _payload()
    payload["_samples"][0]["dimensions"] = {f"key_{index}": index for index in range(33)}
    response = await api_run_kpis_handler(_request(payload))

    assert response.status == 400
    assert json.loads(response.text)["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value, expected_status", [
    pytest.param(10 ** 300, 201, id="finite-large-integer"),
    pytest.param(10 ** 400, 400, id="float-conversion-overflow"),
])
async def test_kpi_upload_normalizes_large_integer_samples(kpi_db, value, expected_status):
    from testrift_server.api_handlers import api_run_kpis_handler

    response = await api_run_kpis_handler(_request(_payload(value=value)))
    assert response.status == expected_status
    rows, count = await kpi_db.get_kpi_samples({})
    assert count == (2 if expected_status == 201 else 0)
    if expected_status == 201:
        assert all(type(row["value"]) is float for row in rows)
        assert all(row["value"] == float(value) for row in rows)


@pytest.fixture(params=["catalog", "history", "cross-target-history"])
def kpi_dimension_reader(request):
    from testrift_server.api_handlers import api_kpi_catalog_handler, api_kpi_history_handler

    targets = [("target", "device-a")]
    if request.param == "cross-target-history":
        targets.append(("target", "device-b"))
    handler = api_kpi_catalog_handler if request.param == "catalog" else api_kpi_history_handler
    query = targets + [
        ("metric_key", "file_download.rx_throughput"),
        ("unit", "bps"),
    ]
    return handler, query


@pytest.mark.asyncio
@pytest.mark.parametrize("key, value", [
    pytest.param("boundary", "[]", id="array"),
    pytest.param("boundary", "{}", id="object"),
    pytest.param("boundary", "null", id="null"),
    pytest.param("boundary", str(10 ** 400), id="huge-integer"),
    pytest.param("boundary", "100000000000000000000", id="sqlite-bind-overflow"),
    pytest.param("boundary", str(-(2 ** 63) - 1), id="below-int64"),
    pytest.param("boundary", str(2 ** 63), id="above-int64"),
    pytest.param("boundary", "NaN", id="nan"),
    pytest.param("boundary", "Infinity", id="infinity"),
    pytest.param("boundary", "-Infinity", id="negative-infinity"),
    pytest.param("boundary", "1e400", id="float-overflow"),
    pytest.param("boundary", "x" * 257, id="long-unquoted-string"),
    pytest.param("boundary", json.dumps("x" * 257), id="long-json-string"),
    pytest.param("", "TCP", id="empty-name"),
    pytest.param("1protocol", "TCP", id="numeric-name-prefix"),
    pytest.param("_protocol", "TCP", id="underscore-name-prefix"),
    pytest.param("protocol.name", "TCP", id="punctuation-in-name"),
    pytest.param("\u00e9protocol", "TCP", id="non-ascii-name"),
    pytest.param("protocol\n", "TCP", id="newline-in-name"),
])
async def test_kpi_reads_reject_invalid_dimension_filters(kpi_db, kpi_dimension_reader, key, value):
    from testrift_server.api_handlers import _kpi_read_filters

    handler, query = kpi_dimension_reader
    request = Request("GET", query=QueryParams(
        query + [(f"dimension.{key}", value)]
    ))
    with pytest.raises(ValueError):
        _kpi_read_filters(request)
    response = await handler(request)

    assert response.status == 400
    assert json.loads(response.text)["success"] is False


@pytest.mark.asyncio
async def test_kpi_reads_reject_too_many_dimension_filters(kpi_db, kpi_dimension_reader):
    handler, query = kpi_dimension_reader
    dimensions = [(f"dimension.key_{index}", "TCP") for index in range(33)]
    response = await handler(Request("GET", query=QueryParams(query + dimensions)))

    assert response.status == 400
    assert json.loads(response.text)["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension, filter_value", [
    pytest.param(-(2 ** 63), str(-(2 ** 63)), id="minimum-int64"),
    pytest.param(2 ** 63 - 1, str(2 ** 63 - 1), id="maximum-int64"),
    pytest.param(0, "0", id="zero"),
    pytest.param(1.7976931348623157e308, "1.7976931348623157e308", id="maximum-float"),
    pytest.param(-1.7976931348623157e308, "-1.7976931348623157e308", id="minimum-float"),
    pytest.param(True, "true", id="true"),
    pytest.param(False, "false", id="false"),
    pytest.param("TCP", "TCP", id="unquoted-string"),
    pytest.param("TCP", '"TCP"', id="json-string"),
    pytest.param("null", '"null"', id="quoted-null"),
    pytest.param("true", '"true"', id="quoted-boolean"),
    pytest.param("false", '"false"', id="quoted-false"),
    pytest.param("True", "True", id="non-json-unquoted-string"),
    pytest.param("123", '"123"', id="quoted-number"),
    pytest.param("", '""', id="empty-json-string"),
    pytest.param("", "", id="empty-unquoted-string"),
    pytest.param("x" * 256, "x" * 256, id="maximum-unquoted-string"),
    pytest.param("x" * 256, json.dumps("x" * 256), id="maximum-json-string"),
])
async def test_kpi_scalar_dimension_boundaries_are_preserved(
    kpi_db, kpi_dimension_reader, dimension, filter_value
):
    from testrift_server.api_handlers import _kpi_read_filters, api_run_kpis_handler

    payload = _payload()
    payload["_samples"] = payload["_samples"][:1]
    payload["_samples"][0]["dimensions"] = {"Boundary_1": dimension}
    assert (await api_run_kpis_handler(_request(payload))).status == 201

    handler, query = kpi_dimension_reader
    request = Request("GET", query=QueryParams(query + [("dimension.Boundary_1", filter_value)]))
    filters, _, _ = _kpi_read_filters(request)
    assert filters["dimensions"]["Boundary_1"] == dimension
    assert type(filters["dimensions"]["Boundary_1"]) is type(dimension)

    response = await handler(request)
    assert response.status == 200
    body = json.loads(response.text)
    assert body["pagination"]["count"] == 1
    assert len(body["data"]) == 1
    if handler.__name__ == "api_kpi_catalog_handler":
        assert body["data"][0]["dimensions"]["Boundary_1"] == dimension
    else:
        assert body["data"][0]["value"] == payload["_samples"][0]["value"]


@pytest.mark.asyncio
async def test_kpi_upload_and_filters_accept_32_dimensions(kpi_db, kpi_dimension_reader):
    from testrift_server.api_handlers import api_run_kpis_handler

    payload = _payload()
    payload["_samples"] = payload["_samples"][:1]
    payload["_samples"][0]["dimensions"] = {f"key_{index}": index for index in range(32)}
    assert (await api_run_kpis_handler(_request(payload))).status == 201

    handler, query = kpi_dimension_reader
    dimensions = [(f"dimension.key_{index}", str(index)) for index in range(32)]
    response = await handler(Request("GET", query=QueryParams(query + dimensions)))
    assert response.status == 200
    assert json.loads(response.text)["pagination"]["count"] == 1


@pytest.mark.asyncio
async def test_run_catalog_and_series_reads_filter_and_paginate(kpi_db):
    from testrift_server.api_handlers import (
        api_kpi_catalog_handler,
        api_kpi_series_handler,
        api_run_kpis_handler,
    )

    uploaded = await api_run_kpis_handler(_request(_payload()))
    assert uploaded.status == 201

    run_page = await api_run_kpis_handler(Request(
        "GET", run_id="pilot-run-1", query={"limit": "1", "offset": "1"}
    ))
    run_json = json.loads(run_page.text)
    assert run_page.status == 200
    assert run_json["pagination"]["count"] == 2
    assert len(run_json["data"]) == 1

    catalog = await api_kpi_catalog_handler(Request("GET", query={
        "target": "device-a",
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
        "dimension.file_size_bytes": "1000000",
        "limit": "1",
        "offset": "0",
    }))
    catalog_json = json.loads(catalog.text)
    assert catalog.status == 200
    assert catalog_json["pagination"]["count"] == 1
    assert catalog_json["data"][0]["dimensions"]["file_size_bytes"] == 1000000

    series = await api_kpi_series_handler(Request("GET", query={
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
        "dimension.tls": "false",
    }))
    series_json = json.loads(series.text)
    assert series.status == 200
    assert series_json["pagination"]["count"] == 1
    assert series_json["data"][0]["match_status"] == "matched"


@pytest.mark.asyncio
async def test_kpi_dimension_options_are_distinct_filtered_and_typed(kpi_db):
    from testrift_server.api_handlers import api_kpi_dimension_options_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    response = await api_kpi_dimension_options_handler(Request("GET", query={
        "target": "device-a",
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
    }))
    body = json.loads(response.text)
    options = {item["dimension_key"]: item["values"] for item in body["data"]}

    assert response.status == 200
    assert set(options["file_size_bytes"]) == {1000000, 5000000}
    assert set(options["tls"]) == {False, True}
    assert options["protocol"] == ["TCP"]

    filtered = await api_kpi_dimension_options_handler(Request("GET", query={
        "target": "device-a",
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
        "dimension.tls": "false",
    }))
    filtered_options = {
        item["dimension_key"]: item["values"]
        for item in json.loads(filtered.text)["data"]
    }
    assert filtered_options["file_size_bytes"] == [1000000]
    assert filtered_options["tls"] == [False]


@pytest.mark.asyncio
async def test_metric_run_list_returns_only_matching_runs(kpi_db):
    from testrift_server.api_handlers import api_kpi_runs_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    response = await api_kpi_runs_handler(Request("GET", query={
        "target": "device-a",
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
    }))

    body = json.loads(response.text)
    assert response.status == 200
    assert body["pagination"]["count"] == 1
    run = body["data"][0]
    assert run["run_id"] == "pilot-run-1"
    assert run["run_name"] == "KPI pilot"
    assert run["sample_count"] == 2
    assert run["status"] == "running"

    missing_filter = await api_kpi_runs_handler(Request("GET", query={"target": "device-a"}))
    assert missing_filter.status == 400


@pytest.mark.asyncio
async def test_kpi_source_options_and_history_filter_build_revision(kpi_db):
    from testrift_server.api_handlers import (
        api_kpi_history_handler,
        api_kpi_source_options_handler,
        api_run_kpis_handler,
    )

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    base_query = {
        "target": "device-a",
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
    }
    options = await api_kpi_source_options_handler(Request("GET", query=base_query))
    option_body = json.loads(options.text)
    assert options.status == 200
    assert option_body["pagination"]["count"] == 1
    assert option_body["data"] == [{"source_role": "test-system", "branch": "main", "revision": "abc123"}]

    filtered = await api_kpi_history_handler(Request("GET", query={
        **base_query,
        "source_role": "test-system",
        "source_branch": "main",
        "source_revision": "abc123",
    }))
    filtered_body = json.loads(filtered.text)
    assert filtered.status == 200
    assert len(filtered_body["data"]) == 2

    absent = await api_kpi_history_handler(Request("GET", query={
        **base_query,
        "source_revision": "missing",
    }))
    assert json.loads(absent.text)["data"] == []


@pytest.mark.asyncio
async def test_kpi_history_lists_testcases_and_filters_selected_series_by_date(kpi_db):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    base_query = {
        "target": "device-a",
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
        "from": "2026-09-23T00:00:00Z",
        "to": "2026-09-24T00:00:00Z",
        "test_name": "NUnitTest.FileDownload.Download_1M",
    }
    response = await api_kpi_history_handler(Request("GET", query=base_query))

    body = json.loads(response.text)
    assert response.status == 200
    assert {case["test_name"] for case in body["testcases"]} == {
        "NUnitTest.FileDownload.Download_1M",
        "NUnitTest.FileDownload.NotInRun",
    }
    assert body["selected_test_name"] == "NUnitTest.FileDownload.Download_1M"
    assert body["series_test_names"] == ["NUnitTest.FileDownload.Download_1M"]
    assert body["summary"] == {"run_count": 1, "sample_count": 1}
    assert body["data"][0]["value"] == 940000
    assert body["data"][0]["test_case_id"] == "tc-1"

    overview = await api_kpi_history_handler(Request("GET", query={
        key: value for key, value in base_query.items() if key != "test_name"
    }))
    overview_body = json.loads(overview.text)
    assert overview.status == 200
    assert overview_body["selected_test_name"] is None
    assert set(overview_body["series_test_names"]) == {
        "NUnitTest.FileDownload.Download_1M",
        "NUnitTest.FileDownload.NotInRun",
    }
    assert {point["test_name"] for point in overview_body["data"]} == set(
        overview_body["series_test_names"]
    )
    assert overview_body["summary"] == {"run_count": 1, "sample_count": 2}

    outside_date = await api_kpi_history_handler(Request("GET", query={
        **base_query,
        "from": "2026-09-24T00:00:00Z",
        "to": "2026-09-25T00:00:00Z",
    }))
    outside_body = json.loads(outside_date.text)
    assert outside_date.status == 200
    assert outside_body["testcases"] == []
    assert outside_body["data"] == []


@pytest.mark.asyncio
async def test_kpi_history_can_compare_the_same_test_across_targets(kpi_db):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    other_run = TestRunData(
        run_id="pilot-run-b26",
        status="finished",
        start_time="2026-09-23T11:00:00Z",
        end_time="2026-09-23T11:05:00Z",
        retention_days=None,
        local_run=True,
        dut="Device B",
        target_key="device-b",
        run_name="Device B KPI pilot",
    )
    assert await kpi_db.insert_test_run(other_run, {}, {})
    assert (await api_run_kpis_handler(_request(_payload(value=820000), other_run.run_id))).status == 201

    response = await api_kpi_history_handler(Request("GET", query=QueryParams(
        "target=device-a&target=device-b&metric_key=file_download.rx_throughput"
        "&unit=bps&test_name=NUnitTest.FileDownload.Download_1M"
    )))

    body = json.loads(response.text)
    assert response.status == 200
    assert {(point["target_key"], point["value"]) for point in body["data"]} == {
        ("device-a", 940000),
        ("device-b", 820000),
    }


@pytest.mark.asyncio
async def test_kpi_history_catalog_and_exact_fixture_group(kpi_db):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    payload = _payload()
    payload["_samples"].append({
        **payload["_samples"][0],
        "test_name": "NUnitTest.Other.Download_1M",
    })
    assert (await api_run_kpis_handler(_request(payload))).status == 201
    query = {"target": "device-a", "metric_key": "file_download.rx_throughput", "unit": "bps"}
    catalog = json.loads((await api_kpi_history_handler(Request("GET", query={
        **query, "catalog_only": "1",
    }))).text)
    assert len(catalog["testcases"]) == 3
    assert catalog["data"] == []

    grouped = json.loads((await api_kpi_history_handler(Request("GET", query={
        **query, "test_group": "NUnitTest.FileDownload",
    }))).text)
    assert set(grouped["series_test_names"]) == {
        "NUnitTest.FileDownload.Download_1M", "NUnitTest.FileDownload.NotInRun",
    }
    assert {point["test_name"] for point in grouped["data"]} == set(grouped["series_test_names"])
    assert grouped["summary"] == {"run_count": 1, "sample_count": 2}
    assert (await api_kpi_history_handler(Request("GET", query={
        **query, "test_group": "NUnitTest.File",
    }))).status == 400
    assert (await api_kpi_history_handler(Request("GET", query={
        **query, "test_group": "NUnitTest.FileDownload", "test_name": "NUnitTest.Other.Download_1M",
    }))).status == 400


@pytest.mark.asyncio
async def test_kpi_history_all_metric_catalog_preserves_metric_and_zero_values(kpi_db):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    payload = _payload()
    payload["_samples"].append({
        **payload["_samples"][0], "metric_key": "throughput.tx_throughput", "value": 0,
    })
    assert (await api_run_kpis_handler(_request(payload))).status == 201
    response = await api_kpi_history_handler(Request("GET", query={
        "target": "device-a", "catalog_only": "1",
    }))
    body = json.loads(response.text)
    assert response.status == 200
    assert body["data"] == []
    assert {(item["metric_key"], item["unit"], item["test_name"], item["max_abs_value"])
            for item in body["testcases"]} == {
        ("file_download.rx_throughput", "bps", "NUnitTest.FileDownload.Download_1M", 940000),
        ("file_download.rx_throughput", "bps", "NUnitTest.FileDownload.NotInRun", 940100),
        ("throughput.tx_throughput", "bps", "NUnitTest.FileDownload.Download_1M", 0),
    }
    assert (await api_kpi_history_handler(Request("GET", query={"target": "device-a"}))).status == 400


@pytest.mark.asyncio
async def test_metric_catalog_collapses_dimensions_into_metric_unit_pairs(kpi_db):
    from testrift_server.api_handlers import api_kpi_metrics_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    response = await api_kpi_metrics_handler(Request("GET", query={"target": "device-a"}))
    body = json.loads(response.text)

    assert response.status == 200
    assert len(body["data"]) == 1
    assert body["data"][0]["metric_key"] == "file_download.rx_throughput"
    assert body["data"][0]["sample_count"] == 2


@pytest.mark.asyncio
async def test_invalid_kpi_dimension_filter_returns_validation_error(kpi_db):
    from testrift_server.api_handlers import api_kpi_catalog_handler

    response = await api_kpi_catalog_handler(Request("GET", query={"dimension.bad.key": "x"}))
    assert response.status == 400


@pytest.mark.asyncio
async def test_large_dimension_numbers_are_validation_errors(kpi_db):
    from testrift_server.api_handlers import api_kpi_catalog_handler, api_run_kpis_handler

    payload = _payload(file_size=10 ** 400)
    assert (await api_run_kpis_handler(_request(payload))).status == 400
    response = await api_kpi_catalog_handler(Request("GET", query={"dimension.size": str(10 ** 400)}))
    assert response.status == 400


@pytest.mark.asyncio
async def test_history_and_catalog_pagination_preserve_total_summary(kpi_db):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    query = {"target": "device-a", "metric_key": "file_download.rx_throughput", "unit": "bps", "limit": "1"}
    pages = []
    for offset in (0, 1, 2):
        body = json.loads((await api_kpi_history_handler(Request("GET", query={**query, "offset": str(offset)}))).text)
        assert body["pagination"]["count"] == 2
        assert body["summary"] == {"run_count": 1, "sample_count": 2}
        assert len(body["data"]) == (1 if offset < 2 else 0)
        pages.extend(body["data"])
    assert len({point["test_name"] for point in pages}) == 2
    for catalog_query in (query, {"target": "device-a", "limit": "1"}):
        body = json.loads((await api_kpi_history_handler(Request("GET", query={
            **catalog_query, "catalog_only": "1", "offset": "1",
        }))).text)
        assert body["pagination"]["count"] == 2
        assert len(body["testcases"]) == 1


@pytest.mark.asyncio
async def test_history_groups_unqualified_names_without_mixing_qualified_tests(kpi_db):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    payload = _payload()
    payload["_samples"][0]["test_name"] = "login_latency"
    assert (await api_run_kpis_handler(_request(payload))).status == 201
    body = json.loads((await api_kpi_history_handler(Request("GET", query={
        "target": "device-a", "metric_key": "file_download.rx_throughput", "unit": "bps", "test_group": "",
    }))).text)
    assert body["series_test_names"] == ["login_latency"]
    assert len(body["data"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("test_name, extra_filters", [
    ("NUnitTest.FileDownload.Missing", {}),
    ("NUnitTest.FileDownload.Download_1M", {"from": "2026-09-23T10:00:30Z"}),
    ("NUnitTest.FileDownload.Download_1M", {"dimension.tls": "true"}),
])
async def test_explicit_history_test_without_samples_never_falls_back(kpi_db, test_name, extra_filters):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    query = {
        "target": "device-a", "metric_key": "file_download.rx_throughput", "unit": "bps",
        **extra_filters,
    }
    overview_response = await api_kpi_history_handler(Request("GET", query=query))
    assert overview_response.status == 200
    overview = json.loads(overview_response.text)
    assert overview["data"]
    assert all(point["test_name"] != test_name for point in overview["data"])

    response = await api_kpi_history_handler(Request("GET", query={**query, "test_name": test_name}))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["selected_test_name"] == test_name
    assert body["series_test_names"] == [test_name]
    assert body["data"] == []
    assert body["pagination"]["count"] == 0
    assert body["summary"] == {"run_count": 0, "sample_count": 0}


@pytest.mark.asyncio
async def test_offset_timestamps_are_normalized_and_old_samples_order_chronologically(kpi_db):
    from testrift_server.api_handlers import api_run_kpis_handler

    payload = _payload()
    payload["_samples"][0]["timestamp_utc"] = "2026-09-23T10:00:00+02:00"
    payload["_samples"][1]["timestamp_utc"] = "2026-09-23T09:00:00Z"
    assert (await api_run_kpis_handler(_request(payload))).status == 201
    rows, _ = await kpi_db.get_kpi_samples({})
    assert rows[0]["sample_time"] == "2026-09-23T08:00:00Z"
    async with kpi_db.get_connection() as connection:
        await connection.execute("UPDATE kpi_samples SET sample_time = ? WHERE ordinal = 0", ("2026-09-23T10:00:00+02:00",))
        await connection.commit()
    rows, _ = await kpi_db.get_kpi_samples({})
    assert rows[0]["ordinal"] == 0
    metrics = await kpi_db.get_kpi_metrics({})
    assert metrics[0]["first_sample"] == "2026-09-23T08:00:00.000Z"
    assert metrics[0]["last_sample"] == "2026-09-23T09:00:00.000Z"


@pytest.mark.asyncio
async def test_history_mean_combines_only_dimensions_matching_the_filter(kpi_db):
    from testrift_server.api_handlers import api_kpi_history_handler, api_run_kpis_handler

    payload = _payload(value=10)
    payload["_samples"][1].update(test_name=payload["_samples"][0]["test_name"], value=30)
    assert (await api_run_kpis_handler(_request(payload))).status == 201
    query = {"target": "device-a", "metric_key": "file_download.rx_throughput", "unit": "bps"}
    body = json.loads((await api_kpi_history_handler(Request("GET", query=query))).text)
    assert body["data"][0]["value"] == 20
    assert body["data"][0]["minimum"] == 10
    assert body["data"][0]["maximum"] == 30
    filtered = json.loads((await api_kpi_history_handler(Request("GET", query={**query, "dimension.tls": "false"}))).text)
    assert filtered["data"][0]["value"] == 10


@pytest.mark.asyncio
async def test_initialization_preserves_existing_kpis_and_adds_tables_to_supported_schema(kpi_db):
    from testrift_server.api_handlers import api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    reopened = database.TestResultsDatabase(kpi_db.db_path)
    await reopened.initialize()
    _, count = await reopened.get_kpi_samples({})
    assert count == 2
    async with reopened.get_connection() as connection:
        await connection.execute("DROP TABLE kpi_samples")
        await connection.execute("DROP TABLE kpi_batches")
        await connection.execute("DELETE FROM schema_migrations WHERE version = 1")
        await connection.commit()
    upgraded = database.TestResultsDatabase(kpi_db.db_path)
    await upgraded.initialize()
    assert await upgraded.get_test_run_by_id("pilot-run-1") is not None
    _, count = await upgraded.get_kpi_samples({})
    assert count == 0
