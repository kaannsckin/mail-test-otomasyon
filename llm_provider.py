"""
llm_provider.py — LLM provider abstraction layer.
Claude API ve Google Generative AI arasında geçiş yapar.
"""

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """LLM provider'ların implement etmesi gereken base class."""

    @abstractmethod
    def analyze(self, prompt: str) -> str:
        """
        Prompt'u işle ve JSON yanıt döndür.
        Returns: JSON string
        """
        pass


class ClaudeProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        import requests

        self.api_key = api_key
        self.model = model
        self.requests = requests
        self.headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def analyze(self, prompt: str) -> str:
        """Claude API'yi çağır."""
        api_url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": self.model,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = self.requests.post(
                api_url, headers=self.headers, json=payload, timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]
        except self.requests.RequestException as e:
            logger.error(f"Claude API hatası: {e}")
            return json.dumps(
                {
                    "passed": False,
                    "confidence": "LOW",
                    "checks": [],
                    "summary": f"Claude API erişim hatası: {str(e)}",
                    "issues": [str(e)],
                    "recommendations": [],
                }
            )


class GoogleProvider(LLMProvider):
    """Google Generative AI provider (Gemini)."""

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        try:
            import google.generativeai as genai

            self.genai = genai
            self.api_key = api_key
            self.model = model
            genai.configure(api_key=api_key)
        except ImportError:
            logger.error(
                "google-generativeai paketi yüklü değil. "
                "pip install google-generativeai çalıştırın."
            )
            raise

    def analyze(self, prompt: str) -> str:
        """Google Generative AI'ı çağır."""
        try:
            model = self.genai.GenerativeModel(self.model)
            response = model.generate_content(
                prompt,
                generation_config=self.genai.types.GenerationConfig(
                    max_output_tokens=1000,
                    temperature=0.3,
                ),
            )
            return response.text
        except Exception as e:
            logger.error(f"Google API hatası: {e}")
            return json.dumps(
                {
                    "passed": False,
                    "confidence": "LOW",
                    "checks": [],
                    "summary": f"Google API erişim hatası: {str(e)}",
                    "issues": [str(e)],
                    "recommendations": [],
                }
            )


def create_provider(provider_type: str, api_key: str, model: Optional[str] = None) -> LLMProvider:
    """
    Provider factory fonksiyonu.

    Args:
        provider_type: "claude" veya "google"
        api_key: API key
        model: Model ismi (opsiyonel)

    Returns:
        LLMProvider instance
    """
    provider_type = provider_type.lower().strip()

    if provider_type == "claude":
        model = model or "claude-sonnet-4-20250514"
        return ClaudeProvider(api_key, model)
    elif provider_type == "google":
        model = model or "gemini-2.0-flash"
        return GoogleProvider(api_key, model)
    else:
        raise ValueError(f"Bilinmeyen provider: {provider_type}. 'claude' veya 'google' kullanın.")
