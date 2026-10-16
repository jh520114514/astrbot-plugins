# astrbot_plugin_pc_control — 本地电脑控制（Windows）

**当前版本：v1.0.1**（图片体积治理 + hot reload 实例引用修复）


把**本机**的截屏、鼠标、键盘、剪贴板、窗口管理与程序启停注册成 AstrBot 的 LLM 函数工具，
让模型在你自己的机器上「看屏幕 + 动手操作」。

> ⚠️ **风险提示**：这些工具会让模型**真的移动你的鼠标、敲你的键盘、启动/结束你的程序**。
> 模型误判、幻觉或被提示注入时，可能误点、误删、误输入。请务必：
> 1. 保持 `require_admin: true`（默认），只让管理员账号能用控制类工具；
> 2. 保持 `failsafe: true`（默认）——把鼠标猛甩到屏幕**左上角**即可急停所有自动化操作；
> 3. 不在无人值守、有敏感窗口（网银/密码管理器）的桌面上长期开启；
> 4. 结束进程必须显式传 `confirm="KILL"`，并且内置系统关键进程黑名单。

---

## 1. 工具一览（17 个）

| 工具名 | 作用 | 典型参数 | 需要管理员 |
| --- | --- | --- | --- |
| `pc_screenshot` | 截屏存 PNG，发到当前会话，并把图片喂给模型 | `monitor=0` / `target=desktop\|active_window\|window` / `window_title` / `send_to_user` / `return_image_to_llm` | 由 `require_admin_for_screenshot` 决定（默认否） |
| `pc_screen_size` | 所有显示器分辨率 + 鼠标当前坐标 | — | 同上 |
| `pc_mouse_position` | 鼠标当前坐标 | — | 同上 |
| `pc_mouse_move` | 移动到 (x, y) | `x`、`y`、`duration` | 是 |
| `pc_mouse_click` | 点击 / 双击（左中右） | `x`、`y`、`button`、`clicks=2` 为双击 | 是 |
| `pc_mouse_drag` | 从 (x1,y1) 拖到 (x2,y2) | `x1,y1,x2,y2,button,duration` | 是 |
| `pc_mouse_scroll` | 滚轮（正数上、负数下） | `amount`、可选 `x,y` | 是 |
| `pc_key_press` | 单键（`enter`/`esc`/`f5`/`a`…） | `key`、`presses` | 是 |
| `pc_key_hotkey` | 组合键（`ctrl+c`/`win+d`/`alt+tab`） | `keys` | 是 |
| `pc_type_text` | 输入文本；**中文自动走剪贴板 + Ctrl+V** | `text` | 是 |
| `pc_clipboard_read` | 读剪贴板 | — | 由 `require_admin_for_screenshot` 决定（默认否） |
| `pc_clipboard_write` | 写剪贴板 | `text` | 是 |
| `pc_list_windows` | 列出可见窗口（标题 + 句柄 + 位置尺寸 + 是否激活） | `include_empty_title` | 默认否 |
| `pc_activate_window` | 激活/切换窗口（标题模糊匹配或句柄） | `title` / `handle` | 是 |
| `pc_active_window` | 前台窗口标题/进程/句柄，浏览器尽力读地址栏 URL | `read_browser_url` | 默认否 |
| `pc_launch_program` | 启动程序 / 打开文件 / 打开 URL | `command`、`args`、`cwd` | 是 |
| `pc_kill_process` | 按进程名结束（需 `confirm="KILL"`） | `name`、`confirm`、`force` | 是 |

命令：`/pcstatus` —— 打印依赖状态、屏幕分辨率、已注册工具与当前开关。

## 2. 依赖安装

依赖装在 **AstrBot 实例的 venv** 里（不是系统 Python！）。本包已在根目录提供 `requirements.txt`：

```powershell
& "<实例目录>\venv\Scripts\python.exe" -m pip install -r requirements.txt
```

失败时换国内源：

