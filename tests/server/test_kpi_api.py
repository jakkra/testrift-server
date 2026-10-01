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
        dut="NORA-W36X",
        target_key="nora-w36x",
        run_name="KPI pilot",
    )
    assert await database.db.insert_test_run(run, {}, {
        "nut": {"branch": "main", "revision": "abc123"},
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
        "target": "nora-w36x",
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
        "target": "nora-w36x",
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
        "target": "nora-w36x",
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
        "target": "nora-w36x",
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

    missing_filter = await api_kpi_runs_handler(Request("GET", query={"target": "nora-w36x"}))
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
        "target": "nora-w36x",
        "metric_key": "file_download.rx_throughput",
        "unit": "bps",
    }
    options = await api_kpi_source_options_handler(Request("GET", query=base_query))
    option_body = json.loads(options.text)
    assert options.status == 200
    assert option_body["pagination"]["count"] == 1
    assert option_body["data"] == [{"source_role": "nut", "branch": "main", "revision": "abc123"}]

    filtered = await api_kpi_history_handler(Request("GET", query={
        **base_query,
        "source_role": "nut",
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
        "target": "nora-w36x",
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
        dut="NORA-B26X",
        target_key="nora-b26x",
        run_name="B26 KPI pilot",
    )
    assert await kpi_db.insert_test_run(other_run, {}, {})
    assert (await api_run_kpis_handler(_request(_payload(value=820000), other_run.run_id))).status == 201

    response = await api_kpi_history_handler(Request("GET", query=QueryParams(
        "target=nora-w36x&target=nora-b26x&metric_key=file_download.rx_throughput"
        "&unit=bps&test_name=NUnitTest.FileDownload.Download_1M"
    )))

    body = json.loads(response.text)
    assert response.status == 200
    assert {(point["target_key"], point["value"]) for point in body["data"]} == {
        ("nora-w36x", 940000),
        ("nora-b26x", 820000),
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
    query = {"target": "nora-w36x", "metric_key": "file_download.rx_throughput", "unit": "bps"}
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
        "target": "nora-w36x", "catalog_only": "1",
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
    assert (await api_kpi_history_handler(Request("GET", query={"target": "nora-w36x"}))).status == 400


@pytest.mark.asyncio
async def test_metric_catalog_collapses_dimensions_into_metric_unit_pairs(kpi_db):
    from testrift_server.api_handlers import api_kpi_metrics_handler, api_run_kpis_handler

    assert (await api_run_kpis_handler(_request(_payload()))).status == 201
    response = await api_kpi_metrics_handler(Request("GET", query={"target": "nora-w36x"}))
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
