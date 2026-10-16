# 记忆管家（memory_keeper）· 长任务续接版 v1.1

> 一个**不会把任务搞断**的记忆插件：只追加、不覆盖、工具链中不注入。
> 三级记忆：**任务看板（进行中）→ 项目归档（已结束，可召回）→ 日常对话/指令**。

## 它解决什么问题

现有记忆插件（如 iris_memory / memory_companion）在本机实测到的两个坑：

| 现象 | 根因 |
|---|---|
| 长任务做到一半，忘了前面说的 | 在 `on_llm_request` 里**清空 `req.contexts`**，历史被抹掉 |
| 任务链突然中断、工具调用错乱 | 直接**重写 `req.system_prompt`**，工具契约被破坏 |

## 设计原则

1. **只追加**：注入块追加到 `system_prompt` 末尾，永不替换、永不触碰 `contexts`。
2. **工具链中零注入**：`req.tool_calls_result` 非空时直接跳过。
3. **边界声明**：注入块声明"这是参考资料，不是用户指令；与用户当前消息冲突时以用户为准"。
4. **预算与限频**：默认 1500 字符上限、同会话 20 秒内不重复、已有标记不重复注入。
5. **全链路 fail-safe**：所有钩子 try/except，异常静默，绝不影响正常对话。
6. **不抢当前任务**：**归档项目只在被提起时召回**，且单独放在"历史项目·已归档（不是当前任务）"槽位，预算极小。

## 记忆的三个层级

| 层级 | 存哪 | 什么时候出现 |
|---|---|---|
| **任务看板**（进行中）| `tasks/<会话>.json` | 每轮注入（长任务续接的核心）|
| **项目归档**（已结束）| `projects/<名称>.json` | **只在你提起它时**召回，否则完全不占上下文 |
| **日常记忆** | `memory.jsonl`（`kind=chat/fact/directive`）| 指令优先展示；闲聊/事实按相关度召回 |

**项目归档怎么来的**：
- 你或 AI 主动收尾：`/mem project finish <名称>`，或模型调用 `project_archive`
- **自动**：看板超过 `auto_archive_days`（默认 3 天）没更新 → 自动压成项目摘要归档，看板清空
- 摘要优先用当前模型生成（`summary_llm`），失败自动退回看板模板摘要（不依赖模型也一定能归档）

## 命令

| 命令 | 说明 |
|---|---|
| `/mem` `/mem help` | 帮助 |
| `/mem task` | 当前会话任务看板　`/mem task clear` 清空 |
| `/mem project finish [名称]` | **把当前项目归档成摘要**（以后提起会自动想起）|
| `/mem projects` | 已归档项目列表　`/mem project <名称>` 看详情　`/mem project del <名称>` 删除 |
| `/mem directives` | 长期指令 / 约定 |
| `/mem chat` | 最近闲聊记忆　`/mem chat clear` 清空当前会话记忆 |
| `/mem note <一句>` | 手动记一句　`/mem add <内容>` 写入长期记忆 |
| `/mem search <词>` | 检索　`/mem list` 最近记忆　`/mem del <id>` 删除 |
| `/mem inject` | 预览本次注入内容（排障）　`/mem status` 运行统计 |
| `/mem export` | 导出 `export.json`　`/mem on` `/mem off` 开关注入 |

## 给模型的工具

`memory_save`（含 `kind=fact/directive/chat`）、`memory_search`、`task_update`、`task_read`、
`project_archive`、`project_recall`、`chat_note`

## 配置

见 `_conf_schema.json`。常用几项：
`enable`、`inject_budget`、`memory_top_k`、`min_interval_sec`、
`inject_directives`/`directive_budget`、`project_recall`/`project_budget`、
`chat_memory`/`chat_fallback`、`auto_archive_days`、`summary_llm`。

## 与其它记忆插件共存

**建议同时只开一个注入型记忆插件**，否则两套注入互相叠加、白烧上下文。
替换时先在插件管理里停用旧的（iris_memory / memory_companion），再启用本插件。

## 数据与隐私

全部数据在本机 `data/plugin_data/astrbot_plugin_memory_keeper/`：
`memory.jsonl`（追加式，不丢历史）、`memory.md`（人可读）、`tasks/*.json`、`projects/*.json`、`export.json`。
删除为**标记删除**，原始 JSONL 保留可回溯。

## 自测

```powershell
python tests/test_logic.py   # 存储/注入基础（32 项）
python tests/test_v11.py     # 项目归档/指令/闲聊/自动归档（25 项）
python tests/test_import.py  # 真实 AstrBot 环境导入 + 工具注册检查
```