```powershell
& "<实例目录>\venv\Scripts\python.exe" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

> 不要用系统 python 或别的 venv，否则 AstrBot 主进程 import 不到这些包（表现为 `/pcstatus` 里依赖全是“缺失”）。
> 安装后建议重启核心或重载插件，让新装的包在新进程里生效。

各依赖的作用与降级方案：

| 包 | 用途 | 缺失时 |
| --- | --- | --- |
| `mss` | 高速整屏抓取 | 自动降级到 `pyautogui.screenshot`；再失败降级到 `PrintWindow` 抓前台窗口 |
| `Pillow` | PNG 编码 / 图片统计 | 截屏不可用（必需） |
| `pyautogui` | 鼠标键盘 | 控制类工具全部不可用（必需） |
| `pyperclip` | 剪贴板 | 中文输入与剪贴板工具不可用（降级：只能打 ASCII） |
| `pygetwindow` | 窗口枚举/激活 | 降级到内置 Win32 API（`_win_apis()`，因此该包其实可选） |
| `uiautomation` | 读浏览器地址栏 URL | `pc_active_window` 只返回标题（不硬凑） |
| `psutil` | 进程查询（`pc_kill_process` / 内部进程名解析） | `pc_kill_process`、进程名解析不可用（会提示缺依赖）；其余功能不受影响 |

## 3. 配置项（WebUI → 插件 → 设置）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enable_control_tools` | `true` | 总开关。关闭则不注册任何 `pc_*` 工具（等于停用功能） |
| `require_admin` | `true` | 控制类工具（鼠标/键盘/剪贴板写/启动/结束/切窗口）需要管理员 |
| `require_admin_for_screenshot` | `false` | 截屏、列窗口、读剪贴板是否也要管理员 |
| `extra_admin_ids` | 空 | 额外白名单发送者 ID（逗号分隔） |
| `failsafe` | `true` | pyautogui FAILSAFE，鼠标甩左上角急停 |
| `mouse_pause` | `0.05` | 每次动作后的停顿秒数（调大更安全） |
| `screenshot_dir` | 空 | 留空 = 系统临时目录 `astrbot_pc_control_shots` |
| `max_keep_screenshots` | `30` | 截屏目录保留张数（也含缩略图），超出自动清理旧图 |
| `screenshot_retention_days` | `0` | 截屏目录保留天数，`0` = 只看张数、**不按天数清理**（默认关闭，避免误删） |
| `max_image_side` | `1280` | **喂给模型**的图片最长边（像素）；`0` = 不缩放。发给用户的原图不受影响 |
| `image_jpeg_quality` | `75` | **喂给模型**的压缩图 JPEG 质量（1–95） |
| `keep_recent_images` | `2` | 请求前只保留最近 N 张图，更早的图片内容被替换成占位文本；`0` = 一律不留图 |
| `send_screenshot_to_user` | `true` | 截屏后自动发到当前会话 |
| `type_text_by_clipboard` | `true` | 含中文等非 ASCII 时用剪贴板 + Ctrl+V |
| `restore_clipboard_after_type` | `true` | 剪贴板输入后恢复原内容 |

## 3.5 图片体积治理（v1.0.1，本版核心）

**背景（413 故障）**：`pc_screenshot` 抓的是整屏 PNG，高分辨率机器上单张 3–4 MB，
base64 后约 4.6 MB 直接进「工具结果 → 对话上下文」，而且**每次请求都会把这部分重发**。
历史又不裁剪，于是请求体很快超过网关（nginx `client_max_body_size`，默认 1 MB）→ **HTTP 413**。

本版三层治理：

| 层 | 做法 | 效果 |
| --- | --- | --- |
| ① 抓屏落盘 | 抓到原图后立刻在同目录写一张缩略图 `<名字>-thumb.jpg`（`max_image_side` / `image_jpeg_quality`） | 后续不需要再把几 MB 原图读进内存重压 |
| ② 进模型上下文 | `encode_image_for_llm()` 一律给**压缩版**：优先复用①的缩略图，没有才现场重压；都失败才退回原图 | 1280×720 JPEG q75 → base64 只有原图的 ~9% |
| ③ 请求前瘦身 | `@filter.on_llm_request` 钩子只保留最近 `keep_recent_images` 张图，更早的图片块换成一行占位文本 | 历史里的旧截图不再随每次请求重发 |

