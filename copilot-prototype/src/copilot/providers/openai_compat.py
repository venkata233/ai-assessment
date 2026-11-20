"""OpenAI-compatible chat-completions provider (OpenAI, Azure OpenAI gateways, GitHub Models, Ollama, vLLM, ...).

NOT verified against any real service in this project: there were no credentials, and tests only use a local stub server.
Safeguards that are tested: https is required except for loopback hosts, redirects are not followed (so the API key is never
re-sent elsewhere), responses are size-limited, and error messages never include the key or the response body.
"""

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from copilot.providers.base import CompletionRequest, CompletionResponse, ProviderError

MAX_RESPONSE_BYTES = 1_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key: str | None, timeout_seconds: float = 60.0) -> None:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname or ""
        if parts.scheme not in {"http", "https"} or not host or parts.username:
            raise ProviderError("base URL must be an http(s) URL without credentials")
        if parts.scheme == "http" and not _is_loopback(host):
            raise ProviderError("plain http is only allowed for loopback hosts; use https")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._host = host
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_seconds

    @property
    def is_external(self) -> bool:
        return not _is_loopback(self._host)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload = json.dumps(
            {
                "model": self._model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
            }
        ).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        http_request = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")  # noqa: S310
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(http_request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"provider returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("could not reach the provider") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("provider response too large")
        try:
            content = json.loads(raw)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("unexpected provider response format") from None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("provider returned no text")
        return CompletionResponse(content, self._model)
