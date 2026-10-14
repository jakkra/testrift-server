# KPI Queries

## Run Purpose

Cross-run KPI queries default to runs with purpose `nightly` or `release`.
This applies to history, comparisons, catalogs, run lists, source options,
and dimension options. Manual, feature, sanity, and rerun measurements remain
stored, but do not enter these default trend views.

Pass `purpose=manual` to inspect manual or sandbox measurements, or
`purpose=all` to include every purpose. A single explicit purpose can be
`nightly`, `release`, `feature`, `manual`, `sanity`, or `rerun`; unsupported
values return HTTP 400. All other filters still apply.

Queries selecting an explicit run ID, including `GET /api/runs/{run_id}/kpis`,
return that run's measurements regardless of purpose unless an explicit
purpose filter is supplied. Uploads and existing stored data are unchanged.
Run purpose is supplied by the producer; a firmware tag alone does not make
a run nightly, and this filter does not enforce tag naming conventions.

## KPI Dimension Options

Dimension options retain the existing scalar `values` array and add an
`options` array in the same order. Each option contains `value`, `value_type`
(SQLite's JSON scalar type), and `filter_token`:

```json
{
  "success": true,
  "data": [
    {
      "dimension_key": "size",
      "values": [9007199254740993],
      "options": [
        {
          "value": 9007199254740993,
          "value_type": "integer",
          "filter_token": "9007199254740993"
        }
      ]
    }
  ]
}
```

`filter_token` is serialized by Python directly from the database scalar with
`json.dumps`, compact separators, and `allow_nan=False`. Pass it unchanged as
the URL-encoded value of `dimension.<dimension_key>`. Do not reconstruct it from
`value`: JavaScript numbers cannot represent all signed 64-bit integers exactly.
Use the token for numeric option labels as well. Existing consumers of `values`
remain compatible; precision-sensitive consumers should use `options`.

Boolean tokens are `true` and `false`, not integer `1` and `0`. String tokens
include JSON quotes and escaping, so the string `"true"` remains distinct from
the boolean `true`. Supported scalar types are `true`, `false`, `integer`,
`real`, and `text`; upload and query validation continue to reject null,
containers, non-finite numbers, and integers outside the signed 64-bit range.

Dimension filters separate booleans, numbers, and strings. Integer and real
values retain numeric equality, so a filter of `1` also matches `1.0`, but never
`true` or `"1"`. These rules apply to history, cross-target comparisons, and
dimension-option queries through the shared database filter builder.
