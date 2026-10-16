# -*- coding: utf-8 -*-
"""PC 控制底层实现（不依赖 AstrBot 框架，可单独用 venv python 调用测试）。

覆盖：截屏、鼠标、键盘、剪贴板、窗口、程序启动/结束。
所有函数都是同步阻塞的，调用方（main.py）负责放进线程池执行。
"""

from __future__ import annotations

import base64
import io
import os
import importlib
import re
import shutil
import subprocess
import traceback
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# 依赖懒加载：装不上时要给出可读的降级提示，而不是 ImportError 崩掉整个插件
# ---------------------------------------------------------------------------

_IMPORT_ERRORS: dict[str, str] = {}


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - 环境相关
        _IMPORT_ERRORS[name] = f"{type(exc).__name__}: {exc}"
        return None


def dependency_report() -> dict[str, dict]:
    """返回各依赖的可用状态，供 /pcstatus 与报告使用。"""
    result: dict[str, dict] = {}
    for mod in ("mss", "PIL", "pyautogui", "pyperclip", "pygetwindow", "uiautomation", "psutil"):
        mod_obj = _try_import(mod)
        version = ""
        if mod_obj is not None:
            version = str(getattr(mod_obj, "__version__", "") or "")
        result[mod] = {
            "available": mod_obj is not None,
            "version": version,
            "error": _IMPORT_ERRORS.get(mod, ""),
        }
    return result


def _require(mod: str, purpose: str):
    obj = _try_import(mod)
    if obj is None:
        raise RuntimeError(
            f"缺少依赖 {mod}（用于 {purpose}），安装失败原因：{_IMPORT_ERRORS.get(mod)}。"
            f"请在该实例 venv 中执行 pip install {mod}"
        )
    return obj


DEFAULT_SCREENSHOT_DIR = str(Path(os.environ.get("TEMP", ".")) / "astrbot_pc_control_shots")

# —— 图片体积治理（v1.0.1）——
# 进模型上下文的一律是压缩版；发给用户的仍然是原图。
# 默认：最长边压到 1280px、JPEG 质量 75。
DEFAULT_MAX_IMAGE_SIDE = 1280
DEFAULT_IMAGE_JPEG_QUALITY = 75


def thumbnail_path_for(src) -> Path:
    """原图路径 -> 同目录缩略图路径（shot-xxx.png -> shot-xxx-thumb.jpg）。"""
    src = Path(src)
    return src.with_name(f"{src.stem}-thumb.jpg")


def _pil_image():
    """懒加载 Pillow.Image；缺了返回 None（调用方走可读降级）。"""
    try:
        from PIL import Image  # type: ignore
        return Image
    except Exception as exc:  # pragma: no cover - 环境相关
        _IMPORT_ERRORS.setdefault("PIL.Image", f"{type(exc).__name__}: {exc}")
        return None


def image_dimensions(path) -> tuple[int, int]:
    """读图片尺寸；失败返回 (0, 0)，不抛异常。"""
    Image = _pil_image()
    if Image is None:
        return (0, 0)
    try:
        with Image.open(path) as im:
            return (int(im.size[0]), int(im.size[1]))
    except Exception:
        return (0, 0)


def _mime_for(path) -> str:
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "bmp": "image/bmp", "webp": "image/webp", "gif": "image/gif",
    }.get(Path(path).suffix.lower().lstrip("."), "image/png")


def _smaller_wins(src: Path, data: bytes, mime: str, w: int, h: int,
                  source: str, original_bytes: int,
                  original_wh: tuple[int, int] = (0, 0)) -> dict:
    """安全阀：若压缩结果反而比原图大（低分辨率小 PNG 很常见），就发原图那份。

    「进模型上下文的一定不比原图大」——压缩是为了省体积，不是为了省体积反而变大。
    """
    if original_bytes and len(data) >= original_bytes:
        try:
            raw = src.read_bytes()
        except Exception:
            raw = b""
        if raw:
            ow, oh = original_wh if original_wh != (0, 0) else image_dimensions(src)
            return {
                "base64": base64.b64encode(raw).decode("ascii"),
                "mime_type": _mime_for(src),
                "width": ow,
                "height": oh,
                "bytes": len(raw),
                "source": source + "+原图更小",
                "original_bytes": original_bytes,
                "original_width": ow,
                "original_height": oh,
            }
    return {
        "base64": base64.b64encode(data).decode("ascii"),
        "mime_type": mime,
        "width": w,
        "height": h,
        "bytes": len(data),
        "source": source,
        "original_bytes": original_bytes,
        "original_width": original_wh[0],
        "original_height": original_wh[1],
    }


