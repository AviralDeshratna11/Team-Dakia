"""Reusable transport policy; live endpoint integration remains intentionally disabled."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
import httpx
from pydantic import BaseModel


class ProviderFailure(RuntimeError):
    def __init__(self, code: str, attempts: int):
        super().__init__(code)  # Never include headers, tokens, query strings or raw response bodies.
        self.code, self.attempts = code, attempts


def retry_delay(headers, attempt, clock=time.time):
    retry = headers.get("retry-after")
    if retry:
        try: return max(0, float(retry))
        except ValueError:
            try: return max(0, parsedate_to_datetime(retry).timestamp() - clock())
            except (ValueError, TypeError): pass
    # Generic fallback; a live adapter must map its documented reset header explicitly.
    return min(8, 2 ** attempt) + random.uniform(0, .25)


class SafeTransport:
    def __init__(self, client: httpx.Client | None = None, attempts=3, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=10, follow_redirects=False)
        self.attempts, self.sleep = attempts, sleep
        self.cache = {}

    def fetch(self, url: str, schema: type[BaseModel], cache_key: str, ttl=60):
        cached = self.cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1].model_copy(deep=True)
        for attempt in range(self.attempts):
            delay = None
            try:
                response = self.client.get(url, timeout=10)
                if response.status_code == 429 or response.status_code >= 500:
                    delay = retry_delay(response.headers, attempt)
                elif response.status_code >= 400:
                    raise ProviderFailure("PROVIDER_ACCESS_ERROR", attempt + 1)
                else:
                    try: result = schema.model_validate(response.json())
                    except (ValueError, TypeError): raise ProviderFailure("PROVIDER_INVALID_RESPONSE", attempt + 1) from None
                    self.cache[cache_key] = (time.monotonic() + ttl, result)
                    return result.model_copy(deep=True)
            except httpx.TransportError:
                delay = retry_delay({}, attempt)
            if attempt + 1 < self.attempts:
                if delay > 30:
                    raise ProviderFailure("PROVIDER_RETRY_DEFERRED", attempt + 1)
                self.sleep(delay)
        raise ProviderFailure("PROVIDER_RETRIES_EXHAUSTED", self.attempts)
