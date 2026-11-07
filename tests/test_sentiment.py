import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sentiment_feed import (
    fetch_cointelegraph_headlines,
    fetch_fear_and_greed,
    get_market_sentiment,
)
from strategy import MacroState, build_system_prompt, fetch_ai_macro_state


class TestSentimentFeed(unittest.TestCase):

    def test_fetch_fear_and_greed_mock(self):
        sample_json = json.dumps({
            "data": [{
                "value": "28",
                "value_classification": "Fear",
                "timestamp": "1730000000"
            }]
        })
        with patch("sentiment_feed._fetch_url_text", return_value=sample_json):
            res = fetch_fear_and_greed()
            self.assertEqual(res["value"], 28)
            self.assertEqual(res["classification"], "Fear")

    def test_fetch_fear_and_greed_fallback(self):
        with patch("sentiment_feed._fetch_url_text", return_value=None):
            res = fetch_fear_and_greed()
            self.assertEqual(res["value"], 50)
            self.assertIn("Neutral", res["classification"])

    def test_fetch_cointelegraph_rss_mock(self):
        sample_xml = """<rss version="2.0">
            <channel>
                <title>Cointelegraph.com News</title>
                <item>
                    <title>Binance says 90% of stolen assets replenished</title>
                    <pubDate>Sun, 01 Nov 2026 05:00:00 +0000</pubDate>
                </item>
                <item>
                    <title>Bitcoin tests $70k resistance</title>
                    <pubDate>Sun, 01 Nov 2026 04:00:00 +0000</pubDate>
                </item>
            </channel>
        </rss>"""
        with patch("sentiment_feed._fetch_url_text", return_value=sample_xml):
            headlines = fetch_cointelegraph_headlines(limit=5)
            self.assertEqual(len(headlines), 2)
            self.assertIn("Binance says 90% of stolen assets replenished", headlines[0])
            self.assertIn("Bitcoin tests $70k resistance", headlines[1])

    def test_get_market_sentiment_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = "test_cache.json"
            cache_path = os.path.join(tmpdir, cache_file)

            # 预热缓存
            cached_data = {
                "status": "ok",
                "timestamp": 2000000000.0,
                "updated_at": "2033-01-01 00:00:00 UTC",
                "fear_and_greed": {"value": 75, "classification": "Greed"},
                "top_headlines_24h": ["Mock headline 1"],
            }
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cached_data, f)

            with patch("time.time", return_value=2000000100.0):
                res = get_market_sentiment(cache_file=cache_file, ttl_seconds=3600, base_dir=tmpdir)
                self.assertEqual(res["fear_and_greed"]["value"], 75)
                self.assertEqual(res["top_headlines_24h"], ["Mock headline 1"])

    def test_strategy_fetch_ai_macro_with_news_analysis(self):
        mock_client = MagicMock()
        mock_client.chat_json.return_value = {
            "suggested_state": "NEUTRAL",
            "news_impact_analysis": "Binance hack is currently causing elevated market fear, but technical support is holding.",
            "reason": "Quant TRS is positive but news panic justifies caution, setting to NEUTRAL."
        }

        macro_ctx = {
            "quant_state": "ONLY_LONG",
            "quant_trs": 0.45,
            "market_sentiment_and_news": {
                "fear_and_greed": {"value": 34, "classification": "Fear"},
                "top_headlines_24h": ["Binance hack under review"]
            }
        }

        # Test default return
        state = fetch_ai_macro_state(macro_ctx, client=mock_client, return_details=False)
        self.assertEqual(state, MacroState.NEUTRAL)

        # Test details return
        state2, details = fetch_ai_macro_state(macro_ctx, client=mock_client, return_details=True)
        self.assertEqual(state2, MacroState.NEUTRAL)
        self.assertIn("Binance hack", details["news_impact_analysis"])
        self.assertIn("Quant TRS", details["reason"])

    def test_build_system_prompt_macro(self):
        prompt = build_system_prompt("AI_macro")
        self.assertIn("AI_macro", prompt)
        self.assertTrue("market_sentiment_and_news" in prompt or "news_impact_analysis" in prompt or "ai_soul.md" in prompt)


if __name__ == "__main__":
    unittest.main()
