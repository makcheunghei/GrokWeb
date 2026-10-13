"""CLI entrypoint; serving never prints diagnostics to MCP stdout."""

import argparse
import asyncio
import json
import os
import sys

from grokweb.client import GrokClient
from grokweb.errors import GrokError
from grokweb.server import create_server


async def doctor(client: GrokClient, search: bool = False) -> int:
    checks = []
    try:
        modes = await client.models()
        checks.append({"check": "chrome_login_and_modes", "ok": True, **modes})
    except GrokError as exc:
        checks.append(
            {"check": "chrome_login_and_modes", "ok": False, "code": exc.code, "message": str(exc)}
        )
    if search and checks[0]["ok"]:
        for kind, query in (
            ("web", "Find the official Python documentation homepage and cite it."),
            ("x", "Find a recent public post from @NASA using native X search, with a post URL."),
        ):
            try:
                result = await client.search(query, kind)
                checks.append(
                    {
                        "check": f"{kind}_search",
                        "ok": True,
                        "answer_characters": len(result["answer"]),
                        "source_count": len(result["sources"]),
                        "citation_count": len(result["citations"]),
                    }
                )
            except GrokError as exc:
                checks.append(
                    {"check": f"{kind}_search", "ok": False, "code": exc.code, "message": str(exc)}
                )
    print(json.dumps({"profile": client.profile, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if all(c["ok"] for c in checks) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Grok Web / X 搜尋 MCP（本機 Chrome 登入）")
    parser.add_argument("--profile", default=os.environ.get("GROKWEB_CHROME_PROFILE", "Default"))
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="啟動 stdio MCP（預設）")
    diagnostic = commands.add_parser("doctor", help="檢查登入及即時模式列表，唔輸出 cookie")
    diagnostic.add_argument("--search", action="store_true", help="另執行 Web／X 各一次真實搜尋")
    args = parser.parse_args()
    try:
        client = GrokClient(args.profile)
        if args.command == "doctor":
            raise SystemExit(asyncio.run(doctor(client, args.search)))
        create_server(client).run(transport="stdio")
    except GrokError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
