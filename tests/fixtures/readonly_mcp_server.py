import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("read-only-test")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def lookup(topic: str) -> str:
    """Return a fixed read-only test result."""
    return f"result for {topic}"


@server.tool(description=f"test parent env passed: {'MCP_TEST_SECRET' in os.environ}", annotations=ToolAnnotations(readOnlyHint=True))
def hinted_but_untrusted() -> str:
    """A hint does not establish administrator trust."""
    return "not trusted"


if __name__ == "__main__":
    server.run(transport="stdio")
