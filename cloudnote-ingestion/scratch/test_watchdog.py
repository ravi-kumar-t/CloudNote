import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import sys
from types import ModuleType

# Setup mocks for dependencies before imports
logger_mock = MagicMock()
logger_mock.info = MagicMock()
logger_mock.warning = MagicMock()
logger_mock.error = MagicMock()

logger_module = ModuleType('app.logger')
logger_module.logger = logger_mock
sys.modules['app.logger'] = logger_module

# Now import modules under test
from app.watchdog import check_session_health, attempt_recovery
from app.selectors import MeetingSelectors

class TestWatchdogHealthCheck(unittest.IsolatedAsyncioTestCase):
    async def test_check_session_health_healthy(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        
        # Mock iframe count
        iframe_element_mock = AsyncMock()
        iframe_element_mock.count = AsyncMock(return_value=1)
        
        # Mock frame with active post-join element (#app)
        frame_mock = AsyncMock()
        frame_mock.query_selector = AsyncMock(side_effect=lambda sel: MagicMock() if sel == "#app" else None)
        
        # Mock frame locator body text
        frame_body_mock = AsyncMock()
        frame_body_mock.inner_text = AsyncMock(return_value="Frame is active, no errors")
        frame_mock.locator = MagicMock(return_value=frame_body_mock)
        page.frames = [frame_mock]
        
        # Mock body text without errors
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="Meeting started, active lecture screen")
        
        def page_locator_mock(selector):
            if selector == "iframe" or selector == MeetingSelectors.IFRAME:
                return iframe_element_mock
            else:
                return body_mock
                
        page.locator = MagicMock(side_effect=page_locator_mock)
        
        healthy = await check_session_health(page, browser)
        self.assertTrue(healthy)

    async def test_check_session_health_browser_disconnected(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=False)
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        
        healthy = await check_session_health(page, browser)
        self.assertFalse(healthy)

    async def test_check_session_health_page_closed(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=True)
        
        healthy = await check_session_health(page, browser)
        self.assertFalse(healthy)

    async def test_check_session_health_iframe_detached(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        
        # Mock iframe count as 0 (detached)
        iframe_element_mock = AsyncMock()
        iframe_element_mock.count = AsyncMock(return_value=0)
        page.locator = MagicMock(side_effect=lambda sel: iframe_element_mock if sel in ["iframe", MeetingSelectors.IFRAME] else AsyncMock())
        
        healthy = await check_session_health(page, browser)
        self.assertFalse(healthy)

    async def test_check_session_health_no_post_join_container(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        
        # Mock iframe count as 1
        iframe_element_mock = AsyncMock()
        iframe_element_mock.count = AsyncMock(return_value=1)
        
        # Mock frame WITHOUT any post-join containers
        frame_mock = AsyncMock()
        frame_mock.query_selector = AsyncMock(return_value=None)
        
        # Mock frame locator body text
        frame_body_mock = AsyncMock()
        frame_body_mock.inner_text = AsyncMock(return_value="Frame has no containers")
        frame_mock.locator = MagicMock(return_value=frame_body_mock)
        page.frames = [frame_mock]
        
        # Mock body text
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="No containers here")
        page.locator = MagicMock(side_effect=lambda sel: iframe_element_mock if sel in ["iframe", MeetingSelectors.IFRAME] else body_mock)
        
        healthy = await check_session_health(page, browser)
        self.assertFalse(healthy)

    async def test_check_session_health_websocket_disconnect(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        
        # Mock iframe count as 1
        iframe_element_mock = AsyncMock()
        iframe_element_mock.count = AsyncMock(return_value=1)
        
        # Mock frame with active post-join element
        frame_mock = AsyncMock()
        frame_mock.query_selector = AsyncMock(side_effect=lambda sel: MagicMock() if sel == "#app" else None)
        
        # Mock frame locator body text WITH disconnect signal
        frame_body_mock = AsyncMock()
        frame_body_mock.inner_text = AsyncMock(return_value="Websocket disconnected error page")
        frame_mock.locator = MagicMock(return_value=frame_body_mock)
        page.frames = [frame_mock]
        
        # Mock body text
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="Standard outer page text")
        page.locator = MagicMock(side_effect=lambda sel: iframe_element_mock if sel in ["iframe", MeetingSelectors.IFRAME] else body_mock)
        
        healthy = await check_session_health(page, browser)
        self.assertFalse(healthy)

class TestWatchdogRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_attempt_recovery_reconnect_success(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.reload = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        
        frame_mock = AsyncMock()
        frame_mock.query_selector = AsyncMock(return_value=MagicMock())
        page.frames = [frame_mock]
        
        context = MagicMock()
        p_mock = MagicMock()
        
        # Patch handle_meeting_room since reload success triggers it
        with patch('app.joiner.handle_meeting_room', new_callable=AsyncMock) as mock_handle:
            mock_handle.return_value = None
            res = await attempt_recovery(page, browser, context, p_mock)
            self.assertIsNotNone(res)
            self.assertEqual(res[0], page)
            page.reload.assert_called_once()
            mock_handle.assert_called_once_with(page)

    async def test_attempt_recovery_full_relogin_and_reopen_success(self):
        browser = MagicMock()
        browser.is_connected = MagicMock(return_value=True)
        browser.close = AsyncMock()
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.close = AsyncMock()
        page.reload = AsyncMock(side_effect=Exception("Reload failed!"))
        
        context = AsyncMock()
        context.close = AsyncMock()
        
        p_mock = MagicMock()
        
        # Recreated resources mocks
        new_browser = MagicMock()
        new_browser.is_connected = MagicMock(return_value=True)
        
        new_context = AsyncMock()
        
        new_page = AsyncMock()
        new_page.is_closed = MagicMock(return_value=False)
        
        # Mock main.create_browser_and_page, login.perform_login, and joiner.join_class_pipeline
        with patch('app.main.create_browser_and_page', new_callable=AsyncMock) as mock_create, \
             patch('app.login.perform_login', new_callable=AsyncMock) as mock_login, \
             patch('app.joiner.join_class_pipeline', new_callable=AsyncMock) as mock_join:
             
            mock_create.return_value = (new_browser, new_context, new_page)
            mock_login.return_value = None
            mock_join.return_value = True
            
            res = await attempt_recovery(page, browser, context, p_mock)
            
            self.assertIsNotNone(res)
            self.assertEqual(res[0], new_page)
            self.assertEqual(res[1], new_browser)
            self.assertEqual(res[2], new_context)
            
            # Verify cleanups were executed
            page.close.assert_called_once()
            context.close.assert_called_once()
            browser.close.assert_called_once()
            
            # Verify recreations were executed
            mock_create.assert_called_once_with(p_mock)
            mock_login.assert_called_once_with(new_page)
            mock_join.assert_called_once_with(new_page)

if __name__ == '__main__':
    unittest.main()
