# mcp-exa
MCP server to give client the ability to connect to Exa

## Setup

```bash
cp .env.example .env   # then fill in EXA_API_KEY
uv sync
```

The server starts without a key; tools return a setup error until `EXA_API_KEY` is set.

Register it in an MCP client (e.g. Claude Desktop):

```json
{
  "mcpServers": {
    "exa": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-exa", "run", "main.py"]
    }
  }
}
```

## Tools

- `exa_search`: web search with highlights or full text; filters for category
  (`company`, `news`, `publication`, `people`, ...), domains, and published-date range
- `exa_get_contents`: read known URLs, optionally only the passages relevant to a query
- `exa_deep_search`: multi-step search that synthesizes a cited answer, as text or
  shaped by a JSON Schema (`output_schema`)
- `exa_research_start` / `exa_research_get`: Exa's research agent for lists and reports.
  Runs take minutes, so `start` returns a `run_id` and `get` waits up to 50 seconds per call

## Cost

The free tier gives $10 of credits per month. Every response includes `cost_dollars`.
Roughly: `instant` search $4/1k, `auto`/`fast` $7/1k (+$1/1k per result above 10),
deep search $12–15/1k, contents $1/1k pages; research runs cost the most. To keep one call
from draining the budget, the server caps results at 25, URLs at 10 per call, and research
effort at `medium`.
