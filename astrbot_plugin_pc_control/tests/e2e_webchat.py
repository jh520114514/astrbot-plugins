"""端到端 / 热加载测试脚本（独立于插件运行，不属于插件本体）。

用法：
    <venv>\\Scripts\python.exe tests\e2e_webchat.py [--keep-session]

流程：
 1. 用 dashboard.jwt_secret 自签 HS256 JWT
 2. POST /api/v1/plugins/astrbot_plugin_pc_control/reload  热加载插件
 3. GET  /api/v1/plugins                     确认插件已加载、拿到工具清单
 4. POST /api/chat/new_session?platform_id=webchat  建测试会话
 5. POST /api/chat/send  {"message": "截个屏发给我", ...} 读 SSE
 6. 拉取会话消息，确认有图片消息落库
 7. DELETE /api/v1/chat/sessions/{id} 清理测试会话
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.parse

import jwt
import requests

BASE_API = "http://127.0.0.1:62523/api/v1"
LEGACY_API = "http://127.0.0.1:62523/api"
CONFIG_PATH = (
    r"C:\Users\Administrator\.astrbot_launcher\instances"
    r"\f3436f89-9e75-4ef3-a1f9-6b6507c1994f\core\data\cmd_config.json"
)
PLUGIN_ID = "astrbot_plugin_pc_control"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8-sig") as fp:
        return json.load(fp)


def make_token(cfg: dict) -> str:
    dash = cfg["dashboard"]
    payload = {
        "username": dash.get("username", "astrbot"),
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=1),
    }
    return jwt.encode(payload, dash["jwt_secret"], algorithm="HS256")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-session", action="store_true", help="不清测试会话")
    parser.add_argument("--message", default="截个屏发给我")
    args = parser.parse_args()

    cfg = load_config()
    token = make_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}

    print("== 1. 热加载插件 ==")
    r = requests.post(
        f"{BASE_API}/plugins/{PLUGIN_ID}/reload", headers=headers, timeout=60
    )
    print(f"  POST {r.status_code} {r.text[:400]}")

    print("== 2. 插件状态 / 工具清单 ==")
    r = requests.get(f"{BASE_API}/plugins", headers=headers, timeout=60)
    tools = []
    if r.status_code == 200:
        body = r.json()
        data = body.get("data", body)
        items = data.get("plugins") if isinstance(data, dict) else data
        for item in items or []:
            name = item.get("name") or item.get("plugin_name")
            if name != PLUGIN_ID:
                continue
            print("  找到插件:", json.dumps(
                {k: item.get(k) for k in
                 ("name", "display_name", "version", "activated", "enabled", "path")},
                ensure_ascii=False))
            meta = item.get("metadata") or {}
            tools = item.get("tools") or meta.get("tools") or []
        print("  工具:", tools)
    else:
        print(f"  GET /plugins -> {r.status_code} {r.text[:300]}")

    print("== 3. 新建 webchat 会话 ==")
    r = requests.get(
        f"{LEGACY_API}/chat/new_session",
        params={"platform_id": "webchat"},
        headers=headers,
        timeout=60,
    )
    print(f"  {r.status_code} {r.text[:300]}")
    session_id = (r.json().get("data") or {}).get("session_id")
    if not session_id:
        print("!! 没拿到 session_id，终止")
        return 1
    print("  session_id =", session_id)

    print(f"== 4. 发消息：{args.message} ==")
    payload = {
        "message": args.message,
        "session_id": session_id,
        "enable_streaming": True,
    }
    tool_calls, images, texts = [], [], []
    try:
        with requests.post(
            f"{LEGACY_API}/chat/send",
            headers={**headers, "Accept": "text/event-stream"},
            json=payload,
            stream=True,
            timeout=180,
        ) as resp:
            print("  HTTP", resp.status_code)
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data in ("[DONE]", ""):
                    continue
                try:
                    event = json.loads(data)
                except Exception:
                    texts.append(data[:200])
                    continue
                etype = event.get("type") or event.get("event")
                if etype in ("tool_call", "tool_call_result", "tool"):
                    tool_calls.append(event)
                    print("  [SSE工具事件]", json.dumps(event, ensure_ascii=False)[:500])
                if etype == "image" or "image" in json.dumps(event, ensure_ascii=False)[:200]:
                    images.append(event)
                    print("  [SSE图片事件]", json.dumps(event, ensure_ascii=False)[:400])
                if etype in ("plain", "text", "message", "complete"):
                    texts.append(json.dumps(event, ensure_ascii=False)[:300])
    except Exception as exc:
        print("  SSE 异常:", exc)

    print("  文本事件样例:", texts[:5])
    print("  工具事件数:", len(tool_calls), " 图片事件数:", len(images))

    print("== 5. 拉取会话消息（确认落库） ==")
    time.sleep(1.0)
    r = requests.get(
        f"{BASE_API}/chat/sessions/{session_id}", headers=headers, timeout=60
    )
    history = []
    if r.status_code == 200:
        data = r.json().get("data", {}) or {}
        history = data.get("history") or data.get("messages") or []
        for msg in history:
            content = msg.get("content") or {}
            items = content.get("message") if isinstance(content, dict) else content
            items = items or []
            kinds = []
            for it in items:
                if not isinstance(it, dict):
                    kinds.append(repr(it)[:40])
                    continue
                itype = it.get("type")
                if itype == "tool_call":
                    names = [c.get("name") for c in it.get("tool_calls", [])]
                    results = [
                        str(c.get("result"))[:120] for c in it.get("tool_calls", [])
                    ]
                    kinds.append(f"tool_call{names} result={results}")
                elif itype == "plain":
                    kinds.append(f"plain:{str(it.get('text'))[:120]}")
                else:
                    kinds.append(json.dumps(it, ensure_ascii=False)[:160])
            print(f"  #{msg.get('id')} [{content.get('type') if isinstance(content, dict) else '?'}] {kinds}")
    else:
        print(f"  {r.status_code} {r.text[:300]}")

    # ---- 校验 ----
    blob = json.dumps(history, ensure_ascii=False)
    called = sorted(set(__import__("re").findall(r'"name": ?"(pc_[a-z_]+)"', blob)))
    image_hits = blob.count('"image"') + blob.count("base64") + blob.count(".png")
    print("  会话中出现的 pc_* 工具调用:", called)
    print("  会话中图片相关标记数:", image_hits)

    if not args.keep_session:
        print("== 6. 清理测试会话 ==")
        r = requests.delete(
            f"{BASE_API}/chat/sessions/{session_id}", headers=headers, timeout=60
        )
        print(f"  DELETE {r.status_code} {r.text[:200]}")
    else:
        print("== 6. 保留会话（--keep-session）：", session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
