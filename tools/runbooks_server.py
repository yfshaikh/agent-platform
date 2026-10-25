"""MCP tool server for runbooks: the team's written how-tos for known incidents, as a read-only knowledge base.

Runbooks are Markdown files in runbooks/ (baked into the image). An NMC² team adds its own by adding a file.
ponytail: keyword search over a handful of files; a vector index when there are hundreds.
"""
import pathlib
import re

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from common import serve

RUNBOOKS = {path.stem: path.read_text() for path in sorted((pathlib.Path(__file__).parent / "runbooks").glob("*.md"))}

mcp = MCPServer("runbooks")


def title(text):
    """A runbook's first heading."""
    return text.splitlines()[0].lstrip("# ").strip()


def search(query):
    """Runbooks ranked by how many of the query's words they contain (best first, misses left out)."""
    words = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
    scored = [(sum(text.lower().count(word) for word in words), name) for name, text in RUNBOOKS.items()]
    return [name for score, name in sorted(scored, reverse=True) if score]


@mcp.tool(annotations=ToolAnnotations(title="List runbooks", read_only_hint=True))
async def list_runbooks() -> list[dict]:
    """Every runbook: its name (pass it to read_runbook) and title."""
    return [{"name": name, "title": title(text)} for name, text in RUNBOOKS.items()]


@mcp.tool(annotations=ToolAnnotations(title="Search runbooks", read_only_hint=True))
async def search_runbooks(query: str) -> list[dict]:
    """Find the runbooks that match an error or symptom, e.g. "CUDA out of memory" or "GPU hot". Best match first."""
    return [{"name": name, "title": title(RUNBOOKS[name])} for name in search(query)[:3]]


@mcp.tool(annotations=ToolAnnotations(title="Read a runbook", read_only_hint=True))
async def read_runbook(name: str) -> str:
    """The full text of one runbook: symptoms, how to diagnose, what to do now, how to prevent it."""
    if name not in RUNBOOKS:
        raise ValueError(f"no runbook {name!r}; choose from {sorted(RUNBOOKS)}")
    return RUNBOOKS[name]


if __name__ == "__main__":
    serve(mcp)
