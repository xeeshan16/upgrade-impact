import httpx as h

from app.http import plain


def test_transport() -> None:
    transport = h.MockTransport(lambda request: h.Response(200))
    client = h.Client(transport=transport)
    assert client is not None
    assert plain is not None
