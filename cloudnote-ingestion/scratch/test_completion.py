import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.main import check_lecture_completion

class TestLectureCompletion(unittest.IsolatedAsyncioTestCase):
    async def test_completion_url_redirect(self):
        """Should detect completion if URL changed back to CodeTantra dashboard away from meeting."""
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://myclass.lpu.in/dashboard.jsp"
        page.frames = []
        
        is_completed = await check_lecture_completion(page)
        self.assertTrue(is_completed)

    async def test_completion_direct_signature_main_page(self):
        """Should detect completion if main page contains 'meeting has ended' text."""
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://myclass.lpu.in/html5client/join"
        
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="The meeting has ended. Thank you for participating.")
        page.locator = MagicMock(side_effect=lambda sel: body_mock if sel == "body" else AsyncMock())
        page.frames = []
        
        is_completed = await check_lecture_completion(page)
        self.assertTrue(is_completed)

    async def test_completion_direct_signature_frame(self):
        """Should detect completion if a frame contains 'session ended' text."""
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://myclass.lpu.in/html5client/join"
        
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="Active meeting UI")
        page.locator = MagicMock(side_effect=lambda sel: body_mock if sel == "body" else AsyncMock())
        
        frame = AsyncMock()
        frame.url = "https://myclass.lpu.in/html5client/frame"
        frame_body = AsyncMock()
        frame_body.inner_text = AsyncMock(return_value="Your session ended.")
        frame.locator = MagicMock(side_effect=lambda sel: frame_body if sel == "body" else AsyncMock())
        
        page.frames = [frame]
        
        is_completed = await check_lecture_completion(page)
        self.assertTrue(is_completed)

    async def test_completion_missing_bbb_frame(self):
        """Should detect completion if there are no frames containing '/html5client/'."""
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://myclass.lpu.in/html5client/join"
        
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="General LPU UI")
        page.locator = MagicMock(side_effect=lambda sel: body_mock if sel == "body" else AsyncMock())
        
        # Frame doesn't contain html5client
        frame = AsyncMock()
        frame.url = "https://myclass.lpu.in/some-other-frame"
        frame_body = AsyncMock()
        frame_body.inner_text = AsyncMock(return_value="General LPU text")
        frame.locator = MagicMock(side_effect=lambda sel: frame_body if sel == "body" else AsyncMock())
        page.frames = [frame]
        
        is_completed = await check_lecture_completion(page)
        self.assertTrue(is_completed)

    async def test_completion_missing_core_containers(self):
        """Should detect completion if html5client frame exists but all core containers (#app, etc) are missing."""
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://myclass.lpu.in/html5client/join"
        
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="General LPU UI")
        page.locator = MagicMock(side_effect=lambda sel: body_mock if sel == "body" else AsyncMock())
        
        frame = AsyncMock()
        frame.url = "https://myclass.lpu.in/html5client/frame"
        frame_body = AsyncMock()
        frame_body.inner_text = AsyncMock(return_value="Some UI text")
        
        # Core containers are missing (count = 0)
        locator_mock = AsyncMock()
        locator_mock.count = AsyncMock(return_value=0)
        
        def frame_locator(sel):
            if sel == "body":
                return frame_body
            return locator_mock
            
        frame.locator = MagicMock(side_effect=frame_locator)
        page.frames = [frame]
        
        is_completed = await check_lecture_completion(page)
        self.assertTrue(is_completed)

    async def test_active_session_not_completed(self):
        """Should return False if session is fully active with iframe and core containers present."""
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://myclass.lpu.in/html5client/join"
        
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="Meeting in progress")
        page.locator = MagicMock(side_effect=lambda sel: body_mock if sel == "body" else AsyncMock())
        
        frame = AsyncMock()
        frame.url = "https://myclass.lpu.in/html5client/frame"
        frame_body = AsyncMock()
        frame_body.inner_text = AsyncMock(return_value="Active meeting UI")
        
        # Core containers are present
        app_mock = AsyncMock()
        app_mock.count = AsyncMock(return_value=1)
        
        def frame_locator(sel):
            if sel == "body":
                return frame_body
            if sel == "#app":
                return app_mock
            mock_el = AsyncMock()
            mock_el.count = AsyncMock(return_value=0)
            return mock_el
            
        frame.locator = MagicMock(side_effect=frame_locator)
        page.frames = [frame]
        
        is_completed = await check_lecture_completion(page)
        self.assertFalse(is_completed)

if __name__ == '__main__':
    unittest.main()
