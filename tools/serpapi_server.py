"""MCP tool server for SerpApi: live web search, shopping prices and local places.

Results are trimmed to a few fields per item to keep model tokens (and cost) low.
The API key travels in the URL, so URLs are never logged or put in error messages.
"""
import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from common import serve

SERPAPI_URL = "https://serpapi.com/search.json"


async def serpapi(parameters):
    """Run one SerpApi search and return its JSON; errors never include the URL."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(SERPAPI_URL, params={**parameters, "api_key": os.environ["SERPAPI_API_KEY"]})
    except httpx.HTTPError as error:
        raise RuntimeError(f"SerpApi unreachable ({type(error).__name__})") from None
    body = response.json()
    if response.status_code >= 400 or "error" in body:
        raise RuntimeError(f"SerpApi error: {body.get('error', response.status_code)}")
    return body


def pick(items, keys, limit):
    """Keep only `keys` from the first `limit` items."""
    return [{key: item.get(key) for key in keys if item.get(key) is not None} for item in (items or [])[:limit]]


mcp = MCPServer("serpapi-search")


@mcp.tool(annotations=ToolAnnotations(title="Search the web", read_only_hint=True))
async def search_web(query: str) -> list[dict]:
    """Search the web (Google) and return the top results: title, link and snippet."""
    body = await serpapi({"engine": "google", "q": query, "num": 5})
    return pick(body.get("organic_results"), ("title", "link", "snippet"), 5)


@mcp.tool(annotations=ToolAnnotations(title="Compare shopping prices", read_only_hint=True))
async def shopping_prices(query: str) -> list[dict]:
    """Find current prices for a product across stores (Google Shopping): title, price, store."""
    body = await serpapi({"engine": "google_shopping", "q": query})
    return pick(body.get("shopping_results"), ("title", "price", "extracted_price", "source", "rating"), 6)


@mcp.tool(annotations=ToolAnnotations(title="Find local places", read_only_hint=True))
async def local_places(query: str, location: str) -> list[dict]:
    """Find places near a location (Google Maps), for example "coffee" near "Austin, TX": name, rating, address."""
    body = await serpapi({"engine": "google_maps", "q": f"{query} {location}", "type": "search"})
    return pick(body.get("local_results"), ("title", "rating", "reviews", "address", "price"), 6)


if __name__ == "__main__":
    serve(mcp)
