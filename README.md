# AstrBot 插件合集（手工编写）

两个自用的 AstrBot 插件，代码与文档都在这儿。

| 插件 | 版本 | 一句话 |
|---|---|---|
| [`astrbot_plugin_pc_control`](./astrbot_plugin_pc_control) | v1.0.1 | 让模型**截屏看屏幕**，并操作本机鼠标 / 键盘 / 剪贴板 / 窗口 / 进程（Windows） |
| [`astrbot_plugin_memory_keeper`](./astrbot_plugin_memory_keeper) | v1.1.0 | **不会把任务搞断**的记忆插件：只追加不覆盖 + 长任务看板 + 项目归档摘要召回 |

---

## 1. astrbot_plugin_pc_control —— 本地电脑控制

把本机（Windows）的桌面能力注册成 LLM 函数工具，模型可直接调用：

```
pc_screenshot / pc_screen_size / pc_mouse_position
pc_mouse_move / pc_mouse_click / pc_mouse_drag / pc_mouse_scroll
pc_key_press / pc_key_hotkey / pc_type_text
pc_clipboard_read / pc_clipboard_write
pc_list_windows / pc_activate_window / pc_active_window
pc_launch_program / pc_kill_process
```

亮点：

- **图片体积治理**：进模型上下文的一律是压缩版（默认最长边 1280px / JPEG q75），
  发给用户的仍是原图；并有上下文瘦身钩子只保留最近 N 张截图，
  避免历史里的 base64 撑爆请求体（网关 413）。
- **安全默认**：控制类工具默认仅管理员可用；`pyautogui` FAILSAFE 默认开启；
  结束进程需要 `confirm="KILL"` 二次确认。

> ⚠️ 模型误操作会**真的**操作你的鼠标键盘。请先在小范围试，别在放着重要窗口的机器上直接放开权限。

---

## 2. astrbot_plugin_memory_keeper —— 记忆管家（长任务续接版）

针对现有记忆插件的两个致命问题设计：

| 现象 | 常见根因 | 本插件的做法 |
|---|---|---|
| 长任务做到一半"忘了前面说的" | 在 `on_llm_request` 里**清空 `req.contexts`** | 只**追加**，绝不碰 `contexts` |
| 任务链突然中断、工具调用错乱 | 直接**重写 `req.system_prompt`** | 只追加到末尾，绝不替换；**工具调用链中完全不注入** |

三级记忆：

| 层级 | 存放 | 何时出现 |
|---|---|---|
| **任务看板**（进行中）| `tasks/<会话>.json` | 每轮注入（长任务续接的关键）|
| **项目归档**（已结束）| `projects/<名称>.json` | **只在你提起它时**召回，单独槽位声明"不是当前任务" → 不抢当前上下文 |
| **日常记忆** | `memory.jsonl`（`chat` / `fact` / `directive`）| 指令优先展示；闲聊与事实按相关度召回 |

- **项目归档怎么来**：`/mem project finish <名称>` 或模型调用 `project_archive`；
  看板超过 3 天没更新会**自动**压成摘要归档（摘要用当前模型生成，失败自动退回模板，不会归档失败）。
- **模型工具**：`memory_save` / `memory_search` / `task_update` / `task_read` / `project_archive` / `project_recall` / `chat_note`
- **命令**：`/mem task|project|projects|directives|chat|note|add|search|list|del|inject|status|export|on|off`
- **安全**：所有钩子 try/except，异常静默；注入有长度硬上限、频率限制、去重标记。

---

## 安装

把插件目录放进 AstrBot 的 `data/plugins/`，然后在面板里重载/重启：

```bash
cd /path/to/astrbot/data/plugins
git clone https://github.com/jh520114514/astrbot-plugins.git _tmp
mv _tmp/astrbot_plugin_pc_control .
mv _tmp/astrbot_plugin_memory_keeper .
rm -rf _tmp
```

或直接在面板「插件管理 → 安装」里上传对应目录的 zip。

依赖：`astrbot_plugin_pc_control` 需要 `pyautogui`、`pygetwindow`、`pillow`
（见其 `requirements.txt`）；`astrbot_plugin_memory_keeper` **零第三方依赖**（纯标准库）。

> 💡 记忆类插件建议**只开一个**：同时开多个注入型记忆插件会互相叠加、白烧上下文。

---

## 目录结构

```
astrbot-plugins/
├── astrbot_plugin_pc_control/
│   ├── main.py              插件入口（工具注册）
│   ├── pc_control.py        截屏/输入/窗口/进程实现
│   ├── _conf_schema.json    配置面板
│   ├── metadata.yaml
│   ├── 部署说明.md
│   └── tests/               自测与 e2e 脚本
└── astrbot_plugin_memory_keeper/
    ├── main.py              钩子 + 命令 + 7 个 LLM 工具
    ├── store.py             存储层（记忆 / 看板 / 项目归档，纯标准库）
    ├── injector.py          安全注入层（只追加 / 槽位 / 预算）
    ├── _conf_schema.json
    ├── metadata.yaml
    ├── README.md
    └── tests/               test_logic.py 32 项 + test_v11.py 25 项 + 导入检查
```

## 自测

```bash
cd astrbot_plugin_memory_keeper && python tests/test_logic.py && python tests/test_v11.py
cd astrbot_plugin_pc_control && python tests/selftest_pc_control.py
```

## License

MIT © 2026 姜黄（见 [LICENSE](./LICENSE)）。
