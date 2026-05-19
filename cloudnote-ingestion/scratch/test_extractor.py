import unittest
import os
from unittest.mock import AsyncMock, MagicMock
from app.extractor import LectureExtractor
from app.config import settings

class TestLectureExtractor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Use temporary files for test safety
        self.original_raw_file = settings.RAW_LECTURE_FILE
        self.original_max_lines = settings.MAX_BUFFER_LINES
        
        settings.RAW_LECTURE_FILE = "logs/test_raw_lecture.txt"
        settings.MAX_BUFFER_LINES = 5
        
        # Ensure log dir exists
        os.makedirs("logs", exist_ok=True)
        if os.path.exists(settings.RAW_LECTURE_FILE):
            os.remove(settings.RAW_LECTURE_FILE)
            
    def tearDown(self):
        # Restore settings
        settings.RAW_LECTURE_FILE = self.original_raw_file
        settings.MAX_BUFFER_LINES = self.original_max_lines
        if os.path.exists("logs/test_raw_lecture.txt"):
            os.remove("logs/test_raw_lecture.txt")
        if os.path.exists("logs/extraction_diagnostics.json"):
            os.remove("logs/extraction_diagnostics.json")

    async def test_extraction_and_deduplication(self):
        extractor = LectureExtractor()
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        
        # Mock element containing text
        el1 = AsyncMock()
        el1.is_visible = AsyncMock(return_value=True)
        el1.inner_text = AsyncMock(return_value="Introduction to Kubernetes architecture.")
        
        # Mock element containing duplicate text
        el2 = AsyncMock()
        el2.is_visible = AsyncMock(return_value=True)
        el2.inner_text = AsyncMock(return_value="Introduction to Kubernetes architecture.")
        
        # Mock element with very short/trivial text
        el3 = AsyncMock()
        el3.is_visible = AsyncMock(return_value=True)
        el3.inner_text = AsyncMock(return_value="hi")
        
        frame = AsyncMock()
        frame.url = "http://test-meeting"
        frame.name = "test-frame"
        frame.evaluate = AsyncMock(return_value="Test Meeting Room")
        
        # Mock body locator
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="Introduction to Kubernetes architecture.")
        
        def locator_mock(sel):
            if sel == "body":
                return body_mock
            return AsyncMock()
            
        frame.locator = MagicMock(side_effect=locator_mock)
        frame.query_selector_all = AsyncMock(side_effect=lambda sel: [el1, el2, el3] if sel == "[data-test='chatMessageText']" else [])
        page.frames = [frame]
        
        new_captured = await extractor.extract_content(page)
        
        # Should only capture 1 unique line due to deduplication
        self.assertEqual(extractor.get_buffer_size(), 1)
        
        # Verify file persisted
        self.assertTrue(os.path.exists(settings.RAW_LECTURE_FILE))
        with open(settings.RAW_LECTURE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("Introduction to Kubernetes architecture.", lines[0])

    async def test_rolling_buffer_size_limit(self):
        extractor = LectureExtractor()
        
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        
        # Generate 10 different elements
        elements = []
        for i in range(10):
            el = AsyncMock()
            el.is_visible = AsyncMock(return_value=True)
            el.inner_text = AsyncMock(return_value=f"This is lecture sentence {i}")
            elements.append(el)
            
        frame = AsyncMock()
        frame.url = "http://test-meeting"
        frame.name = "test-frame"
        frame.evaluate = AsyncMock(return_value="Test Meeting Room")
        
        # Mock body locator with dummy non-empty content so we scan it
        body_mock = AsyncMock()
        body_mock.inner_text = AsyncMock(return_value="Non-empty frame body")
        frame.locator = MagicMock(side_effect=lambda sel: body_mock if sel == "body" else AsyncMock())
        
        frame.query_selector_all = AsyncMock(side_effect=lambda sel: elements if sel == "[data-test='chatMessageText']" else [])
        page.frames = [frame]
        
        new_captured = await extractor.extract_content(page)
        
        # Should capture 10 from specific elements and 1 from fallback body, total 11
        self.assertEqual(new_captured, 11)
        self.assertEqual(extractor.get_buffer_size(), 5)
        
        # Verify stored raw text only contains last 5 lines
        raw_text = extractor.get_raw_text()
        self.assertNotIn("sentence 0", raw_text)
        self.assertNotIn("sentence 5", raw_text)
        self.assertIn("sentence 6", raw_text)
        self.assertIn("sentence 9", raw_text)

if __name__ == '__main__':
    unittest.main()
