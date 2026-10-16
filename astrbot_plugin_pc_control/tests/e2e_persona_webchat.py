# -*- coding: utf-8 -*-
"""端到端测试：webchat 会话里让模型调用 pc_screenshot 并把图片发回来。

背景（很重要）：
    AstrBot 的人格（persona）如果配置了「工具白名单」，不在白名单里的工具对模型是不可见的。
    本实例的默认人格「默认」白名单里只有 6 个记忆类工具，所以 pc_* 工具默认**不会**被模型看到
    （症状：模型回“我没有截屏工具”，然后在 astrbot_execute_python 里手搓 PIL 截屏）。

    本脚本因此临时创建一个人格（tools 留空 = 不限制工具），把它绑到**本次测试会话**上，
    跑完自动删掉会话和这个人格 —— 不动用户原有任何配置。

用法：
    <venv>\\Scripts\\python.exe tests\\e2e_persona_webchat.py [--message "截个屏发给我"] [--keep]
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time

import jwt
import requests

BASE_API = "http://127.0.0.1:62523/api/v1"
LEGACY_API = "http://127.0.0.1:62523/api"
CONFIG_PATH = (
    r"C:\Users\Administrator\.astrbot_launcher\instances"
    r"\f3436f89-9e75-4ef3-a1f9-6b6507c1994f\core\data\cmd_config.json"
)
PLUGIN_ID = "astrbot_plugin_pc_control"
TMP_PERSONA = "pc_e2e_tmp"
TMP_PERSONA_PROMPT = (
    "你是一个能操作本机的助手。需要查看屏幕、操作鼠标键盘时，优先调用 pc_* 系列工具，"
    "不要用 Python/Shell 自己造轮子。回答保持简短。"
)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8-sig") as fp:
        return json.load(fp)


def make_token(cfg: dict) -> str:
    dash = cfg["dashboard"]
    return jwt.encode(
        {
            "username": dash.get("username", "astrbot"),
            "exp": datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1),
        },
        dash["jwt_secret"],
        algorithm="HS256",
    )


def find_conversation(headers: dict, session_id: str) -> dict | None:
    """在 WebUI 的对话列表里找到本次 webchat 会话对应的 (user_id, conversation_id)。

    注意：webchat 的 conversation 行是在**第一条消息**落地时才创建的，
    所以调用本函数前必须先用一条无关紧要的消息把这个会话「热身」出来，
    否则只能匹配到别的旧会话（踩过坑）。
    """
    r = requests.get(
        f"{BASE_API}/conversations",
        params={"page": 1, "page_size": 20, "platforms": "webchat"},
        headers=headers,
        timeout=60,
    )
    body = r.json()
    data = body.get("data", body)
    items = data.get("conversations") if isinstance(data, dict) else data
    for it in items or []:
        if session_id in json.dumps(it, ensure_ascii=False):
            return it
    return None


def send_message(headers: dict, session_id: str, message: str, timeout: int = 300):
    """发一条消息并读 SSE，返回 (tool_blobs, image_blobs, text_blobs, session_id)。"""
    tool_blobs, image_blobs, text_blobs = [], [], []
    with requests.post(
        f"{LEGACY_API}/chat/send",
        headers={**headers, "Accept": "text/event-stream"},
        json={"message": message, "session_id": session_id, "enable_streaming": True},
        stream=True,
        timeout=timeout,
    ) as resp:
        print("  HTTP", resp.status_code)
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("[DONE]", ""):
                continue
            try:
                event = json.loads(payload)
            except Exception:
                text_blobs.append(payload[:200])
                continue
            blob = json.dumps(event, ensure_ascii=False)
            etype = str(event.get("type") or event.get("event"))
            ctype = str(event.get("chain_type") or "")
            if "tool" in etype or "tool" in ctype or '"tool_call"' in blob[:300]:
                tool_blobs.append(blob)
                print("  [SSE tool]", blob[:300])
            if '"image"' in blob or "base64" in blob:
                image_blobs.append(blob)
                print("  [SSE image]", blob[:200])
            if etype in ("plain", "text", "complete"):
                text_blobs.append(blob[:400])
    return tool_blobs, image_blobs, text_blobs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default="截个屏发给我")
    parser.add_argument("--keep", action="store_true", help="保留测试会话/人格（不清理）")
    args = parser.parse_args()

    cfg = load_config()
    headers = {"Authorization": f"Bearer {make_token(cfg)}"}
    problems: list[str] = []
    session_id = None
    persona_created = False

    try:
        print("== 1. 热加载插件 ==")
        r = requests.post(
            f"{BASE_API}/plugins/{PLUGIN_ID}/reload", headers=headers, timeout=90
        )
        print(f"  POST reload -> {r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            problems.append(f"热加载失败：{r.status_code} {r.text[:200]}")

        print("== 2. 注册的工具 ==")
        r = requests.get(f"{BASE_API}/tools", headers=headers, timeout=60)
        data = r.json().get("data", {})
        tools = data.get("tools") if isinstance(data, dict) else data
        if isinstance(tools, dict):
            tools = tools.get("tools") or []
        pc = sorted(
            t["name"] for t in tools
            if t.get("name", "").startswith("pc_") and t.get("active")
            and t["name"] in {
                "pc_screenshot", "pc_screen_size", "pc_mouse_position", "pc_mouse_move",
                "pc_mouse_click", "pc_mouse_drag", "pc_mouse_scroll", "pc_key_press",
                "pc_key_hotkey", "pc_type_text", "pc_clipboard_read", "pc_clipboard_write",
                "pc_list_windows", "pc_activate_window", "pc_active_window",
                "pc_launch_program", "pc_kill_process",
            }
        )
        print(f"  本插件 active 工具 {len(pc)} 个: {pc}")
        if len(pc) != 17:
            problems.append(f"工具数量不对：{len(pc)}")

        print("== 3. 建临时人格（tools 留空 = 不限制工具）==")
        r = requests.post(
            f"{BASE_API}/personas",
            headers=headers,
            json={"persona_id": TMP_PERSONA, "system_prompt": TMP_PERSONA_PROMPT},
            timeout=60,
        )
        print(f"  POST /personas -> {r.status_code} {r.text[:200]}")
        persona_created = r.status_code in (200, 201)

        print("== 4. 新建 webchat 会话 ==")
        r = requests.get(
            f"{LEGACY_API}/chat/new_session",
            params={"platform_id": "webchat"},
            headers=headers,
            timeout=60,
        )
        session_id = (r.json().get("data") or {}).get("session_id")
        print(f"  session_id = {session_id}")
        if not session_id:
            problems.append(f"没拿到 session_id：{r.status_code} {r.text[:200]}")
            return 1

        print("== 5. 先热身一条消息，让会话对应的 conversation 落库 ==")
        send_message(headers, session_id, "你好", timeout=180)

        print("== 6. 把临时人格绑到本会话 ==")
        conv = find_conversation(headers, session_id)
        if not conv:
            problems.append("没找到会话对应的 conversation，模型将看不到 pc_* 工具")
        else:
            umo = conv.get("user_id")
            cid = conv.get("conversation_id") or conv.get("cid") or conv.get("id")
            print("  cid=", cid)
            print("  umo=", umo)
            if session_id not in str(umo):
                problems.append(f"匹配到的 conversation 不是本次会话：{umo}")
            r = requests.patch(
                f"{BASE_API}/conversations/{cid}",
                params={"user_id": umo},
                headers=headers,
                json={"persona_id": TMP_PERSONA},
                timeout=60,
            )
            print(f"  PATCH persona -> {r.status_code} {r.text[:200]}")
            if r.status_code != 200:
                problems.append(f"设置人格失败：{r.status_code} {r.text[:200]}")

        print(f"== 7. 发消息：{args.message} ==")
        tool_hits, image_hits, text_out = send_message(headers, session_id, args.message)
        print("  工具事件数:", len(tool_hits), "图片事件数:", len(image_hits))
        print("  文本/其它事件样例:")
        for t in text_out[:6]:
            print("    ", t[:300])

        print("== 8. 查会话落库内容 ==")
        time.sleep(1.5)
        r = requests.get(f"{BASE_API}/chat/sessions/{session_id}", headers=headers, timeout=60)
        blob = r.text
        print(f"  GET session -> {r.status_code}, {len(blob)} bytes")
        called = sorted(set(re.findall(r'"(?:name|tool_name)": ?"(pc_[a-z_]+)"', blob)))
        print("  会话里出现的 pc_* 工具调用:", called)
        print("  会话里 image 相关标记数:", blob.count('"image"'), " base64:", blob.count("base64"), " .png:", blob.count(".png"))
        body = r.json().get("data", {}) or {}
        for msg in (body.get("history") or body.get("messages") or []):
            content = msg.get("content")
            items = content.get("message") if isinstance(content, dict) else content
            kinds = []
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "tool_call":
                    for c in it.get("tool_calls", []):
                        kinds.append(f"call {c.get('name')} -> {str(c.get('result'))[:130]}")
                elif it.get("type") in ("plain", "text"):
                    kinds.append(f"text: {str(it.get('text'))[:130]}")
                else:
                    kinds.append(it.get("type"))
            print(f"    #{msg.get('id')}: {kinds}")

        if "pc_screenshot" not in called:
            problems.append("模型这次没有调用 pc_screenshot（看上面的会话内容）")
        if blob.count('"image"') == 0:
            problems.append("会话里没看到图片内容落库")

    finally:
        print("== 9. 清理 ==")
        if session_id and not args.keep:
            r = requests.delete(
                f"{BASE_API}/chat/sessions/{session_id}", headers=headers, timeout=60
            )
            print(f"  DELETE 会话 {session_id} -> {r.status_code} {r.text[:120]}")
        elif session_id:
            print("  保留会话（--keep）:", session_id)
        if persona_created and not args.keep:
            r = requests.delete(
                f"{BASE_API}/personas/by-id",
                params={"persona_id": TMP_PERSONA},
                headers=headers,
                timeout=60,
            )
            print(f"  DELETE 临时人格 -> {r.status_code} {r.text[:120]}")

    print()
    if problems:
        print("!! 未通过/需关注：")
        for p in problems:
            print("  -", p)
        return 1
    print("端到端测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
