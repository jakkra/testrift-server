# KPI Measurements

TestRift stores numeric measurements against a test run and exposes history at
`/targets/{target_key}/kpis`. Targets, metric keys, and dimensions are supplied
by the test producer; no hardware product catalog or importer is required.

## Upload Format

Once a run exists, POST UTF-8 JSON to `/api/runs/{run_id}/kpis`:

```json
{
  "_schemaVersion": 1,
  "_samples": [
    {
      "metric_key": "request.latency",
      "value": 12.5,
      "unit": "ms",
      "test_name": "login_latency",
      "dimensions": {"protocol": "HTTPS", "concurrency": 4},
      "timestamp_utc": "2026-10-06T10:00:00Z"
    }
  ]
}
```

- `metric_key`: 1-128 lowercase ASCII letters, digits, dots, underscores, or
  hyphens; the first character must be a letter or digit.
- `value`: a finite JSON number, including negative values and zero, stored as
  a double-precision value. Boolean values are not measurements.
- `unit`: 1-32 ASCII characters matching `[A-Za-z0-9][A-Za-z0-9._/%*-]*`.
  Units are case-sensitive identifiers, not a unit conversion language.
- `test_name`: a nonblank string of at most 1024 characters. Exact matches to
  run test cases enable log links; unmatched measurements are still stored.
  Dot-qualified names form families; plain names appear under Ungrouped.
- `dimensions`: optional object with at most 32 scalar values. Keys begin with
  an ASCII letter and contain only letters, digits, or underscores. Values may
  be booleans, strings of at most 256 characters, finite floating-point numbers,
  or signed 64-bit integers. Arrays, objects, and null are not dimensions.
- `timestamp_utc`: optional timezone-aware ISO-8601 timestamp, normalized to
  UTC on ingest. Without it, time filters use the run start time. History points
  are positioned by run start time because each point summarizes one run/test.

Requests are limited to 10 MiB and 10,000 samples. Invalid payloads return 400,
oversized requests 413, and unknown runs 404. The payload must contain `_samples`;
legacy fields may coexist, but are not converted or used by the server.

## Producer And Retry Contract

The first integration is TestRift.NUnit, which uploads an output-directory
`kpi.json` at run completion. Other clients can submit the same JSON format.
Version 1 reserves one upload slot per run, internally named `nunit:kpi.json`.
The slot and provenance label retain the initial integration name; they do not
restrict metric or target identities. Multiple independent writers to the same
run are not supported: the last different payload replaces that slot.

Byte-identical retries return 200 with `idempotent: true`. A different payload
atomically replaces the previous slot and returns 201. This is replacement,
not append. An empty `_samples` array clears the slot. Raw payload JSON and
normalized samples are stored together; unknown legacy fields remain only in
the raw payload. Do not place secrets in payloads or dimensions.

## Queries And Comparisons

Metric keys and units are exact pairs. Producers should agree on names and
canonical units, for example `ms`, `bps`, `bytes`, or `count`. `bps` and `B/s`
are distinct; TestRift does not convert between them. The dashboard may scale
`bps` to `kb/s` or `Mb/s` for display without changing the stored values.

History shows the arithmetic mean of matching samples per run and exact test
name, with minimum, maximum, and measurement count. Unfiltered dimensions are
combined. To avoid mixing workloads, select dimensions such as concurrency,
protocol, or payload size, or use distinct metric/test identities. There is no
automatic percentile calculation or pass/fail threshold evaluation.

Cross-target comparison requires the same metric key, unit, and exact test name.
Dimension and source/build filters apply to every selected target. Matching
identifiers indicate available data, not scientific equivalence; producers are
responsible for comparable workloads and environments. At most 50 targets may
be queried together.

Paginated reads use `limit` (1-500, default 100) and `offset` (0-1,000,000).
`pagination.count` reports the total matching items. For `/api/kpis/history`,
pages contain aggregate `data` points, or `testcases` when `catalog_only=1`;
`summary` covers the full selected history. The dashboard fetches all pages.
Use date, metric, and dimension filters to bound large queries.

## Access, Retention, And Upgrades

Uploads use the existing ingest policy: when `auth.ingest_token` is configured,
send `X-TestRift-Ingest-Token` or a Bearer token. With no ingest token, ingest is
open, even if browser login is enabled. Honor the configured ingest TLS policy.
Reads use the existing `runs.read` permission when authentication is enabled.
There is no per-target tenant isolation; this follows the shared-server access
model. See [authentication](authentication.md) and [TLS](tls.md).

Artifact retention removes run directories, not database history, KPI samples,
or raw KPI payloads. Deleting a run from the database removes its dependent KPI
data. Back up the database and plan storage capacity separately from artifact
retention. Log links cannot recover artifacts that have expired.

Initialization adds KPI tables to the supported Target/Collection database
schema without rewriting existing runs. It does not migrate the retired
group-based schema. Back up the database before upgrading server versions.

## Bundled Browser Libraries

ECharts 6.1.0 and Fuse.js 7.1.0 are pinned in `package.json` and served locally.
Their distributions retain their upstream license headers. ECharts NOTICE and
both licenses are bundled under `static/*-NOTICE.txt` and `static/*-LICENSE.txt`.
When updating versions, refresh the browser builds and these accompanying files.