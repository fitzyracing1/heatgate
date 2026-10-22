"""MCPB entry point: run heatgate's stdio MCP server from the bundled copy in server/lib."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from heatgate.mcp import serve  # noqa: E402
sys.exit(serve())