def encode_image_for_llm(
    path: str,
    max_side: int = DEFAULT_MAX_IMAGE_SIDE,
    quality: int = DEFAULT_IMAGE_JPEG_QUALITY,
    preferred_thumb: str | None = None,
) -> dict:
    """把本地图片转成「喂给模型」的压缩版 base64（v1.0.1 核心）。

    设计要点：
    - **进模型上下文的一律是压缩版**：最长边 <= max_side 的 JPEG（默认 1280px / q75）；
    - **发给用户的仍然是原图**：原图 PNG 由 pc_screenshot 的 send_to_user 走文件路径发送；
    - 抓屏阶段已经把缩略图写到磁盘时（preferred_thumb），直接读它 ——
      避免为了压缩再把几 MB 的原图读进内存、解码、重编码（那正是 413 的根源之一）；
    - 安全阀：若压缩结果反而比原图大，则用原图（低分辨率小 PNG 会遇到）。

    返回 dict：
      base64 / mime_type / width / height / bytes / source
      / original_bytes / original_width / original_height
    source ∈ {"thumbnail", "reencoded", "original"}，后缀 "+原图更小" 表示走了安全阀。
    """
    src = Path(path)
    original_bytes = src.stat().st_size if src.exists() else 0
    max_side = max(0, int(max_side))
    quality = max(1, min(95, int(quality)))

    if preferred_thumb:
        tp = Path(preferred_thumb)
        try:
            if tp.exists() and tp.stat().st_size > 0:
                data = tp.read_bytes()
                w, h = image_dimensions(tp)
                return _smaller_wins(
                    src, data, "image/jpeg", w, h, "thumbnail",
                    original_bytes, image_dimensions(src),
                )
        except Exception:
            pass  # 缩略图读失败就退回现场重压

    Image = _pil_image()
    if Image is None or not src.exists():
        data = src.read_bytes() if src.exists() else b""
        w, h = image_dimensions(src)
        return {
            "base64": base64.b64encode(data).decode("ascii"),
            "mime_type": _mime_for(src),
            "width": w,
            "height": h,
            "bytes": len(data),
            "source": "original",
            "original_bytes": original_bytes,
            "original_width": w,
            "original_height": h,
        }

    with Image.open(src) as im:
        ow, oh = int(im.size[0]), int(im.size[1])
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if max_side > 0 and max(ow, oh) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        w, h = int(im.size[0]), int(im.size[1])
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
    return _smaller_wins(
        src, buf.getvalue(), "image/jpeg", w, h, "reencoded",
        original_bytes, (ow, oh),
    )


def write_thumbnail(
    src, max_side: int = DEFAULT_MAX_IMAGE_SIDE,
    quality: int = DEFAULT_IMAGE_JPEG_QUALITY, target=None,
) -> dict | None:
    """在抓屏落盘阶段顺手产出缩略图（JPEG）。失败/未开启返回 None，不抛异常。"""
    if int(max_side) <= 0 or not Path(src).exists():
        return None
    Image = _pil_image()
    if Image is None:
        return None
    dst = Path(target) if target else thumbnail_path_for(src)
    try:
        src_size = Path(src).stat().st_size
        with Image.open(src) as im:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            if max(int(im.size[0]), int(im.size[1])) > int(max_side):
                im.thumbnail((int(max_side), int(max_side)), Image.LANCZOS)
            w, h = int(im.size[0]), int(im.size[1])
            im.save(str(dst), format="JPEG", quality=max(1, min(95, int(quality))), optimize=True)
        if dst.stat().st_size >= src_size:
            # 压出来反而更大（小尺寸截图常见）：不占磁盘，交给 encode_image_for_llm 用原图
            dst.unlink()
            return None
        return {
            "path": str(dst),
            "width": w,
            "height": h,
            "size_bytes": dst.stat().st_size,
        }
    except Exception:
        try:
            dst.unlink()
        except Exception:
            pass
        return None

# 严禁杀掉的系统关键进程（即使 force=True 也要用户手改代码才能绕过）
_PROTECTED_PROCESSES = {
    "system", "registry", "memory compression", "idle",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "smss.exe", "svchost.exe", "explorer.exe", "dwm.exe", "fontdrvhost.exe",
    "audiodg.exe", "conhost.exe", "sihost.exe", "taskhostw.exe",
}


_WIN_APIS_CACHE: dict[str, tuple] = {}


