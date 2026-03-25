#!/usr/bin/env python3
"""CLI bridge — interactive stdin/stdout chat via daemon HTTP API.

Streams responses via SSE for progressive output.

Run:  python3 channels/cli.py
Env:  LUCYD_URL  (default: http://127.0.0.1:8100)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

URL = os.environ.get("LUCYD_URL", "http://127.0.0.1:8100")


async def main():
    async with httpx.AsyncClient(timeout=300) as client:
        while True:
            try:
                text = await asyncio.get_event_loop().run_in_executor(None, input, "You> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            text = text.strip()
            if not text:
                continue

            body = {"message": text, "sender": "cli"}

            try:
                # Stream response via SSE
                async with client.stream(
                    "POST", f"{URL}/api/v1/chat/stream", json=body,
                ) as resp:
                    sys.stdout.write("Agent> ")
                    sys.stdout.flush()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if event.get("text"):
                            sys.stdout.write(event["text"])
                            sys.stdout.flush()
                        if event.get("done"):
                            break
                    print()
            except httpx.ConnectError:
                print("Error: cannot connect to daemon. Is it running?", file=sys.stderr)
            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
