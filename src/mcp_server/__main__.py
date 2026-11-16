"""Run the MCP server over stdio for one user: `python -m src.mcp_server`.

The token comes from the FRA_MCP_TOKEN environment variable (create one with
scripts/create_mcp_token.py). Logs go to stderr; stdout carries the protocol.
"""

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format=LOG_FORMAT)
    token = os.environ.get("FRA_MCP_TOKEN", "").strip()
    if not token:
        print("Set FRA_MCP_TOKEN to an MCP token (see scripts/create_mcp_token.py).", file=sys.stderr)
        return 2

    from src.mcp_server.auth import InvalidMcpToken, decode_mcp_token
    from src.mcp_server.server import mcp, set_stdio_caller

    try:
        caller, _ = decode_mcp_token(token)
    except InvalidMcpToken as error:
        print(str(error), file=sys.stderr)
        return 2
    set_stdio_caller(caller)
    mcp.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
