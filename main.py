from mcp.server.fastmcp import FastMCP
from exa_py import AsyncExa
from dotenv import load_dotenv
from pathlib import Path
import dataclasses
import asyncio
import json
import os
import sys
import time

load_dotenv(Path(__file__).resolve().parent / ".env")

mcp = FastMCP("exa", dependencies=["exa-py", "python-dotenv"])

# Hard caps keep a single tool call from burning the monthly free credits
MAX_SEARCH_RESULTS = 25
MAX_CONTENT_URLS = 10
MAX_CHARACTERS = 20000
MAX_WAIT_SECONDS = 50

SEARCH_TYPES = ("auto", "fast", "instant", "neural")
DEEP_SEARCH_TYPES = ("deep-lite", "deep", "deep-reasoning")
CATEGORIES = ("company", "news", "publication", "personal site", "financial report", "people")
CONTENT_MODES = ("highlights", "text", "none")
RESEARCH_EFFORTS = ("minimal", "low", "medium", "auto")
FINISHED_STATUSES = ("completed", "failed", "cancelled")

_client: AsyncExa | None = None


def log(message: str) -> None:
    # stdout carries the MCP stdio protocol; diagnostics must go to stderr
    print(message, file=sys.stderr, flush=True)


def get_client() -> AsyncExa:
    """Create the Exa client on first use, so the server starts even without a key."""
    global _client
    if _client is None:
        api_key = os.environ.get("EXA_API_KEY")
        if not api_key:
            raise RuntimeError("EXA_API_KEY is not set. Add it to .env (see .env.example).")
        _client = AsyncExa(api_key)
    return _client


def to_jsonable(value):
    """Turn Exa's dataclasses and pydantic models into plain JSON-friendly data."""
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def error(message: str) -> str:
    return json.dumps({"error": message}, indent=2)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def format_result(index: int, result) -> dict:
    item = {
        "index": index + 1,
        "source": "exa",
        "title": result.title,
        "url": result.url,
        "published_date": result.published_date,
        "author": result.author,
        "highlights": result.highlights,
        "summary": result.summary,
        "text": result.text,
    }
    return {k: v for k, v in item.items() if v}


def format_response(response, extra: dict | None = None) -> str:
    payload = dict(extra or {})
    payload["results"] = [format_result(i, r) for i, r in enumerate(response.results)]
    if response.cost_dollars:
        payload["cost_dollars"] = to_jsonable(response.cost_dollars)
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_run(run) -> str:
    payload = {
        "run_id": run.id,
        "status": run.status,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }
    if run.output:
        payload["output"] = to_jsonable(run.output)
    if run.error:
        payload["error"] = to_jsonable(run.error)
    if run.cost_dollars:
        payload["cost_dollars"] = to_jsonable(run.cost_dollars)
    if run.status not in FINISHED_STATUSES:
        payload["hint"] = "Still running. Call exa_research_get again with this run_id."
    return json.dumps({k: v for k, v in payload.items() if v is not None}, indent=2, ensure_ascii=False)


@mcp.tool()
async def exa_search(
    query: str,
    num_results: int = 10,
    type: str = "auto",
    category: str = None,
    include_domains: list[str] = None,
    exclude_domains: list[str] = None,
    start_published_date: str = None,
    end_published_date: str = None,
    content: str = "highlights",
    max_characters: int = 3000,
) -> str:
    """
    Search the web with Exa and return ranked results with page content.

    Args:
        query: Natural-language search query (e.g., "AI consultant companies in Indonesia")
        num_results: Number of results (1-25, default: 10; each result above 10 costs extra)
        type: "auto" (default), "fast", "instant" (cheapest) or "neural"
        category: Optional focus: "company", "news", "publication", "personal site",
            "financial report" or "people"
        include_domains: Only return results from these domains (e.g., ["arxiv.org"])
        exclude_domains: Never return results from these domains
        start_published_date: Only results published on/after this date (YYYY-MM-DD)
        end_published_date: Only results published on/before this date (YYYY-MM-DD)
        content: "highlights" (default, relevant snippets), "text" (full page text) or "none"
        max_characters: Max characters of page text per result when content="text" (default: 3000)
    """
    if type not in SEARCH_TYPES:
        return error(f"type must be one of {SEARCH_TYPES}; use exa_deep_search for deep searches.")
    if category and category not in CATEGORIES:
        return error(f"category must be one of {CATEGORIES}.")
    if content not in CONTENT_MODES:
        return error(f"content must be one of {CONTENT_MODES}.")

    contents = {
        "highlights": {"highlights": True},
        "text": {"text": {"max_characters": clamp(max_characters, 100, MAX_CHARACTERS)}},
        "none": False,
    }[content]

    try:
        log(f"Exa search ({type}): {query}")
        response = await get_client().search(
            query,
            num_results=clamp(num_results, 1, MAX_SEARCH_RESULTS),
            type=type,
            category=category,
            include_domains=include_domains or None,
            exclude_domains=exclude_domains or None,
            start_published_date=start_published_date,
            end_published_date=end_published_date,
            contents=contents,
        )
        return format_response(response, {"query": query, "search_type": response.resolved_search_type or type})
    except Exception as e:
        return error(f"Error occurred: {e}")