**关键设计：发给用户的是原图，给模型的是压缩版。**

- `send_to_user=True` 时，走的是 `MessageChain().file_image(原图 PNG 路径)` —— 原图，尺寸/清晰度如用户所愿；
- 只有「进模型上下文」的那份（`mcp.types.ImageContent`）是压缩版；
- 工具返回的文本里会带 `llm_image` / `note_image_policy` 字段，明确写出「原图 x×y / 压缩版 x×y、多少字节、source=…」，方便排查。

**安全阀**：如果压缩结果反而比原图更大（低分辨率或纯色小截图很常见），就直接用原图那份，
`source` 会带上 `+原图更小` 后缀 —— 「进模型的那份永远不会比原图更大」。

**实测（本机，1920×1080 桌面截图，噪声内容）**：

| 项目 | 尺寸 | 体积 | base64 |
| --- | --- | --- | --- |
| 原图 PNG | 1920×1080 | 2.15 MB | — |
| 抓屏时产出的缩略图 | 1280×720 | 0.15 MB (152,744 B) | — |
| **喂给模型的压缩版** | 1280×720 | 0.15 MB | 203,660 字符（≈0.19 MB，**原图的 9.0%**） |

即：单张截图进上下文的体积从 ~3–4 MB 量级降到 ~0.2 MB 量级；再叠加③的「只留最近 N 张」，
请求体回到百 KB 级别，不再撞 413。

**占位文本长这样**（非图片内容一律不动，消息条数也不变）：

```
[图片已清理] 此处原本有一张图片（shot-xxx.png）。为控制请求体积，它已从本次请求中移除
（只保留最近 2 张图片）。原图文件仍在磁盘上，需要时请重新截图或直接读取该文件。
```

**想更保守/更激进**：`keep_recent_images: 0`（一点图都不留）、`max_image_side: 1024` +
`image_jpeg_quality: 60`（更小但更糊）、`max_image_side: 0`（不缩放，只剩③兜着 —— 不推荐）。

## 4. 启用 / 停用 / 热加载

