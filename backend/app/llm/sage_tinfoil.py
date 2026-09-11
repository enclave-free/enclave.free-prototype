"""
OpenAI-compatible provider for Sage/Tinfoil.

The current prototype routes public AI traffic through Sage and Tinfoil. The
Python control plane still uses this provider for legacy utility paths and
health checks. Generic LLM_* keys are canonical for Python-side deployment
metadata and diagnostics.
"""

import logging
import threading
from typing import Optional

import httpx
from openai import OpenAI

from .provider import LLMProvider, LLMResponse

logger = logging.getLogger("enclave.llm.sage_tinfoil")


class SageTinfoilProvider(LLMProvider):
    """Generic OpenAI-compatible endpoint used by Python diagnostics."""

    def __init__(self, provider_name: str = "sage") -> None:
        if provider_name != "sage":
            raise ValueError('SageTinfoilProvider only supports provider_name="sage"')
        self._lock = threading.RLock()
        self.provider_name = provider_name

        from config_loader import get_config

        self.base_url = (
            get_config("LLM_API_URL")
            or "http://tinfoil-proxy:8089/v1"
        )
        self.api_key = get_config("LLM_API_KEY") or ""
        self.default_model = get_config("LLM_MODEL") or "glm-5-3-flash"

        self._init_client()

    def _init_client(self) -> None:
        """Initialize or reinitialize the OpenAI client."""
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key or "not-required",
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
        )

    def _refresh_config(self) -> None:
        """Refresh config from config_loader if available."""
        with self._lock:
            try:
                from config_loader import get_config

                new_base_url = get_config("LLM_API_URL") or self.base_url
                new_api_key = get_config("LLM_API_KEY") or self.api_key
                new_model = get_config("LLM_MODEL") or self.default_model

                if new_base_url != self.base_url or new_api_key != self.api_key:
                    self.base_url = new_base_url
                    self.api_key = new_api_key
                    self._init_client()

                self.default_model = new_model
            except Exception as exc:
                if not isinstance(exc, ImportError):
                    logger.warning("Config refresh failed, using cached values: %s", exc)

    @property
    def name(self) -> str:
        return self.provider_name

    def health_check(self) -> bool:
        """Check an OpenAI-compatible runtime via /health or /v1/models."""
        self._refresh_config()
        base_url = self.base_url
        api_key = self.api_key
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        base = base_url.rstrip("/")
        health_base = base[:-3] if base.endswith("/v1") else base

        try:
            health_resp = httpx.get(f"{health_base}/health", headers=headers, timeout=5.0)
            if health_resp.status_code == 200:
                return True
        except Exception as exc:
            logger.debug("Sage/Tinfoil health probe failed for %s: %s", health_base, exc)

        try:
            models_resp = httpx.get(f"{base}/models", headers=headers, timeout=5.0)
            return models_resp.status_code == 200
        except Exception as exc:
            logger.error("Sage/Tinfoil models probe failed for %s/models: %s", base, exc, exc_info=True)
            return False

    def complete(
        self,
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
    ) -> LLMResponse:
        """Generate completion using a streaming OpenAI-compatible endpoint."""
        self._refresh_config()

        with self._lock:
            client = self.client
            model = model or self.default_model

        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            stream_options={"include_usage": True},
            temperature=temperature,
        )

        content_parts = []
        usage = None
        for chunk in stream:
            if not chunk.choices and getattr(chunk, "usage", None) is not None:
                usage_obj = chunk.usage
                usage = usage_obj.model_dump() if hasattr(usage_obj, "model_dump") else getattr(usage_obj, "__dict__", usage_obj)
            if chunk.choices and chunk.choices[0].delta.content:
                content_parts.append(chunk.choices[0].delta.content)

        return LLMResponse(
            content="".join(content_parts),
            model=model,
            provider=self.name,
            usage=usage,
        )
