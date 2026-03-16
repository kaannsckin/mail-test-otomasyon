"""
test_llm_provider.py — LLM provider abstraction testleri.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from llm_provider import (
    LLMProvider,
    ClaudeProvider,
    create_provider,
)


# ── Factory testleri ───────────────────────────────────────────
class TestCreateProvider:
    """create_provider() factory fonksiyonu."""

    @patch("llm_provider.ClaudeProvider.__init__", return_value=None)
    def test_claude_provider_creation(self, mock_init):
        provider = create_provider("claude", "test-key")
        assert isinstance(provider, ClaudeProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Bilinmeyen provider"):
            create_provider("openai", "test-key")

    @patch("llm_provider.ClaudeProvider.__init__", return_value=None)
    def test_case_insensitive(self, mock_init):
        provider = create_provider("CLAUDE", "test-key")
        assert isinstance(provider, ClaudeProvider)

    @patch("llm_provider.ClaudeProvider.__init__", return_value=None)
    def test_strips_whitespace(self, mock_init):
        provider = create_provider("  claude  ", "test-key")
        assert isinstance(provider, ClaudeProvider)

    def test_google_provider_without_package_raises(self):
        """google-generativeai paketi yoksa ImportError."""
        with pytest.raises(ImportError):
            create_provider("google", "test-key")


# ── LLMProvider ABC ────────────────────────────────────────────
class TestLLMProviderABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            LLMProvider()

    def test_subclass_must_implement_analyze(self):
        class BadProvider(LLMProvider):
            pass

        with pytest.raises(TypeError):
            BadProvider()

    def test_valid_subclass(self):
        class GoodProvider(LLMProvider):
            def analyze(self, prompt: str) -> str:
                return '{"result": "ok"}'

        p = GoodProvider()
        assert p.analyze("test") == '{"result": "ok"}'


# ── ClaudeProvider yapısı (mocked requests) ────────────────────
class TestClaudeProvider:
    @patch.dict("sys.modules", {"requests": MagicMock()})
    def test_headers_contain_api_key(self):
        provider = ClaudeProvider("sk-test-123")
        assert provider.headers["x-api-key"] == "sk-test-123"

    @patch.dict("sys.modules", {"requests": MagicMock()})
    def test_headers_contain_version(self):
        provider = ClaudeProvider("sk-test-123")
        assert "anthropic-version" in provider.headers

    @patch.dict("sys.modules", {"requests": MagicMock()})
    def test_headers_content_type(self):
        provider = ClaudeProvider("sk-test-123")
        assert provider.headers["content-type"] == "application/json"

    @patch.dict("sys.modules", {"requests": MagicMock()})
    def test_default_model(self):
        provider = ClaudeProvider("sk-test-123")
        assert "claude" in provider.model

    @patch.dict("sys.modules", {"requests": MagicMock()})
    def test_custom_model(self):
        provider = ClaudeProvider("sk-test-123", model="claude-3-haiku")
        assert provider.model == "claude-3-haiku"