@mcp.tool()
async def exa_get_contents(
    urls: list[str], highlight_query: str = None, max_characters: int = 5000
) -> str:
    """
    Read the content of known web pages through Exa (handles JS-heavy and blocked sites).

    Args:
        urls: Page URLs to read (max 10)
        highlight_query: Optional; return only the passages relevant to this query
            instead of the full text (e.g., "IEEE papers")
        max_characters: Max characters of text per page when no highlight_query (default: 5000)
    """
    if not urls:
        return error("urls must contain at least one URL.")
    if len(urls) > MAX_CONTENT_URLS:
        return error(f"At most {MAX_CONTENT_URLS} URLs per call.")

    if highlight_query:
        options = {"highlights": {"query": highlight_query}}
    else:
        options = {"text": {"max_characters": clamp(max_characters, 100, MAX_CHARACTERS)}}

    try:
        log(f"Exa contents for {len(urls)} URL(s)")
        response = await get_client().get_contents(urls, **options)
        payload = {"urls": urls}
        failed = [s for s in to_jsonable(response.statuses) or [] if s.get("status") != "success"]
        if failed:
            payload["failed"] = failed
        return format_response(response, payload)
    except Exception as e:
        return error(f"Error occurred: {e}")


@mcp.tool()
async def exa_deep_search(
    query: str, output_schema: dict = None, type: str = "deep", num_results: int = 10
) -> str:
    """
    Multi-step Exa search that synthesizes an answer with citations. Costs ~2x a normal search.

    Args:
        query: The question to answer (e.g., "Who is Braincore Indonesia's CEO and where is it based?")
        output_schema: Optional JSON Schema for a structured answer, e.g.
            {"type": "object", "required": ["ceo"], "properties": {"ceo": {"type": "string"}}}.
            Without it the answer is plain text.
        type: "deep-lite" (cheapest), "deep" (default) or "deep-reasoning" (most thorough)
        num_results: Number of source results to return alongside the answer (1-25, default: 10)
    """
    if type not in DEEP_SEARCH_TYPES:
        return error(f"type must be one of {DEEP_SEARCH_TYPES}.")

    try:
        log(f"Exa deep search ({type}): {query}")
        response = await get_client().search(
            query,
            type=type,
            num_results=clamp(num_results, 1, MAX_SEARCH_RESULTS),
            output_schema=output_schema or {"type": "text"},
            contents={"highlights": True},
        )
        return format_response(response, {"query": query, "output": to_jsonable(response.output)})
    except Exception as e:
        return error(f"Error occurred: {e}")


@mcp.tool()
async def exa_research_start(
    query: str, output_schema: dict = None, effort: str = "low"
) -> str:
    """
    Start an Exa research agent for open-ended tasks (building lists, analyzing a topic
    across many sources). Runs take minutes, so this returns a run_id right away;
    fetch the result with exa_research_get. This is the most expensive Exa tool.

    Args:
        query: The research task (e.g., "Find 10 AI consultant companies in Indonesia")
        output_schema: Optional JSON Schema for a structured result; without it the result is a report
        effort: "minimal", "low" (default), "medium" or "auto"
    """
    if effort not in RESEARCH_EFFORTS:
        return error(f"effort must be one of {RESEARCH_EFFORTS}.")

    try:
        log(f"Exa research run ({effort}): {query}")
        run = await get_client().agent.runs.create(
            query=query, output_schema=output_schema, effort=effort
        )
        return format_run(run)
    except Exception as e:
        return error(f"Error occurred: {e}")


@mcp.tool()
async def exa_research_get(run_id: str, wait_seconds: int = 30) -> str:
    """
    Get the status and result of a research run started with exa_research_start.

    Args:
        run_id: The run_id returned by exa_research_start
        wait_seconds: Wait up to this long for the run to finish before returning (0-50, default: 30)
    """
    deadline = time.monotonic() + clamp(wait_seconds, 0, MAX_WAIT_SECONDS)
    try:
        while True:
            run = await get_client().agent.runs.get(run_id)
            if run.status in FINISHED_STATUSES or time.monotonic() >= deadline:
                return format_run(run)
            await asyncio.sleep(3)
    except Exception as e:
        return error(f"Error occurred: {e}")


if __name__ == "__main__":
    mcp.run()
