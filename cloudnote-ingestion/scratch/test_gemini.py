import unittest
import os
import json
from unittest.mock import MagicMock, patch
from app.gemini_service import summarize_lecture, generate_fallback_summary
from app.config import settings

class TestGeminiService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_key = settings.GEMINI_API_KEY
        self.original_summary_file = settings.AI_SUMMARY_FILE
        
        settings.AI_SUMMARY_FILE = "logs/test_ai_summary.json"
        
        # Ensure log dir exists
        os.makedirs("logs", exist_ok=True)
        if os.path.exists(settings.AI_SUMMARY_FILE):
            os.remove(settings.AI_SUMMARY_FILE)
            
    def tearDown(self):
        settings.GEMINI_API_KEY = self.original_key
        settings.AI_SUMMARY_FILE = self.original_summary_file
        
        if os.path.exists("logs/test_ai_summary.json"):
            os.remove("logs/test_ai_summary.json")
            
        # Clean up any leftover timestamped json files
        for f in os.listdir("logs"):
            if f.startswith("test_ai_summary_") or f.startswith("ai_summary_"):
                try:
                    os.remove(os.path.join("logs", f))
                except:
                    pass

    async def test_summarize_lecture_missing_key_fallback(self):
        """When the API key is not configured, it should return fallback summary without making requests."""
        settings.GEMINI_API_KEY = None
        
        res = summarize_lecture("Some lecture content")
        self.assertIsNotNone(res)
        self.assertIn("summary", res)
        self.assertIn("topics", res)
        self.assertIn("key_points", res)
        self.assertIn("API Key missing", res["summary"])
        
        # Verify saved files
        self.assertTrue(os.path.exists(settings.AI_SUMMARY_FILE))
        with open(settings.AI_SUMMARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertEqual(data["summary"], res["summary"])

    @patch('urllib.request.urlopen')
    async def test_summarize_lecture_api_success(self, mock_urlopen):
        """When the API responds successfully, it should parse and save the JSON response."""
        settings.GEMINI_API_KEY = "test-key-123"
        
        # Mock Response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "summary": "This is a clean AI summary.",
                            "topics": ["Cloud Computing", "Microservices"],
                            "key_points": ["Point A", "Point B"]
                        })
                    }]
                }
            }]
        }).encode("utf-8")
        
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        
        res = summarize_lecture("Highly educational lecture notes")
        
        self.assertEqual(res["summary"], "This is a clean AI summary.")
        self.assertEqual(len(res["topics"]), 2)
        self.assertEqual(res["key_points"][0], "Point A")
        
        # Verify persistence
        self.assertTrue(os.path.exists(settings.AI_SUMMARY_FILE))
        with open(settings.AI_SUMMARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertEqual(data["summary"], "This is a clean AI summary.")

    @patch('urllib.request.urlopen')
    async def test_summarize_lecture_api_failure_fallback(self, mock_urlopen):
        """When the API call raises a network error, it should fail gracefully and generate a fallback summary."""
        settings.GEMINI_API_KEY = "test-key-123"
        
        # Force Exception
        mock_urlopen.side_effect = Exception("Connection timed out!")
        
        res = summarize_lecture("Important lecture texts")
        
        # Ensure we got a fallback response instead of crash
        self.assertIsNotNone(res)
        self.assertIn("summary", res)
        self.assertIn("AI generation encountered", res["summary"])
        
        # Ensure it was persisted
        self.assertTrue(os.path.exists(settings.AI_SUMMARY_FILE))

if __name__ == '__main__':
    unittest.main()
