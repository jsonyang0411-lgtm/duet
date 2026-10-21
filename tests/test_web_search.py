"""Tests for web search trigger detection and prompt formatting."""

import unittest
from unittest.mock import patch

from src.web_search import WebSearchClient, WebSearchResult


class WebSearchClientTests(unittest.TestCase):
    def test_extract_query_from_explicit_search_request(self):
        client = WebSearchClient(enabled=True)

        query = client.extract_query("林小满，帮我查一下今天鸣潮最新活动", bot_name="林小满")

        self.assertEqual(query, "今天鸣潮最新活动")

    def test_extract_query_from_current_info_request(self):
        client = WebSearchClient(enabled=True)

        query = client.extract_query("DeepSeek 最新模型是什么")

        self.assertEqual(query, "DeepSeek 最新模型")

    def test_casual_now_chat_does_not_trigger_search(self):
        client = WebSearchClient(enabled=True)

        self.assertEqual(client.extract_query("你现在在干嘛"), "")
        self.assertEqual(client.extract_query("今天心情怎么样"), "")

    def test_negative_search_instruction_disables_search(self):
        client = WebSearchClient(enabled=True)

        self.assertEqual(client.extract_query("不用联网，聊聊今天心情"), "")

    def test_format_for_prompt_includes_sources(self):
        client = WebSearchClient(enabled=True, max_results=2)
        results = [
            WebSearchResult(
                title="测试标题",
                url="https://example.com/news",
                snippet="这是摘要",
            )
        ]

        prompt = client.format_for_prompt("测试", results)

        self.assertIn("联网搜索请求：测试", prompt)
        self.assertIn("测试标题", prompt)
        self.assertIn("example.com", prompt)

    def test_weather_query_without_location_asks_for_city(self):
        client = WebSearchClient(enabled=True)

        results = client.search("今日天气")

        self.assertEqual(len(results), 1)
        self.assertIn("需要城市", results[0].title)

    def test_weather_query_uses_open_meteo_lookup(self):
        client = WebSearchClient(enabled=True)
        responses = [
            {
                "results": [
                    {
                        "name": "东莞",
                        "country": "中国",
                        "admin1": "广东",
                        "latitude": 23.02,
                        "longitude": 113.75,
                    }
                ]
            },
            {
                "current": {
                    "temperature_2m": 30,
                    "apparent_temperature": 34,
                    "relative_humidity_2m": 70,
                    "weather_code": 61,
                    "wind_speed_10m": 12,
                },
                "daily": {
                    "weather_code": [63],
                    "temperature_2m_max": [32],
                    "temperature_2m_min": [27],
                    "precipitation_probability_max": [80],
                },
            },
        ]

        with patch.object(client, "_get_json", side_effect=responses):
            results = client.search("东莞今日天气")

        self.assertEqual(len(results), 1)
        self.assertIn("东莞今日天气", results[0].title)
        self.assertIn("中雨", results[0].snippet)
        self.assertIn("27~32°C", results[0].snippet)
        self.assertIn("降雨概率80%", results[0].snippet)

    def test_tomorrow_weather_query_extracts_city_and_day(self):
        client = WebSearchClient(enabled=True)
        responses = [
            {
                "results": [
                    {
                        "name": "东莞",
                        "country": "中国",
                        "admin1": "广东",
                        "latitude": 23.02,
                        "longitude": 113.75,
                    }
                ]
            },
            {
                "current": {
                    "temperature_2m": 30,
                    "apparent_temperature": 34,
                    "relative_humidity_2m": 70,
                    "weather_code": 61,
                    "wind_speed_10m": 12,
                },
                "daily": {
                    "weather_code": [61, 2],
                    "temperature_2m_max": [32, 35],
                    "temperature_2m_min": [27, 28],
                    "precipitation_probability_max": [80, 20],
                },
            },
        ]

        with patch.object(client, "_get_json", side_effect=responses) as get_json:
            results = client.search("明天东莞的天气")

        self.assertIn("name=%E4%B8%9C%E8%8E%9E", get_json.call_args_list[0].args[0])
        self.assertIn("forecast_days=2", get_json.call_args_list[1].args[0])
        self.assertEqual(len(results), 1)
        self.assertIn("东莞明天天气", results[0].title)
        self.assertIn("局部多云", results[0].snippet)
        self.assertIn("28~35°C", results[0].snippet)
        self.assertIn("降雨概率20%", results[0].snippet)


if __name__ == "__main__":
    unittest.main()
