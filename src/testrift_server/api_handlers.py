"""
API handlers for TestRift server.

All /api/* endpoints for test results analysis and data access.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
from datetime import datetime, timezone

from .http_compat import RequestBodyTooLarge, web

from .config import (
    CONFIG,
    CONFIG_PATH_USED,
    get_config_fingerprint,
    get_config_hash,
)
from .models import TestRunData
from .utils import (
    get_run_path,
    get_case_log_path,
    get_case_stack_path,
    find_test_case_by_tc_id,
    read_jsonl,
    read_mplog,
    validate_run_id,
    TC_ID_FIELD,
    TC_FULL_NAME_FIELD,
)
from . import database
from .summary_profiles import select_profile_from_database, resolve_run_set
from .ai_analysis import create_collection_report

logger = logging.getLogger(__name__)

TARGET_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PURPOSES = {"nightly", "release", "feature", "manual", "sanity", "rerun"}
MAX_KPI_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_KPI_SAMPLES = 10000
KPI_METRIC_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
KPI_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/%*-]{0,31}$")


async def _json_body(request):
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def _validate_key(value, field="key"):
    if not isinstance(value, str) or not TARGET_KEY_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must contain lowercase letters, digits, and hyphens")
    return value


def _validate_display_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("display_name cannot be blank")
    return value.strip()


def _validation_error(error):
    return web.json_response({"success": False, "error": str(error)}, status=400)


def _validate_kpi_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    version = payload.get("_schemaVersion")
    if type(version) is not int or version != 1:
        raise ValueError("Unsupported or missing _schemaVersion")
    samples = payload.get("_samples")
    if not isinstance(samples, list) or len(samples) > MAX_KPI_SAMPLES:
        raise ValueError("_samples must be an array with at most 10000 items")

    normalized = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ValueError(f"_samples[{index}] must be an object")
        metric_key = sample.get("metric_key")
        unit = sample.get("unit")
        test_name = sample.get("test_name")
        value = sample.get("value")
        if not isinstance(metric_key, str) or not KPI_METRIC_KEY_PATTERN.fullmatch(metric_key):
            raise ValueError(f"_samples[{index}].metric_key is invalid")
        if not isinstance(unit, str) or not KPI_UNIT_PATTERN.fullmatch(unit):
            raise ValueError(f"_samples[{index}].unit is invalid")
        if not isinstance(test_name, str) or not test_name.strip() or len(test_name) > 1024:
            raise ValueError(f"_samples[{index}].test_name is invalid")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"_samples[{index}].value must be numeric")
        try:
            numeric_value = float(value)
        except (OverflowError, ValueError):
            raise ValueError(f"_samples[{index}].value must be finite")
        if not math.isfinite(numeric_value):
            raise ValueError(f"_samples[{index}].value must be finite")

        dimensions = sample.get("dimensions", {})
        if not isinstance(dimensions, dict) or len(dimensions) > 32:
            raise ValueError(f"_samples[{index}].dimensions must be an object with at most 32 items")
        for key, dimension in dimensions.items():
            if not isinstance(key, str) or not key.isascii() or not key.replace("_", "").isalnum() or not key[0].isalpha():
                raise ValueError(f"_samples[{index}] has an invalid dimension name")
            if isinstance(dimension, bool):
                continue
            if isinstance(dimension, (int, float)):
                if not math.isfinite(float(dimension)):
                    raise ValueError(f"_samples[{index}] has a non-finite dimension")
            elif not isinstance(dimension, str) or len(dimension) > 256:
                raise ValueError(f"_samples[{index}] has an invalid dimension value")

        timestamp = sample.get("timestamp_utc")
        if timestamp is not None:
            if not isinstance(timestamp, str):
                raise ValueError(f"_samples[{index}].timestamp_utc must be a string")
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"_samples[{index}].timestamp_utc must be ISO-8601")
            if parsed_timestamp.tzinfo is None:
                raise ValueError(f"_samples[{index}].timestamp_utc must include a timezone")

        normalized.append({
            "metric_key": metric_key,
            "value": numeric_value,
            "unit": unit,
            "test_name": test_name,
            "dimensions": dimensions,
            "timestamp_utc": timestamp,
        })
    return version, normalized


def _kpi_read_filters(request, require_series=False):
    query = request.query
    filters = {}
    if hasattr(query, "getall"):
        target_values = query.getall("target", [])
    elif hasattr(query, "getlist"):
        target_values = query.getlist("target")
    else:
        target_values = query.get("target")
    if isinstance(target_values, str):
        target_values = [target_values]
    elif not target_values:
        target_values = []
    target_keys = list(dict.fromkeys(value for value in target_values if value))
    if len(target_keys) > 50:
        raise ValueError("at most 50 targets may be queried at once")
    if any(not TARGET_KEY_PATTERN.fullmatch(value) for value in target_keys):
        raise ValueError("target is invalid")
    if len(target_keys) == 1:
        filters["target_key"] = target_keys[0]
    elif target_keys:
        filters["target_keys"] = target_keys

    for query_name, filter_name in (("run_id", "run_id"),
                                    ("metric_key", "metric_key"), ("unit", "unit")):
        value = query.get(query_name)
        if value:
            filters[filter_name] = value
    if filters.get("metric_key") and not KPI_METRIC_KEY_PATTERN.fullmatch(filters["metric_key"]):
        raise ValueError("metric_key is invalid")
    if filters.get("unit") and not KPI_UNIT_PATTERN.fullmatch(filters["unit"]):
        raise ValueError("unit is invalid")
    if require_series and (not filters.get("metric_key") or not filters.get("unit")):
        raise ValueError("metric_key and unit are required")

    for key in ("source_role", "source_branch", "source_revision"):
        value = query.get(key)
        if value:
            if len(value) > 256:
                raise ValueError(f"{key} is too long")
            filters[key] = value

    for query_name, filter_name in (("from", "start_time"), ("to", "end_time")):
        value = query.get(query_name)
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"{query_name} must be ISO-8601")
            if parsed.tzinfo is None:
                raise ValueError(f"{query_name} must include a timezone")
            filters[filter_name] = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    dimensions = {}
    for name, value in query.items():
        if name.startswith("dimension."):
            key = name[len("dimension."):]
            try:
                dimensions[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                dimensions[key] = value
    if dimensions:
        filters["dimensions"] = dimensions

    try:
        limit = int(query.get("limit", 100))
        offset = int(query.get("offset", 0))
    except (TypeError, ValueError):
        raise ValueError("limit and offset must be integers")
    if not 1 <= limit <= 500 or not 0 <= offset <= 1000000:
        raise ValueError("limit must be 1..500 and offset must be 0..1000000")
    return filters, limit, offset


async def api_run_kpis_handler(request):
    run_id = request.match_info["run_id"]
    if not validate_run_id(run_id):
        return web.json_response({"success": False, "error": "Invalid run ID"}, status=400)

    run = await database.db.get_test_run_by_id(run_id)
    if run is None:
        return web.json_response({"success": False, "error": "Run not found"}, status=404)

    if request.method == "GET":
        try:
            filters, limit, offset = _kpi_read_filters(request)
            filters["run_id"] = run_id
            samples, total = await database.db.get_kpi_samples(filters, limit, offset)
            for sample in samples:
                sample["dimensions"] = json.loads(sample.pop("dimensions_json"))
                sample["provenance"] = json.loads(sample.pop("provenance_json"))
            return web.json_response({
                "success": True,
                "data": samples,
                "pagination": {"limit": limit, "offset": offset, "count": total},
            })
        except ValueError as error:
            return _validation_error(error)

    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > MAX_KPI_UPLOAD_BYTES:
            return web.json_response({"success": False, "error": "KPI payload exceeds 10 MB"}, status=413)
    except ValueError:
        return _validation_error(ValueError("Invalid Content-Length"))
    try:
        raw_body = await request.read(MAX_KPI_UPLOAD_BYTES)
    except RequestBodyTooLarge:
        return web.json_response({"success": False, "error": "KPI payload exceeds 10 MB"}, status=413)
    except Exception:
        return _validation_error(ValueError("Invalid JSON body"))
    try:
        raw_json = raw_body.decode("utf-8")
        payload = json.loads(raw_json)
        version, samples = _validate_kpi_payload(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return _validation_error(error)

    checksum = hashlib.sha256(raw_body).hexdigest()
    provenance = {"client": "TestRift.NUnit", "source_file": "kpi.json"}
    try:
        result = await database.db.replace_kpi_batch(
            run_id=run_id,
            source_key="nunit:kpi.json",
            checksum_sha256=checksum,
            schema_version=version,
            source_type="nunit-json-kpi",
            raw_json=raw_json,
            payload_size=len(raw_body),
            provenance=provenance,
            samples=samples,
        )
    except ValueError as error:
        return web.json_response({"success": False, "error": str(error)}, status=404)
    return web.json_response({"success": True, **result}, status=200 if result["idempotent"] else 201)


async def api_kpi_catalog_handler(request):
    try:
        filters, limit, offset = _kpi_read_filters(request)
        items, total = await database.db.get_kpi_catalog(filters, limit, offset)
        for item in items:
            item["dimensions"] = json.loads(item.pop("dimensions_json"))
        return web.json_response({
            "success": True,
            "data": items,
            "pagination": {"limit": limit, "offset": offset, "count": total},
        })
    except ValueError as error:
        return _validation_error(error)


async def api_kpi_dimension_options_handler(request):
    try:
        filters, _, _ = _kpi_read_filters(request)
        options = await database.db.get_kpi_dimension_options(filters)
        return web.json_response({"success": True, "data": options})
    except ValueError as error:
        return _validation_error(error)


async def api_kpi_metrics_handler(request):
    try:
        filters, _, _ = _kpi_read_filters(request)
        if not filters.get("target_key") and not filters.get("target_keys"):
            raise ValueError("target is required")
        metrics = await database.db.get_kpi_metrics(filters)
        return web.json_response({"success": True, "data": metrics})
    except ValueError as error:
        return _validation_error(error)


async def api_kpi_runs_handler(request):
    try:
        filters, limit, offset = _kpi_read_filters(request)
        if (not filters.get("target_key") and not filters.get("target_keys")) or not filters.get("metric_key") or not filters.get("unit"):
            raise ValueError("target, metric_key, and unit are required")
        runs, total = await database.db.get_kpi_runs(filters, limit, offset)
        return web.json_response({
            "success": True,
            "data": runs,
            "pagination": {"limit": limit, "offset": offset, "count": total},
        })
    except ValueError as error:
        return _validation_error(error)


async def api_kpi_source_options_handler(request):
    try:
        filters, limit, offset = _kpi_read_filters(request)
        if not filters.get("target_key") and not filters.get("target_keys"):
            raise ValueError("target is required")
        options, total = await database.db.get_kpi_source_options(filters, limit, offset)
        return web.json_response({
            "success": True,
            "data": options,
            "pagination": {"limit": limit, "offset": offset, "count": total},
        })
    except ValueError as error:
        return _validation_error(error)


async def api_kpi_series_handler(request):
    try:
        filters, limit, offset = _kpi_read_filters(request, require_series=True)
        samples, total = await database.db.get_kpi_samples(filters, limit, offset)
        for sample in samples:
            sample["dimensions"] = json.loads(sample.pop("dimensions_json"))
            sample["provenance"] = json.loads(sample.pop("provenance_json"))
        return web.json_response({
            "success": True,
            "data": samples,
            "pagination": {"limit": limit, "offset": offset, "count": total},
        })
    except ValueError as error:
        return _validation_error(error)


async def api_kpi_history_handler(request):
    try:
        catalog_only = request.query.get("catalog_only") == "1"
        filters, _, _ = _kpi_read_filters(request, require_series=not catalog_only)
        if not filters.get("target_key") and not filters.get("target_keys"):
            raise ValueError("target is required")
        test_name = request.query.get("test_name")
        if test_name is not None:
            if not test_name.strip() or len(test_name) > 1024:
                raise ValueError("test_name must contain 1..1024 characters")
            filters["test_name"] = test_name
        test_group = request.query.get("test_group")
        if test_group is not None:
            if not test_group.strip() or len(test_group) > 1024 or test_name is not None:
                raise ValueError("test_group must contain 1..1024 characters and cannot be combined with test_name")
            filters["test_group"] = test_group
        if catalog_only:
            filters["catalog_only"] = True
        result = await database.db.get_kpi_history(filters)
        return web.json_response({"success": True, **result})
    except ValueError as error:
        return _validation_error(error)


async def _context_run_ids(request, current_run_id=None):
    """Return shared Run-set IDs when a Target or Collection context is requested."""
    target_key = request.query.get("target")
    collection_key = request.query.get("collection")
    if not target_key and not collection_key and current_run_id:
        run = await database.db.get_test_run_by_id(current_run_id)
        target_key = run.get("target_key") if run else None
    if not target_key and not collection_key:
        return None
    requested_at = request.query.get("at")
    run_set = await resolve_run_set(
        database.db,
        target_key=target_key,
        collection_key=collection_key,
        profile_id=int(request.query.get("profile_id") or request.query.get("profile")) if request.query.get("profile_id") or request.query.get("profile") else None,
        requested_at=datetime.fromisoformat(requested_at.replace("Z", "+00:00")) if requested_at else None,
    )
    return run_set["run_ids"]


async def api_targets_handler(request):
    if request.method == "GET":
        targets = await database.db.list_targets(request.query.get("needs_setup") == "true")
        return web.json_response({"success": True, "data": targets})
    try:
        body = await _json_body(request)
        key = _validate_key(body.get("key"), "key")
        target = await database.db.get_or_create_target(key, _validate_display_name(body.get("display_name", key)))
        return web.json_response({"success": True, "data": target}, status=201)
    except (ValueError, TypeError) as error:
        return _validation_error(error)


async def api_target_handler(request):
    key = request.match_info["key"]
    if request.method == "GET":
        target = await database.db.get_target(key)
        return web.json_response({"success": True, "data": target}) if target else web.json_response({"success": False, "error": "Target not found"}, status=404)
    if request.method == "DELETE":
        if request.query.get("cascade") != "true":
            return _validation_error("Target deletion requires cascade=true")
        deleted = await database.db.delete_target(key)
        return web.json_response({"success": True}) if deleted else web.json_response({"success": False, "error": "Target not found"}, status=404)
    try:
        body = await _json_body(request)
        state = body.get("setup_state")
        if state not in {"needs_setup", "ready"}:
            raise ValueError("setup_state must be needs_setup or ready")
        target = await database.db.update_target(key, _validate_display_name(body.get("display_name")), state)
        return web.json_response({"success": True, "data": target}) if target else web.json_response({"success": False, "error": "Target not found"}, status=404)
    except (ValueError, TypeError) as error:
        return _validation_error(error)


async def api_target_complete_setup_handler(request):
    try:
        body = await _json_body(request)
        collection_ids = body.get("collection_ids", [])
        if not isinstance(collection_ids, list) or len(collection_ids) != len(set(collection_ids)):
            raise ValueError("collection_ids must be a unique list")
        target = await database.db.complete_target_setup(
            request.match_info["key"], _validate_display_name(body.get("display_name")), collection_ids
        )
        return web.json_response({"success": True, "data": target}) if target else web.json_response({"success": False, "error": "Target not found"}, status=404)
    except (ValueError, TypeError) as error:
        return _validation_error(error)


async def api_collections_handler(request):
    if request.method == "GET":
        return web.json_response({"success": True, "data": await database.db.list_collections()})
    try:
        body = await _json_body(request)
        collection_id = await database.db.create_collection(
            _validate_key(body.get("key"), "key"), _validate_display_name(body.get("display_name")), body.get("description"),
            bool(body.get("ai_summary_enabled", False)), bool(body.get("email_enabled", False)), body.get("recipients", []),
        )
        return web.json_response({"success": True, "data": {"id": collection_id}}, status=201)
    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as error:
        return web.json_response({"success": False, "error": str(error)}, status=409)


async def api_collection_handler(request):
    key = request.match_info["key"]
    if request.method == "GET":
        collection = await database.db.get_collection(key)
        return web.json_response({"success": True, "data": collection}) if collection else web.json_response({"success": False, "error": "Collection not found"}, status=404)
    if request.method == "DELETE":
        if request.query.get("cascade") != "true":
            return _validation_error("Collection deletion requires cascade=true")
        deleted = await database.db.delete_collection(key)
        return web.json_response({"success": True}) if deleted else web.json_response({"success": False, "error": "Collection not found"}, status=404)
    try:
        body = await _json_body(request)
        collection = await database.db.update_collection(key, {**body, "display_name": _validate_display_name(body.get("display_name"))})
        return web.json_response({"success": True, "data": collection}) if collection else web.json_response({"success": False, "error": "Collection not found"}, status=404)
    except (ValueError, TypeError) as error:
        return _validation_error(error)


async def api_collection_members_handler(request):
    collection = await database.db.get_collection(request.match_info["key"])
    if not collection:
        return web.json_response({"success": False, "error": "Collection not found"}, status=404)
    try:
        target_ids = (await _json_body(request)).get("target_ids")
        if not isinstance(target_ids, list) or len(target_ids) != len(set(target_ids)):
            raise ValueError("target_ids must be a unique list")
        known_targets = {target["id"] for target in await database.db.list_targets()}
        if not set(target_ids).issubset(known_targets):
            raise ValueError("target_ids contains an unknown Target")
        await database.db.replace_collection_membership(collection["id"], target_ids)
        return web.json_response({"success": True})
    except (ValueError, TypeError) as error:
        return _validation_error(error)


async def api_collection_profile_filter_options_handler(request):
    """Suggest profile filter values observed in finished runs for Collection Targets."""
    collection = await database.db.get_collection(request.match_info["key"])
    if not collection:
        return web.json_response({"success": False, "error": "Collection not found"}, status=404)
    options = await database.db.get_collection_profile_filter_options(collection["id"])
    return web.json_response({"success": True, "data": options})


def _profile_values(body):
    name = _validate_display_name(body.get("name"))
    purpose = body.get("purpose")
    window_hours = body.get("window_hours")
    if purpose not in PURPOSES:
        raise ValueError("purpose is unsupported")
    if not isinstance(window_hours, int) or window_hours <= 0:
        raise ValueError("window_hours must be a positive integer")
    selectors = body.get("selectors", [])
    if not isinstance(selectors, list) or any(
        not isinstance(selector, dict) or not isinstance(selector.get("source_role"), str) or not selector["source_role"].strip()
        or not isinstance(selector.get("branch"), str) or not selector["branch"].strip()
        for selector in selectors
    ):
        raise ValueError("selectors must contain nonblank source_role and exact branch")
    return name, purpose, window_hours, selectors


async def api_collection_profiles_handler(request):
    collection = await database.db.get_collection(request.match_info["key"])
    if not collection:
        return web.json_response({"success": False, "error": "Collection not found"}, status=404)
    try:
        body = await _json_body(request)
        name, purpose, window_hours, selectors = _profile_values(body)
        profile_id = await database.db.create_summary_profile(collection["id"], name, purpose, window_hours, bool(body.get("is_primary", False)))
        await database.db.replace_summary_profile_sources(profile_id, [(item["source_role"], item["branch"], item.get("target_id")) for item in selectors])
        return web.json_response({"success": True, "data": await database.db.get_summary_profile(profile_id)}, status=201)
    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as error:
        return web.json_response({"success": False, "error": str(error)}, status=409)


async def api_profile_handler(request):
    profile_id = int(request.match_info["profile_id"])
    if request.method == "GET":
        profile = await database.db.get_summary_profile(profile_id)
        return web.json_response({"success": True, "data": profile}) if profile else web.json_response({"success": False, "error": "Profile not found"}, status=404)
    if request.method == "DELETE":
        if request.query.get("cascade") != "true":
            return _validation_error("Profile deletion requires cascade=true")
        deleted = await database.db.delete_summary_profile(profile_id)
        return web.json_response({"success": True}) if deleted else web.json_response({"success": False, "error": "Profile not found"}, status=404)
    try:
        body = await _json_body(request)
        name, purpose, window_hours, selectors = _profile_values(body)
        updated = await database.db.update_summary_profile(profile_id, {**body, "name": name, "purpose": purpose, "window_hours": window_hours})
        if not updated:
            return web.json_response({"success": False, "error": "Profile not found"}, status=404)
        await database.db.replace_summary_profile_sources(profile_id, [(item["source_role"], item["branch"], item.get("target_id")) for item in selectors])
        return web.json_response({"success": True, "data": await database.db.get_summary_profile(profile_id)})
    except (ValueError, TypeError) as error:
        return _validation_error(error)


async def api_collection_summary_handler(request):
    collection = await database.db.get_collection(request.match_info["key"])
    if not collection:
        return web.json_response({"success": False, "error": "Collection not found"}, status=404)
    profile_id = request.query.get("profile_id") or next((profile["id"] for profile in collection["profiles"] if profile["is_primary"]), None)
    if not profile_id:
        return _validation_error("A profile_id or primary profile is required")
    try:
        requested_at = datetime.fromisoformat(request.query.get("at", "").replace("Z", "+00:00"))
        selections = await select_profile_from_database(database.db, int(profile_id), requested_at.astimezone(timezone.utc))
        run_ids = [selection.run_id for selection in selections if selection.run_id]
        runs = await database.db.get_test_runs(limit=len(run_ids), run_ids=run_ids)
        runs_by_id = {run["run_id"]: run for run in runs}
        result_fields = (
            "status",
            "start_time",
            "end_time",
            "passed_count",
            "failed_count",
            "skipped_count",
            "aborted_count",
            "error_count",
            "test_case_count",
        )
        data = []
        for selection in selections:
            item = selection.__dict__.copy()
            if selection.run_id in runs_by_id:
                run = runs_by_id[selection.run_id]
                item.update({field: run[field] for field in result_fields})
            data.append(item)
        return web.json_response({"success": True, "data": data})
    except (ValueError, TypeError) as error:
        return _validation_error(error)


async def api_collection_report_handler(request):
    collection = await database.db.get_collection(request.match_info["key"])
    if not collection:
        return web.json_response({"success": False, "error": "Collection not found"}, status=404)
    try:
        body = await _json_body(request)
        profile_id = int(body["profile_id"])
        if profile_id not in {profile["id"] for profile in collection["profiles"]}:
            raise ValueError("Profile does not belong to Collection")
        requested_at = datetime.fromisoformat(body["requested_at"].replace("Z", "+00:00"))
        report = await create_collection_report(database.db, profile_id, requested_at)
        return web.json_response({"success": True, "data": report}, status=201)
    except (KeyError, ValueError, TypeError) as error:
        return _validation_error(error)


async def api_run_set_handler(request):
    """Resolve a reproducible Target or Collection/profile Run set for views."""
    try:
        requested_at = request.query.get("at")
        run_set = await resolve_run_set(
            database.db,
            target_key=request.query.get("target"),
            collection_key=request.query.get("collection"),
            profile_id=int(request.query.get("profile_id") or request.query.get("profile")) if request.query.get("profile_id") or request.query.get("profile") else None,
            requested_at=datetime.fromisoformat(requested_at.replace("Z", "+00:00")) if requested_at else None,
        )
        return web.json_response({"success": True, "data": run_set})
    except (ValueError, TypeError) as error:
        return _validation_error(error)


# --- Test Results Analyzer API ---

async def api_test_runs_handler(request):
    """Get test runs with filtering capabilities."""
    try:
        # Parse query parameters
        limit = int(request.query.get('limit', 100))
        offset = int(request.query.get('offset', 0))
        status = request.query.get('status')
        target_key = request.query.get('target')
        purpose = request.query.get('purpose')
        source_role = request.query.get('source_role')
        source_branch = request.query.get('source_branch')
        revision = request.query.get('revision')
        start_at = request.query.get('start_at')
        end_at = request.query.get('end_at')
        collection_key = request.query.get('collection')
        run_ids = await _context_run_ids(request)

        # Parse metadata filters
        metadata_filters = {}
        for key, value in request.query.items():
            if key.startswith('metadata.'):
                metadata_key = key[9:]  # Remove 'metadata.' prefix
                metadata_filters[metadata_key] = value

        # Get test runs from database
        runs = await database.db.get_test_runs(
            limit=limit,
            offset=offset,
            status_filter=status,
            metadata_filters=metadata_filters if metadata_filters else None,
            target_key=target_key,
            purpose=purpose,
            source_role=source_role,
            source_branch=source_branch,
            revision=revision,
            start_at=start_at,
            end_at=end_at,
            collection_key=collection_key,
            run_ids=run_ids,
        )

        return web.json_response({
            "success": True,
            "data": runs,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "count": len(runs)
            }
        })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_test_runs_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_test_run_details_handler(request):
    """Get detailed information about a specific test run."""
    try:
        run_id = request.match_info["run_id"]

        # Get test run details
        run = await database.db.get_test_run_by_id(run_id)
        if not run:
            return web.json_response({
                "success": False,
                "error": "Test run not found"
            }, status=404)

        # Get test cases for this run
        test_cases = await database.db.get_test_cases_for_run(run_id)

        # Get metadata for this run
        user_metadata = await database.db.get_user_metadata_for_run(run_id)
        return web.json_response({
            "success": True,
            "data": {
                "run": run,
                "test_cases": test_cases,
                "user_metadata": user_metadata,
            }
        })

    except Exception as e:
        logger.error(f"Error in api_test_run_details_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_test_results_for_runs_handler(request):
    """Get test results for multiple runs efficiently."""
    try:
        run_ids_param = request.query.get('run_ids', '')
        if not run_ids_param:
            return web.json_response({
                "success": False,
                "error": "run_ids parameter is required"
            }, status=400)

        # Parse run IDs (comma-separated)
        run_ids = [run_id.strip() for run_id in run_ids_param.split(',') if run_id.strip()]

        if not run_ids:
            return web.json_response({
                "success": False,
                "error": "No valid run IDs provided"
            }, status=400)

        # Get test results for all runs in one efficient query
        raw_test_results = await database.db.get_test_results_for_runs(run_ids)

        enriched_results = {}
        for run_id, cases in raw_test_results.items():
            enriched_cases = []
            for case in cases:
                case_copy = dict(case)
                # Get the full name and tc_id from the database
                full_name = case_copy.get('tc_full_name')
                tc_id = case_copy.get('tc_id')

                if full_name:
                    case_copy[TC_FULL_NAME_FIELD] = full_name
                if tc_id:
                    case_copy[TC_ID_FIELD] = tc_id
                else:
                    case_copy[TC_ID_FIELD] = ""

                enriched_cases.append(case_copy)

            enriched_results[run_id] = enriched_cases

        return web.json_response({
            "success": True,
            "data": enriched_results
        })

    except Exception as e:
        logger.error(f"Error in api_test_results_for_runs_handler: {e}")
        return web.json_response({
            "success": False,
            "error": f"Internal server error: {str(e)}"
        }, status=500)


async def api_test_results_over_time_handler(request):
    """Get test results aggregated over time for trending analysis."""
    try:
        days_back = int(request.query.get('days_back', 30))

        # Parse metadata filters
        metadata_filters = {}
        for key, value in request.query.items():
            if key.startswith('metadata.'):
                metadata_key = key[9:]  # Remove 'metadata.' prefix
                metadata_filters[metadata_key] = value

        run_ids = await _context_run_ids(request)
        # Get test runs over time (individual runs, not aggregated by date)
        results = await database.db.get_test_runs_over_time(
            days_back=days_back,
            metadata_filters=metadata_filters if metadata_filters else None,
            run_ids=run_ids,
        )

        # Log the results
        logger.debug(f"API test-runs-over-time: {len(results)} test runs")
        for result in results[:3]:  # Show first 3 runs
            logger.debug(f"  Run: {result.get('run_id')[:8]}..., Passed: {result.get('passed_tests')}, Failed: {result.get('failed_tests')}, Skipped: {result.get('skipped_tests')}")

        return web.json_response({
            "success": True,
            "data": results
        })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_test_results_over_time_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_test_case_history_handler(request):
    """Get execution history for a specific test case."""
    try:
        tc_full_name = request.query.get('tc_full_name')
        if not tc_full_name:
            return web.json_response({
                "success": False,
                "error": "tc_full_name parameter is required"
            }, status=400)

        limit = int(request.query.get('limit', 50))

        # Parse metadata filters
        metadata_filters = {}
        for key, value in request.query.items():
            if key.startswith('metadata.'):
                metadata_key = key[9:]  # Remove 'metadata.' prefix
                metadata_filters[metadata_key] = value

        run_ids = await _context_run_ids(request, request.query.get('current_run_id'))
        # Get test case history
        history = await database.db.get_test_case_history(
            tc_full_name=tc_full_name,
            limit=limit,
            metadata_filters=metadata_filters if metadata_filters else None,
            run_ids=run_ids,
        )

        return web.json_response({
            "success": True,
            "data": history
        })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_test_case_history_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_test_case_history_with_links_handler(request):
    """Get test case history with log file existence check."""
    try:
        tc_full_name = request.query.get('tc_full_name')
        if not tc_full_name:
            return web.json_response({
                "success": False,
                "error": "tc_full_name is required"
            }, status=400)

        limit = int(request.query.get('limit', 10))
        current_run_id = request.query.get('current_run_id')  # Exclude current run

        run_ids = await _context_run_ids(request, current_run_id)
        # Get test case history
        history = await database.db.get_test_case_history(
            tc_full_name=tc_full_name,
            limit=limit + 1,  # Get one extra to account for current run exclusion
            run_ids=run_ids,
        )

        # Filter out current run and check log existence
        result = []
        for item in history:
            run_id = item.get('run_id')
            if current_run_id and run_id == current_run_id:
                continue

            # Check if run directory exists (logs may be merged after run finishes)
            tc_id = item.get('tc_id')
            item['has_log'] = tc_id and get_run_path(run_id).exists()

            result.append(item)

            if len(result) >= limit:
                break

        return web.json_response({
            "success": True,
            "data": result
        })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_test_case_history_with_links_handler: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_metadata_keys_handler(request):
    """Get all available metadata keys."""
    try:
        keys = await database.db.get_all_metadata_keys()
        return web.json_response({
            "success": True,
            "data": keys
        })

    except Exception as e:
        logger.error(f"Error in api_metadata_keys_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_metadata_values_handler(request):
    """Get unique values for a specific metadata key."""
    try:
        key = request.query.get('key')
        if not key:
            return web.json_response({
                "success": False,
                "error": "key parameter is required"
            }, status=400)

        values = await database.db.get_unique_metadata_values(key)
        return web.json_response({
            "success": True,
            "data": values
        })

    except Exception as e:
        logger.error(f"Error in api_metadata_values_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_failures_toplist_handler(request):
    """Get top failing test cases or symptoms."""
    try:
        mode = request.query.get('mode', 'by_test_case')
        days_back = int(request.query.get('days', 30))
        top_n = int(request.query.get('top', 20))

        # Parse metadata filters
        metadata_filters = {}
        for key, value in request.query.items():
            if key.startswith('metadata.'):
                metadata_key = key[9:]  # Remove 'metadata.' prefix
                metadata_filters[metadata_key] = value

        run_ids = await _context_run_ids(request)
        if mode == 'by_symptom':
            # Get failed test cases and analyze by stack trace
            failed_cases = await database.db.get_failed_test_cases(
                days_back=days_back,
                limit=1000,  # Get more to analyze symptoms
                metadata_filters=metadata_filters if metadata_filters else None,
                run_ids=run_ids,
            )

            # Cache loaded runs to avoid re-loading the same run multiple times
            run_cache = {}

            def _load_stack_traces_for_case(case):
                """Load stack traces for a test case, handling both live and finished runs."""
                run_id = case['run_id']
                tc_id = case.get('tc_id')
                if not tc_id:
                    return []

                # First try individual stack file (works for in-progress/aborted runs)
                try:
                    stack_path = get_case_stack_path(run_id, tc_id=tc_id)
                    if stack_path.exists():
                        return read_mplog(stack_path)
                except Exception:
                    pass

                # For finished runs, load via run model (reads from merged file)
                if run_id not in run_cache:
                    try:
                        run_cache[run_id] = TestRunData.load_from_disk(run_id)
                    except Exception:
                        run_cache[run_id] = None

                run = run_cache[run_id]
                if run is None:
                    return []

                tc = find_test_case_by_tc_id(run, tc_id)
                if tc is None:
                    return []

                if not tc.stack_traces and tc.log_offset is not None:
                    tc.load_log_from_disk()

                return tc.stack_traces or []

            # Group by first line of stack trace (symptom)
            symptom_map = {}
            for case in failed_cases:
                traces = _load_stack_traces_for_case(case)
                symptom = None
                stack_trace_sample = None

                if traces:
                    first_trace = traces[0]
                    stack_lines = first_trace.get('stack_trace', [])
                    if stack_lines and len(stack_lines) > 0:
                        # Use first line of stack trace as symptom
                        symptom = stack_lines[0].strip() if isinstance(stack_lines[0], str) else str(stack_lines[0])
                        # Store full trace for sample
                        stack_trace_sample = '\n'.join(stack_lines[:10])  # First 10 lines

                if not symptom:
                    symptom = "No stack trace available"

                if symptom not in symptom_map:
                    symptom_map[symptom] = {
                        'symptom': symptom,
                        'failure_count': 0,
                        'affected_test_cases': {},  # Dict: tc_full_name -> {run_id, time}
                        'last_failure': None,
                        'last_failure_run_id': None,
                        'last_failure_test_case': None,
                        'stack_trace_sample': stack_trace_sample
                    }

                symptom_map[symptom]['failure_count'] += 1

                # Track last failure and count for each test case
                tc_full_name = case['tc_full_name']
                tc_id = case.get('tc_id', '')
                case_time = case.get('start_time')
                if tc_full_name not in symptom_map[symptom]['affected_test_cases']:
                    symptom_map[symptom]['affected_test_cases'][tc_full_name] = {
                        'run_id': case['run_id'],
                        'tc_id': tc_id,
                        'time': case_time,
                        'count': 1
                    }
                else:
                    symptom_map[symptom]['affected_test_cases'][tc_full_name]['count'] += 1
                    if case_time and case_time > (symptom_map[symptom]['affected_test_cases'][tc_full_name].get('time') or ''):
                        symptom_map[symptom]['affected_test_cases'][tc_full_name]['run_id'] = case['run_id']
                        symptom_map[symptom]['affected_test_cases'][tc_full_name]['tc_id'] = tc_id
                        symptom_map[symptom]['affected_test_cases'][tc_full_name]['time'] = case_time

                # Track overall last failure for the symptom
                if case_time:
                    current_last = symptom_map[symptom]['last_failure']
                    if not current_last or case_time > current_last:
                        symptom_map[symptom]['last_failure'] = case_time
                        symptom_map[symptom]['last_failure_run_id'] = case['run_id']
                        symptom_map[symptom]['last_failure_test_case'] = case['tc_full_name']
                        symptom_map[symptom]['last_failure_tc_id'] = tc_id
                        if stack_trace_sample:
                            symptom_map[symptom]['stack_trace_sample'] = stack_trace_sample

            # Convert to list and sort
            results = list(symptom_map.values())
            for r in results:
                # Convert affected_test_cases dict to list of objects with tc_id and count
                affected_list = []
                for tc_full_name, info in r['affected_test_cases'].items():
                    run_id = info['run_id']
                    tc_id = info.get('tc_id', '')
                    count = info.get('count', 1)
                    # Check if run directory exists (logs may be merged after run finishes)
                    has_log = tc_id and get_run_path(run_id).exists()
                    affected_list.append({
                        TC_ID_FIELD: tc_id,
                        TC_FULL_NAME_FIELD: tc_full_name,
                        'last_failure_run_id': run_id if has_log else None,
                        'failure_count': count
                    })
                # Sort by failure count descending
                affected_list.sort(key=lambda x: x['failure_count'], reverse=True)
                r['affected_test_cases'] = affected_list

                # Also check if the overall last failure log exists
                if r['last_failure_run_id'] and r['last_failure_test_case']:
                    last_tc_id = r.get('last_failure_tc_id', '')
                    # Check if run directory exists (logs may be merged after run finishes)
                    has_last_log = last_tc_id and get_run_path(r['last_failure_run_id']).exists()
                    if has_last_log:
                        r['last_failure_test_case'] = {
                            TC_ID_FIELD: last_tc_id,
                            TC_FULL_NAME_FIELD: r['last_failure_test_case']
                        }
                    else:
                        r['last_failure_run_id'] = None
                        r['last_failure_test_case'] = None

            results.sort(key=lambda x: x['failure_count'], reverse=True)
            results = results[:top_n]

            return web.json_response({
                "success": True,
                "data": results
            })
        else:
            # By test case name
            results = await database.db.get_failure_counts_by_test_case(
                days_back=days_back,
                top_n=top_n,
                metadata_filters=metadata_filters if metadata_filters else None,
                run_ids=run_ids,
            )

            # Check if log files exist for each result while enriching identifiers
            for r in results:
                full_name = r.get('tc_full_name')
                tc_id = r.get('last_failure_tc_id', '')
                if full_name:
                    r[TC_FULL_NAME_FIELD] = full_name
                if tc_id:
                    r[TC_ID_FIELD] = tc_id
                else:
                    r[TC_ID_FIELD] = ""

                if r.get('last_failure_run_id') and tc_id:
                    # Check if run directory exists (logs may be merged after run finishes)
                    if not get_run_path(r['last_failure_run_id']).exists():
                        r['last_failure_run_id'] = None

            return web.json_response({
                "success": True,
                "data": results
            })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_failures_toplist_handler: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_classifications_for_run_handler(request):
    """Get test case classifications for all TCs in a run."""
    try:
        run_id = request.match_info.get('run_id')
        if not run_id:
            return web.json_response({
                "success": False,
                "error": "run_id is required"
            }, status=400)

        if not validate_run_id(run_id):
            return web.json_response({
                "success": False,
                "error": "Invalid run_id"
            }, status=400)

        run_data = await database.db.get_test_run_by_id(run_id)
        if not run_data:
            return web.json_response({
                "success": False,
                "error": "Run not found"
            }, status=404)

        run_ids = await _context_run_ids(request, run_id)
        classifications = await database.db.get_classifications_for_run(run_id, run_ids=run_ids)

        # Add has_log info to history items
        for tc_id, class_data in classifications.items():
            if 'history' in class_data:
                for hist_item in class_data['history']:
                    hist_run_id = hist_item.get('run_id')
                    # Check if run directory exists (logs may be merged after run finishes)
                    hist_item['has_log'] = bool(hist_run_id) and get_run_path(hist_run_id).exists()

        return web.json_response({
            "success": True,
            "data": classifications
        })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_classifications_for_run_handler: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_tc_hover_history_handler(request):
    """Get test case history for hover tooltip."""
    try:
        tc_full_name = request.query.get('tc_full_name')
        if not tc_full_name:
            return web.json_response({
                "success": False,
                "error": "tc_full_name is required"
            }, status=400)

        current_run_id = request.query.get('current_run_id')
        run_ids = await _context_run_ids(request, current_run_id)
        # Get current run's start time if we have a run_id
        current_run_start_time = None
        if current_run_id:
            async with database.db.get_connection() as db:
                cursor = await db.execute(
                    "SELECT start_time FROM test_runs WHERE run_id = ?",
                    (current_run_id,)
                )
                row = await cursor.fetchone()
                if row:
                    current_run_start_time = row[0]

        # Get previous results (before current run)
        previous_history = await database.db.get_test_case_classification_data(
            tc_full_name=tc_full_name,
            limit=10,
            current_run_id=current_run_id,
            current_run_start_time=current_run_start_time,
            run_ids=run_ids,
        )

        # Get latest results (all runs, including current and future)
        latest_history = await database.db.get_test_case_classification_data(
            tc_full_name=tc_full_name,
            limit=10,
            run_ids=run_ids,
        )

        # Helper function to add has_log and format
        def format_history(history_items):
            result = []
            for item in history_items:
                run_id = item.get('run_id')
                tc_id = item.get('tc_id')
                # Check if run directory exists (logs may be merged after run finishes)
                has_log = tc_id and get_run_path(run_id).exists()
                result.append({
                    'status': item['status'],
                    'run_id': run_id,
                    'tc_id': tc_id,
                    'run_name': item.get('run_name'),
                    'run_start_time': item.get('run_start_time'),
                    'has_log': has_log
                })
            return result

        return web.json_response({
            "success": True,
            "data": {
                "previous": format_history(previous_history),
                "latest": format_history(latest_history)
            }
        })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_tc_hover_history_handler: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_run_hover_history_handler(request):
    """Get test Run history for hover tooltip within a shared Run set."""
    try:
        current_run_id = request.query.get('current_run_id')
        run_ids = await _context_run_ids(request, current_run_id)
        if run_ids is None:
            return web.json_response({
                "success": False,
                "error": "Target or Collection context is required"
            }, status=400)
        current_run_start_time = None
        if current_run_id:
            async with database.db.get_connection() as db:
                cursor = await db.execute(
                    "SELECT start_time FROM test_runs WHERE run_id = ?",
                    (current_run_id,)
                )
                row = await cursor.fetchone()
                if row:
                    current_run_start_time = row[0]

        # Previous runs: before the current run, exclude current
        previous_history = await database.db.get_test_run_history(
            run_ids=run_ids,
            limit=10,
            exclude_run_id=current_run_id,
            current_run_start_time=current_run_start_time
        )

        # Latest runs: recent runs excluding current
        latest_history = await database.db.get_test_run_history(
            run_ids=run_ids,
            limit=10,
            exclude_run_id=current_run_id
        )

        return web.json_response({
            "success": True,
            "data": {
                "previous": previous_history,
                "latest": latest_history
            }
        })

    except (ValueError, TypeError) as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_run_hover_history_handler: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_migrate_data_handler(request):
    """Trigger migration of existing test data from disk to database."""
    try:
        return web.json_response({
            "success": False,
            "error": "Migration module not available in this build."
        }, status=501)

    except Exception as e:
        logger.error(f"Error in api_migrate_data_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_server_info_handler(request):
    """Returns server identity and config fingerprint for startup checks."""
    try:
        from importlib.metadata import version as _pkg_version
        ver = _pkg_version("testrift-server")
    except Exception:
        ver = "unknown"

    host = request.headers.get("host") or "127.0.0.1"
    from . import config as config_mod
    from .tls_certs import current_material, ingest_base_url
    live = config_mod.CONFIG
    material = current_material()
    return web.json_response({
        "service": "testrift-server",
        "version": ver,
        "config_path": str(config_mod.CONFIG_PATH_USED) if config_mod.CONFIG_PATH_USED else None,
        "config": get_config_fingerprint(live),
        "config_hash": get_config_hash(live),
        "ingest_url": ingest_base_url(live, host),
        "tls_ca_fingerprint": material.ca_fingerprint if material else None,
    })


async def api_admin_shutdown_handler(request):
    """Shutdown endpoint used for local auto-restart flows."""
    remote = request.remote or ""
    if remote not in ("127.0.0.1", "::1", "localhost"):
        return web.json_response({"success": False, "error": "forbidden"}, status=403)

    expected = get_config_hash(CONFIG)
    provided = request.headers.get("X-TestRift-Config-Hash")
    if not provided:
        try:
            body = await request.json()
            provided = body.get("config_hash")
        except Exception:
            provided = None

    if provided != expected:
        return web.json_response({"success": False, "error": "config_hash mismatch"}, status=403)

    # Respond first, then hard-exit quickly to ensure the port is released
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, lambda: os._exit(0))
    return web.json_response({"success": True})


# --- Commit/Diff Endpoints for testrift-collector ---

async def api_run_commit_baselines_handler(request):
    try:
        baselines = await database.db.get_commit_baselines_for_run(request.match_info["run_id"])
        return web.json_response({"success": True, "baselines": baselines})
    except ValueError as error:
        return web.json_response({"success": False, "error": str(error)}, status=404)


async def api_run_commits_upload_handler(request):
    """Upload commit diffs for a run."""
    try:
        run_id = request.match_info["run_id"]

        if not validate_run_id(run_id):
            return web.json_response({
                "success": False,
                "error": "Invalid run ID"
            }, status=400)

        # Parse JSON body
        try:
            body = await request.json()
        except Exception:
            return web.json_response({
                "success": False,
                "error": "Invalid JSON body"
            }, status=400)

        diffs = body.get("diffs", [])
        if not isinstance(diffs, list):
            return web.json_response({
                "success": False,
                "error": "'diffs' must be an array"
            }, status=400)

        expected_sources = await database.db.get_run_sources(run_id)
        for diff in diffs:
            if diff.get("name") not in expected_sources or diff.get("current_sha") != expected_sources[diff["name"]]:
                return web.json_response({"success": False, "error": "Diff SHA does not match prepared Run source"}, status=400)

        # Store commit SHAs in database for future queries
        commits_to_store = []
        for diff in diffs:
            if diff.get("name") and diff.get("current_sha"):
                commits_to_store.append({
                    "repo_name": diff["name"],
                    "commit_sha": diff["current_sha"],
                    "repo_url": diff.get("url"),
                })

        if commits_to_store:
            await database.db.insert_run_commits(run_id, commits_to_store)

        # Store full diff data as JSON file in run directory
        run_path = get_run_path(run_id)
        run_path.mkdir(parents=True, exist_ok=True)

        diffs_file = run_path / "commits.json"
        import json
        import aiofiles
        async with aiofiles.open(diffs_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps({"diffs": diffs}, indent=2))

        return web.json_response({
            "success": True,
            "stored_commits": len(commits_to_store),
            "stored_diffs": len(diffs)
        })

    except Exception as e:
        logger.error(f"Error in api_run_commits_upload_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def api_run_commits_get_handler(request):
    """Get commit diffs for a run."""
    try:
        run_id = request.match_info["run_id"]

        if not validate_run_id(run_id):
            return web.json_response({
                "success": False,
                "error": "Invalid run ID"
            }, status=400)

        # Try to read from commits.json file
        run_path = get_run_path(run_id)
        diffs_file = run_path / "commits.json"

        if diffs_file.exists():
            import aiofiles
            async with aiofiles.open(diffs_file, "r", encoding="utf-8") as f:
                content = await f.read()
            import json
            data = json.loads(content)
            return web.json_response({
                "success": True,
                "diffs": data.get("diffs", [])
            })

        # Fall back to database records (just the SHA, no full diffs)
        commits = await database.db.get_commits_for_run(run_id)
        return web.json_response({
            "success": True,
            "commits": commits,
            "diffs": []
        })

    except Exception as e:
        logger.error(f"Error in api_run_commits_get_handler: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


# --- AI Analysis API ---

async def api_trigger_analysis_handler(request):
    """Trigger AI failure analysis for a run. POST /api/runs/{run_id}/analyze"""
    try:
        run_id = request.match_info["run_id"]
        if not validate_run_id(run_id):
            return web.json_response({"success": False, "error": "Invalid run ID"}, status=400)

        from .ai_analysis import get_analysis_status, run_failure_analysis
        from .config import AI_ANALYSIS_CONFIG

        status = get_analysis_status(run_id)
        if status.status == "running":
            return web.json_response({"success": True, "status": "already_running"})
        if status.status == "completed":
            return web.json_response({"success": True, "status": "already_completed"})

        if not AI_ANALYSIS_CONFIG.get("enabled", False) and not AI_ANALYSIS_CONFIG.get("openai_api_key"):
            return web.json_response({"success": False, "error": "AI analysis not configured"}, status=400)

        ws_server = request.app.get("ws_server")
        broadcast_fn = ws_server.broadcast_ui if ws_server else None
        asyncio.create_task(run_failure_analysis(run_id, broadcast_fn=broadcast_fn))

        return web.json_response({"success": True, "status": "started"}, status=202)

    except Exception as e:
        logger.error(f"Error in api_trigger_analysis_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_analysis_status_handler(request):
    """Get analysis status for a run. GET /api/runs/{run_id}/analysis"""
    try:
        run_id = request.match_info["run_id"]
        if not validate_run_id(run_id):
            return web.json_response({"success": False, "error": "Invalid run ID"}, status=400)

        from .ai_analysis import get_analysis_status
        status = get_analysis_status(run_id)

        return web.json_response({
            "success": True,
            "status": status.status,
            "analyzed_count": status.analyzed_count,
            "deduped_count": status.deduped_count,
            "skipped_count": status.skipped_count,
            "total_failures": status.total_failures,
            "error": status.error,
            "completed_at": status.completed_at,
        })

    except Exception as e:
        logger.error(f"Error in api_analysis_status_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_analysis_for_tc_handler(request):
    """Get analysis result for a specific test case. GET /api/runs/{run_id}/analysis/{tc_full_name}"""
    try:
        run_id = request.match_info["run_id"]
        tc_full_name = request.match_info["tc_full_name"]

        result = await database.db.get_analysis_for_test_case(run_id, tc_full_name)
        if not result:
            return web.json_response({"success": False, "error": "No analysis found"}, status=404)

        import json as json_mod
        refs = []
        if result.get("references_json"):
            try:
                refs = json_mod.loads(result["references_json"])
            except json_mod.JSONDecodeError:
                pass

        return web.json_response({
            "success": True,
            "tc_full_name": tc_full_name,
            "summary": result.get("summary"),
            "summary_html": result.get("summary_html"),
            "references": refs,
            "confidence": result.get("confidence"),
            "category": result.get("category"),
            "model_used": result.get("model_used"),
            "tier_used": result.get("tier_used"),
            "reasoning": result.get("reasoning"),
            "deep_html": result.get("deep_html"),
            "token_count": result.get("token_count"),
            "created_at": result.get("created_at"),
        })

    except Exception as e:
        logger.error(f"Error in api_analysis_for_tc_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_analysis_summary_handler(request):
    """Get all analysis results for a run. GET /api/runs/{run_id}/analysis/summary"""
    try:
        run_id = request.match_info["run_id"]
        if not validate_run_id(run_id):
            return web.json_response({"success": False, "error": "Invalid run ID"}, status=400)

        analyses = await database.db.get_analyses_for_run(run_id)
        import json as json_mod

        results = []
        for a in analyses:
            refs = []
            if a.get("references_json"):
                try:
                    refs = json_mod.loads(a["references_json"])
                except json_mod.JSONDecodeError:
                    pass

            results.append({
                "tc_full_name": a.get("tc_full_name"),
                "tc_id": a.get("tc_id"),
                "summary": a.get("summary"),
                "summary_html": a.get("summary_html"),
                "references": refs,
                "confidence": a.get("confidence"),
                "category": a.get("category"),
                "model_used": a.get("model_used"),
                "tier_used": a.get("tier_used"),
                "reasoning": a.get("reasoning"),
                "token_count": a.get("token_count"),
                "created_at": a.get("created_at"),
            })

        return web.json_response({"success": True, "analyses": results})

    except Exception as e:
        logger.error(f"Error in api_analysis_summary_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_trigger_deep_analysis_handler(request):
    """Trigger deep analysis for a single test case. POST /api/runs/{run_id}/analyze/{tc_full_name}/deep"""
    try:
        run_id = request.match_info["run_id"]
        tc_full_name = request.match_info["tc_full_name"]
        if not validate_run_id(run_id):
            return web.json_response({"success": False, "error": "Invalid run ID"}, status=400)

        from .ai_analysis import get_deep_analysis_status, run_deep_analysis
        from .config import AI_ANALYSIS_CONFIG

        status = get_deep_analysis_status(run_id, tc_full_name)
        if status.get("status") == "running":
            return web.json_response({"success": True, "status": "already_running"})

        if not AI_ANALYSIS_CONFIG.get("openai_api_key"):
            return web.json_response({"success": False, "error": "AI analysis not configured"}, status=400)

        asyncio.create_task(run_deep_analysis(run_id, tc_full_name))
        return web.json_response({"success": True, "status": "started"}, status=202)

    except Exception as e:
        logger.error(f"Error in api_trigger_deep_analysis_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_deep_analysis_status_handler(request):
    """Get deep analysis status. GET /api/runs/{run_id}/analyze/{tc_full_name}/deep"""
    try:
        run_id = request.match_info["run_id"]
        tc_full_name = request.match_info["tc_full_name"]

        from .ai_analysis import get_deep_analysis_status
        status = get_deep_analysis_status(run_id, tc_full_name)
        return web.json_response({"success": True, **status})

    except Exception as e:
        logger.error(f"Error in api_deep_analysis_status_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_email_recipients_get_handler(request):
    """Get current email recipients. GET /api/settings/email-recipients"""
    try:
        import json as json_mod
        from .config import EMAIL_CONFIG

        db_value = await database.db.get_setting("email_recipients")
        if db_value:
            try:
                addresses = json_mod.loads(db_value)
                source = "database"
            except json_mod.JSONDecodeError:
                addresses = EMAIL_CONFIG.get("to_addresses", [])
                source = "config_file"
        else:
            addresses = EMAIL_CONFIG.get("to_addresses", [])
            source = "config_file"

        return web.json_response({
            "success": True,
            "to_addresses": addresses,
            "source": source,
        })

    except Exception as e:
        logger.error(f"Error in api_email_recipients_get_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_email_recipients_put_handler(request):
    """Update email recipients. PUT /api/settings/email-recipients"""
    try:
        import json as json_mod
        body = await request.json()
        addresses = body.get("to_addresses", [])

        if not isinstance(addresses, list):
            return web.json_response({"success": False, "error": "to_addresses must be a list"}, status=400)

        await database.db.set_setting("email_recipients", json_mod.dumps(addresses))

        return web.json_response({"success": True, "to_addresses": addresses, "source": "database"})

    except Exception as e:
        logger.error(f"Error in api_email_recipients_put_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_email_recipients_delete_handler(request):
    """Reset email recipients to config defaults. DELETE /api/settings/email-recipients"""
    try:
        from .config import EMAIL_CONFIG
        await database.db.delete_setting("email_recipients")
        return web.json_response({
            "success": True,
            "to_addresses": EMAIL_CONFIG.get("to_addresses", []),
            "source": "config_file",
        })

    except Exception as e:
        logger.error(f"Error in api_email_recipients_delete_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_ai_usage_handler(request):
    """Get AI usage and budget status. GET /api/settings/ai-usage"""
    try:
        from .config import AI_ANALYSIS_CONFIG

        month = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = await database.db.get_ai_usage_for_month(month)

        budget = AI_ANALYSIS_CONFIG.get("monthly_budget_usd", 0)
        cost = usage["estimated_cost_usd"] if usage else 0

        return web.json_response({
            "success": True,
            "current_month": month,
            "estimated_cost_usd": cost,
            "monthly_budget_usd": budget,
            "budget_utilization": cost / budget if budget > 0 else 0,
            "warning_threshold": AI_ANALYSIS_CONFIG.get("budget_warning_threshold", 0.8),
            "warning_sent": usage.get("warning_sent", False) if usage else False,
            "prompt_tokens": usage.get("prompt_tokens", 0) if usage else 0,
            "completion_tokens": usage.get("completion_tokens", 0) if usage else 0,
        })

    except Exception as e:
        logger.error(f"Error in api_ai_usage_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_logs_handler(request):
    """Get recent server log entries. GET /api/logs?after_seq=0&level=INFO&limit=500"""
    try:
        from .log_buffer import log_buffer

        after_seq = int(request.query.get("after_seq", 0))
        level = request.query.get("level", None)
        limit = int(request.query.get("limit", 500))

        entries = log_buffer.get_entries(after_seq=after_seq, level=level, limit=limit)
        return web.json_response({"success": True, "entries": entries})

    except Exception as e:
        logger.error(f"Error in api_logs_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _parse_user_id(request):
    raw = request.match_info.get("user_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _user_to_admin_view(user: dict) -> dict:
    from . import auth
    view = auth.public_user(user)
    lockout = None
    if user.get("username"):
        lockout = await auth.username_lockout_info(user["username"])
    view["lockout"] = lockout
    return view


async def api_auth_login_handler(request):
    from . import auth
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    user, cookie_value = await auth.local_login(
        str(body.get("username") or ""),
        str(body.get("password") or ""),
        request.remote or "",
    )
    if user is None or cookie_value is None:
        return web.json_response(
            {"success": False, "error": auth.LOGIN_ERROR_MESSAGE},
            status=401,
        )
    response = web.json_response({"success": True, "user": auth.public_user(user)})
    auth.apply_session_cookie(response, cookie_value)
    return response


async def api_auth_logout_handler(request):
    from . import auth
    cookies = getattr(request, "cookies", {}) or {}
    await auth.destroy_request_session(cookies.get(auth.SESSION_COOKIE_NAME))
    response = web.json_response({"success": True})
    auth.clear_session_cookie(response)
    return response


async def api_auth_me_handler(request):
    from . import auth
    user = getattr(request, "user", None)
    if not user:
        return web.json_response({"success": False, "error": "Authentication required"}, status=401)
    return web.json_response({"success": True, "user": auth.public_user(user)})


async def api_users_handler(request):
    from . import auth
    if request.method == "GET":
        users = await database.db.list_users()
        return web.json_response({
            "success": True,
            "users": [await _user_to_admin_view(user) for user in users],
        })

    try:
        body = await _json_body(request)
    except ValueError as error:
        return _validation_error(error)

    username = auth.normalize_username(str(body.get("username") or ""))
    password = str(body.get("password") or "")
    role = str(body.get("role") or "member").strip().lower()
    display_name = str(body.get("display_name") or "").strip() or username
    email = str(body.get("email") or "").strip() or None
    min_length = int(auth.auth_config().get("password_min_length") or 8)

    if not USERNAME_PATTERN.fullmatch(username):
        return _validation_error("username must be lowercase letters, digits, dots, underscores, or hyphens")
    if len(password) < min_length:
        return _validation_error(f"password must be at least {min_length} characters")
    if role not in auth.VALID_ROLES:
        return _validation_error("role must be member or admin")

    try:
        user_id = await database.db.create_user(
            display_name=display_name,
            email=email,
            username=username,
            password_hash=auth.hash_password(password),
            role=role,
            enabled=True,
            auth_source="local",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        )
    except sqlite3.IntegrityError:
        return web.json_response({"success": False, "error": "username already exists"}, status=409)

    user = await database.db.get_user_by_id(user_id)
    return web.json_response({"success": True, "user": await _user_to_admin_view(user)}, status=201)


async def api_user_handler(request):
    from . import auth
    user_id = _parse_user_id(request)
    if user_id is None:
        return _validation_error("Invalid user id")
    user = await database.db.get_user_by_id(user_id)
    if not user:
        return web.json_response({"success": False, "error": "User not found"}, status=404)

    if request.method == "GET":
        return web.json_response({"success": True, "user": await _user_to_admin_view(user)})

    try:
        body = await _json_body(request)
    except ValueError as error:
        return _validation_error(error)

    updates = {}
    if "display_name" in body:
        display_name = str(body.get("display_name") or "").strip()
        if not display_name:
            return _validation_error("display_name cannot be blank")
        updates["display_name"] = display_name
    if "email" in body:
        email = str(body.get("email") or "").strip()
        updates["email"] = email or None
    if "role" in body:
        role = str(body.get("role") or "").strip().lower()
        if role not in auth.VALID_ROLES:
            return _validation_error("role must be member or admin")
        updates["role"] = role
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            return _validation_error("enabled must be a boolean")
        updates["enabled"] = body["enabled"]

    if await auth.would_remove_last_admin(
        user,
        new_role=updates.get("role"),
        new_enabled=updates.get("enabled"),
    ):
        return web.json_response(
            {"success": False, "error": "Cannot disable or demote the last Admin"},
            status=400,
        )

    if updates:
        await database.db.update_user(user_id, **updates)
        if updates.get("enabled") is False:
            await database.db.delete_sessions_for_user(user_id)

    user = await database.db.get_user_by_id(user_id)
    return web.json_response({"success": True, "user": await _user_to_admin_view(user)})


async def api_user_reset_password_handler(request):
    from . import auth
    user_id = _parse_user_id(request)
    if user_id is None:
        return _validation_error("Invalid user id")
    user = await database.db.get_user_by_id(user_id)
    if not user:
        return web.json_response({"success": False, "error": "User not found"}, status=404)
    if user.get("auth_source") != "local":
        return web.json_response({"success": False, "error": "Cannot set a password on an SSO user"}, status=400)
    try:
        body = await _json_body(request)
    except ValueError as error:
        return _validation_error(error)
    password = str(body.get("password") or "")
    min_length = int(auth.auth_config().get("password_min_length") or 8)
    if len(password) < min_length:
        return _validation_error(f"password must be at least {min_length} characters")
    await database.db.update_user(user_id, password_hash=auth.hash_password(password))
    await database.db.delete_sessions_for_user(user_id)
    return web.json_response({"success": True})


async def api_user_unlock_handler(request):
    user_id = _parse_user_id(request)
    if user_id is None:
        return _validation_error("Invalid user id")
    user = await database.db.get_user_by_id(user_id)
    if not user:
        return web.json_response({"success": False, "error": "User not found"}, status=404)
    if not user.get("username"):
        return web.json_response({"success": False, "error": "User has no local username"}, status=400)
    await database.db.clear_login_attempts_for_username(user["username"])
    return web.json_response({"success": True, "user": await _user_to_admin_view(user)})


# --- Comments ---

COMMENT_BODY_MAX = 8000
COMMENT_NAME_MAX = 80


def _comment_auth(request):
    from .auth import is_auth_enabled
    user = getattr(request, "user", None)
    return is_auth_enabled(), user


def _serialize_comment(row):
    created = row.get("created_at")
    updated = row.get("updated_at")
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "scope": row["scope"],
        "tc_id": row.get("tc_id"),
        "line_start": row.get("line_start"),
        "line_end": row.get("line_end"),
        "body": row["body"],
        "author_user_id": row.get("author_user_id"),
        "author_name": row["author_name"],
        "created_at": created,
        "updated_at": updated,
        "edited": bool(created and updated and created != updated),
    }


def _comment_viewer_payload(request):
    enabled, user = _comment_auth(request)
    viewer = None
    if user:
        viewer = {
            "id": user.get("id"),
            "display_name": user.get("display_name") or user.get("username") or "",
            "role": user.get("role"),
        }
    return {"auth_enabled": enabled, "current_user": viewer}


def _normalize_comment_body(body):
    if body is None or not isinstance(body, str):
        raise ValueError("body is required")
    text = body.strip()
    if not text:
        raise ValueError("body cannot be blank")
    if len(text) > COMMENT_BODY_MAX:
        raise ValueError(f"body must be at most {COMMENT_BODY_MAX} characters")
    return text


def _is_comment_author(comment, user) -> bool:
    if not user or comment.get("author_user_id") is None:
        return False
    return int(comment["author_user_id"]) == int(user["id"])


def _is_admin_user(user) -> bool:
    return bool(user) and (user.get("role") or "") == "admin"


async def api_run_comments_handler(request):
    """List run-level comments and per-test-case presence, or create a comment."""
    run_id = request.match_info["run_id"]
    if not validate_run_id(run_id):
        return web.json_response({"success": False, "error": "Invalid run ID"}, status=400)
    run = await database.db.get_test_run_by_id(run_id)
    if not run:
        return web.json_response({"success": False, "error": "Test run not found"}, status=404)

    if request.method == "GET":
        comments = await database.db.get_comments_for_run(run_id)
        run_comments = []
        test_cases = {}
        for row in comments:
            item = _serialize_comment(row)
            if row["scope"] == "run":
                run_comments.append(item)
            elif row.get("tc_id"):
                tc_id = row["tc_id"]
                if tc_id not in test_cases:
                    test_cases[tc_id] = {
                        "has_comments": True,
                        "first_comment_id": item["id"],
                    }
        payload = {
            "success": True,
            "run_comments": run_comments,
            "test_cases": test_cases,
            **_comment_viewer_payload(request),
        }
        return web.json_response(payload)

    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        body = _normalize_comment_body(data.get("body"))
        scope = (data.get("scope") or "").strip()
        if scope not in ("run", "log"):
            raise ValueError("scope must be 'run' or 'log'")
        tc_id = data.get("tc_id")
        line_start = data.get("line_start")
        line_end = data.get("line_end")
        if scope == "log":
            if not tc_id or not isinstance(tc_id, str):
                raise ValueError("tc_id is required for log comments")
            if not isinstance(line_start, int) or not isinstance(line_end, int):
                raise ValueError("line_start and line_end must be integers")
            if line_start < 0 or line_end < line_start:
                raise ValueError("invalid line range")
            if line_end - line_start > 10000:
                raise ValueError("line range is too large")
        else:
            tc_id = None
            line_start = None
            line_end = None
        enabled, user = _comment_auth(request)
        if enabled:
            if not user:
                return web.json_response({"success": False, "error": "Authentication required"}, status=401)
            author_user_id = user["id"]
            author_name = (user.get("display_name") or user.get("username") or "User").strip()
        else:
            author_user_id = None
            author_name = (data.get("author_name") or "").strip()
            if not author_name:
                raise ValueError("author_name is required")
            if len(author_name) > COMMENT_NAME_MAX:
                raise ValueError(f"author_name must be at most {COMMENT_NAME_MAX} characters")
        comment = await database.db.insert_comment(
            run_id=run_id,
            scope=scope,
            body=body,
            author_name=author_name,
            author_user_id=author_user_id,
            tc_id=tc_id,
            line_start=line_start,
            line_end=line_end,
        )
        return web.json_response({"success": True, "comment": _serialize_comment(comment)}, status=201)
    except ValueError as error:
        return _validation_error(error)
    except Exception as e:
        logger.error(f"Error in api_run_comments_handler: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def api_log_comments_handler(request):
    """Full comment threads for one test case log."""
    run_id = request.match_info["run_id"]
    tc_id = request.match_info["tc_id"]
    if not validate_run_id(run_id):
        return web.json_response({"success": False, "error": "Invalid run ID"}, status=400)
    run = await database.db.get_test_run_by_id(run_id)
    if not run:
        return web.json_response({"success": False, "error": "Test run not found"}, status=404)
    comments = await database.db.get_comments_for_run(run_id, scope="log", tc_id=tc_id)
    return web.json_response({
        "success": True,
        "comments": [_serialize_comment(row) for row in comments],
        **_comment_viewer_payload(request),
    })


async def api_comment_item_handler(request):
    """Edit or delete a single comment."""
    try:
        comment_id = int(request.match_info["comment_id"])
    except (TypeError, ValueError):
        return web.json_response({"success": False, "error": "Invalid comment id"}, status=400)
    comment = await database.db.get_comment_by_id(comment_id)
    if not comment:
        return web.json_response({"success": False, "error": "Comment not found"}, status=404)

    enabled, user = _comment_auth(request)
    is_author = _is_comment_author(comment, user)
    is_admin = _is_admin_user(user)

    if request.method == "PATCH":
        if enabled and not is_author:
            return web.json_response({"success": False, "error": "Forbidden"}, status=403)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            body = _normalize_comment_body(data.get("body"))
        except ValueError as error:
            return _validation_error(error)
        updated = await database.db.update_comment_body(comment_id, body)
        return web.json_response({"success": True, "comment": _serialize_comment(updated)})

    if enabled and not is_author and not is_admin:
        return web.json_response({"success": False, "error": "Forbidden"}, status=403)
    await database.db.delete_comment(comment_id)
    return web.json_response({"success": True})


async def api_comments_presence_handler(request):
    """Bulk comment presence for run list pages."""
    run_ids_param = request.query.get("run_ids", "")
    run_ids = [item.strip() for item in run_ids_param.split(",") if item.strip()]
    if len(run_ids) > 500:
        return web.json_response({"success": False, "error": "Too many run_ids"}, status=400)
    presence = await database.db.get_comments_presence(run_ids)
    return web.json_response({"success": True, "data": presence})


# --- Route Registration ---

def get_routes():
    """Return list of (methods, path, handler) tuples for API handlers."""
    return [
        (("GET", "POST"), "/api/targets", api_targets_handler),
        (("GET", "PUT", "DELETE"), "/api/targets/{key}", api_target_handler),
        (("PUT",), "/api/targets/{key}/complete-setup", api_target_complete_setup_handler),
        (("GET", "POST"), "/api/collections", api_collections_handler),
        (("GET", "PUT", "DELETE"), "/api/collections/{key}", api_collection_handler),
        (("PUT",), "/api/collections/{key}/members", api_collection_members_handler),
        (("GET",), "/api/collections/{key}/profile-filter-options", api_collection_profile_filter_options_handler),
        (("POST",), "/api/collections/{key}/profiles", api_collection_profiles_handler),
        (("GET",), "/api/collections/{key}/summary", api_collection_summary_handler),
        (("POST",), "/api/collections/{key}/reports", api_collection_report_handler),
        (("GET",), "/api/run-set", api_run_set_handler),
        (("GET", "PUT", "DELETE"), "/api/profiles/{profile_id}", api_profile_handler),
        (("GET",), "/api/test-runs", api_test_runs_handler),
        (("GET", "POST"), "/api/runs/{run_id}/comments", api_run_comments_handler),
        (("GET",), "/api/runs/{run_id}/comments/log/{tc_id}", api_log_comments_handler),
        (("PATCH", "DELETE"), "/api/comments/{comment_id}", api_comment_item_handler),
        (("GET",), "/api/comments/presence", api_comments_presence_handler),
        (("GET",), "/api/test-runs/{run_id}", api_test_run_details_handler),
        (("GET",), "/api/test-results/for-runs", api_test_results_for_runs_handler),
        (("GET",), "/api/test-results/over-time", api_test_results_over_time_handler),
        (("GET",), "/api/test-case/history", api_test_case_history_handler),
        (("GET",), "/api/test-case/history-with-links", api_test_case_history_with_links_handler),
        (("GET",), "/api/metadata/keys", api_metadata_keys_handler),
        (("GET",), "/api/metadata/values", api_metadata_values_handler),
        (("GET",), "/api/failures/toplist", api_failures_toplist_handler),
        (("GET",), "/api/classifications/{run_id}", api_classifications_for_run_handler),
        (("GET",), "/api/tc-hover-history", api_tc_hover_history_handler),
        (("GET",), "/api/run-hover-history", api_run_hover_history_handler),
        (("POST",), "/api/migrate-data", api_migrate_data_handler),
        (("GET",), "/api/server-info", api_server_info_handler),
        (("POST",), "/api/admin/shutdown", api_admin_shutdown_handler),
        (("POST",), "/api/runs/{run_id}/commits", api_run_commits_upload_handler),
        (("GET",), "/api/runs/{run_id}/commits", api_run_commits_get_handler),
        (("GET", "POST"), "/api/runs/{run_id}/kpis", api_run_kpis_handler),
        (("GET",), "/api/kpis/catalog", api_kpi_catalog_handler),
        (("GET",), "/api/kpis/dimension-options", api_kpi_dimension_options_handler),
        (("GET",), "/api/kpis/metrics", api_kpi_metrics_handler),
        (("GET",), "/api/kpis/runs", api_kpi_runs_handler),
        (("GET",), "/api/kpis/source-options", api_kpi_source_options_handler),
        (("GET",), "/api/kpis/series", api_kpi_series_handler),
        (("GET",), "/api/kpis/history", api_kpi_history_handler),
        (("GET",), "/api/runs/{run_id}/commit-baselines", api_run_commit_baselines_handler),
        (("POST",), "/api/runs/{run_id}/analyze", api_trigger_analysis_handler),
        (("GET",), "/api/runs/{run_id}/analysis", api_analysis_status_handler),
        (("GET",), "/api/runs/{run_id}/analysis/summary", api_analysis_summary_handler),
        (("GET",), "/api/runs/{run_id}/analysis/{tc_full_name:.+}", api_analysis_for_tc_handler),
        (("POST",), "/api/runs/{run_id}/analyze/{tc_full_name:.+}/deep", api_trigger_deep_analysis_handler),
        (("GET",), "/api/runs/{run_id}/analyze/{tc_full_name:.+}/deep", api_deep_analysis_status_handler),
        (("GET",), "/api/settings/email-recipients", api_email_recipients_get_handler),
        (("PUT",), "/api/settings/email-recipients", api_email_recipients_put_handler),
        (("DELETE",), "/api/settings/email-recipients", api_email_recipients_delete_handler),
        (("GET",), "/api/settings/ai-usage", api_ai_usage_handler),
        (("GET",), "/api/logs", api_logs_handler),
        (("POST",), "/api/auth/login", api_auth_login_handler),
        (("POST",), "/api/auth/logout", api_auth_logout_handler),
        (("GET",), "/api/auth/me", api_auth_me_handler),
        (("GET", "POST"), "/api/users", api_users_handler),
        (("GET", "PUT"), "/api/users/{user_id}", api_user_handler),
        (("POST",), "/api/users/{user_id}/reset-password", api_user_reset_password_handler),
        (("POST",), "/api/users/{user_id}/unlock", api_user_unlock_handler),
    ]
