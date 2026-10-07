#!/usr/bin/env python3
"""
Integration tests using Playwright to verify the flow:
WebSocket client -> Server -> UI rendering

These tests:
1. Start the server
2. Send WebSocket messages (simulating NUnit client) using optimized binary protocol
3. Use Playwright to open browser and verify UI displays correctly

Note: These are integration tests (WebSocket -> Server -> UI), not full E2E tests.
Full E2E tests would run actual NUnit tests (NUnit -> WebSocket -> Server -> UI).
"""

import pytest
import pytest_asyncio
import asyncio
import tempfile
import shutil
import subprocess
import time
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
UTC = timezone.utc

# Add the server package source directory to the path
repo_root = Path(__file__).resolve().parent.parent
server_src_dir = repo_root / "src"
sys.path.insert(0, str(server_src_dir))

# Add tests directory to path for protocol_helpers
tests_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(tests_dir))

try:
    from playwright.async_api import async_playwright, Page, expect
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("Warning: playwright not installed. E2E tests will be skipped.")
    print("Install with: pip install playwright && playwright install chromium")

from aiohttp import ClientSession, WSMsgType
import aiohttp

from protocol_helpers import ProtocolClient


# Default timeout for integration tests (30 seconds)
INTEGRATION_TEST_TIMEOUT = 30


