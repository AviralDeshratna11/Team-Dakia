"""Keep the automated suite independent of developer credentials and provider networks."""
import httpx
import pytest


@pytest.fixture(autouse=True)
def offline_providers(monkeypatch):
    for name in ("CRUSTDATA_API_KEY", "GEMINI_API_KEY", "GEMINI_BACKEND", "GEMINI_MODEL",
                 "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("Offline test attempted a real HTTP request. Use an in-process client or mock transport.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected_request)
