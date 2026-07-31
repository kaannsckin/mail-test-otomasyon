"""
test_analyzer_errors.py — LLM sağlayıcı hata yolları.

Bu dalların hepsi kullanıcıya gösterilen mesaj üretir (ör. "API key geçersiz");
hata durumunda çalışmanın çökmemesi ve anlaşılır bir FAIL sonucu dönmesi test edilir.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer import DEFAULT_GEMINI_MODEL, DEFAULT_MODEL, MailAnalyzer


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=_request())


def _analyze_with(error: Exception, received_msg, combination_meta) -> dict:
    client = MagicMock()
    client.messages.create.side_effect = error
    with patch("analyzer.anthropic.Anthropic", return_value=client):
        analyzer = MailAnalyzer("sk-ant-test")
        return analyzer.analyze("plain_text", {"msg_id": "<a@b>"}, received_msg, combination_meta)


class TestClaudeApiErrors:

    def test_invalid_api_key_message(self, received_msg, combination_meta):
        err = anthropic.AuthenticationError(
            "invalid x-api-key", response=_response(401), body=None)
        result = _analyze_with(err, received_msg, combination_meta)
        assert result["passed"] is False
        assert result["confidence"] == "LOW"
        assert "API key geçersiz" in result["summary"]

    def test_rate_limit_message(self, received_msg, combination_meta):
        err = anthropic.RateLimitError(
            "rate limited", response=_response(429), body=None)
        result = _analyze_with(err, received_msg, combination_meta)
        assert result["passed"] is False
        assert "rate limit" in result["summary"].lower()

    def test_api_status_error_includes_status_code(self, received_msg, combination_meta):
        err = anthropic.APIStatusError(
            "server error", response=_response(529), body=None)
        result = _analyze_with(err, received_msg, combination_meta)
        assert result["passed"] is False
        assert "529" in result["summary"]

    def test_connection_error_message(self, received_msg, combination_meta):
        err = anthropic.APIConnectionError(request=_request())
        result = _analyze_with(err, received_msg, combination_meta)
        assert result["passed"] is False
        assert "bağlanılamadı" in result["summary"]

    def test_error_detail_kept_in_issues(self, received_msg, combination_meta):
        err = anthropic.AuthenticationError(
            "invalid x-api-key", response=_response(401), body=None)
        result = _analyze_with(err, received_msg, combination_meta)
        assert result["issues"] and isinstance(result["issues"][0], str)

    def test_refusal_stop_reason(self, received_msg, combination_meta):
        resp = MagicMock()
        resp.stop_reason = "refusal"
        client = MagicMock()
        client.messages.create.return_value = resp
        with patch("analyzer.anthropic.Anthropic", return_value=client):
            result = MailAnalyzer("sk-ant-test").analyze(
                "plain_text", {}, received_msg, combination_meta)
        assert result["passed"] is False
        assert "reddetti" in result["summary"]

    def test_sdk_configured_with_retries(self):
        with patch("analyzer.anthropic.Anthropic") as cls:
            MailAnalyzer("sk-ant-test")
        assert cls.call_args.kwargs["max_retries"] == 3
        assert cls.call_args.kwargs["timeout"] == 60.0


class TestGeminiApiErrors:

    def _gemini_result(self, side_effect, received_msg, combination_meta):
        analyzer = MailAnalyzer("gem-key", provider="gemini")
        with patch("analyzer.httpx.post", side_effect=side_effect):
            return analyzer.analyze("plain_text", {}, received_msg, combination_meta)

    def test_network_error_returns_fail(self, received_msg, combination_meta):
        result = self._gemini_result(
            httpx.ConnectError("bağlantı kurulamadı"), received_msg, combination_meta)
        assert result["passed"] is False
        assert "Gemini API erişim hatası" in result["summary"]

    def test_timeout_returns_fail(self, received_msg, combination_meta):
        result = self._gemini_result(
            httpx.ReadTimeout("zaman aşımı"), received_msg, combination_meta)
        assert result["passed"] is False

    def test_unexpected_payload_shape_handled(self, received_msg, combination_meta):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"beklenmeyen": "yapı"}
        analyzer = MailAnalyzer("gem-key", provider="gemini")
        with patch("analyzer.httpx.post", return_value=resp):
            result = analyzer.analyze("plain_text", {}, received_msg, combination_meta)
        assert result["passed"] is False
        assert result["confidence"] == "LOW"

    def test_empty_candidates_handled(self, received_msg, combination_meta):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"candidates": []}
        analyzer = MailAnalyzer("gem-key", provider="gemini")
        with patch("analyzer.httpx.post", return_value=resp):
            result = analyzer.analyze("plain_text", {}, received_msg, combination_meta)
        assert result["passed"] is False

    def test_api_key_sent_in_header_not_url(self, received_msg, combination_meta):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"candidates": [
            {"content": {"parts": [{"text": json.dumps({"passed": True, "confidence": "HIGH"})}]}}]}
        analyzer = MailAnalyzer("gizli-anahtar", provider="gemini")
        with patch("analyzer.httpx.post", return_value=resp) as post:
            analyzer.analyze("plain_text", {}, received_msg, combination_meta)
        url = post.call_args[0][0]
        assert "gizli-anahtar" not in url          # anahtar loglara/URL'e sızmamalı
        assert post.call_args.kwargs["headers"]["x-goog-api-key"] == "gizli-anahtar"


class TestProviderDefaults:

    def test_claude_default_model(self):
        with patch("analyzer.anthropic.Anthropic"):
            assert MailAnalyzer("k").model == DEFAULT_MODEL

    def test_gemini_default_model(self):
        assert MailAnalyzer("k", provider="gemini").model == DEFAULT_GEMINI_MODEL

    def test_gemini_has_no_anthropic_client(self):
        assert MailAnalyzer("k", provider="gemini").client is None

    @pytest.mark.parametrize("provider", ["CLAUDE", " claude ", "Claude"])
    def test_provider_normalized(self, provider):
        with patch("analyzer.anthropic.Anthropic"):
            assert MailAnalyzer("k", provider=provider).provider == "claude"

    @pytest.mark.parametrize("provider", ["openai", "llama", "gpt"])
    def test_unsupported_provider_raises(self, provider):
        with pytest.raises(ValueError, match="Bilinmeyen provider"):
            MailAnalyzer("k", provider=provider)

    def test_empty_provider_defaults_to_claude(self):
        with patch("analyzer.anthropic.Anthropic"):
            assert MailAnalyzer("k", provider="").provider == "claude"
