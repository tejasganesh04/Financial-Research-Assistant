"""Create an MCP token for a user and print how to connect an MCP client.

Signs in to the running API with the user's ordinary login, asks for a search-only MCP token
(it carries that user's role), and prints the Claude Code command. Standard library only, so it
runs with any Python 3.

    python3 scripts/create_mcp_token.py --username analyst --password analyst123
    python3 scripts/create_mcp_token.py --username admin --password admin123 --days 7 --token-only
"""

from __future__ import annotations

import argparse
import getpass
import json
import shlex
import sys
import urllib.error
import urllib.request


def post(url: str, body: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        sys.exit(f"{url} returned {error.code}: {detail}")
    except urllib.error.URLError as error:
        sys.exit(f"Cannot reach {url}: {error.reason}. Is the API running (make up)?")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000", help="API address (default: %(default)s)")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", help="Prompted for when omitted")
    parser.add_argument("--days", type=int, help="Token lifetime in days (server default when omitted)")
    parser.add_argument("--name", default="financial-research", help="Server name in Claude Code")
    parser.add_argument("--token-only", action="store_true", help="Print only the token")
    args = parser.parse_args()

    api = args.api.rstrip("/")
    password = args.password or getpass.getpass(f"Password for {args.username}: ")
    login = post(f"{api}/v1/auth/login", {"username": args.username, "password": password})
    issued = post(f"{api}/v1/auth/mcp-token", {"days": args.days} if args.days else {}, login["access_token"])
    if args.token_only:
        print(issued["access_token"])
        return

    header = f"Authorization: Bearer {issued['access_token']}"
    command = ["claude", "mcp", "add", "--transport", "http", args.name, issued["mcp_url"], "--header", header]
    print(f"MCP token for {args.username} (role: {issued['role']}, scope: {issued['scope']})")
    print(f"Expires: {issued['expires_at']}    Token id (for revocation): {issued['token_id']}")
    print("\nConnect Claude Code (run in the folder you will use Claude Code from):\n")
    print("  " + shlex.join(command))
    print("\nOther MCP clients: Streamable HTTP at", issued["mcp_url"], "with the header above.")
    print("Keep the token private: anyone holding it can search with this user's access until it expires.")


if __name__ == "__main__":
    main()
