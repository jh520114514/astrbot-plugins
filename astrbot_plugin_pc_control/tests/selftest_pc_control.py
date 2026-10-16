# -*- coding: utf-8 -*-
r"""本地自检（**外部子进程**里跑，不依赖 AstrBot）。

注意：本脚本跑在外部子进程里，它的窗口站/桌面可能和 AstrBot 主进程不同。
在断开远程桌面的机器上，这里会出现「mss BitBlt 失败」「鼠标跳过」——
那说明的是**当前子进程**所在桌面的状态，不代表插件在 AstrBot 进程里也不可用。
插件进程内的真实情况请用命令 /pcdiag 查看。

脚本动作（全部无害）：截屏 / 鼠标移动 / 剪贴板 / 窗口列表 / 屏幕信息。

用法（在插件目录下）：
    <实例>\\venv\\Scripts\python.exe tests\selftest_pc_control.py

不会点击、不会打字、不会关窗口。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from pc_control import PCController, dependency_report  # noqa: E402


def main() -> int:
    ctl = PCController()
    failures: list[str] = []

    print("== 1. 依赖 ==")
    deps = dependency_report()
    print(json.dumps(deps, ensure_ascii=False, indent=2))
    for name, info in deps.items():
        if not info["available"]:
            failures.append(f"依赖缺失：{name} -> {info['error']}")

    print("== 2. 屏幕信息 ==")
    screen = ctl.screen_size()
    print(json.dumps(screen, ensure_ascii=False))
    if screen["width"] <= 0 or screen["height"] <= 0:
        failures.append("屏幕分辨率获取异常")

    print("== 3. 截屏 ==")
    shot = ctl.screenshot()
    print(json.dumps(shot, ensure_ascii=False))
    p = Path(shot["path"])
    if not p.exists() or p.stat().st_size <= 1024:
        failures.append("截屏文件不存在或过小")
    if shot["width"] <= 0 or shot["height"] <= 0:
        failures.append("截屏尺寸异常")
    print(f"  文件存在={p.exists()} 大小={shot['size_bytes']} bytes 尺寸={shot['width']}x{shot['height']}")

    print("== 4. 鼠标（只移动到 300,300 并读回坐标）==")
    info = ctl.session_info()
    print("  会话信息:", json.dumps(info, ensure_ascii=False))
    if not info.get("input_desktop_available", False):
        # 当前 Windows 会话没有输入桌面（例如远程桌面已断开）：
        # GetCursorPos/SetCursorPos 都会被拒绝（ERROR_ACCESS_DENIED），
        # 属于环境限制而不是插件缺陷。这里改为验证“错误处理是否给出了可读提示”。
        print("  [跳过] 当前会话没有输入桌面，系统拒绝所有鼠标/键盘 API（Access denied）")
        try:
            ctl.mouse_move(300, 300)
            failures.append("无输入桌面时 mouse_move 竟然成功了？行为异常")
        except Exception as exc:
            msg = str(exc)
            print("  预期内的报错:", msg[:120], "...")
            if "FAILSAFE" not in msg and "ACCESS" not in msg.upper():
                failures.append(f"鼠标错误提示不可读：{msg[:200]}")
    else:
        before = ctl.mouse_position()
        moved = ctl.mouse_move(300, 300)
        after = ctl.mouse_position()
        print(f"  移动前={before} 移动后={after} 返回={moved}")
        if abs(after["x"] - 300) > 3 or abs(after["y"] - 300) > 3:
            failures.append(f"鼠标移动后坐标不匹配：期望 (300,300) 实际 ({after['x']},{after['y']})")

    print("== 5. 剪贴板写入/读回 ==")
    token = "pc_control_selftest_中文_42"
    ctl.clipboard_write(token)
    back = ctl.clipboard_read()
    print(f"  写回内容={back['text']!r}")
    if back["text"] != token:
        failures.append(f"剪贴板回读不一致：{back['text']!r} != {token!r}")

    print("== 6. 窗口列表 ==")
    wins = ctl.list_windows()
    print(f"  窗口数量={wins['count']}")
    for w in wins["windows"][:10]:
        print(f"    - {w['title'][:60]} hwnd={w['handle']} active={w['active']}")
    if wins["count"] <= 0:
        failures.append("窗口列表为空")

    print("== 7. 活动窗口（含浏览器 URL 尽力读取）==")
    active = ctl.active_window()
    print(json.dumps(active, ensure_ascii=False))

    print("== 8. 工具清单（不依赖 AstrBot 的静态检查）==")
    main_py = PLUGIN_DIR / "main.py"
    text = main_py.read_text(encoding="utf-8")
    tool_names = sorted(set(__import__("re").findall(r'name: str = "(pc_[a-z_]+)"', text)))
    print(f"  发现 {len(tool_names)} 个工具: {tool_names}")
    if len(tool_names) < 17:
        failures.append(f"工具数量不足：{len(tool_names)}")

    print()
    if failures:
        print("!! 失败项：")
        for f in failures:
            print("  -", f)
        return 1
    print("全部自检通过 ✅")
    print("截图文件：", shot["path"], "(如需清理请手动删除)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