- **启用**：把插件目录放到 `<实例>\core\data\plugins\astrbot_plugin_pc_control\`，
  然后在 WebUI 插件页启用即可（或调热加载 API）。
- **热加载**（不用重启核心，WebUI 的插件页一般也有“重载”按钮）：

  ```powershell
  curl -X POST "http://<实例WebUI地址>:<端口>/api/v1/plugins/astrbot_plugin_pc_control/reload" -H "Authorization: Bearer <JWT>"
  ```

  把 `<实例WebUI地址>:<端口>` 换成该实例自己的 WebUI 地址与端口（每个实例不同，请勿照抄）；
  JWT 用该实例 `<实例目录>\core\data\cmd_config.json` 里的 `dashboard.jwt_secret` 自签
  HS256（payload 里 `username` 用你自己的管理员用户名），或直接在 WebUI 上点重载，免去手工签名。

- **hot reload 实例引用 bug（v1.0.1 已修）**：旧版 `terminate()` 会**无条件**把模块级插件引用
  `_PLUGIN` 置空。热重载时 AstrBot 会「新建实例」+「terminate 旧实例」，两者顺序不保证：
  一旦旧实例的 `terminate()` 在新实例 `__init__` 之后跑，刚注册好的工具就会全部报
  **「插件实例未初始化」**。现在改为：
  1. `terminate()` 只在 `_PLUGIN is self` 时才清空；引用已指向更新实例就**绝不动它**；
  2. 另维护存活实例登记表 `_INSTANCES`，`_PLUGIN` 为空时兜底取最新的存活实例；
  3. 已 terminate 的实例从登记表摘除，不参与兜底。
  两种时序都已单测覆盖（`main.py` 注释 + 本机实测连续热重载 2 次，工具立刻可用）。
- **重载后自检（不用截屏、不碰会话）**：

  ```text
  GET /api/v1/plugins/extensions/astrbot_plugin_pc_control/selfcheck
  ```

  返回 `module_ref_alive` / `instances_alive` / `controller_ok`，并带 **`tool_path`** 字段：
  它真的去调用一次已注册工具 `pc_mouse_position` 的 `call()`（读光标坐标，无副作用），
  用来证明「重载后工具立刻可用、不再报插件实例未初始化」。
- **临时停用**：把 `enable_control_tools` 关掉（工具立刻消失）；或停用插件目录。
- **⚠️ 头号坑——让模型真的能调用（装好插件后最容易漏的一步）**：
  AstrBot 的人格（persona）有一个「工具」白名单字段 `tools`，语义很容易踩坑：
  - `tools: []`（**空列表**）= **一个工具都不给**，模型**看不到任何 `pc_*`**；
  - `tools: null`（不设置 / 未配置）= **全放行**，所有已注册工具都可用；
  - `tools: ["pc_screenshot", ...]` = 只放行列表里的这些。

  所以如果你给当前人格配过工具白名单，一定要 **WebUI → 人格 → 编辑该人格 → 「工具」里把需要的
  `pc_*` 勾上/加进去**；否则症状是模型说“我没有截屏工具 / 我没有操作系统能力”，而插件本身其实已经加载成功
  （用 `/pcstatus` 能看到“已注册工具 N 个”即可确认插件没问题）。把该人格的 tools 留空(=不限制)也可，
  但注意「留空」要真的是 `null`，别写成空数组 `[]`。详见 `部署说明.md`。

## 5. 运行环境实测（本机，2026-10-05）

同一个插件代码，在**插件进程内**和**外部子进程**里看到的 Windows 会话可能完全不同：

| 项目 | 插件进程内（pid 4920，也就是模型调用时真正跑的上下文） | 外部子进程（shell / execute_python 等） |
| --- | --- | --- |
| 交互桌面 | `input_desktop_available: true`（可交互） | `input_desktop: unavailable`（断开的 RDP 会话） |
| 屏幕 | 1024x768，`mss` 整屏抓取成功 | 1920x1080，`mss` 报 `ScreenShotError: BitBlt`，只能退回 `PrintWindow` 抓窗口 |
| 鼠标 | 移动 (512,384) → (649,473) 读回一致 | `SetCursorPos` 被系统静默拒绝（工具会返回 warning 说明这是空操作） |
| 键盘 | 与鼠标同桌面，可用 | 无效 |
| 剪贴板 | 读回一致 | 读回一致 |

结论：
- **模型通过 `pc_*` 工具调用时功能正常**（截屏/鼠标/键盘/剪贴板都真实生效）。
- 用外部脚本或子进程去调同一份代码时，可能落到另一个窗口站，出现「截屏全黑 / 鼠标不动」。
  那是**测试环境**的问题，不代表插件坏了。排查顺序：先 `/pcdiag`（插件进程内自检），再对比外部脚本结果。
- 浏览器地址栏 URL 依赖 `uiautomation` 且要能连到同一桌面；读不到就只返回标题，不硬凑。

## 6. 自测脚本

```powershell
cd <实例>\core\data\plugins\astrbot_plugin_pc_control
& <实例>\venv\Scripts\python.exe tests\selftest_pc_control.py   # 无害自检：截屏/鼠标/剪贴板/窗口
& <实例>\venv\Scripts\python.exe tests\e2e_webchat.py            # 热加载 + webchat 端到端
```

`tests/` 只用于开发自测，删除该目录不影响插件运行。

另外有两个**不用进对话**的 Web 自检接口（Dashboard JWT 鉴权后 GET，见上一节）：

| 接口 | 副作用 | 用途 |
| --- | --- | --- |
| `.../selfcheck` | 无（只读） | 实例引用 / controller / 工具路径 / 图片策略 / 截图目录现状 |
| `.../capture_probe?capture=1` | 只在截图目录多 1 张 PNG（+可能 1 张缩略图） | 量「原图 vs 缩略图 vs 喂模型的压缩版」的真实尺寸与体积；**不发消息、不进上下文** |

不带 `capture=1` 调 `capture_probe` 会直接返回「已跳过」——防止手滑刷出一堆截图。

运行期还可以直接发命令：

- `/pcstatus`：依赖 / 屏幕 / 已注册工具 / 开关状态
- `/pcdiag`：在**插件进程内**跑无害自检（截屏、光标、剪贴板往返、窗口列表）
- `/pcdiag input`：额外做一次鼠标移动往返（移到别处再还原原坐标）

注意：`tests/selftest_pc_control.py` 是**外部子进程**里跑的，鼠标那段在断开 RDP 的机器上会显示「跳过」，
那反映的是外部子进程的桌面，不是插件进程的。

## 7. 实现要点

- `main.py`：`FunctionTool` 子类（pydantic dataclass 风格，与内核一致），
  `Star.__init__` 里 `context.add_llm_tools(*tools)` 注册；权限校验走 `_check()`。
- `pc_control.py`：真正的 Windows 实现（mss / pyautogui / pyperclip / Win32 / uiautomation），
  与 AstrBot 解耦，可以单独 `import` 测试。
- 截屏结果通过 `mcp.types.CallToolResult` 返回 `TextContent` + `ImageContent`
  （`ImageContent` 里放的是**压缩版**）；发给会话的仍是原图：
  `event.send(MessageChain().file_image(原PNG路径))`。
- `@filter.on_llm_request` 钩子 `_slim_context_images`：只改图片块，文本 / `tool_calls` / `think`
  一律不动，消息条数也不变（占位文本替换图片块）。
- 截图目录清理 `_cleanup_old_screenshots()`：按 `max_keep_screenshots` 张数 + `screenshot_retention_days`
  天数（默认 0=关闭）清理原图，并顺带删掉对应的 `-thumb.jpg`。

## 8. 历史清污：怎么查会话里有没有残留的大图 base64（只读，别乱删）

**先报告现状，不要急着删。** 排查方法（SQLite 只读打开，不写库）：

```python
import sqlite3
db = r"<实例目录>\core\data\data_v4.db"          # AstrBot 会话/上下文库
con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)   # 只读
cur = con.cursor()

