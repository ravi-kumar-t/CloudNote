import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import sys
from types import ModuleType

# Setup mocks for dependencies before imports
logger_mock = MagicMock()
logger_module = ModuleType('app.logger')
logger_module.logger = logger_mock
sys.modules['app.logger'] = logger_module

# Now import the function under test
from app.joiner import analyze_lecture_state

class TestLectureStatePriority(unittest.IsolatedAsyncioTestCase):
    async def test_both_countdown_and_active_join_button(self):
        """When both countdown text and active join button are present, priority should be JOINABLE_ACTIVE."""
        page = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.screenshot = AsyncMock()
        page.content = AsyncMock(return_value="<html></html>")
        page.inner_text = AsyncMock(return_value="Class starts in 01h 05m 12s. Join not available.")
        
        # Mock locator for elements
        element_mock = AsyncMock()
        element_mock.is_visible = AsyncMock(return_value=True)
        element_mock.inner_text = AsyncMock(return_value="Join Lecture Now")
        element_mock.get_attribute = AsyncMock(side_effect=lambda attr: {
            "href": "jnr.jsp?id=123",
            "class": "joinBtn btn-success",
            "aria-label": "Join"
        }.get(attr, ""))
        element_mock.is_disabled = AsyncMock(return_value=False)
        
        locator_mock = AsyncMock()
        locator_mock.all = AsyncMock(return_value=[element_mock])
        page.locator = MagicMock(return_value=locator_mock)
        
        res = await analyze_lecture_state(page)
        self.assertEqual(res["state"], "JOINABLE_ACTIVE")

    async def test_only_countdown_no_active_join_button(self):
        """When countdown is present but no active join button, state should be UPCOMING."""
        page = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.screenshot = AsyncMock()
        page.content = AsyncMock(return_value="<html></html>")
        page.inner_text = AsyncMock(return_value="Class starts in 01h 05m 12s.")
        
        # Empty interactive elements
        locator_mock = AsyncMock()
        locator_mock.all = AsyncMock(return_value=[])
        page.locator = MagicMock(return_value=locator_mock)
        
        res = await analyze_lecture_state(page)
        self.assertEqual(res["state"], "UPCOMING")

    async def test_not_yet_available_explicit(self):
        """When page contains explicit 'not available' and no join button or countdown, state should be NOT_YET_AVAILABLE."""
        page = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.screenshot = AsyncMock()
        page.content = AsyncMock(return_value="<html></html>")
        page.inner_text = AsyncMock(return_value="Join is not available at this time.")
        
        # Empty elements
        locator_mock = AsyncMock()
        locator_mock.all = AsyncMock(return_value=[])
        page.locator = MagicMock(return_value=locator_mock)
        
        res = await analyze_lecture_state(page)
        self.assertEqual(res["state"], "NOT_YET_AVAILABLE")

    async def test_completed_state(self):
        """When body contains completed/ended, state should be COMPLETED."""
        page = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.screenshot = AsyncMock()
        page.content = AsyncMock(return_value="<html></html>")
        page.inner_text = AsyncMock(return_value="This session has been completed.")
        
        locator_mock = AsyncMock()
        locator_mock.all = AsyncMock(return_value=[])
        page.locator = MagicMock(return_value=locator_mock)
        
        res = await analyze_lecture_state(page)
        self.assertEqual(res["state"], "COMPLETED")

if __name__ == '__main__':
    unittest.main()
