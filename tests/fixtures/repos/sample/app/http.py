"""Sample code that uses httpx in the ways real projects do."""

import httpx
from httpx import AsyncClient as AC
from httpx import HTTPStatusError


def make_client(proxy_url: str) -> httpx.Client:
    return httpx.Client(proxies=proxy_url, timeout=10)


async def fetch(url: str, **options: object) -> bytes:
    async with AC(**options) as client:
        response = await client.get(url)
    try:
        response.raise_for_status()
    except HTTPStatusError:
        return b""
    return response.content


def plain() -> httpx.Response:
    return httpx.get("https://example.org", timeout=httpx.Timeout(5.0))
