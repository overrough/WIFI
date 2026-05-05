"""
Browser automation tools — Playwright-based.

Two levels of capability:
  1. extract_webpage(url)     — scrape and clean page content (no JS needed)
  2. run_browser_task(goal)   — full agentic browser control via Playwright

extract_webpage uses httpx for lightweight scraping (fast, no browser needed).
run_browser_task launches a real Chromium instance for JS-heavy sites and
multi-step interaction (filling forms, clicking, navigating, etc.)

Install: pip install playwright && python -m playwright install chromium
"""

import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_MAX_PAGE_CHARS = 12_000   # cap page content sent to LLM


def _clean_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace — no external dependency."""
    # Remove script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                          ("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(entity, char)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_browser_tools():

    @tool
    async def extract_webpage(url: str, question: str = "") -> str:
        """
        Fetch a webpage and extract its readable text content.
        Optionally ask a specific question about the page content.

        Use this to:
        - Read articles, documentation, or any public webpage
        - Extract data from websites
        - Answer questions about web content

        Examples:
          extract_webpage("https://docs.python.org/3/library/asyncio.html")
          extract_webpage("https://news.ycombinator.com", "What are the top stories?")
        """
        try:
            parsed = urlparse(url)
            if not parsed.scheme:
                url = "https://" + url

            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0; research bot)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text

            text = _clean_html(html)
            if len(text) > _MAX_PAGE_CHARS:
                text = text[:_MAX_PAGE_CHARS] + f"\n... [truncated — full page is {len(text)} chars]"

            if question:
                # Let the calling agent answer the question using this context
                return f"[Page: {url}]\n\nContent:\n{text}\n\n---\nYou asked: {question}\nAnswer from the content above."
            return f"[Page: {url}]\n\n{text}"

        except httpx.HTTPStatusError as exc:
            return f"HTTP {exc.response.status_code} fetching {url}"
        except httpx.RequestError as exc:
            return f"Request failed for {url}: {exc}"
        except Exception as exc:
            return f"Error fetching {url}: {exc}"

    @tool
    async def run_browser_task(
        goal: str,
        start_url: str = "",
        headless: bool = True,
    ) -> str:
        """
        Execute a multi-step browser automation task using Playwright.
        The agent will navigate, click, fill forms, and extract data to achieve the goal.

        Use for: logging into sites, filling forms, extracting dynamic content,
        automating repetitive web workflows.

        goal: describe what you want to achieve in plain English
        start_url: where to start (optional)
        headless: True = invisible browser, False = visible (for debugging)

        Examples:
          run_browser_task("Go to GitHub trending and list the top 5 Python repos")
          run_browser_task("Search for 'langchain tutorial' on YouTube and get the top 3 results")
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return (
                "Playwright not installed. Run:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            )

        steps_taken = []
        result_data = []

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=headless)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                )
                page = await context.new_page()

                if start_url:
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=15000)
                    steps_taken.append(f"Navigated to: {start_url}")

                # Extract page content as context for the LLM to reason about
                async def get_page_context() -> str:
                    try:
                        content = await page.inner_text("body")
                        url = page.url
                        title = await page.title()
                        return f"URL: {url}\nTitle: {title}\n\nContent (first 3000 chars):\n{content[:3000]}"
                    except Exception:
                        return f"URL: {page.url}"

                page_ctx = await get_page_context()
                steps_taken.append(f"Initial page: {page.url}")

                # Use the LLM to decide what to do next (simple agentic loop)
                from providers.factory import get_provider
                from providers.base import Message

                provider = get_provider()

                prompt = f"""You are controlling a web browser to accomplish this goal:
GOAL: {goal}

CURRENT PAGE:
{page_ctx}

You have these actions available (respond with EXACTLY ONE action per turn):
- NAVIGATE <url>         → go to a URL
- CLICK <css_selector>   → click an element
- FILL <css_selector> <text>  → fill an input field
- EXTRACT <description>  → extract text from page and return result
- SCROLL_DOWN            → scroll down the page
- DONE <result>          → goal accomplished, return this result

Think step by step. What is the single next action to take?"""

                for _ in range(10):   # max 10 browser actions per task
                    response = await provider.chat(
                        [Message(role="user", content=prompt)],
                        temperature=0.1,
                        max_tokens=256,
                    )
                    action_text = response.content.strip()
                    steps_taken.append(f"LLM action: {action_text}")

                    try:
                        if action_text.startswith("NAVIGATE "):
                            nav_url = action_text[9:].strip()
                            await page.goto(nav_url, wait_until="domcontentloaded", timeout=10000)
                            page_ctx = await get_page_context()

                        elif action_text.startswith("CLICK "):
                            selector = action_text[6:].strip()
                            await page.click(selector, timeout=5000)
                            await page.wait_for_load_state("domcontentloaded")
                            page_ctx = await get_page_context()

                        elif action_text.startswith("FILL "):
                            parts = action_text[5:].strip().split(" ", 1)
                            if len(parts) == 2:
                                selector, text = parts
                                await page.fill(selector, text, timeout=5000)

                        elif action_text.startswith("EXTRACT"):
                            content = await page.inner_text("body")
                            result_data.append(content[:4000])
                            page_ctx = await get_page_context()

                        elif action_text.startswith("SCROLL_DOWN"):
                            await page.evaluate("window.scrollBy(0, window.innerHeight)")
                            page_ctx = await get_page_context()

                        elif action_text.startswith("DONE "):
                            final_result = action_text[5:].strip()
                            await browser.close()
                            summary = f"Goal: {goal}\nResult: {final_result}"
                            if result_data:
                                summary += f"\n\nExtracted data:\n{result_data[0][:2000]}"
                            return summary

                    except Exception as step_exc:
                        steps_taken.append(f"Step error: {step_exc}")

                    # Update prompt with new page context
                    prompt = f"""Goal: {goal}
Current page: {page_ctx}
Steps taken so far: {len(steps_taken)}

What is the next single action? (NAVIGATE / CLICK / FILL / EXTRACT / SCROLL_DOWN / DONE)"""

                await browser.close()
                return (
                    f"Completed {len(steps_taken)} steps for goal: {goal}\n"
                    + ("\n\nExtracted:\n" + result_data[0][:3000] if result_data else "\nNo data extracted.")
                )

        except Exception as exc:
            logger.error("Browser task failed: %s", exc, exc_info=True)
            return f"Browser task failed: {exc}\nSteps taken: {steps_taken}"

    return [extract_webpage, run_browser_task]
