import httpx
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never hit the real API: any request through a real transport fails.

    httpx.MockTransport is unaffected, so API tests keep working.
    """

    def blocked(self, request):
        raise RuntimeError(f"network disabled in tests: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
