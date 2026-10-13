#!/usr/bin/env python3
"""
Tests for HTTP API endpoints and handlers.
"""

import json
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
UTC = timezone.utc
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from testrift_server import database
from testrift_server.database import TestRunData, TestCaseData, UserMetadata
from testrift_server.summary_profiles import SummarySelection


class TestHTTPAPI:
    """Test HTTP API endpoints and handlers."""

    @pytest_asyncio.fixture
    async def temp_db(self):
        """Create a temporary database for testing."""
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "test.db"

        # Initialize database
        database.initialize_database(Path(temp_dir))
        await database.db.initialize()

        yield db_path

        # Cleanup
        shutil.rmtree(temp_dir)

    @pytest_asyncio.fixture
    async def initialized_db(self, temp_db):
        """Ensure database is initialized for tests."""
        assert database.db is not None
        return database.db

    @pytest_asyncio.fixture
    async def sample_test_run(self, initialized_db):
        """Create a sample test run in the database."""
        test_run = TestRunData(
            run_id="test-run-123",
            status="finished",
            start_time=datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            end_time=datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            retention_days=7,
            local_run=False,
            dut="TestDevice-001"
        )

        user_metadata = {
            "DUT": {"value": "TestDevice-001"},
            "Environment": {"value": "Test"}
        }

        await initialized_db.insert_test_run(test_run, user_metadata)

        # Add some test cases
        test_cases = [
            TestCaseData(0, "test-run-123", "Test.Passed", "tc_passed_001", "passed",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"),
            TestCaseData(0, "test-run-123", "Test.Failed", "tc_failed_001", "failed",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"),
        ]

        for tc in test_cases:
            await initialized_db.insert_test_case(tc)

        return test_run

    @pytest.mark.asyncio
    async def test_health_handler_basic(self):
        """Test the health check endpoint basic functionality."""
        from testrift_server.handlers import health_handler

        # Create a mock request
        request = MagicMock()

        # Call the handler
        response = await health_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_test_runs_handler_basic(self, initialized_db, sample_test_run):
        """Test the API test runs endpoint basic functionality."""
        from testrift_server.api_handlers import api_test_runs_handler

        # Create a mock request with query parameters
        request = MagicMock()
        request.query = {
            'limit': '10',
            'offset': '0'
        }

        # Call the handler
        response = await api_test_runs_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_test_runs_handler_with_status_filter(self, initialized_db, sample_test_run):
        """Test the API test runs endpoint with status filter."""
        from testrift_server.api_handlers import api_test_runs_handler

        # Create a mock request with status filter
        request = MagicMock()
        request.query = {
            'limit': '10',
            'offset': '0',
            'status': 'finished'
        }

        # Call the handler
        response = await api_test_runs_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_test_runs_handler_with_metadata_filters(self, initialized_db, sample_test_run):
        """Test the API test runs endpoint with metadata filters."""
        from testrift_server.api_handlers import api_test_runs_handler

        # Create a mock request with metadata filter
        request = MagicMock()
        request.query = {
            'limit': '10',
            'offset': '0',
            'metadata.DUT': 'TestDevice-001'
        }

        # Call the handler
        response = await api_test_runs_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_collection_profile_accepts_custom_run_purpose(self, initialized_db):
        from testrift_server.api_handlers import api_collection_profiles_handler, api_profile_handler

        await initialized_db.create_collection("history", "History")
        request = MagicMock()
        request.match_info = {"key": "history"}
        request.json = AsyncMock(return_value={
            "name": "Imported history",
            "purpose": "historical-import",
            "window_hours": 2160,
            "is_primary": True,
            "selectors": [],
        })

        response = await api_collection_profiles_handler(request)

        assert response.status == 201
        created = json.loads(response.text)["data"]
        assert created["purpose"] == "historical-import"
        assert created["window_hours"] == 2160
        assert created["is_primary"]
        assert created["selectors"] == []

        request.method = "PUT"
        request.match_info = {"profile_id": str(created["id"])}
        request.json.return_value["name"] = "Renamed history"
        response = await api_profile_handler(request)

        assert response.status == 200
        assert json.loads(response.text)["data"]["purpose"] == "historical-import"

    @pytest.mark.parametrize("purpose", [None, "", " \t", 42, [], {}])
    def test_collection_profile_rejects_invalid_run_purpose(self, purpose):
        from testrift_server.api_handlers import _profile_values

        with pytest.raises(ValueError, match="purpose must be a nonblank string"):
            _profile_values({"name": "History", "purpose": purpose, "window_hours": 2160})

    @pytest.mark.asyncio
    async def test_collection_summary_includes_run_results_and_missing_targets(self):
        from testrift_server.api_handlers import api_collection_summary_handler

        request = MagicMock()
        request.match_info = {'key': 'nightly'}
        request.query = {'profile_id': '7', 'at': '2026-07-20T12:00:00Z'}
        mock_database = AsyncMock()
        mock_database.get_collection.return_value = {
            'profiles': [{'id': 7, 'is_primary': True}],
        }
        mock_database.get_test_runs.return_value = [{
            'run_id': 'selected-run',
            'status': 'finished',
            'start_time': '2026-07-20T11:00:00Z',
            'end_time': '2026-07-20T11:30:00Z',
            'passed_count': 12,
            'failed_count': 2,
            'skipped_count': 1,
            'aborted_count': 0,
            'error_count': 0,
            'test_case_count': 15,
        }]
        selections = [
            SummarySelection('nora-b26x', 'selected-run', None),
            SummarySelection('sara-b26x', None, 'no_matching_run'),
        ]

        with patch('testrift_server.api_handlers.database.db', mock_database), patch(
            'testrift_server.api_handlers.select_profile_from_database',
            AsyncMock(return_value=selections),
        ):
            response = await api_collection_summary_handler(request)

        payload = json.loads(response.text)['data']
        assert payload[0] == {
            'target_key': 'nora-b26x',
            'run_id': 'selected-run',
            'reason': None,
            'status': 'finished',
            'start_time': '2026-07-20T11:00:00Z',
            'end_time': '2026-07-20T11:30:00Z',
            'passed_count': 12,
            'failed_count': 2,
            'skipped_count': 1,
            'aborted_count': 0,
            'error_count': 0,
            'test_case_count': 15,
        }
        assert payload[1] == {
            'target_key': 'sara-b26x',
            'run_id': None,
            'reason': 'no_matching_run',
        }

    @pytest.mark.asyncio
    async def test_api_test_run_details_handler_basic(self, initialized_db, sample_test_run):
        """Test the API test run details endpoint basic functionality."""
        from testrift_server.api_handlers import api_test_run_details_handler

        # Create a mock request
        request = MagicMock()
        request.match_info = {'run_id': 'test-run-123'}

        # Call the handler
        response = await api_test_run_details_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_test_run_details_handler_not_found(self, initialized_db):
        """Test the API test run details endpoint with non-existent run."""
        from testrift_server.api_handlers import api_test_run_details_handler

        # Create a mock request
        request = MagicMock()
        request.match_info = {'run_id': 'non-existent-run'}

        # Call the handler
        response = await api_test_run_details_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_test_results_for_runs_handler_basic(self, initialized_db, sample_test_run):
        """Test the API test results for runs endpoint basic functionality."""
        from testrift_server.api_handlers import api_test_results_for_runs_handler

        # Create a mock request
        request = MagicMock()
        request.query = {
            'run_ids': 'test-run-123'
        }

        # Call the handler
        response = await api_test_results_for_runs_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_test_results_over_time_handler_basic(self, initialized_db, sample_test_run):
        """Test the API test results over time endpoint basic functionality."""
        from testrift_server.api_handlers import api_test_results_over_time_handler

        # Create a mock request
        request = MagicMock()
        request.query = {
            'days': '30'
        }

        # Call the handler
        response = await api_test_results_over_time_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_test_case_history_handler_basic(self, initialized_db, sample_test_run):
        """Test the API test case history endpoint basic functionality."""
        from testrift_server.api_handlers import api_test_case_history_handler

        # Create a mock request
        request = MagicMock()
        request.query = {
            'test_case_id': 'Test.Passed',
            'limit': '10'
        }

        # Call the handler
        response = await api_test_case_history_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_metadata_keys_handler_basic(self, initialized_db, sample_test_run):
        """Test the API metadata keys endpoint basic functionality."""
        from testrift_server.api_handlers import api_metadata_keys_handler

        # Create a mock request
        request = MagicMock()

        # Call the handler
        response = await api_metadata_keys_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_metadata_values_handler_basic(self, initialized_db, sample_test_run):
        """Test the API metadata values endpoint basic functionality."""
        from testrift_server.api_handlers import api_metadata_values_handler

        # Create a mock request
        request = MagicMock()
        request.query = {
            'key': 'DUT'
        }

        # Call the handler
        response = await api_metadata_values_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_api_metadata_values_handler_invalid_key(self, initialized_db):
        """Test the API metadata values endpoint with invalid key."""
        from testrift_server.api_handlers import api_metadata_values_handler

        # Create a mock request
        request = MagicMock()
        request.query = {
            'key': 'InvalidKey'
        }

        # Call the handler
        response = await api_metadata_values_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')


class TestDatabaseAPI:
    """Test database functions used by API endpoints."""

    @pytest_asyncio.fixture
    async def temp_db(self):
        """Create a temporary database for testing."""
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "test.db"

        # Initialize database
        database.initialize_database(Path(temp_dir))
        await database.db.initialize()

        yield db_path

        # Cleanup
        shutil.rmtree(temp_dir)

    @pytest_asyncio.fixture
    async def initialized_db(self, temp_db):
        """Ensure database is initialized for tests."""
        assert database.db is not None
        return database.db

    @pytest_asyncio.fixture
    async def sample_test_run(self, initialized_db):
        """Create a sample test run in the database."""
        test_run = TestRunData(
            run_id="test-run-123",
            status="finished",
            start_time=datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            end_time=datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            retention_days=7,
            local_run=False,
            dut="TestDevice-001"
        )

        user_metadata = {
            "DUT": {"value": "TestDevice-001"},
            "Environment": {"value": "Test"}
        }

        await initialized_db.insert_test_run(test_run, user_metadata)

        # Add some test cases
        test_cases = [
            TestCaseData(0, "test-run-123", "Test.Passed", "tc_passed_002", "passed",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"),
            TestCaseData(0, "test-run-123", "Test.Failed", "tc_failed_002", "failed",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
                        datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"),
        ]

        for tc in test_cases:
            await initialized_db.insert_test_case(tc)

        return test_run

    @pytest.mark.asyncio
    async def test_get_test_runs_with_filters(self, initialized_db, sample_test_run):
        """Test get_test_runs with various filters."""
        # Test with status filter
        runs = await initialized_db.get_test_runs(limit=10, status_filter="finished")
        assert len(runs) >= 1
        for run in runs:
            assert run["status"] == "finished"

        # Test with metadata filter
        metadata_filters = {"DUT": "TestDevice-001"}
        runs = await initialized_db.get_test_runs(limit=10, metadata_filters=metadata_filters)
        assert len(runs) >= 1

        # Test with limit and offset
        runs = await initialized_db.get_test_runs(limit=1, offset=0)
        assert len(runs) <= 1

    @pytest.mark.asyncio
    async def test_get_test_runs_over_time(self, initialized_db, sample_test_run):
        """Test get_test_runs_over_time function."""
        runs = await initialized_db.get_test_runs_over_time(days_back=30)
        assert isinstance(runs, list)
        assert len(runs) >= 1

    @pytest.mark.asyncio
    async def test_get_test_results_for_runs(self, initialized_db, sample_test_run):
        """Test get_test_results_for_runs function."""
        run_ids = ["test-run-123"]
        results = await initialized_db.get_test_results_for_runs(run_ids)
        assert isinstance(results, dict)
        assert "test-run-123" in results
        assert len(results["test-run-123"]) >= 1

    @pytest.mark.asyncio
    async def test_get_test_case_history(self, initialized_db, sample_test_run):
        """Test get_test_case_history function."""
        history = await initialized_db.get_test_case_history("Test.Passed", limit=10)
        assert isinstance(history, list)
        assert len(history) >= 1

    @pytest.mark.asyncio
    async def test_get_user_metadata_for_run(self, initialized_db, sample_test_run):
        """Test get_user_metadata_for_run function."""
        metadata = await initialized_db.get_user_metadata_for_run("test-run-123")
        assert isinstance(metadata, dict)
        assert "DUT" in metadata
        assert "Environment" in metadata

    @pytest.mark.asyncio
    async def test_get_all_metadata_keys(self, initialized_db, sample_test_run):
        """Test get_all_metadata_keys function."""
        keys = await initialized_db.get_all_metadata_keys()
        assert isinstance(keys, list)
        assert "DUT" in keys
        assert "Environment" in keys

    @pytest.mark.asyncio
    async def test_get_unique_metadata_values(self, initialized_db, sample_test_run):
        """Test get_unique_metadata_values function."""
        values = await initialized_db.get_unique_metadata_values("DUT")
        assert isinstance(values, list)
        assert "TestDevice-001" in values

    @pytest.mark.asyncio
    async def test_update_test_run(self, initialized_db, sample_test_run):
        """Test update_test_run function."""
        # Update the test run
        success = await initialized_db.update_test_run(
            "test-run-123",
            status="aborted",
            end_time=datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
        )
        assert success is True

        # Verify the update
        runs = await initialized_db.get_test_runs(limit=10)
        updated_run = next((r for r in runs if r["run_id"] == "test-run-123"), None)
        assert updated_run is not None
        assert updated_run["status"] == "aborted"


class TestStaticHandlers:
    """Test static file and export handlers."""

    @pytest_asyncio.fixture
    async def temp_db(self):
        """Create a temporary database for testing."""
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "test.db"

        # Initialize database
        database.initialize_database(Path(temp_dir))
        await database.db.initialize()

        yield db_path

        # Cleanup
        shutil.rmtree(temp_dir)

    @pytest_asyncio.fixture
    async def initialized_db(self, temp_db):
        """Ensure database is initialized for tests."""
        assert database.db is not None
        return database.db

    @pytest.mark.asyncio
    async def test_static_file_handler_basic(self):
        """Test static file handler basic functionality."""
        from testrift_server.handlers import static_file_handler

        # Create a mock request
        request = MagicMock()
        request.match_info = {'path': 'test_case_log.css'}

        # Call the handler
        response = await static_file_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')

    @pytest.mark.asyncio
    async def test_zip_export_handler_basic(self, initialized_db):
        """Test ZIP export handler basic functionality."""
        from testrift_server.handlers import zip_export_handler

        # Create a mock request
        request = MagicMock()
        request.match_info = {'run_id': 'test-run-123'}

        # Call the handler
        response = await zip_export_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')

    @pytest.mark.asyncio
    async def test_analyzer_handler_basic(self, initialized_db):
        """Test analyzer page handler basic functionality."""
        from testrift_server.handlers import analyzer_handler

        # Create a mock request
        request = MagicMock()

        # Call the handler
        response = await analyzer_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_matrix_handler_basic(self, initialized_db):
        """Test matrix page handler basic functionality."""
        from testrift_server.handlers import matrix_handler

        # Create a mock request
        request = MagicMock()

        # Call the handler
        response = await matrix_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')


class TestAttachmentAPI:
    """Test attachment API endpoints (if enabled)."""

    @pytest_asyncio.fixture
    async def temp_db(self):
        """Create a temporary database for testing."""
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "test.db"

        # Initialize database
        database.initialize_database(Path(temp_dir))
        await database.db.initialize()

        yield db_path

        # Cleanup
        shutil.rmtree(temp_dir)

    @pytest_asyncio.fixture
    async def initialized_db(self, temp_db):
        """Ensure database is initialized for tests."""
        assert database.db is not None
        return database.db

    @pytest.mark.asyncio
    async def test_upload_attachment_handler_basic(self, initialized_db):
        """Test upload attachment handler basic functionality."""
        from testrift_server.handlers import upload_attachment_handler

        # Create a mock request
        request = MagicMock()
        request.match_info = {
            'run_id': 'test-run-123',
            'test_case_id': 'Test.TestMethod'
        }

        # Mock multipart data
        request.post = AsyncMock(return_value={
            'file': MagicMock()
        })

        # Call the handler
        response = await upload_attachment_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_list_attachments_handler_basic(self, initialized_db):
        """Test list attachments handler basic functionality."""
        from testrift_server.handlers import list_attachments_handler
        from testrift_server.utils import generate_storage_id
        from types import SimpleNamespace

        # Generate tc_id
        tc_id = generate_storage_id()

        # Create a mock request with real app structure
        request = MagicMock()
        request.match_info = {
            'run_id': 'test-run-123',
            'test_case_id': tc_id
        }
        ws_server = SimpleNamespace(test_runs={})
        request.app = {"ws_server": ws_server}

        # Call the handler
        response = await list_attachments_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')
        assert hasattr(response, 'content_type')

    @pytest.mark.asyncio
    async def test_download_attachment_handler_basic(self, initialized_db):
        """Test download attachment handler basic functionality."""
        from testrift_server.handlers import download_attachment_handler
        from testrift_server.utils import generate_storage_id
        from types import SimpleNamespace

        # Generate tc_id
        tc_id = generate_storage_id()

        # Create a mock request with real app structure
        request = MagicMock()
        request.match_info = {
            'run_id': 'test-run-123',
            'test_case_id': tc_id,
            'filename': 'test.txt'
        }
        ws_server = SimpleNamespace(test_runs={})
        request.app = {"ws_server": ws_server}

        # Call the handler
        response = await download_attachment_handler(request)

        # Verify response is a web.Response
        assert response is not None
        assert hasattr(response, 'status')


class TestValidationFunctions:
    """Test validation functions used by handlers."""

    @pytest.mark.asyncio
    async def test_validate_run_id(self):
        """Test run ID validation function."""
        from testrift_server.utils import validate_run_id

        # Test valid run IDs
        assert validate_run_id("test-run-123") is True
        assert validate_run_id("run-abc-def") is True
        assert validate_run_id("simple") is True

        # Test invalid run IDs
        assert validate_run_id("") is False
        assert validate_run_id(None) is False
        assert validate_run_id("../invalid") is False
        assert validate_run_id("invalid/path") is False
        assert validate_run_id("invalid\\path") is False

    @pytest.mark.asyncio
    async def test_validate_test_case_id(self):
        """Test test case ID validation function."""
        from testrift_server.utils import validate_test_case_id

        # Test valid NUnit test IDs (alphanumeric and hyphens, e.g., "0-1009")
        assert validate_test_case_id("0-1009") is True
        assert validate_test_case_id("0-1008") is True
        assert validate_test_case_id("abc123") is True
        assert validate_test_case_id("test-42") is True

        # Test invalid test case IDs
        assert validate_test_case_id("") is False
        assert validate_test_case_id(None) is False
        assert validate_test_case_id("../invalid") is False
        assert validate_test_case_id("invalid/path") is False
        assert validate_test_case_id("invalid\\path") is False
        assert validate_test_case_id("Test.TestMethod") is False  # Dots not allowed (old format)


if __name__ == "__main__":
    pytest.main([__file__])