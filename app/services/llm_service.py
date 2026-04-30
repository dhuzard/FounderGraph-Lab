"""Ollama-backed JSON-only LLM service."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import requests


class LLMServiceError(RuntimeError):
    """Base error for LLM service failures."""


class LLMInvalidJSONError(LLMServiceError):
    """Raised when the model returns non-JSON or schema-invalid content."""


@dataclass(frozen=True)
class OllamaLLMService:
    """Small Ollama client that only accepts strict JSON responses."""

    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def resolved_base_url(self) -> str:
        return (self.base_url or os.getenv("OLLAMA_URL") or "http://localhost:11434").rstrip("/")

    @property
    def resolved_model(self) -> str:
        model = self.model or os.getenv("LLM_MODEL")
        if not model:
            raise LLMServiceError("LLM_MODEL is required for Ollama extraction")
        return model

    def generate_text(self, prompt: str) -> str:
        """Call Ollama /api/generate and return the raw response field."""

        payload = {
            "model": self.resolved_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        try:
            response = requests.post(
                f"{self.resolved_base_url}/api/generate",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LLMServiceError(f"Ollama request failed: {exc}") from exc

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise LLMInvalidJSONError("Ollama returned a non-JSON HTTP body") from exc

        text = body.get("response")
        if not isinstance(text, str):
            raise LLMInvalidJSONError("Ollama response did not include a string 'response' field")
        return text.strip()

    def generate_json(self, prompt: str) -> Any:
        """Call Ollama and parse the model response as strict JSON."""

        text = self.generate_text(prompt)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMInvalidJSONError("Model response was not strict JSON") from exc