@pytest.mark.timeout(INTEGRATION_TEST_TIMEOUT)
class TestIntegrationUI:
    """Integration tests with browser automation (WebSocket -> Server -> UI)."""

    @pytest_asyncio.fixture
    async def temp_data_dir(self):
        """Create a temporary data directory for testing."""
        temp_dir = tempfile.mkdtemp()
        data_dir = Path(temp_dir) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        yield data_dir

        # Cleanup
        shutil.rmtree(temp_dir)

    @pytest_asyncio.fixture
    async def server_process(self, temp_data_dir):
        """Start the server in a subprocess for testing."""
        server_dir = repo_root

        # Use a fixed test port to avoid conflicts
        test_port = 18080

        # Create a dedicated test config file and point the server to it via env var
        test_config_path = temp_data_dir.parent / "testrift_server_test.yaml"
        config_content = f"""
server:
  port: {test_port}
  localhost_only: true

data:
  directory: {temp_data_dir.resolve()}
  default_retention_days: 1
"""
        test_config_path.write_text(config_content)

        # Start server - don't pipe stdout/stderr to avoid blocking
        env = os.environ.copy()
        env['PYTHONPATH'] = str(server_src_dir)
        env['TESTRIFT_SERVER_YAML'] = str(test_config_path)

        # Use DEVNULL or a log file to avoid blocking
        log_file = temp_data_dir.parent / "server_test.log"
        with open(log_file, 'w') as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "testrift_server"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(server_dir)
            )

        # Wait for server to start with better error handling
        # Check more frequently (every 0.1s) but with shorter total timeout
        max_wait = 50  # 50 * 0.1s = 5 seconds max
        started = False
        last_error = None

        for i in range(max_wait):
            try:
                async with ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{test_port}/", timeout=aiohttp.ClientTimeout(total=0.5)) as resp:
                        if resp.status == 200:
                            started = True
                            break
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(0.1)  # Check every 100ms instead of 500ms

            # Check if process died
            if process.poll() is not None:
                # Process exited, read error from log
                try:
                    with open(log_file, 'r') as f:
                        error_output = f.read()
                    raise RuntimeError(f"Server process exited with code {process.returncode}. Log: {error_output}")
                except:
                    raise RuntimeError(f"Server process exited with code {process.returncode}")

        if not started:
            process.terminate()
            try:
                process.wait(timeout=2)
            except:
                process.kill()
            raise RuntimeError(f"Server failed to start after {max_wait} attempts. Last error: {last_error}")

        yield test_port, process

        # Cleanup
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        except:
            pass

    @pytest_asyncio.fixture
    async def browser_page(self):
        """Create a Playwright browser page."""
        if not PLAYWRIGHT_AVAILABLE:
            pytest.skip("Playwright not available")

        async with async_playwright() as p:
            # Try to launch browser - use system Chrome/Edge if Playwright browser not available
            browser = None
            error_msgs = []

            # Try system Chrome first (most reliable)
            for attempt_name, launch_func in [
                ("system Chrome", lambda: p.chromium.launch(channel="chrome", headless=True)),
                ("system Edge", lambda: p.chromium.launch(channel="msedge", headless=True)),
                ("Playwright Chromium", lambda: p.chromium.launch(headless=True)),
            ]:
                try:
                    browser = await launch_func()
                    break
                except Exception as e:
                    error_msgs.append(f"{attempt_name}: {str(e)}")
                    continue

            if not browser:
                raise RuntimeError(
                    f"Failed to launch any browser. Tried:\n" + "\n".join(error_msgs) +
                    "\n\nTo fix: Install system Chrome/Edge, or run: python -m playwright install chromium"
                )

            context = await browser.new_context()
            page = await context.new_page()

            yield page

            await browser.close()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    async def test_log_message_with_dir_field_displays_direction(self, server_process, browser_page):
        """Test that explicit log directions control message styling and tooltips."""
        port, server_proc = server_process
        page = browser_page

        test_case_name = "E2ETest.DirectionBadge"
        tc_id = "00000001"

        # Step 1: Send WebSocket messages using optimized protocol
        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                # Send run_started
                response = await client.send_run_started()
                assert response.get("type") == "run_started_response"
                run_id = response.get("run_id")

                # Send test_case_started
                await client.send_test_case_started(run_id, test_case_name, tc_id)

                # Send log_batch with direction
                await client.send_log_batch(run_id, tc_id, [
                    {"message": "AT+TEST=1", "dir": "tx", "component": "TestDevice", "channel": "COM1"},
                    {"message": "OK", "dir": "rx", "component": "TestDevice", "channel": "COM1"},
                ])

                # Send test_case_finished
                await client.send_test_case_finished(run_id, tc_id, "passed")

                await asyncio.sleep(0.2)

        # Step 2: Open the test case log page in browser
        url = f"http://127.0.0.1:{port}/testRun/{run_id}/log/{tc_id}.html"
        await page.goto(url, wait_until="domcontentloaded", timeout=5000)

        await page.wait_for_selector("#msg_table tbody tr", timeout=5000)

        tx_message = page.locator("#msg_table .at-command-detail.at-tx")
        await expect(tx_message).to_have_count(1)
        await expect(tx_message).to_be_visible()
        await expect(tx_message).to_have_attribute("title", "Command sent by host to DUT")
        await expect(tx_message.locator(".at_cmd")).to_have_text("AT+TEST=1")

        rx_message = page.locator("#msg_table .at-command-detail.at-rx")
        await expect(rx_message).to_have_count(1)
        await expect(rx_message).to_be_visible()
        await expect(rx_message).to_have_attribute("title", "Response received by host from DUT")
        await expect(rx_message.locator(".at_cmd")).to_have_text("OK")

        # Verify component/channel badges are displayed
        await expect(page.locator("#msg_table").get_by_text("TestDevice").first).to_be_visible()
        await expect(page.locator("#msg_table").get_by_text("COM1").first).to_be_visible()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    async def test_exception_message_displays_in_ui(self, server_process, browser_page):
        """Test that exception messages display correctly in the UI."""
        port, server_proc = server_process
        page = browser_page

        test_case_name = "E2ETest.ExceptionDisplay"
        tc_id = "00000001"

        # Step 1: Send WebSocket messages with exception
        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                response = await client.send_run_started()
                run_id = response.get("run_id")

                await client.send_test_case_started(run_id, test_case_name, tc_id)

                await client.send_exception(
                    run_id, tc_id,
                    message="Test assertion failed",
                    exception_type="NUnit.Framework.AssertionException",
                    stack_trace=[
                        "at E2ETest.ExceptionDisplay() in ExampleTests.cs:line 42",
                        "at NUnit.Framework.Internal.Commands.TestMethodCommand.Execute(TestExecutionContext context)"
                    ],
                    is_error=False
                )

                await client.send_test_case_finished(run_id, tc_id, "failed")
                await asyncio.sleep(0.5)

        # Open test case log page
        url = f"http://127.0.0.1:{port}/testRun/{run_id}/log/{tc_id}.html"
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

        # Verify exception is displayed
        await page.wait_for_selector("#stackTraceList", timeout=5000)
        await expect(page.locator("#stackTraceList").get_by_text("Test assertion failed")).to_be_visible()
        await expect(page.locator("#stackTraceList").get_by_text("NUnit.Framework.AssertionException")).to_be_visible()
        await expect(page.locator("#stackTraceList").get_by_text("at E2ETest.ExceptionDisplay")).to_be_visible()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    async def test_exception_is_error_flag_displays_correctly(self, server_process, browser_page):
        """Test that is_error flag distinguishes Error from Failure in UI."""
        port, server_proc = server_process
        page = browser_page

        tc_full_name = "E2ETest.IsErrorFlag"
        tc_id = "00000001"

        # Step 1: Send exception with is_error=true
        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                response = await client.send_run_started()
                run_id = response.get("run_id")

                await client.send_test_case_started(run_id, tc_full_name, tc_id)

                await client.send_exception(
                    run_id, tc_id,
                    message="Unexpected runtime error",
                    exception_type="System.Exception",
                    stack_trace=["at E2ETest.IsErrorFlag() in ExampleTests.cs:line 42"],
                    is_error=True
                )

                await client.send_test_case_finished(run_id, tc_id, "failed")
                await asyncio.sleep(0.5)

        # Step 2: Open the page
        url = f"http://127.0.0.1:{port}/testRun/{run_id}/log/{tc_id}.html"
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

        # Step 3: Verify that is_error=true shows exception type (not "Failure")
        await page.wait_for_selector("#stackTraceList", timeout=5000)

        # The exception card title should show the exception_type when is_error=true
        # Since exception_type is "System.Exception", it should show that (not "Failure")
        # Scope to stack trace panel to avoid matching the log table too.
        error_title = page.locator("#stackTraceList").locator(".stack-trace-title", has_text="System.Exception")
        await expect(error_title).to_be_visible()

        # Should NOT show "Failure" title (since is_error=true)
        failure_title = page.locator("#stackTraceList").locator(".stack-trace-title", has_text="Failure")
        await expect(failure_title).not_to_be_visible()

        # Verify the exception message is visible
        await expect(page.locator("#stackTraceList").get_by_text("Unexpected runtime error")).to_be_visible()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    async def test_collapsed_subtree_shows_classification_indicators(self, server_process, browser_page):
        """Test that collapsed subtrees show classification indicators for child test cases.

        This tests the scenario where:
        1. First run contains some test cases in a namespace
        2. Second run adds a NEW test case to the same namespace
        3. When the namespace parent node is collapsed, it should show the "new" indicator
        """
        port, server_proc = server_process
        page = browser_page

        target_key = f"classification-{int(time.time())}"

        # Step 1: Create first run with one test case
        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                response = await client.send_run_started(target_key=target_key)
                first_run_id = response.get("run_id")

                tc_id_1 = "00000001"
                await client.send_test_case_started(first_run_id, "MyNamespace.MyTests.ExistingTest", tc_id_1)
                await client.send_test_case_finished(first_run_id, tc_id_1, "passed")
                await client.send_run_finished(first_run_id)
                await asyncio.sleep(0.3)

        # Step 2: Create second run with two test cases (one existing, one new)
        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                response = await client.send_run_started(target_key=target_key)
                second_run_id = response.get("run_id")

                # Existing test case
                tc_id_1 = "00000001"
                await client.send_test_case_started(second_run_id, "MyNamespace.MyTests.ExistingTest", tc_id_1)
                await client.send_test_case_finished(second_run_id, tc_id_1, "passed")

                # NEW test case
                tc_id_2 = "00000002"
                await client.send_test_case_started(second_run_id, "MyNamespace.MyTests.NewTest", tc_id_2)
                await client.send_test_case_finished(second_run_id, tc_id_2, "passed")

                await client.send_run_finished(second_run_id)
                await asyncio.sleep(0.3)

        # Step 3: Open the second run's test run page
        url = f"http://127.0.0.1:{port}/testRun/{second_run_id}/index.html"
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

        await page.wait_for_selector("#test-cases-list", timeout=5000)
        await asyncio.sleep(1.0)

        new_indicator = page.locator(".new-tc-indicator")
        await expect(new_indicator).to_be_visible()

        collapse_btn = page.locator("#collapse-all-btn")
        await collapse_btn.click()
        await asyncio.sleep(0.3)

        collapsed_classification = page.locator(".collapsed-classification-container .new-tc-indicator").first
        await expect(collapsed_classification).to_be_visible()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    async def test_status_badges_are_horizontally_aligned_in_tree_view(self, server_process, browser_page):
        """Test that status badges are horizontally aligned in tree view."""
        port, server_proc = server_process
        page = browser_page

        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                response = await client.send_run_started()
                run_id = response.get("run_id")

                # Add test cases with varying name lengths
                test_cases = [
                    ("MyNamespace.Tests.Short", "00000001"),
                    ("MyNamespace.Tests.AVeryLongTestCaseNameThatShouldPushTheBadge", "00000002"),
                    ("MyNamespace.Tests.Medium", "00000003"),
                ]

                for tc_name, tc_id in test_cases:
                    await client.send_test_case_started(run_id, tc_name, tc_id)
                    await client.send_test_case_finished(run_id, tc_id, "passed")

                await client.send_run_finished(run_id)
                await asyncio.sleep(0.3)

        # Open the test run page
        url = f"http://127.0.0.1:{port}/testRun/{run_id}/index.html"
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

        await page.wait_for_selector("#test-cases-list", timeout=5000)
        await asyncio.sleep(0.5)

        tc_right_elements = page.locator("li.test-case-node .tc-right")
        await expect(tc_right_elements).to_have_count(len(test_cases))

        left_positions = []
        for element in await tc_right_elements.all():
            await expect(element).to_be_visible()
            box = await element.bounding_box()
            assert box is not None, "Test case badge should have a bounding box"
            left_positions.append(box['x'])

        first_left = left_positions[0]
        for pos in left_positions:
            assert abs(pos - first_left) < 5, f"Status badges not aligned: positions {left_positions}"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    async def test_status_badges_visible_in_list_view(self, server_process, browser_page):
        """Test that status badges are visible and within viewport in list view."""
        port, server_proc = server_process
        page = browser_page

        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                response = await client.send_run_started()
                run_id = response.get("run_id")

                test_cases = [
                    ("MyNamespace.Tests.Short", "00000001"),
                    ("MyNamespace.Tests.AVeryLongTestCaseNameThatShouldPushTheBadge", "00000002"),
                    ("MyNamespace.Tests.Medium", "00000003"),
                ]

                for tc_name, tc_id in test_cases:
                    await client.send_test_case_started(run_id, tc_name, tc_id)
                    await client.send_test_case_finished(run_id, tc_id, "passed")

                await client.send_run_finished(run_id)
                await asyncio.sleep(0.3)

        # Open the test run page
        url = f"http://127.0.0.1:{port}/testRun/{run_id}/index.html"
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

        await page.wait_for_selector("#test-cases-list", timeout=5000)
        await asyncio.sleep(0.3)

        view_toggle = page.locator("#view-toggle-btn")
        await view_toggle.click()
        await asyncio.sleep(0.3)

        viewport = page.viewport_size
        viewport_width = viewport['width'] if viewport else 1280

        list_items = await page.locator(".list-view > li").all()
        assert len(list_items) >= 3, "Expected at least 3 test case items in list view"

        for item in list_items:
            badge = item.locator(".badge")
            box = await badge.bounding_box()
            assert box is not None, "Badge should have a bounding box"
            assert box['x'] < viewport_width, f"Badge is off-screen: x={box['x']}, viewport_width={viewport_width}"
            assert box['x'] >= 0, f"Badge is off-screen to the left: x={box['x']}"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright not installed")
    async def test_tree_hover_does_not_shift_tc_right(self, server_process, browser_page):
        """Test that hovering over a test case doesn't move the status badge."""
        port, server_proc = server_process
        page = browser_page

        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws/nunit") as ws:
                client = ProtocolClient(ws)

                response = await client.send_run_started()
                run_id = response.get("run_id")

                await client.send_test_case_started(run_id, "MyNamespace.Tests.TestCase1", "00000001")
                await client.send_test_case_finished(run_id, "00000001", "passed")
                await client.send_run_finished(run_id)
                await asyncio.sleep(0.3)

        # Open the test run page
        url = f"http://127.0.0.1:{port}/testRun/{run_id}/index.html"
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

        await page.wait_for_selector("#test-cases-list", timeout=5000)
        await asyncio.sleep(0.3)

        test_case_li = page.locator("li.test-case-node").first
        await test_case_li.scroll_into_view_if_needed()
        tc_right = test_case_li.locator(".tc-right")

        # Get position before hover
        box_before = await tc_right.bounding_box()
        assert box_before is not None, "Could not get bounding box of .tc-right"
        x_before = box_before['x']

        await test_case_li.hover()
        await asyncio.sleep(0.3)  # Wait for any transition

        # Get position after hover
        box_after = await tc_right.bounding_box()
        assert box_after is not None, "Could not get bounding box after hover"
        x_after = box_after['x']

        # The .tc-right should NOT move horizontally
        x_shift = abs(x_after - x_before)
        assert x_shift < 2, f".tc-right moved {x_shift}px on hover (expected <2px)"
