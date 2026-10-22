"""A tiny real MCP server for tests/test_mcp.py — two tools, stdio.

Run as a child process by the client under test, exactly the way a
user's configured server would be. Deliberately boring: `echo`
returns its text (so a test can prove the round trip carried the
arguments and the result), `fail` always errors (so a test can
prove an isError result is reported as one).
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back, marked, so the caller can verify it."""
    return f"echo:{text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def fail() -> str:
    """Always raise — the client must surface this as an error."""
    raise RuntimeError("fixture failure by design")


if __name__ == "__main__":
    mcp.run()
