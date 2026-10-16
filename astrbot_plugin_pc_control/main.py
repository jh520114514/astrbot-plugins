# -*- coding: utf-8 -*-
"""astrbot_plugin_pc_control —— 本地电脑控制插件（AstrBot 4.27.x）。

把本机的截屏 / 鼠标 / 键盘 / 剪贴板 / 窗口 / 程序启动结束能力注册成 LLM 函数工具。

安全模型：
- 全部工具受插件配置 `enable_control_tools` 总开关控制；
- 控制类（鼠标、键盘、剪贴板写入、启动/结束程序）默认要求管理员（`require_admin`）；
- 截屏 / 窗口列举默认不要求管理员，但可通过 `require_admin_for_screenshot` 打开；
- pyautogui.FAILSAFE 默认开启：鼠标猛拖到屏幕左上角会立刻抛异常中断（救命开关）；
- 结束进程必须传 confirm="KILL" 作为二次确认，且内置系统关键进程黑名单。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mcp.types
from pydantic.dataclasses import dataclass

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain

try:  # 4.27.x 有 ProviderRequest；老内核没有时只用于类型标注，降级为 Any
    from astrbot.api.provider import ProviderRequest
except Exception:  # pragma: no cover
    ProviderRequest = Any  # type: ignore[assignment,misc]

from .pc_control import (
    DEFAULT_IMAGE_JPEG_QUALITY,
    DEFAULT_MAX_IMAGE_SIDE,
    PCController,
    PCControlError,
    dependency_report,
    encode_image_for_llm,
    thumbnail_path_for,
)

PLUGIN_NAME = "astrbot_plugin_pc_control"

# ---------------------------------------------------------------------------
# 插件实例引用（hot reload 安全）
#
# 旧实现只在 terminate() 里无条件 `_PLUGIN = None`：热重载时 AstrBot 会
# 新建实例、再 terminate 旧实例（顺序不保证），旧实例的 terminate 一旦把全局
# 引用清空，新实例刚注册的工具就会全部报「插件实例未初始化」。
# v1.0.1 做法：
#   1) terminate() 只在全局引用**仍指向自己**时才清空；
#   2) 另外维护存活实例登记表，全局引用为空时兜底取最新的存活实例；
#   3) 已 terminate 的实例从登记表移除，不参与兜底。
# ---------------------------------------------------------------------------
_PLUGIN: "PCControlPlugin | None" = None
_INSTANCES: list["PCControlPlugin"] = []

_CONTROL = "control"
_CAPTURE = "capture"

# 清理上下文图片时用的占位文本模板
_IMAGE_STUB = (
    "[图片已清理] 此处原本有一张图片{ident}。为控制请求体积，"
    "它已从本次请求中移除（只保留最近 {keep} 张图片）。"
    "原图文件仍在磁盘上，需要时请重新截图或直接读取该文件。"
)


def _live_plugin() -> "PCControlPlugin | None":
    """返回当前生效的插件实例（hot reload 后也能拿到最新的那个）。"""
    if _PLUGIN is not None and not getattr(_PLUGIN, "_terminated", False):
        return _PLUGIN
    for inst in reversed(_INSTANCES):
        if not getattr(inst, "_terminated", False):
            return inst
    return None


# ---------------------------------------------------------------------------
# 通用工具函数
# ---------------------------------------------------------------------------

def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _short(err: Exception) -> str:
    return str(err) or type(err).__name__


def _cfg(key: str, default: Any) -> Any:
    plugin = _live_plugin()
    if plugin is None:
        return default
    try:
        value = plugin.config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _is_admin(event: AstrMessageEvent) -> bool:
    try:
        if event.role == "admin":
            return True
    except Exception:
        pass
    extra = str(_cfg("extra_admin_ids", "") or "")
    ids = {x.strip() for x in re.split(r"[,\s;]+", extra) if x.strip()}
    try:
        sender = str(event.get_sender_id())
    except Exception:
        sender = ""
    return bool(sender and sender in ids)


def _check(kind: str, operation: str, context: ContextWrapper[AstrAgentContext]) -> str | None:
    """权限与开关校验。返回非 None 表示应直接把该字符串当作工具结果返回。"""
    if not _cfg("enable_control_tools", True):
        return f"error: 本地电脑控制插件已被禁用（enable_control_tools=false），无法执行：{operation}"
    required = _cfg("require_admin", True) if kind == _CONTROL else _cfg(
        "require_admin_for_screenshot", False
    )
    if required:
        event = context.context.event
        if not _is_admin(event):
            try:
                sender_id = event.get_sender_id()
            except Exception:
                sender_id = "unknown"
            return (
                f"error: 权限不足。{operation} 仅允许管理员调用。"
                f"当前调用者 ID：{sender_id}。"
                "如需放开，请在 AstrBot WebUI -> 配置 -> 管理员列表 中加入该 ID，"
                f"或在插件 {PLUGIN_NAME} 配置里关闭 require_admin。"
            )
    return None


def _controller() -> PCController:
    plugin = _live_plugin()
    if plugin is None:
        raise PCControlError("插件实例未初始化")
    return plugin.controller


# ---------------------------------------------------------------------------
# 图片体积治理（v1.0.1）
#
# 背景：pc_screenshot 抓的是整屏 PNG（高分辨率机器上单张 3~4 MB），base64 后
# 会被塞进「工具结果 -> 上下文」并随每次请求重发，请求体超过网关
# (nginx client_max_body_size) 就 413。策略：
#   * 进模型上下文的一律是压缩版（默认最长边 1280 / JPEG q75，抓屏时已落盘缩略图）；
#   * 发给用户的仍然是原图路径（send_to_user 走原 PNG 文件，不受压缩影响）。
# ---------------------------------------------------------------------------

def _image_cfg() -> tuple[int, int, int]:
    """读取 (max_image_side, image_jpeg_quality, keep_recent_images)，带兜底与夹取。"""
    try:
        max_side = int(_cfg("max_image_side", DEFAULT_MAX_IMAGE_SIDE))
    except Exception:
        max_side = DEFAULT_MAX_IMAGE_SIDE
    try:
        quality = int(_cfg("image_jpeg_quality", DEFAULT_IMAGE_JPEG_QUALITY))
    except Exception:
        quality = DEFAULT_IMAGE_JPEG_QUALITY
    try:
        keep = int(_cfg("keep_recent_images", 2))
    except Exception:
        keep = 2
    return max(0, max_side), max(1, min(95, quality)), max(0, keep)


def _is_image_part(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") in ("image_url", "image")


def _image_part_info(item: dict) -> tuple[str, str]:
    """返回 (data_url_or_url, 标识串)。标识串用于占位文本，尽量给出原始文件路径。"""
    payload = item.get("image_url")
    if isinstance(payload, dict):
        url = str(payload.get("url") or "")
        ident = str(payload.get("id") or "")
    else:
        url = str(payload or "")
        ident = ""
    if not ident:
        ident = item.get("path") or item.get("id") or ""
    return url, str(ident or "")


def _slim_context_images(contexts: list, keep: int) -> dict:
    """只保留最近 keep 张图片，更早的图片块替换成简短占位文本。

    只动「图片块」（content 里 type=image_url/image 的项），文本、tool_calls、
    think 等其它内容一律不碰。返回统计信息（供日志/自检）。
    """
    positions: list[tuple[int, int, dict]] = []
    for mi, msg in enumerate(contexts):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for ci, item in enumerate(content):
            if _is_image_part(item):
                positions.append((mi, ci, item))

    info = {
        "images_total": len(positions),
        "images_kept": min(len(positions), keep),
        "images_dropped": 0,
        "dropped_base64_chars": 0,
    }
    if len(positions) <= keep:
        return info

    for mi, ci, item in positions[: len(positions) - keep]:
        url, ident = _image_part_info(item)
        info["dropped_base64_chars"] += len(url)
        try:
            contexts[mi]["content"][ci] = {
                "type": "text",
                "text": _IMAGE_STUB.format(
                    ident=f"（{ident}）" if ident else "", keep=keep
                ),
            }
            info["images_dropped"] += 1
        except Exception:
            continue
    return info


def _plugin_reference_check(current: "PCControlPlugin") -> str:
    """自检：工具真正走的那条路径（模块级引用 -> controller）现在是否可用。

    hot reload 后如果这里报「插件实例未初始化」，就说明引用又被清空了。
    """
    plugin = _live_plugin()
    if plugin is None:
        return "异常：模块级插件引用为空（工具会报「插件实例未初始化」）"
    try:
        _controller()
    except Exception as exc:
        return f"异常：_controller() 失败 -> {exc}"
    same = "self" if plugin is current else f"另一实例 id={id(plugin)}"
    live = sum(1 for i in _INSTANCES if not getattr(i, "_terminated", False))
    return f"OK（模块级引用 -> {same}；存活实例 {live}）"


def _screenshot_dir_stats(controller: PCController) -> dict:
    """截图目录现状（只读，用于自检/文档排查）。"""
    folder = controller.screenshot_dir
    stats: dict[str, Any] = {
        "dir": str(folder),
        "exists": folder.exists(),
        "png_count": 0,
        "thumb_count": 0,
        "total_bytes": 0,
        "newest": None,
    }
    if not folder.exists():
        return stats
    try:
        files = [p for p in folder.glob("shot-*") if p.is_file()]
        stats["png_count"] = len([p for p in files if p.suffix.lower() == ".png"])
        stats["thumb_count"] = len([p for p in files if p.suffix.lower() == ".jpg"])
        stats["total_bytes"] = sum(p.stat().st_size for p in files)
        if files:
            newest = max(files, key=lambda p: p.stat().st_mtime)
            stats["newest"] = f"{newest.name} ({newest.stat().st_size} bytes)"
    except Exception as exc:  # 自检绝不该抛
        stats["error"] = _short(exc)
    return stats


class _ProbeEvent:
    """自检用的假事件：只要 role == 'admin' 就能过权限门槛（不碰真实会话）。"""

    role = "admin"

    @staticmethod
    def get_sender_id() -> str:
        return "selfcheck"


class _ProbeContext:
    """自检用的假 Agent 上下文（ContextWrapper 只是类型标注，鸭子类型即可）。"""

    def __init__(self) -> None:
        self.context = SimpleNamespace(event=_ProbeEvent())


async def _tool_path_probe() -> dict[str, Any]:
    """真实工具路径探针：调用**已注册工具**的 call()，走的就是模型调用那条路。

    只挑无副作用的 pc_mouse_position（读光标坐标：不截屏、不动键鼠、不发消息），
    目的就是验证热重载之后 `_controller()` 不再抛「插件实例未初始化」。
    """
    probe: dict[str, Any] = {"tool": "pc_mouse_position"}
    plugin = _live_plugin()
    if plugin is None:
        probe.update(ok=False, error="插件实例未初始化")
        return probe
    tool = next(
        (
            t
            for t in getattr(plugin, "tools", [])
            if getattr(t, "name", "") == "pc_mouse_position"
        ),
        None,
    )
    if tool is None:
        probe.update(ok=False, error="已注册工具里找不到 pc_mouse_position")
        return probe
    try:
        out = await tool.call(context=_ProbeContext())  # type: ignore[arg-type]
        text = out if isinstance(out, str) else _json(out)
        probe["output"] = text[:160]
        probe["ok"] = not text.startswith("error:")
    except Exception as exc:
        probe.update(ok=False, error=f"{type(exc).__name__}: {_short(exc)}")
    return probe


async def _web_selfcheck(**_kwargs) -> dict:
    """只读自检接口（Dashboard JWT 鉴权后可用）：

        GET /api/v1/plugins/extensions/astrbot_plugin_pc_control/selfcheck

    不截屏、不写文件、不改状态。用途：热重载后立刻确认
    「模块级实例引用没被清空、工具真正走的 _controller() 路径可用」，
    从而不必为了验证而在真实会话里连续截屏。
    """
    plugin = _live_plugin()
    max_side, quality, keep = _image_cfg()
    result: dict[str, Any] = {
        "ok": plugin is not None,
        "pid": os.getpid(),
        "module_ref_alive": bool(
            _PLUGIN is not None and not getattr(_PLUGIN, "_terminated", False)
        ),
        "instances_registered": len(_INSTANCES),
        "instances_alive": sum(
            1 for i in _INSTANCES if not getattr(i, "_terminated", False)
        ),
        "live_instance_id": hex(id(plugin)) if plugin is not None else None,
        "image_policy": {
            "max_image_side": max_side,
            "image_jpeg_quality": quality,
            "keep_recent_images": keep,
            "send_screenshot_to_user": bool(_cfg("send_screenshot_to_user", True)),
        },
        "tools": [t.name for t in build_tools()],
        "enabled_control_tools": bool(_cfg("enable_control_tools", True)),
    }
    if plugin is None:
        result["controller_ok"] = False
        result["error"] = "插件实例未初始化：所有 pc_* 工具都会失败，请重载插件"
        return result
    try:
        controller = _controller()
        result["controller_ok"] = True
        size = controller.screen_size()
        result["screen_size"] = dict(size) if hasattr(size, "items") else list(size)
        result["dependency"] = dependency_report()
        result["screenshot_dir"] = _screenshot_dir_stats(controller)
    except Exception as exc:
        result["controller_ok"] = False
        result["error"] = f"{type(exc).__name__}: {_short(exc)}"
    try:
        # 走一遍真实工具的 call()（无副作用），确认「重载后工具立刻可用」
        result["tool_path"] = await _tool_path_probe()
    except Exception as exc:  # 探针失败不影响自检主结果
        result["tool_path"] = {"ok": False, "error": f"{type(exc).__name__}: {_short(exc)}"}
    try:
        result["llm_request_hooks"] = _llm_request_hook_status()
    except Exception as exc:
        result["llm_request_hooks"] = {"error": f"{type(exc).__name__}: {_short(exc)}"}
    return result


def _llm_request_hook_status() -> dict[str, Any]:
    """自检：`@filter.on_llm_request()` 钩子在本实例内核里是否真的注册上了。

    只查内核 star_handlers_registry（只读），不触发任何请求。
    """
    try:
        from astrbot.core.star.star_handler import (  # noqa: PLC0415
            EventType,
            star_handlers_registry,
        )
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {_short(exc)}"}
    handlers = star_handlers_registry.get_handlers_by_event_type(
        EventType.OnLLMRequestEvent,
        only_activated=False,
        plugins_name=["astrbot_plugin_pc_control"],
    )
    return {
        "available": True,
        "count": len(handlers),
        "names": [getattr(h, "handler_name", "?") for h in handlers],
    }


def _query_param(name: str, default: str = "") -> str:
    """读插件 Web 请求的 query 参数（拿不到就返回默认值，不抛异常）。"""
    try:
        from astrbot.api.web import request as web_request

        return str(web_request.query.get(name, default) or default)
    except Exception:
        return default


async def _web_capture_probe(**_kwargs) -> dict:
    """**可选**的一次性抓屏探针（默认关闭，只有显式带上 capture=1 才真的截图）：

        GET /api/v1/plugins/extensions/astrbot_plugin_pc_control/selfcheck?capture=1

    用途：在**插件进程内**量出「抓屏原图 vs 缩略图 vs 喂给模型的压缩版」的真实体积/尺寸，
    验证「发给用户的是原图、进模型的是压缩版」。副作用只有：截图目录里多 1 张 PNG
    （以及体积确实更小时多 1 张 -thumb.jpg）。**不发消息、不进任何会话上下文**。
    """
    if _query_param("capture").lower() not in ("1", "true", "yes", "on"):
        return {
            "ok": False,
            "skipped": True,
            "message": "只读模式：本接口只在带 capture=1 时才真的抓屏，"
                       "以免无意间在截图目录堆文件。",
        }
    plugin = _live_plugin()
    if plugin is None:
        return {"ok": False, "error": "插件实例未初始化"}
    max_side, quality, _ = _image_cfg()
    try:
        controller = _controller()
        shot = await asyncio.to_thread(
            controller.screenshot, 0, None, None, "desktop", None, None
        )
        out: dict[str, Any] = {
            "ok": True,
            "image_policy": {"max_image_side": max_side, "image_jpeg_quality": quality},
            "original": {
                "path": shot["path"],
                "width": shot["width"],
                "height": shot["height"],
                "bytes": shot["size_bytes"],
                "exists": Path(shot["path"]).exists(),
                "backend": shot.get("backend"),
            },
        }
        # 发给用户的原图：路径必须真实可读，且能被 MessageChain.file_image 接受
        try:
            chain = MessageChain().file_image(shot["path"])
            out["user_message_chain"] = {
                "ok": True,
                "components": len(getattr(chain, "chain", []) or []),
            }
        except Exception as exc:
            out["user_message_chain"] = {"ok": False, "error": _short(exc)}
        if shot.get("thumb_path"):
            out["thumbnail"] = {
                "path": shot["thumb_path"],
                "width": shot.get("thumb_width"),
                "height": shot.get("thumb_height"),
                "bytes": shot.get("thumb_size_bytes"),
            }
        else:
            out["thumbnail"] = None
            out["thumbnail_note"] = "本次没产出缩略图（原图本身比缩略图还小，安全阀生效）"
        enc = await asyncio.to_thread(
            encode_image_for_llm, shot["path"], max_side, quality, shot.get("thumb_path")
        )
        out["llm_image"] = {
            "width": enc["width"],
            "height": enc["height"],
            "bytes": enc["bytes"],
            "base64_chars": len(enc["base64"]),
            "mime_type": enc["mime_type"],
            "source": enc["source"],
            "original_bytes": enc.get("original_bytes"),
        }
        orig = shot["size_bytes"] or 1
        out["llm_image"]["base64_to_original_ratio"] = round(len(enc["base64"]) / orig, 4)
        out["screenshot_dir"] = _screenshot_dir_stats(controller)
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {_short(exc)}"}



def _cleanup_old_screenshots() -> None:
    """截图目录清理：按张数上限 + 保留天数（默认关闭），并顺带清掉对应缩略图。"""
    try:
        keep = int(_cfg("max_keep_screenshots", 30))
    except Exception:
        keep = 30
    try:
        days = float(_cfg("screenshot_retention_days", 0) or 0)
    except Exception:
        days = 0.0
    keep = max(0, keep)
    controller = _controller()
    folder = controller.screenshot_dir
    if not folder.exists():
        return
    now = time.time()
    primaries = sorted(
        folder.glob("shot-*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for index, old in enumerate(primaries):
        too_many = index >= keep
        too_old = days > 0 and (now - old.stat().st_mtime) > days * 86400
        if not (too_many or too_old):
            continue
        for target in (old, thumbnail_path_for(old)):
            try:
                target.unlink()
            except Exception:
                pass
    # 孤儿缩略图（原图已不在）也一并清掉，避免 %TEMP% 无限膨胀
    for thumb in folder.glob("shot-*-thumb.jpg"):
        origin = Path(str(thumb)[: -len("-thumb.jpg")] + ".png")
        if not origin.exists():
            try:
                thumb.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 工具 1：截屏
# ---------------------------------------------------------------------------

@dataclass
class PCScreenshotTool(FunctionTool):
    name: str = "pc_screenshot"
    description: str = (
        "截取本机屏幕并保存为 PNG 图片。默认抓整屏；"
        "整屏抓取不可用（远程会话断开/锁屏）或需要单窗口画面时，可指定 target。"
        "可把图片直接发到当前会话（发给用户的是原图），"
        "同时把压缩后的版本放进模型上下文供你查看（避免撑爆请求体）。"
        "返回文件路径、屏幕分辨率与图片尺寸。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "monitor": {
                "type": "integer",
                "description": "显示器索引：0=所有显示器拼接，1=主显示器，2=第二显示器…默认 0。",
                "default": 0,
            },
            "target": {
                "type": "string",
                "description": (
                    '抓取目标：desktop=整屏（默认）；active_window=当前前台窗口；'
                    'window=按 window_title/window_handle 指定的窗口。'
                ),
                "default": "desktop",
            },
            "window_title": {
                "type": "string",
                "description": "target=window 时的窗口标题关键词（模糊匹配）。",
            },
            "send_to_user": {
                "type": "boolean",
                "description": "是否把截图发送到当前会话（默认 true）。",
                "default": True,
            },
            "return_image_to_llm": {
                "type": "boolean",
                "description": "是否把图片内容放进工具结果供模型查看（默认 true）。",
                "default": True,
            },
        },
    })

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        monitor: int = 0,
        target: str = "desktop",
        window_title: str | None = None,
        send_to_user: bool = True,
        return_image_to_llm: bool = True,
    ) -> ToolExecResult:
        if err := _check(_CAPTURE, "截屏", context):
            return err
        try:
            controller = _controller()
            result = await asyncio.to_thread(
                controller.screenshot,
                int(monitor),
                None,
                None,
                str(target or "desktop"),
                window_title,
                None,
            )
            screen = await asyncio.to_thread(controller.screen_size)
            payload: dict[str, Any] = {
                "success": True,
                "path": result["path"],
                "width": result["width"],
                "height": result["height"],
                "size_bytes": result["size_bytes"],
                "backend": result["backend"],
                "screen_width": screen["width"],
                "screen_height": screen["height"],
            }
            for key in ("window", "handle", "note", "fallback", "warning", "tried"):
                if key in result:
                    payload[key] = result[key]
            # 发给用户：始终用原图路径（send_to_user -> file_image(原 PNG)）
            image_to_llm: dict | None = None
            if return_image_to_llm or send_to_user:
                max_side, quality, _ = _image_cfg()
                if return_image_to_llm:
                    # 进模型上下文的永远是压缩版：优先复用抓屏时就写好的缩略图
                    image_to_llm = await asyncio.to_thread(
                        encode_image_for_llm,
                        result["path"],
                        max_side,
                        quality,
                        result.get("thumb_path"),
                    )
                    payload["llm_image"] = {
                        "source": image_to_llm["source"],
                        "width": image_to_llm["width"],
                        "height": image_to_llm["height"],
                        "size_bytes": image_to_llm["bytes"],
                        "original_size_bytes": result["size_bytes"],
                        "mime_type": image_to_llm["mime_type"],
                    }
                    if result.get("thumb_path"):
                        payload["thumb_path"] = result["thumb_path"]
                    payload["note_image_policy"] = (
                        "发送给用户的是原图（%s, %dx%d, %d bytes）；"
                        "进入本次模型上下文的是压缩版（%dx%d, %d bytes, source=%s）。"
                        % (
                            Path(result["path"]).name,
                            result["width"], result["height"], result["size_bytes"],
                            image_to_llm["width"], image_to_llm["height"],
                            image_to_llm["bytes"], image_to_llm["source"],
                        )
                    )
            if send_to_user and _cfg("send_screenshot_to_user", True):
                try:
                    await context.context.event.send(
                        MessageChain().file_image(result["path"])
                    )
                    payload["sent_to_user"] = True
                except Exception as exc:
                    payload["sent_to_user"] = False
                    payload["send_error"] = _short(exc)
            content: list[mcp.types.ContentBlock] = [
                mcp.types.TextContent(type="text", text=_json(payload))
            ]
            if return_image_to_llm and image_to_llm and image_to_llm["base64"]:
                content.append(mcp.types.ImageContent(
                    type="image",
                    data=image_to_llm["base64"],
                    mimeType=image_to_llm["mime_type"],
                ))
            await asyncio.to_thread(_cleanup_old_screenshots)
            return mcp.types.CallToolResult(content=content)
        except Exception as exc:
            logger.error(f"[pc_control] screenshot failed: {exc}")
            return f"error: 截屏失败：{_short(exc)}"


# ---------------------------------------------------------------------------
# 工具 2：屏幕信息 / 鼠标坐标
# ---------------------------------------------------------------------------

@dataclass
class PCScreenSizeTool(FunctionTool):
    name: str = "pc_screen_size"
    description: str = "获取本机屏幕（所有显示器）分辨率与鼠标当前坐标。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object", "properties": {},
    })

    async def call(self, context: ContextWrapper[AstrAgentContext]) -> ToolExecResult:
        if err := _check(_CAPTURE, "查询屏幕信息", context):
            return err
        try:
            controller = _controller()
            screen = await asyncio.to_thread(controller.screen_size)
            pos = await asyncio.to_thread(controller.mouse_position)
            return _json({"screen": screen, "mouse": pos})
        except Exception as exc:
            return f"error: 查询屏幕信息失败：{_short(exc)}"


@dataclass
class PCMousePositionTool(FunctionTool):
    name: str = "pc_mouse_position"
    description: str = "获取鼠标当前坐标 (x, y)。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object", "properties": {},
    })

    async def call(self, context: ContextWrapper[AstrAgentContext]) -> ToolExecResult:
        if err := _check(_CAPTURE, "查询鼠标坐标", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.mouse_position))
        except Exception as exc:
            return f"error: 获取鼠标坐标失败：{_short(exc)}"


# ---------------------------------------------------------------------------
# 工具 3：鼠标
# ---------------------------------------------------------------------------

@dataclass
class PCMouseMoveTool(FunctionTool):
    name: str = "pc_mouse_move"
    description: str = "把鼠标移动到屏幕坐标 (x, y)。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "目标 X 坐标。"},
            "y": {"type": "integer", "description": "目标 Y 坐标。"},
            "duration": {"type": "number", "description": "移动耗时秒数，默认 0.2。", "default": 0.2},
        },
        "required": ["x", "y"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        x: int, y: int, duration: float = 0.2,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "移动鼠标", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.mouse_move, x, y, duration))
        except Exception as exc:
            return f"error: 移动鼠标失败：{_short(exc)}"


@dataclass
class PCMouseClickTool(FunctionTool):
    name: str = "pc_mouse_click"
    description: str = "在指定坐标点击鼠标，支持左/右/中键与双击（clicks=2）。不传坐标则在当前位置点击。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "X 坐标，省略则在鼠标当前位置点击。"},
            "y": {"type": "integer", "description": "Y 坐标，省略则在鼠标当前位置点击。"},
            "button": {"type": "string", "description": "left / right / middle，默认 left。"},
            "clicks": {"type": "integer", "description": "点击次数，2 表示双击。默认 1。", "default": 1},
            "interval": {"type": "number", "description": "多次点击间隔秒，默认 0.1。", "default": 0.1},
        },
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        x: int | None = None, y: int | None = None,
        button: str = "left", clicks: int = 1, interval: float = 0.1,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "点击鼠标", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(
                controller.mouse_click, x, y, button, clicks, interval
            ))
        except Exception as exc:
            return f"error: 点击失败：{_short(exc)}"


@dataclass
class PCMouseDragTool(FunctionTool):
    name: str = "pc_mouse_drag"
    description: str = "按住鼠标从 (x1,y1) 拖拽到 (x2,y2)。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "x1": {"type": "integer", "description": "起点 X。"},
            "y1": {"type": "integer", "description": "起点 Y。"},
            "x2": {"type": "integer", "description": "终点 X。"},
            "y2": {"type": "integer", "description": "终点 Y。"},
            "button": {"type": "string", "description": "left / right / middle，默认 left。"},
            "duration": {"type": "number", "description": "拖拽耗时秒，默认 0.5。", "default": 0.5},
        },
        "required": ["x1", "y1", "x2", "y2"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        x1: int, y1: int, x2: int, y2: int,
        button: str = "left", duration: float = 0.5,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "拖拽鼠标", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(
                controller.mouse_drag, x1, y1, x2, y2, button, duration
            ))
        except Exception as exc:
            return f"error: 拖拽失败：{_short(exc)}"


@dataclass
class PCMouseScrollTool(FunctionTool):
    name: str = "pc_mouse_scroll"
    description: str = "滚动鼠标滚轮。amount 正数向上、负数向下（单位=滚轮格）。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "amount": {"type": "integer", "description": "滚动量，正数向上、负数向下。"},
            "x": {"type": "integer", "description": "可选：先在 X 处滚动。"},
            "y": {"type": "integer", "description": "可选：先在 Y 处滚动。"},
        },
        "required": ["amount"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        amount: int, x: int | None = None, y: int | None = None,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "滚动鼠标", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.mouse_scroll, amount, x, y))
        except Exception as exc:
            return f"error: 滚动失败：{_short(exc)}"


# ---------------------------------------------------------------------------
# 工具 4：键盘
# ---------------------------------------------------------------------------

@dataclass
class PCKeyPressTool(FunctionTool):
    name: str = "pc_key_press"
    description: str = "按下单个按键（如 enter、esc、tab、f5、up、a、1）。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "键名，例如 enter / esc / tab / f5 / up。"},
            "presses": {"type": "integer", "description": "按几次，默认 1。", "default": 1},
        },
        "required": ["key"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        key: str, presses: int = 1,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "按键", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.key_press, key, presses))
        except Exception as exc:
            return f"error: 按键失败：{_short(exc)}"


@dataclass
class PCKeyHotkeyTool(FunctionTool):
    name: str = "pc_key_hotkey"
    description: str = '按组合键，例如 keys="ctrl+c"、"win+d"、"ctrl+shift+esc" 或 ["ctrl","alt","delete"]。'
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "keys": {
                "type": "string",
                "description": '组合键字符串，例如 "ctrl+c"、"alt+tab"、"win+d"。',
            },
        },
        "required": ["keys"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext], keys: str,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "组合键", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.key_hotkey, keys))
        except Exception as exc:
            return f"error: 组合键失败：{_short(exc)}"


@dataclass
class PCTypeTextTool(FunctionTool):
    name: str = "pc_type_text"
    description: str = (
        "向当前焦点窗口输入文本。纯英文数字走键盘直打；"
        "含中文等非 ASCII 字符时自动改用剪贴板 + Ctrl+V。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要输入的文本。"},
        },
        "required": ["text"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext], text: str,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "输入文本", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.type_text, text))
        except Exception as exc:
            return f"error: 输入文本失败：{_short(exc)}"


# ---------------------------------------------------------------------------
# 工具 5：剪贴板
# ---------------------------------------------------------------------------

@dataclass
class PCClipboardReadTool(FunctionTool):
    name: str = "pc_clipboard_read"
    description: str = "读取系统剪贴板中的文本内容。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object", "properties": {},
    })

    async def call(self, context: ContextWrapper[AstrAgentContext]) -> ToolExecResult:
        if err := _check(_CAPTURE, "读取剪贴板", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.clipboard_read))
        except Exception as exc:
            return f"error: 读取剪贴板失败：{_short(exc)}"


@dataclass
class PCClipboardWriteTool(FunctionTool):
    name: str = "pc_clipboard_write"
    description: str = "把文本写入系统剪贴板。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要写入剪贴板的文本。"},
        },
        "required": ["text"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext], text: str,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "写入剪贴板", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(controller.clipboard_write, text))
        except Exception as exc:
            return f"error: 写入剪贴板失败：{_short(exc)}"


# ---------------------------------------------------------------------------
# 工具 6：窗口
# ---------------------------------------------------------------------------

@dataclass
class PCListWindowsTool(FunctionTool):
    name: str = "pc_list_windows"
    description: str = "列出当前所有可见窗口的标题、句柄、位置尺寸与是否激活。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "include_empty_title": {
                "type": "boolean",
                "description": "是否包含无标题窗口（默认 false）。",
                "default": False,
            },
        },
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        include_empty_title: bool = False,
    ) -> ToolExecResult:
        if err := _check(_CAPTURE, "列举窗口", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(
                controller.list_windows, include_empty_title
            ))
        except Exception as exc:
            return f"error: 列举窗口失败：{_short(exc)}"


@dataclass
class PCActivateWindowTool(FunctionTool):
    name: str = "pc_activate_window"
    description: str = "激活/切换到指定窗口（按标题模糊匹配，或直接给窗口句柄）。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "窗口标题关键词（模糊匹配）。"},
            "handle": {"type": "integer", "description": "可选：窗口句柄，优先于 title。"},
        },
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        title: str | None = None, handle: int | None = None,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "切换窗口", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(
                controller.activate_window, title, handle
            ))
        except Exception as exc:
            return f"error: 切换窗口失败：{_short(exc)}"


@dataclass
class PCActiveWindowTool(FunctionTool):
    name: str = "pc_active_window"
    description: str = (
        "获取当前前台窗口的标题、进程名与句柄。"
        "若前台是浏览器，会尽力用 uiautomation 读取地址栏 URL（失败则只返回标题）。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "read_browser_url": {
                "type": "boolean",
                "description": "是否尝试读取浏览器地址栏 URL（默认 true）。",
                "default": True,
            },
        },
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        read_browser_url: bool = True,
    ) -> ToolExecResult:
        if err := _check(_CAPTURE, "获取活动窗口", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(
                controller.active_window, read_browser_url
            ))
        except Exception as exc:
            return f"error: 获取活动窗口失败：{_short(exc)}"


# ---------------------------------------------------------------------------
# 工具 7：程序
# ---------------------------------------------------------------------------

@dataclass
class PCLaunchProgramTool(FunctionTool):
    name: str = "pc_launch_program"
    description: str = (
        "启动程序或打开文件/URL。command 可以是可执行文件全路径、PATH 中的命令名，"
        '或带参数的命令串（如 "notepad C:\\a.txt"、URL）。args 为可选参数数组。'
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "程序路径 / 命令 / URL。"},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选参数数组。",
            },
            "cwd": {"type": "string", "description": "可选工作目录。"},
        },
        "required": ["command"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        command: str, args: list | None = None, cwd: str | None = None,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "启动程序", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(
                controller.launch_program, command, args, cwd
            ))
        except Exception as exc:
            return f"error: 启动程序失败：{_short(exc)}"


@dataclass
class PCKillProcessTool(FunctionTool):
    name: str = "pc_kill_process"
    description: str = (
        '按进程名结束进程（如 "notepad.exe"）。二次确认：必须显式传 confirm="KILL" 才会执行，'
        "否则只返回提示。系统关键进程会被拒绝。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "进程名，例如 notepad.exe。"},
            "confirm": {
                "type": "string",
                "description": '二次确认标记，必须为 "KILL"。',
            },
            "force": {
                "type": "boolean",
                "description": "优雅结束超时后是否强杀（默认 false）。",
                "default": False,
            },
        },
        "required": ["name"],
    })

    async def call(
        self, context: ContextWrapper[AstrAgentContext],
        name: str, confirm: str = "", force: bool = False,
    ) -> ToolExecResult:
        if err := _check(_CONTROL, "结束进程", context):
            return err
        try:
            controller = _controller()
            return _json(await asyncio.to_thread(
                controller.kill_process, name, confirm, force
            ))
        except Exception as exc:
            return f"error: 结束进程失败：{_short(exc)}"


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

def build_tools() -> list[FunctionTool]:
    return [
        PCScreenshotTool(),
        PCScreenSizeTool(),
        PCMousePositionTool(),
        PCMouseMoveTool(),
        PCMouseClickTool(),
        PCMouseDragTool(),
        PCMouseScrollTool(),
        PCKeyPressTool(),
        PCKeyHotkeyTool(),
        PCTypeTextTool(),
        PCClipboardReadTool(),
        PCClipboardWriteTool(),
        PCListWindowsTool(),
        PCActivateWindowTool(),
        PCActiveWindowTool(),
        PCLaunchProgramTool(),
        PCKillProcessTool(),
    ]


class PCControlPlugin(Star):
    """本地电脑控制插件：截屏 / 鼠标 / 键盘 / 剪贴板 / 窗口 / 程序。

    指令：
      /pcstatus  查看依赖状态、屏幕信息与已注册工具
      /pcdiag    在插件进程内做无害自检（截屏/光标/剪贴板/窗口）
      /pcdiag input  额外做一次鼠标移动往返测试
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        global _PLUGIN
        self._terminated = False
        _PLUGIN = self
        _INSTANCES.append(self)
        # 防御性裁剪：只保留最近若干实例（terminate 时会自行摘除）
        if len(_INSTANCES) > 8:
            del _INSTANCES[:-8]

        screenshot_dir = str(config.get("screenshot_dir", "") or "").strip() or None
        max_side, quality, _ = _image_cfg()
        self.controller = PCController(
            screenshot_dir=screenshot_dir,
            failsafe=bool(config.get("failsafe", True)),
            pause=float(config.get("mouse_pause", 0.05)),
            type_text_by_clipboard=bool(config.get("type_text_by_clipboard", True)),
            restore_clipboard_after_type=bool(
                config.get("restore_clipboard_after_type", True)
            ),
            thumb_max_side=max_side,
            thumb_jpeg_quality=quality,
        )
        self.tools: list[FunctionTool] = []
        if config.get("enable_control_tools", True):
            self.tools = build_tools()
            for tool in self.tools:
                tool.active = True
            self.context.add_llm_tools(*self.tools)
            logger.info(f"[pc_control] registered {len(self.tools)} LLM tools")
        else:
            logger.info("[pc_control] enable_control_tools=false，跳过工具注册")
        logger.info(
            "[pc_control] v1.0.1 实例就绪 pid=%s 图片策略=模型看压缩版(%spx/q%s)"
            "/用户收原图 retention_days=%s keep_screenshots=%s keep_recent_images=%s",
            os.getpid(), max_side, quality,
            config.get("screenshot_retention_days", 0),
            config.get("max_keep_screenshots", 30),
            config.get("keep_recent_images", 2),
        )
        # 只读自检接口（热重载后确认工具立刻可用，不必在会话里连续截屏）
        try:
            self.context.register_web_api(
                "/astrbot_plugin_pc_control/selfcheck",
                _web_selfcheck,
                ["GET"],
                "pc_control 只读自检：模块级实例引用 / controller / 图片压缩策略",
            )
            # 可选的抓屏探针：只有显式 capture=1 才真的截一张图（不发消息、不进上下文）
            self.context.register_web_api(
                "/astrbot_plugin_pc_control/capture_probe",
                _web_capture_probe,
                ["GET"],
                "pc_control 抓屏探针（需 capture=1）：量原图/缩略图/喂模型压缩版的体积与尺寸",
            )
            logger.info(
                "[pc_control] 自检接口: "
                "GET /api/v1/plugins/extensions/astrbot_plugin_pc_control/selfcheck ｜ "
                "抓屏探针: GET .../capture_probe?capture=1"
            )
        except Exception as exc:
            logger.warning(f"[pc_control] 自检 Web API 注册失败（不影响插件）: {_short(exc)}")

    async def terminate(self):
        global _PLUGIN
        self._terminated = True
        try:
            _INSTANCES.remove(self)
        except ValueError:
            pass
        # 关键修复：只有当全局引用仍指向自己时才清空；
        # 若热重载已经生成了更新的实例，绝不能把它的引用抹掉。
        if _PLUGIN is self:
            _PLUGIN = next(
                (i for i in reversed(_INSTANCES) if not getattr(i, "_terminated", False)),
                None,
            )
        logger.info(f"[pc_control] plugin terminated（模块级引用现在指向 {_PLUGIN!r}）")

    # ------------------------------------------------------------------
    # 上下文瘦身：请求前只保留最近 N 张图，更早的图片换成占位文本
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def _slim_context_images(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            max_side, quality, keep = _image_cfg()
            contexts = getattr(req, "contexts", None)
            if not isinstance(contexts, list) or not contexts:
                return
            info = _slim_context_images(contexts, keep)
            if info["images_dropped"]:
                logger.info(
                    "[pc_control] 上下文瘦身：保留最近 %s 张图，清理 %s 张"
                    "（共 %s 张；释放 base64 约 %.2f MB）",
                    keep, info["images_dropped"], info["images_total"],
                    info["dropped_base64_chars"] / 1048576,
                )
            else:
                logger.debug(
                    "[pc_control] 上下文图片 %s 张，未超过 keep_recent_images=%s，不清理",
                    info["images_total"], keep,
                )
        except Exception as exc:  # 瘦身失败绝不能影响正常请求
            logger.warning(f"[pc_control] 上下文瘦身钩子异常（已忽略）：{_short(exc)}")

    @filter.command("pcstatus")
    async def pcstatus(self, event: AstrMessageEvent):
        """查看本地电脑控制插件状态：依赖、屏幕、已注册工具、图片压缩与实例引用。"""
        deps = dependency_report()
        max_side, quality, keep = _image_cfg()
        lines = [f"PC 控制插件状态（{PLUGIN_NAME}）"]
        lines.append(f"  版本: v1.0.1 pid={os.getpid()}")
        lines.append(f"  实例引用自检: {_plugin_reference_check(self)}")
        for name, info in deps.items():
            flag = "OK" if info["available"] else "缺失"
            lines.append(f"  - {name}: {flag} {info['version']}".rstrip())
        try:
            screen = self.controller.screen_size()
            lines.append(
                f"  屏幕: {screen['width']}x{screen['height']}"
                f"（显示器 {len(screen['monitors'])} 个）"
            )
        except Exception as exc:
            lines.append(f"  屏幕: 获取失败 {exc}")
        lines.append(f"  已注册工具 {len(self.tools)} 个: " + ", ".join(t.name for t in self.tools))
        lines.append(
            f"  总开关={self.config.get('enable_control_tools', True)} "
            f"控制类需管理员={self.config.get('require_admin', True)} "
            f"FAILSAFE={self.config.get('failsafe', True)}"
        )
        lines.append(
            f"  图片策略: 模型看压缩版 max_image_side={max_side}px "
            f"image_jpeg_quality={quality}；发给用户=原图"
        )
        lines.append(f"  上下文瘦身: keep_recent_images={keep}（更早的图片换成占位文本）")
        lines.append(
            f"  截图目录清理: max_keep_screenshots={self.config.get('max_keep_screenshots', 30)} "
            f"screenshot_retention_days={self.config.get('screenshot_retention_days', 0)}"
        )
        folder = self.controller.screenshot_dir
        try:
            shots = list(folder.glob("shot-*.png")) if folder.exists() else []
            thumbs = list(folder.glob("shot-*-thumb.jpg")) if folder.exists() else []
            size_mb = sum(p.stat().st_size for p in shots) / 1048576
            lines.append(
                f"  截图目录: {folder}（{len(shots)} 张 PNG/{size_mb:.2f} MB，"
                f"{len(thumbs)} 张缩略图）"
            )
        except Exception as exc:
            lines.append(f"  截图目录: {folder}（统计失败 {exc}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("pcdiag")
    async def pcdiag(self, event: AstrMessageEvent):
        """在本进程内做一次无害自检：截屏 / 光标 / 剪贴板往返 / 窗口列表。

        /pcdiag input 会额外做一次鼠标移动往返测试（移到屏幕中心再还原）。
        """
        if not _cfg("enable_control_tools", True):
            yield event.plain_result("本地电脑控制插件已禁用（enable_control_tools=false）")
            return
        if _cfg("require_admin", True) and not _is_admin(event):
            yield event.plain_result(
                f"权限不足：/pcdiag 仅管理员可用。当前调用者 ID：{event.get_sender_id()}"
            )
            return
        try:
            extra = (event.message_str or "").lower()
            info = self.controller.diagnose(include_input="input" in extra)
        except Exception as exc:
            yield event.plain_result(f"诊断失败：{type(exc).__name__}: {exc}")
            return
        yield event.plain_result(
            "本地电脑控制 · 进程内自检（pid=%s）\n%s" % (info.get("pid"), _json(info))
        )


if __name__ == "__main__":
    # 供本地自检使用：打印工具清单与依赖情况（不连接 AstrBot）
    print("tools:", [t.name for t in build_tools()])
    print("deps:", _json(dependency_report()))