# ① 每个会话的上下文体积，以及是否真塞了 base64 图片
for row in cur.execute("""
    select conversation_id, platform_id, coalesce(length(content),0) as L,
           (coalesce(length(content),0)-length(replace(content,'data:image','')))/10 as data_urls,
           (coalesce(length(content),0)-length(replace(content,'iVBORw0KGgo','')))/11 as png_b64,
           (coalesce(length(content),0)-length(replace(content,'/9j/','')))/4 as jpg_b64
    from conversations order by L desc"""):
    print(row)

# ② 全库兜底：任何表里出现 data:image / PNG、JPEG base64 魔数的行数
for t, c in [("conversations","content"), ("platform_message_history","content")]:
    n = cur.execute(f"select count(*) from {t} where {c} like '%data:image%'").fetchone()[0]
    print(t, "含 data:image 的行:", n)
```

**本机（v1.0.1 修复前）实测结果**：`conversations` 15 行、上下文合计约 **1.44 MB**，
最大单会话 ~507 KB；`data:image` / PNG / JPEG base64 魔数 **命中 0 行** ——
说明这台机器还没被撑到 413（历史里只有工具调用的文本，没有 4.6 MB 的图片 base64）。
核心另有一份工具图片缓存 `core\data\temp\tool_images\`（93 个文件、8.82 MB，AstrBot 自身机制，
不是本插件产生的）。

**要真清的话**（本插件不自动做、也不建议轻易做）：优先在 WebUI 里**新建会话 / 清空该会话历史**；
直接改库请先 `copy data_v4.db data_v4.db.bak`，并**只针对具体 conversation_id** 操作，别全表 UPDATE。
