"""
Web search tool — DuckDuckGo (free, no key) → Tavily → Serper.
"""

import logging

import httpx
from langchain_core.tools import tool

from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def _duckduckgo_search(query: str, max_results: int = 5) -> list[dict]:
    """Completely free search via DuckDuckGo Instant Answer API + HTML scrape fallback."""
    results = []
    # 1) Try Instant Answer API (returns structured answers for many queries)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers={"User-Agent": "Jarvis-Assistant/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", "Answer"),
                "content": data["AbstractText"],
                "url": data.get("AbstractURL", ""),
            })
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic.get("Text", "")[:80],
                    "content": topic.get("Text", ""),
                    "url": topic.get("FirstURL", ""),
                })
    except Exception as exc:
        logger.debug("DDG instant answer failed: %s", exc)

    # 2) HTML search for richer results when instant API returns nothing
    if not results:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "content": r.get("body", ""),
                        "url": r.get("href", ""),
                    })
        except ImportError:
            logger.warning("duckduckgo-search not installed. Run: pip install duckduckgo-search")
        except Exception as exc:
            logger.warning("DDG text search failed: %s", exc)

    return results[:max_results]


async def _tavily_search(query: str, max_results: int = 5) -> list[dict]:
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": True,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    results = []
    if data.get("answer"):
        results.append({"title": "Direct Answer", "content": data["answer"], "url": ""})
    for r in data.get("results", [])[:max_results]:
        results.append({
            "title": r.get("title", ""),
            "content": r.get("content", "")[:500],
            "url": r.get("url", ""),
        })
    return results


async def _serper_search(query: str, max_results: int = 5) -> list[dict]:
    url = "https://google.serper.dev/search"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json={"q": query, "num": max_results},
            headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    if data.get("answerBox", {}).get("answer"):
        results.append({
            "title": "Answer Box",
            "content": data["answerBox"]["answer"],
            "url": "",
        })
    for r in data.get("organic", [])[:max_results]:
        results.append({
            "title": r.get("title", ""),
            "content": r.get("snippet", ""),
            "url": r.get("link", ""),
        })
    return results


def make_search_tools():
    """Return search tools (not user-scoped)."""

    @tool
    async def search_web(query: str) -> str:
        """
        Search the web for current information, news, or facts.
        Use this when you need information you don't have in memory or context.
        Returns a concise summary of the top results.
        DuckDuckGo is used by default (free). Add TAVILY_API_KEY for richer results.
        """
        try:
            if settings.tavily_api_key:
                results = await _tavily_search(query)
            elif settings.serper_api_key:
                results = await _serper_search(query)
            else:
                results = await _duckduckgo_search(query)
        except Exception as exc:
            logger.warning("Web search failed: %s", exc)
            return f"Search failed: {exc}"

        if not results:
            return "No results found."

        lines = []
        for r in results:
            url_part = f" ({r['url']})" if r.get("url") else ""
            lines.append(f"• {r['title']}{url_part}\n  {r['content']}")

        return "\n\n".join(lines)

    return [search_web]
