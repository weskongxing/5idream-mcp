#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地端到端协议冒烟测试 (stdio JSON-RPC 2.0)。"""

import json
import os
import subprocess
import sys

PY = sys.executable
SRV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


def send_rpc(proc: subprocess.Popen, payload: dict) -> dict | None:
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    if "id" not in payload:
        return None
    line = proc.stdout.readline()
    return json.loads(line)


def main() -> None:
    proc = subprocess.Popen(
        [PY, "-u", SRV],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        r_init = send_rpc(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
        })
        assert r_init["result"]["serverInfo"]["name"] == "5idream-mcp", r_init
        send_rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        r_tools = send_rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in r_tools["result"]["tools"]]
        print("Registered tools:", tool_names)
        assert len(tool_names) == 8

        r_st = send_rpc(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "dm_status", "arguments": {}},
        })
        print("dm_status:", r_st["result"]["content"][0]["text"])

        r_pub = send_rpc(proc, {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "dm_available_activities", "arguments": {"scope": "national", "page_size": 3}},
        })
        print("dm_available_activities(national):\n", r_pub["result"]["content"][0]["text"][:500])

        r_qr = send_rpc(proc, {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "dm_login_qrcode", "arguments": {"open_image": False}},
        })
        content_types = [c["type"] for c in r_qr["result"]["content"]]
        print("dm_login_qrcode content types:", content_types)
        assert content_types == ["image", "text"]

        r_chk = send_rpc(proc, {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "dm_check_login", "arguments": {"wait_seconds": 0}},
        })
        print("dm_check_login(0s):", r_chk["result"]["content"][0]["text"])
        print("ALL SMOKE TESTS PASSED!")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