def _win_apis():
    """返回显式声明了 argtypes/restype 的 (user32, gdi32, wintypes)。

    坑：不要直接用 `ctypes.windll.user32`。uiautomation / mss 等库会把
    共享 DLL 对象上的 `restype` 改成 `c_void_p`，于是句柄变成 64 位大整数，
    再传给那些仍按默认 `c_int` 转换的 GDI/User 函数时就会抛
    `ctypes.ArgumentError: argument 1: OverflowError: int too long to convert`。
    自己 `WinDLL()` 出来的独立实例 + 显式签名可以彻底避免这种串扰。
    """
    if "apis" in _WIN_APIS_CACHE:
        return _WIN_APIS_CACHE["apis"]
    import ctypes
    import ctypes.wintypes as wt
    from ctypes import POINTER, WINFUNCTYPE, c_int, c_void_p

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    enum_proc = WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetDesktopWindow.argtypes = []
    user32.GetDesktopWindow.restype = wt.HWND
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindow.restype = wt.BOOL
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, c_int]
    user32.GetWindowTextW.restype = c_int
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, c_int]
    user32.GetClassNameW.restype = c_int
    user32.GetWindowRect.argtypes = [wt.HWND, POINTER(wt.RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetClientRect.argtypes = [wt.HWND, POINTER(wt.RECT)]
    user32.GetClientRect.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.EnumWindows.argtypes = [enum_proc, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.BringWindowToTop.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, c_int]
    user32.ShowWindow.restype = wt.BOOL
    user32.SetWindowPos.argtypes = [
        wt.HWND, wt.HWND, c_int, c_int, c_int, c_int, wt.UINT
    ]
    user32.SetWindowPos.restype = wt.BOOL
    user32.SetProcessDPIAware.argtypes = []
    user32.SetProcessDPIAware.restype = wt.BOOL
    user32.GetWindowDC.argtypes = [wt.HWND]
    user32.GetWindowDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.ReleaseDC.restype = c_int
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    user32.PrintWindow.restype = wt.BOOL
    user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    user32.OpenInputDesktop.restype = wt.HANDLE
    user32.CloseDesktop.argtypes = [wt.HANDLE]
    user32.CloseDesktop.restype = wt.BOOL

    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, c_int, c_int]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.GetDIBits.argtypes = [
        wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, c_void_p, c_void_p, wt.UINT
    ]
    gdi32.GetDIBits.restype = c_int
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteObject.restype = wt.BOOL
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.DeleteDC.restype = wt.BOOL

    _WIN_APIS_CACHE["apis"] = (user32, gdi32, wt, enum_proc)
    return _WIN_APIS_CACHE["apis"]


def _normalize_hwnd(hwnd) -> int:
    """把可能是 c_void_p / None / int 的窗口句柄统一成 int。"""
    if hwnd is None:
        return 0
    if isinstance(hwnd, int):
        return hwnd
    try:
        return int(hwnd)
    except Exception:
        try:
            return int(hwnd.value or 0)
        except Exception:
            return 0


class PCControlError(RuntimeError):
    """业务层可见的错误。"""


class PCController:
    """本地电脑控制的具体实现。"""

    def __init__(
        self,
        screenshot_dir: str | None = None,
        failsafe: bool = True,
        pause: float = 0.05,
        type_text_by_clipboard: bool = True,
        restore_clipboard_after_type: bool = True,
        thumb_max_side: int = DEFAULT_MAX_IMAGE_SIDE,
        thumb_jpeg_quality: int = DEFAULT_IMAGE_JPEG_QUALITY,
    ) -> None:
        self.screenshot_dir = Path(screenshot_dir or DEFAULT_SCREENSHOT_DIR)
        self.failsafe = bool(failsafe)
        self.pause = max(0.0, float(pause))
        self.type_text_by_clipboard = bool(type_text_by_clipboard)
        self.restore_clipboard_after_type = bool(restore_clipboard_after_type)
        # 缩略图参数：>0 时每次抓屏顺带产出 <name>-thumb.jpg，供「喂给模型」用
        self.thumb_max_side = max(0, int(thumb_max_side))
        self.thumb_jpeg_quality = max(1, min(95, int(thumb_jpeg_quality)))
        self._gui = None

    def thumbnail_for(self, path) -> dict | None:
        """为已落盘的原图产出/复用缩略图。"""
        if self.thumb_max_side <= 0:
            return None
        src = Path(path)
        dst = thumbnail_path_for(src)
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            w, h = image_dimensions(dst)
            return {"path": str(dst), "width": w, "height": h, "size_bytes": dst.stat().st_size}
        return write_thumbnail(
            src, self.thumb_max_side, self.thumb_jpeg_quality, target=dst
        )

    def _with_thumbnail(self, result: dict, path) -> dict:
        thumb = self.thumbnail_for(path)
        if thumb:
            result["thumb_path"] = thumb["path"]
            result["thumb_width"] = thumb["width"]
            result["thumb_height"] = thumb["height"]
            result["thumb_size_bytes"] = thumb["size_bytes"]
        return result

    # ------------------------------------------------------------------ gui
    @property
    def gui(self):
        """懒加载 pyautogui，并设置 FAILSAFE / PAUSE。"""
        if self._gui is None:
            pag = _require("pyautogui", "鼠标键盘控制")
            pag.FAILSAFE = self.failsafe
            pag.PAUSE = self.pause
            self._gui = pag
        else:
            self._gui.FAILSAFE = self.failsafe
        return self._gui

    # --------------------------------------------------------------- 屏幕
    def screen_size(self) -> dict:
        import ctypes

        user32, _, _, _ = _win_apis()
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        get_metrics = ctypes.windll.user32.GetSystemMetrics
        w = int(get_metrics(0))
        h = int(get_metrics(1))
        virtual_w = int(get_metrics(78))
        virtual_h = int(get_metrics(79))
        return {
            "width": w,
            "height": h,
            "virtual_width": virtual_w,
            "virtual_height": virtual_h,
            "primary": {"left": 0, "top": 0, "width": w, "height": h},
            "monitors": self._monitors(),
        }

    def _monitors(self) -> list[dict]:
        mss = _try_import("mss")
        if mss is None:
            return []
        try:
            with mss.mss() as sct:
                return [
                    {
                        "index": i,
                        "left": int(m["left"]),
                        "top": int(m["top"]),
                        "width": int(m["width"]),
                        "height": int(m["height"]),
                    }
                    for i, m in enumerate(sct.monitors)
                ]
        except Exception:
            return []

    def session_info(self) -> dict:
        """当前 Windows 会话状态。input_desktop_available=False 时，

        所有鼠标/键盘 API 都会被系统拒绝（ERROR_ACCESS_DENIED），
        典型场景是远程桌面会话已断开或尚未登录交互桌面。
        """
        import ctypes

        try:
            user32, _, _, _ = _win_apis()
            pid = ctypes.windll.kernel32.GetCurrentProcessId()
            sid = ctypes.c_ulong(0)
            ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
            input_desktop = user32.OpenInputDesktop(0, False, 0x0100)
            if input_desktop:
                user32.CloseDesktop(input_desktop)
            try:
                cursor_corner = self.mouse_position() == {"x": 0, "y": 0}
            except Exception:
                cursor_corner = None
            return {
                "session_id": int(sid.value),
                "input_desktop_available": bool(input_desktop),
                "foreground_hwnd": self._foreground_handle(),
                "cursor_at_origin": cursor_corner,
            }
        except Exception as exc:  # pragma: no cover
            return {"error": str(exc)}

    def _desktop_session_state(self) -> str:
        """返回当前会话桌面状态描述（用于诊断整屏抓取失败的原因）。"""
        info = self.session_info()
        return (
            f"session_id={info.get('session_id')}, "
            f"input_desktop={'ok' if info.get('input_desktop_available') else 'unavailable'}, "
            f"foreground_hwnd={info.get('foreground_hwnd')}"
        )

    # ---------------------------------------------------- 单窗口截取（PrintWindow）
    def _print_window_image(self, hwnd: int):
        """用 PrintWindow 把窗口画面渲染到内存位图；成功返回 PIL.Image。"""
        import ctypes

        user32, gdi32, wt, _ = _win_apis()
        PIL_Image = _require("PIL.Image", "窗口截屏")
        hwnd = _normalize_hwnd(hwnd)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD),
            ]

        rect = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise PCControlError(f"GetWindowRect 失败（窗口 {hwnd}）")
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            raise PCControlError(f"窗口 {hwnd} 尺寸无效：{width}x{height}")
        hdc = user32.GetWindowDC(hwnd)
        if not hdc:
            raise PCControlError(f"GetWindowDC 失败（窗口 {hwnd}）")
        memdc = gdi32.CreateCompatibleDC(hdc)
        bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
        try:
            if not memdc or not bitmap:
                raise PCControlError("创建兼容 DC/位图失败")
            gdi32.SelectObject(memdc, bitmap)
            # PW_RENDERFULLCONTENT = 2，可抓到 DWM 合成内容
            if not user32.PrintWindow(hwnd, memdc, 2):
                raise PCControlError(f"PrintWindow 失败（窗口 {hwnd}）")
            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0
            buf = ctypes.create_string_buffer(width * height * 4)
            got = gdi32.GetDIBits(
                memdc, bitmap, 0, height, buf, ctypes.byref(header), 0
            )
            if not got:
                raise PCControlError("GetDIBits 失败")
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memdc:
                gdi32.DeleteDC(memdc)
            user32.ReleaseDC(hwnd, hdc)
        return PIL_Image.frombuffer(
            "RGBA", (width, height), buf, "raw", "BGRA", 0, 1
        ).convert("RGB")

    def _enum_top_windows(self) -> list[dict]:
        """枚举顶层可见窗口（pygetwindow 在无前台窗口时不够用）。"""
        import ctypes

        user32, _, wt, enum_proc = _win_apis()
        results: list[dict] = []

        def _cb(hwnd, lparam):
            hwnd = _normalize_hwnd(hwnd)
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            results.append({
                "handle": hwnd,
                "title": buf.value,
                "class_name": cls.value,
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
                "minimized": bool(user32.IsIconic(hwnd)),
            })
            return True

        user32.EnumWindows(enum_proc(_cb), 0)
        return results

    _SKIP_WINDOW_CLASSES = {
        "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
        "Windows.UI.Core.CoreWindow", "ApplicationFrameWindow_x",
        "DV2ControlHost", "MsgrIMEWindowClass",
    }

    def _candidate_windows(self, limit: int = 8) -> list[dict]:
        """挑出最可能是“用户正在看的窗口”的候选项，按面积从大到小。"""
        wins = []
        for win in self._enum_top_windows():
            if win["class_name"] in self._SKIP_WINDOW_CLASSES:
                continue
            area = win["width"] * win["height"]
            if win["width"] < 120 or win["height"] < 120:
                continue
            win["area"] = area
            wins.append(win)
        titled = [w for w in wins if w["title"].strip()]
        pool = titled or wins
        pool.sort(key=lambda w: w["area"], reverse=True)
        return pool[:limit]

    def _resolve_target_hwnd(self, handle=None, title=None, use_foreground=True) -> int:
        if handle:
            return int(handle)
        if title:
            return self._find_window_handle(title)
        if use_foreground:
            hwnd = self._foreground_handle()
            if hwnd:
                return hwnd
        raise PCControlError("当前没有前台窗口，需要显式指定 window_title / handle")

    def capture_window(
        self,
        handle: int | None = None,
        title: str | None = None,
        use_foreground: bool = True,
        filename: str | None = None,
    ) -> dict:
        """截取单个窗口（PrintWindow）。整屏抓取不可用时的降级方案。

        未指定窗口时按“前台窗口 -> 面积最大的可见窗口”依次尝试，
        并跳过 PrintWindow 返回空白图的窗口。
        """
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        name = filename or f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        path = self.screenshot_dir / name

        tried: list[str] = []
        last_error: Exception | None = None
        candidates: list[tuple[int, str]] = []
        if handle or title:
            candidates.append((self._resolve_target_hwnd(handle, title, use_foreground), title or ""))
        else:
            fg = self._foreground_handle()
            if fg:
                candidates.append((fg, self._window_title(fg)))
            for win in self._candidate_windows():
                if win["handle"] == fg:
                    continue
                candidates.append((win["handle"], win["title"]))
        if not candidates:
            raise PCControlError("找不到任何可截取的窗口")

        for hwnd, label in candidates:
            try:
                img = self._print_window_image(hwnd)
            except Exception as exc:
                last_error = exc
                tb = traceback.format_exc(limit=3).strip().splitlines()[-1]
                tried.append(f"{hwnd}（PrintWindow 失败：{exc} @ {tb}）")
                continue
            colors = img.convert("L").getextrema()
            if colors[0] == colors[1]:
                # 纯色图，多半是合成器返回的空图，换下一个窗口
                tried.append(f"{hwnd}（空图）")
                continue
            img.save(str(path))
            return self._with_thumbnail({
                "path": str(path),
                "width": img.size[0],
                "height": img.size[1],
                "size_bytes": path.stat().st_size,
                "backend": "printwindow",
                "window": self._window_title(hwnd) or label,
                "handle": hwnd,
                "tried": tried,
            }, path)
        raise PCControlError(
            f"所有候选窗口都无法截取：{'；'.join(tried) or last_error}"
        )

    def _blank_warning(self, path) -> str | None:
        """截图是否几乎纯色（全黑/全白）——断开的远程会话/锁屏时的典型症状。"""
        try:
            PIL_Image = _try_import("PIL.Image")
            ImageStat = _try_import("PIL.ImageStat")
            if PIL_Image is None or ImageStat is None:
                return None
            with PIL_Image.open(str(path)) as img:
                stat = ImageStat.Stat(img.convert("L"))
                spread = float(stat.stddev[0]) if stat.stddev else 0.0
            if spread < 1.0:
                return (
                    "截图像是一张纯色图（没有真实画面内容），通常说明当前 Windows 会话没有可渲染的桌面"
                    "（远程桌面断开/锁屏/未登录交互桌面）。"
                    f"会话状态：{self._desktop_session_state()}"
                )
        except Exception:
            return None
        return None

    def screenshot(
        self,
        monitor: int = 0,
        region: dict | None = None,
        filename: str | None = None,
        target: str = "desktop",
        window_title: str | None = None,
        window_handle: int | None = None,
    ) -> dict:
        """截屏并保存 png，返回文件路径与分辨率。

        monitor: 0=所有显示器拼接，1..n=单个显示器（见 screen_size().monitors）
        region: {"left","top","width","height"}，给定时优先于 monitor
        target: "desktop"=整屏；"active_window"/"window"=用 PrintWindow 抓单个窗口
        """
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        if target in ("active_window", "window"):
            return self.capture_window(
                handle=window_handle,
                title=window_title,
                use_foreground=(target == "active_window"),
                filename=filename,
            )

        name = filename or f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        path = self.screenshot_dir / name

        mss = _try_import("mss")
        width = height = 0
        grab_error: Exception | None = None
        if mss is not None:
            try:
                with mss.mss() as sct:
                    monitors = sct.monitors
                    if region:
                        box = {
                            "left": int(region["left"]),
                            "top": int(region["top"]),
                            "width": int(region["width"]),
                            "height": int(region["height"]),
                        }
                    else:
                        idx = int(monitor)
                        if idx < 0 or idx >= len(monitors):
                            raise PCControlError(
                                f"monitor 索引 {idx} 超出范围，可用 0..{len(monitors) - 1}"
                            )
                        box = monitors[idx]
                    grab = sct.grab(box)
                    width, height = int(grab.width), int(grab.height)
                    raw = mss.tools.to_png(grab.rgb, grab.size)
                path.write_bytes(raw)
            except Exception as exc:
                grab_error = exc
        else:
            try:
                pag = self.gui
                img = pag.screenshot(
                    region=(region["left"], region["top"], region["width"], region["height"])
                    if region
                    else None
                )
                img.save(str(path))
                width, height = img.size
            except Exception as exc:
                grab_error = exc

        if grab_error is not None or not path.exists() or path.stat().st_size <= 0:
            # 降级：断开的远程会话 / 锁屏 / 无输入桌面时 BitBlt 会失败，
            # 此时用 PrintWindow 抓前台窗口，至少能拿到画面。
            fallback_note = (
                f"整屏抓取失败（{type(grab_error).__name__ if grab_error else 'unknown'}: "
                f"{grab_error if grab_error else '空文件'}）；"
                f"已降级为 PrintWindow 抓取前台窗口。桌面状态：{self._desktop_session_state()}"
            )
            try:
                shot = self.capture_window(use_foreground=True, filename=name)
            except Exception as fallback_exc:
                raise PCControlError(
                    f"{fallback_note}；PrintWindow 降级同样失败：{fallback_exc}"
                ) from fallback_exc
            shot["fallback"] = True
            shot["note"] = fallback_note
            shot["requested_monitor"] = monitor
            warn = self._blank_warning(shot.get("path", path))
            if warn:
                shot["warning"] = warn
            return self._with_thumbnail(shot, shot.get("path", path))

        size_bytes = path.stat().st_size
        result = {
            "path": str(path),
            "width": width,
            "height": height,
            "size_bytes": size_bytes,
            "monitor": monitor if not region else "region",
            "backend": "mss" if mss is not None else "pyautogui",
        }
        warn = self._blank_warning(path)
        if warn:
            result["warning"] = warn
        return self._with_thumbnail(result, path)

    def _find_window_handle(self, title: str) -> int:
        """优先用 Win32 精确查找，找不到再退回 pygetwindow。"""
        needle = (title or "").lower()
        if not needle:
            raise PCControlError("窗口标题关键词为空")
        matches = [
            w for w in self._enum_top_windows()
            if needle in (w["title"] or "").lower()
        ]
        if matches:
            matches.sort(key=lambda w: w["width"] * w["height"], reverse=True)
            return int(matches[0]["handle"])
        gw = _try_import("pygetwindow")
        if gw is not None:
            try:
                others = [
                    w for w in gw.getAllWindows()
                    if needle in (w.title or "").lower()
                ]
                if others:
                    return _normalize_hwnd(getattr(others[0], "_hWnd", 0))
            except Exception:
                pass
        raise PCControlError(f"没有找到标题含 “{title}” 的窗口")

    def _window_title(self, hwnd: int) -> str:
        import ctypes

        user32, _, _, _ = _win_apis()
        hwnd = _normalize_hwnd(hwnd)
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    # --------------------------------------------------------------- 鼠标
    def _run(self, func, *args, **kwargs):
        """执行 pyautogui 动作，并把 FAILSAFE 拦截翻译成可读错误。"""
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if type(exc).__name__ == "FailSafeException":
                try:
                    pos = self.mouse_position()
                except Exception:  # pragma: no cover
                    pos = {"x": "?", "y": "?"}
                info = self.session_info()
                extra = ""
                if not info.get("input_desktop_available", True):
                    extra = (
                        f" 另外检测到当前会话（session {info.get('session_id')}）没有输入桌面，"
                        "系统会拒绝一切鼠标/键盘 API（ERROR_ACCESS_DENIED，last error 5）——"
                        "常见于远程桌面已断开/未登录交互桌面的情形，此时任何自动化操作都执行不了。"
                    )
                raise PCControlError(
                    "pyautogui FAILSAFE 拦截了本次操作：鼠标当前位于屏幕角落 "
                    f"({pos['x']}, {pos['y']})，FAILSAFE 把它当成用户急停信号。"
                    "若当前会话的光标本来就卡在角落（无交互/断开的远程桌面会话很常见），"
                    "需要先人工把鼠标移离角落，或在插件配置里关闭 failsafe（不推荐）。"
                    + extra
                ) from exc
            raise

    def mouse_position(self) -> dict:
        x, y = self.gui.position()
        return {"x": int(x), "y": int(y)}

    def mouse_move(self, x: int, y: int, duration: float = 0.2) -> dict:
        self._run(self.gui.moveTo, int(x), int(y), duration=max(0.0, float(duration)))
        pos = self.gui.position()
        result = {"moved_to": [int(x), int(y)], "x": int(pos.x), "y": int(pos.y)}
        if abs(int(pos.x) - int(x)) > 2 or abs(int(pos.y) - int(y)) > 2:
            # 无输入桌面时 SetCursorPos 会静默失败：pyautogui 不报错，但光标没动。
            result["warning"] = (
                f"鼠标没有真的移动（当前仍在 {int(pos.x)},{int(pos.y)}）："
                "系统拒绝了光标移动，本次操作相当于空操作。"
                f"会话状态：{self._desktop_session_state()}。"
                "常见原因是远程桌面已断开/未登录交互桌面，此时鼠标键盘全部无效。"
            )
        return result

    def mouse_click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.1,
    ) -> dict:
        button = (button or "left").lower()
        if button not in ("left", "right", "middle"):
            raise PCControlError(f"不支持的按键：{button}（可用 left/right/middle）")
        if x is not None and y is not None:
            self._run(self.gui.moveTo, int(x), int(y), duration=0.1)
            moved = self.gui.position()
            if abs(int(moved.x) - int(x)) > 2 or abs(int(moved.y) - int(y)) > 2:
                return {
                    "warning": (
                        f"点击未生效：鼠标没能移动到目标点 ({int(x)},{int(y)})，"
                        f"当前仍在 ({int(moved.x)},{int(moved.y)})。"
                        "系统拒绝了鼠标操作，本次点击是空操作。"
                        f"会话状态：{self._desktop_session_state()}"
                    )
                }
        clicks = max(1, int(clicks))
        self._run(self.gui.click, button=button, clicks=clicks, interval=float(interval))
        pos = self.gui.position()
        return {
            "clicked_at": [int(pos.x), int(pos.y)],
            "button": button,
            "clicks": clicks,
            "double": clicks >= 2,
        }

    def mouse_drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        button: str = "left",
        duration: float = 0.5,
    ) -> dict:
        self._run(self.gui.moveTo, int(x1), int(y1), duration=0.1)
        self._run(
            self.gui.dragTo, int(x2), int(y2),
            duration=max(0.05, float(duration)), button=button,
        )
        return {"from": [int(x1), int(y1)], "to": [int(x2), int(y2)], "button": button}

    def mouse_scroll(self, amount: int, x: int | None = None, y: int | None = None) -> dict:
        if x is not None and y is not None:
            self._run(self.gui.moveTo, int(x), int(y), duration=0.1)
        self._run(self.gui.scroll, int(amount))
        return {"scrolled": int(amount)}

    # --------------------------------------------------------------- 键盘
    @staticmethod
    def _normalize_keys(keys) -> list[str]:
        if isinstance(keys, str):
            parts = [p.strip() for p in re.split(r"[+\s]*\+[\s]*|\s+", keys) if p.strip()]
        else:
            parts = [str(k).strip() for k in keys if str(k).strip()]
        if not parts:
            raise PCControlError("未提供任何按键")
        return parts

    def key_hotkey(self, keys) -> dict:
        parts = self._normalize_keys(keys)
        if len(parts) == 1:
            self._run(self.gui.press, parts[0])
        else:
            self._run(self.gui.hotkey, *parts)
        return {"keys": parts}

    def key_press(self, key: str, presses: int = 1, interval: float = 0.05) -> dict:
        self._run(
            self.gui.press, str(key),
            presses=max(1, int(presses)), interval=float(interval),
        )
        return {"key": key, "presses": max(1, int(presses))}

    def type_text(self, text: str) -> dict:
        """输入文本。中文等非 ASCII 走剪贴板 + Ctrl+V（pyautogui 打不出来）。"""
        text = "" if text is None else str(text)
        if not text:
            raise PCControlError("text 为空")
        pyperclip = _require("pyperclip", "剪贴板")
        is_ascii = all(ord(ch) < 128 for ch in text)
        if is_ascii:
            self._run(self.gui.write, text, interval=0.02)
            return {"typed": text, "method": "typewrite", "length": len(text)}
        if not self.type_text_by_clipboard:
            raise PCControlError("文本含非 ASCII 字符且已禁用剪贴板输入方式，无法输入中文")
        old = None
        if self.restore_clipboard_after_type:
            try:
                old = pyperclip.paste()
            except Exception:
                old = None
        pyperclip.copy(text)
        time.sleep(0.1)
        self._run(self.gui.hotkey, "ctrl", "v")
        time.sleep(0.15)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
        return {"typed": text, "method": "clipboard+ctrl_v", "length": len(text)}

    # ------------------------------------------------------------- 剪贴板
    def clipboard_read(self) -> dict:
        pyperclip = _require("pyperclip", "剪贴板")
        try:
            content = pyperclip.paste()
        except Exception as exc:
            raise PCControlError(f"读取剪贴板失败：{exc}") from exc
        return {"text": content, "length": len(content or "")}

    def clipboard_write(self, text: str) -> dict:
        pyperclip = _require("pyperclip", "剪贴板")
        try:
            pyperclip.copy("" if text is None else str(text))
        except Exception as exc:
            raise PCControlError(f"写入剪贴板失败：{exc}") from exc
        return {"written_length": len(str(text or ""))}

    # --------------------------------------------------------------- 诊断
    def diagnose(self, include_input: bool = False) -> dict:
        """在**本进程内**跑一遍无害自检：截屏 / 光标坐标 / 剪贴板往返 / 窗口列表。

        排查「为什么截屏失败 / 鼠标键盘没反应」时最有用：
        同一个插件在 AstrBot 主进程里和在外部子进程里，看到的 Windows 会话/桌面可能不同。
        """
        import os

        info: dict = {"pid": os.getpid()}
        try:
            info["session"] = self.session_info()
        except Exception as exc:
            info["session"] = {"error": str(exc)}
        try:
            info["screen"] = self.screen_size()
        except Exception as exc:
            info["screen"] = {"error": str(exc)}
        try:
            info["cursor"] = self.mouse_position()
        except Exception as exc:
            info["cursor"] = {"error": str(exc)}
        try:
            shot = self.screenshot()
            info["screenshot"] = {
                "path": shot.get("path"),
                "width": shot.get("width"),
                "height": shot.get("height"),
                "backend": shot.get("backend"),
                "warning": shot.get("warning"),
                "note": shot.get("note"),
            }
        except Exception as exc:
            info["screenshot"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            original = self.clipboard_read().get("text", "")
            marker = "pc_control_diag"
            self.clipboard_write(marker)
            back = self.clipboard_read().get("text", "")
            self.clipboard_write(original)
            info["clipboard"] = {
                "roundtrip_ok": back == marker,
                "read_back": back,
                "restored_chars": len(original or ""),
            }
        except Exception as exc:
            info["clipboard"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            windows = self.list_windows().get("windows", [])
            info["windows"] = {
                "count": len(windows),
                "sample": [w.get("title") for w in windows[:5]],
            }
        except Exception as exc:
            info["windows"] = {"error": f"{type(exc).__name__}: {exc}"}
        if include_input:
            # 只做「移动光标」这一种无害动作：移到屏幕中心，读回坐标，再还原到原位置。
            try:
                before = self.mouse_position()
                screen = self.screen_size()
                target = (int(screen["width"]) // 2, int(screen["height"]) // 2)
                if abs(target[0] - int(before["x"])) < 8 and abs(target[1] - int(before["y"])) < 8:
                    # 光标本来就在中心：换个点，避免「移动到自己」这种无效验证
                    target = (
                        max(0, min(int(screen["width"]) - 2, target[0] + 137)),
                        max(0, min(int(screen["height"]) - 2, target[1] + 89)),
                    )
                moved = self.mouse_move(target[0], target[1], duration=0.05)
                self.mouse_move(int(before["x"]), int(before["y"]), duration=0.05)
                info["mouse_move"] = {
                    "target": list(target),
                    "read_back": [moved.get("x"), moved.get("y")],
                    "warning": moved.get("warning"),
                    "restored_to": [before["x"], before["y"]],
                }
            except Exception as exc:
                info["mouse_move"] = {"error": f"{type(exc).__name__}: {exc}"}

        return info

    # --------------------------------------------------------------- 窗口
    def list_windows(self, include_empty_title: bool = False) -> dict:
        """列出可见窗口。主路径用自己的 Win32 枚举（稳定），pygetwindow 仅作兜底。"""
        fg = self._foreground_handle()
        windows: list[dict] = []
        for win in self._enum_top_windows():
            title = (win.get("title") or "").strip()
            if not title and not include_empty_title:
                continue
            windows.append({
                "title": title,
                "handle": int(win["handle"]),
                "class_name": win.get("class_name", ""),
                "visible": True,
                "minimized": bool(win.get("minimized")),
                "active": int(win["handle"]) == fg,
                "rect": {
                    "left": int(win.get("left", 0)),
                    "top": int(win.get("top", 0)),
                    "width": int(win["width"]),
                    "height": int(win["height"]),
                },
            })
        if not windows:
            gw = _try_import("pygetwindow")
            if gw is not None:
                for win in gw.getAllWindows():
                    try:
                        title = (win.title or "").strip()
                        if not title and not include_empty_title:
                            continue
                        windows.append({
                            "title": title,
                            "handle": _normalize_hwnd(getattr(win, "_hWnd", 0)),
                            "class_name": "",
                            "visible": bool(getattr(win, "visible", True)),
                            "minimized": bool(getattr(win, "isMinimized", False)),
                            "active": bool(getattr(win, "isActive", False)),
                            "rect": None,
                        })
                    except Exception:
                        continue
        windows.sort(key=lambda w: w["rect"]["width"] * w["rect"]["height"]
                     if w.get("rect") else 0, reverse=True)
        return {"count": len(windows), "windows": windows, "foreground_hwnd": fg}

    def _foreground_handle(self) -> int:
        user32, _, _, _ = _win_apis()
        return _normalize_hwnd(user32.GetForegroundWindow())

    def _hwnd_pid(self, hwnd: int) -> int:
        import ctypes

        user32, _, wt, _ = _win_apis()
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(_normalize_hwnd(hwnd), ctypes.byref(pid))
        return int(pid.value)

    def _process_name(self, pid: int) -> str:
        psutil = _try_import("psutil")
        if psutil is None:
            return ""
        try:
            return psutil.Process(pid).name()
        except Exception:
            return ""

    def active_window(self, read_browser_url: bool = True) -> dict:
        hwnd = self._foreground_handle()
        if not hwnd:
            candidates = self._candidate_windows(limit=1)
            return {
                "title": None,
                "handle": 0,
                "pid": 0,
                "process": "",
                "is_browser": False,
                "error": "当前会话没有前台窗口（远程桌面会话断开或未登录交互桌面）",
                "best_candidate": candidates[0] if candidates else None,
            }
        title = self._window_title(hwnd)
        pid = self._hwnd_pid(hwnd)
        proc = self._process_name(pid)
        info = {
            "title": title,
            "handle": hwnd,
            "pid": pid,
            "process": proc,
            "is_browser": proc.lower() in {
                "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                "opera.exe", "vivaldi.exe", "360se.exe", "qqbrowser.exe",
            },
        }
        if read_browser_url and info["is_browser"]:
            url = self.browser_url(hwnd)
            if url:
                info["url"] = url
                info["url_source"] = "uiautomation"
            else:
                info["url"] = None
                info["url_note"] = "浏览器地址栏读取失败（uiautomation 不可用或控件结构不匹配）"
        return info

    def browser_url(self, hwnd: int | None = None) -> str | None:
        """尽力用 uiautomation 读取浏览器地址栏；失败返回 None，不抛异常。"""
        auto = _try_import("uiautomation")
        if auto is None:
            return None
        try:
            control = (
                auto.ControlFromHandle(int(hwnd)) if hwnd else auto.GetForegroundControl()
            )
            if control is None:
                return None
            pattern = re.compile(r"(地址|address|搜索栏|search bar|omnibox|网址|url)", re.I)
            stack = [(control, 0)]
            visited = 0
            while stack and visited < 400:
                node, depth = stack.pop(0)
                visited += 1
                if depth > 12:
                    continue
                try:
                    children = node.GetChildren()
                except Exception:
                    children = []
                for child in children:
                    stack.append((child, depth + 1))
                    try:
                        if child.ControlType != auto.ControlType.EditControl:
                            continue
                        name = child.Name or ""
                        if not pattern.search(name):
                            continue
                        value = child.GetValuePattern().Value
                        if value:
                            return str(value).strip()
                    except Exception:
                        continue
            return None
        except Exception:
            return None

    def activate_window(self, title: str | None = None, handle: int | None = None) -> dict:
        user32, _, _, _ = _win_apis()
        SW_RESTORE = 9
        if handle:
            hwnd = _normalize_hwnd(handle)
        else:
            if not title:
                raise PCControlError("需要提供 title 或 handle")
            hwnd = self._find_window_handle(title)
        if not hwnd or not user32.IsWindow(hwnd):
            raise PCControlError("无法解析窗口句柄或窗口已关闭")
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.2)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        return {
            "activated": True,
            "handle": hwnd,
            "title": self._window_title(hwnd),
            "foreground_is_target": self._foreground_handle() == hwnd,
            "foreground_hwnd": self._foreground_handle(),
        }

    # ----------------------------------------------------------- 程序管理
    def launch_program(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        wait_seconds: float = 0.0,
    ) -> dict:
        if not command or not str(command).strip():
            raise PCControlError("command 为空")
        command = str(command).strip()
        args = [str(a) for a in (args or [])]
        resolved = None
        if os.path.exists(command):
            resolved = command
        else:
            resolved = shutil.which(command)
        try:
            if resolved:
                proc = subprocess.Popen(
                    [resolved, *args],
                    cwd=cwd or None,
                    shell=False,
                    close_fds=True,
                )
                used = [resolved, *args]
            else:
                # 允许 os.startfile / start 之类；交给 shell 解析
                proc = subprocess.Popen(
                    " ".join([command, *args]),
                    cwd=cwd or None,
                    shell=True,
                    close_fds=True,
                )
                used = " ".join([command, *args])
        except Exception as exc:
            raise PCControlError(f"启动失败：{exc}") from exc
        if wait_seconds and float(wait_seconds) > 0:
            time.sleep(min(float(wait_seconds), 10.0))
        return {"pid": proc.pid, "used": used, "shell": resolved is None}

    def list_processes(self, keyword: str | None = None) -> dict:
        psutil = _require("psutil", "进程查询")
        items = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = proc.info.get("name") or ""
                if keyword and keyword.lower() not in name.lower():
                    continue
                items.append({"pid": proc.info["pid"], "name": name})
            except Exception:
                continue
        return {"count": len(items), "processes": items[:200]}

    def kill_process(self, name: str, confirm: str = "", force: bool = False) -> dict:
        """按进程名结束进程。必须提供 confirm="KILL" 作为二次确认。"""
        if not name or not str(name).strip():
            raise PCControlError("name 为空")
        if (confirm or "").strip().upper() != "KILL":
            raise PCControlError(
                '结束进程需要二次确认：请再次调用并传入 confirm="KILL"'
            )
        target = str(name).strip().lower()
        if not target.endswith(".exe") and "." not in target:
            match_names = {target, f"{target}.exe"}
        else:
            match_names = {target}
        if match_names & _PROTECTED_PROCESSES:
            raise PCControlError(f"拒绝结束系统关键进程：{name}")
        psutil = _require("psutil", "结束进程")
        killed, failed = [], []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                if pname not in match_names:
                    continue
                pid = proc.info["pid"]
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    if force:
                        proc.kill()
                killed.append({"pid": pid, "name": pname})
            except Exception as exc:
                failed.append({"pid": proc.info.get("pid"), "error": str(exc)})
        if not killed and not failed:
            raise PCControlError(f"没有找到名为 {name} 的运行中进程")
        return {"killed": killed, "failed": failed, "count": len(killed)}


if __name__ == "__main__":
    # 手工自检（无害动作）：截屏 + 屏幕信息 + 剪贴板读写 + 窗口列表 + 图片压缩实测
    ctl = PCController()
    print("deps:", dependency_report())
    print("screen:", ctl.screen_size())
    shot = ctl.screenshot()
    print("shot:", shot)
    print("clip write/read:", ctl.clipboard_write("pc_control self test"),
          ctl.clipboard_read())
    print("windows:", ctl.list_windows()["count"])
    print("active:", ctl.active_window())
    enc = encode_image_for_llm(
        shot["path"], preferred_thumb=shot.get("thumb_path")
    )
    print("llm image:", {k: v for k, v in enc.items() if k != "base64"})
    print("llm base64 chars:", len(enc["base64"]))
