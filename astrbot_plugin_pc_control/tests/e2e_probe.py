# -*- coding: utf-8 -*-
"""诊断用：发一条消息并打印全部 SSE 事件类型 + 会话消息原文。

用法：
    <venv>\\Scripts\python.exe tests\e2e_webchat.py --keep-session
"""

from __future__ import annotations

import json

import e2e_webchat as base
import requests

MESSAGE = "截个屏发给我"


def main() -> int:
    cfg = base.load_config()
    headers = {"Authorization": f"Bearer {base.make_token(cfg)}"}

    r = requests.get(
        f"{base.LEGACY_API}/chat/new_session",
        params={"platform_id": "webchat"},
        headers=headers,
        timeout=60,
    )
    session_id = (r.json().get("data") or {}).get("session_id")
    print("session_id =", session_id)

    events = []
    with requests.post(
        f"{base.LEGACY_API}/chat/send",
        headers={**headers, "Accept": "text/event-stream"},
        json={"message": MESSAGE, "session_id": session_id, "enable_streaming": True},
        stream=True,
        timeout=240,
    ) as resp:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                print("[raw]", line[:200])
                continue
            data = line[5:].strip()
            if data in ("[DONE]", ""):
                print("[DONE]")
                continue
            try:
                event = json.loads(data)
            except Exception:
                print("[data]", data[:300])
                continue
            events.append(event)

    print("== 事件类型统计 ==")
    counts: dict[str, int] = {}
    for e in events:
        counts[e.get("type", "?")] = counts.get(e.get("type", "?"), 0) + 1
    print(counts)

    print("== 非 plain 事件 ==")
    for e in events:
        if e.get("type") != "plain":
            print(json.dumps(e, ensure_ascii=False)[:600])

    print("== 文本合并 ==")
    print("".join(e.get("data", "") for e in events if e.get("type") == "plain"))

    print("== 会话消息原文 ==")
    r = requests.get(
        f"{base.BASE_API}/chat/sessions/{session_id}", headers=headers, timeout=60
    )
    print("GET 状态:", r.status_code)
    print(r.text[:3000])

    print("SESSION_ID_FOR_CLEANUP=", session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
