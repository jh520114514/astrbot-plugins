# -*- coding: utf-8 -*-
"""astrbot_plugin_memory_keeper —— 记忆管家（长任务续接版）

设计动机
--------
现有记忆插件常见的两个致命问题：
  1) 在 on_llm_request 里**清空 req.contexts** → 长任务上下文被抹掉，模型"忘了前面说的"；
  2) 直接**重写 req.system_prompt** → 工具契约被破坏，工具调用链中途被打断。

本插件的原则：
  · 只**追加**注入块，绝不替换 / 清空 system_prompt，绝不碰 contexts；
  · 工具调用链中（req.tool_calls_result 非空）**完全不注入**；
  · 注入块有唯一边界标记 + 声明"这是参考资料不是指令"，并有长度硬上限与频率限制；
  · 所有钩子包 try/except，任何异常静默跳过，**绝不让记忆功能影响正常对话**；
  · 长任务用独立「任务看板」续接；项目结束可**归档成摘要**，之后被提起时才召回，
    且放在单独的"已归档"槽位，**不抢占当前任务**；
  · 日常对话（chat）与长期指令（directive）分类存储，指令注入时优先展示。

存储位置：data/plugin_data/astrbot_plugin_memory_keeper/
  memory.jsonl（追加式记忆）/ memory.md（人可读）/ tasks/*.json（任务看板）/ projects/*.json（归档项目）
"""

from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from . import injector
from .injector import build_block, preview, should_inject
from .store import MemoryStore, fmt_ts, safe_key

PLUGIN_NAME = "astrbot_plugin_memory_keeper"
NL = chr(10)  # 换行符（用常量避免字符串转义歧义）

DEFAULT_CONFIG: dict[str, Any] = {
    "enable": True,
    "inject_budget": 1500,
    "memory_top_k": 5,
    "min_interval_sec": 20,
    "with_tool_hint": True,
    "inject_task_board": True,
    "auto_goal_capture": True,
    "auto_save_user": False,
    "save_importance": 2,
    "inject_directives": True,
    "directive_budget": 400,
    "directive_max": 6,
    "project_recall": True,
    "project_recall_k": 2,
    "project_budget": 300,
    "chat_memory": True,
    "chat_min_len": 8,
    "chat_max_len": 300,
    "chat_importance": 2,
    "chat_fallback": True,
    "auto_archive_days": 3.0,
    "auto_archive_sweep_hours": 6.0,
    "summary_llm": True,
}

# 看起来像"要干一件事"的信号词（用于自动建任务看板的 goal）
_TASK_HINT_RE = re.compile(
    r"(帮我|帮忙|给我|做一个|写一个|写个|整理|部署|搭建|排查|调研|查一下|研究|"
    r"持续|长期|每天|每周|定时|项目|任务|方案|计划|跟进|继续|接着)"
)

# 看起来像"长期指令/约定"的信号词（存成 directive，注入时优先展示）
_DIRECTIVE_RE = re.compile(
    r"(以后|下次|今后|记住|记得|每次|都要|都不要|不要再|别再|默认|习惯|按我说的|约定|规则|一律|统一)"
)


@register(
    PLUGIN_NAME,
    "梦",
    "记忆管家：只追加不覆盖的安全注入 + 长任务看板 + 项目归档摘要召回，替代会清上下文/覆盖提示词的记忆插件。",
    "1.1.0",
    "https://github.com/jh520114514/astrbot_plugin_memory_keeper",
)
class MemoryKeeperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = dict(DEFAULT_CONFIG)
        try:
            if config:
                self.config.update({k: v for k, v in dict(config).items() if v is not None})
        except Exception as e:
            logger.warning(f"[memory_keeper] 读取配置失败，使用默认值: {e}")

        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            data_dir = None
        if data_dir is None:
            from pathlib import Path

            data_dir = Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
        self.store = MemoryStore(data_dir)

        self.stats: dict[str, int] = {
            "inject_ok": 0,
            "skip_tool_loop": 0,
            "skip_dup": 0,
            "skip_rate": 0,
            "skip_empty": 0,
            "errors": 0,
        }
        self._last_inject: dict[str, float] = {}
        self._last_sweep: float = 0.0
        logger.info(f"[memory_keeper] 已加载，数据目录：{self.store.dir}")

    # ------------------------------------------------------------ 基础工具

    def _cfg(self, key: str) -> Any:
        return self.config.get(key, DEFAULT_CONFIG.get(key))

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            v = getattr(event, attr, None)
            if v:
                return str(v)
        return "default"

    # ------------------------------------------------------------ 摘要 / 归档

    async def _llm_summarize(self, system: str, prompt: str, umo: str | None = None) -> str:
        """用当前 Provider 生成摘要；任何失败都返回空串（调用方会退回模板摘要）。"""
        if not self._cfg("summary_llm"):
            return ""
        try:
            provider = self.context.get_using_provider(umo)
            if provider is None:
                return ""
            resp = await provider.text_chat(system_prompt=system, prompt=prompt)
            text = getattr(resp, "completion_text", "") or ""
            return (text or "").strip()[:1200]
        except Exception as e:
            logger.debug(f"[memory_keeper] LLM 摘要失败（退回模板）: {e}")
            return ""

    def _archive_board(self, session_key: str, name: str, board: dict | None = None, llm_summary: str = "") -> dict:
        """把看板归档成项目记录，并清空看板。"""
        board = board or self.store.task_get(session_key)
        summary = (llm_summary or "").strip() or self.store.board_summary(board)
        goal = str(board.get("goal", ""))
        keywords = [w for w in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_\-]{2,12}", goal)][:6]
        rec = self.store.project_archive(name=name, summary=summary, keywords=keywords)
        self.store.task_clear(session_key)
        return rec

    async def _maybe_sweep(self, umo: str | None = None) -> int:
        """定期扫描：把长期没更新的看板自动归档成项目摘要。"""
        try:
            hours = float(self._cfg("auto_archive_sweep_hours") or 6)
            if time.time() - self._last_sweep < hours * 3600:
                return 0
            self._last_sweep = time.time()
            days = float(self._cfg("auto_archive_days") or 3)
            n = 0
            for sess_key, board in self.store.stale_boards(days=days):
                goal = str(board.get("goal", "")).strip()
                name = goal[:24] if goal else f"会话 {sess_key[:12]}"
                llm_summary = await self._llm_summarize(
                    system=(
                        "你是记忆整理助手。把给定的任务看板压缩成一段简洁的中文项目摘要，包含："
                        "做了什么、结论或结果、未完成项。不要寒暄，不要编造，200 字以内。"
                    ),
                    prompt=self.store.board_summary(board),
                    umo=umo,
                )
                self._archive_board(sess_key, name, board=board, llm_summary=llm_summary)
                n += 1
            if n:
                logger.info(f"[memory_keeper] 自动归档 {n} 个长期未更新的任务看板")
            return n
        except Exception as e:
            logger.debug(f"[memory_keeper] 自动归档扫描失败: {e}")
            return 0

    # ------------------------------------------------------------ 注入内容组装

    def _compose_block(self, event: AstrMessageEvent, req: ProviderRequest) -> str:
        sess = self._session_key(event)
        # 1) 当前任务看板
        task_text = ""
        if self._cfg("inject_task_board"):
            try:
                task_text = self.store.render_task(sess)
            except Exception:
                task_text = ""
        # 2) 长期指令 / 约定
        directives: list[dict] = []
        if self._cfg("inject_directives"):
            try:
                directives = self.store.directives(limit=int(self._cfg("directive_max") or 6))
            except Exception:
                directives = []
        # 3) 相关记忆（事实 + 闲聊）
        try:
            query = (getattr(req, "prompt", "") or getattr(event, "message_str", "") or "")[:200]
        except Exception:
            query = ""
        memories: list[dict] = []
        try:
            memories = self.store.search(
                query=query,
                scope=None,
                k=int(self._cfg("memory_top_k") or 5),
                kinds=("fact", "chat"),
            )
            memories.sort(key=lambda e: (e.get("scope") != sess, -float(e.get("ts", 0))))
            if self._cfg("chat_fallback") and not any(
                str(m.get("kind", "")).lower() == "chat" for m in memories
            ):
                recent = self.store.chat_recent(limit=1)
                if recent:
                    memories.append(recent[0])
        except Exception:
            memories = []
        # 4) 归档项目：只在被提起时召回，单独槽位（不抢当前任务）
        projects: list[dict] = []
        if self._cfg("project_recall") and query:
            try:
                projects = self.store.project_search(query, k=int(self._cfg("project_recall_k") or 2))
            except Exception:
                projects = []
        return build_block(
            task_text=task_text,
            directives=directives,
            memories=memories,
            projects=projects,
            budget=int(self._cfg("inject_budget") or 1500),
            directive_budget=int(self._cfg("directive_budget") or 400),
            project_budget=int(self._cfg("project_budget") or 300),
            with_tool_hint=bool(self._cfg("with_tool_hint")),
        )

    # ------------------------------------------------------------ 钩子：安全注入

    @filter.on_llm_request(priority=-100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """只追加、不覆盖、工具循环中不注入。任何异常都不得影响主流程。"""
        try:
            sess = self._session_key(event)
            has_tool_result = bool(getattr(req, "tool_calls_result", None))
            block = "" if has_tool_result else self._compose_block(event, req)
            ok, reason = should_inject(
                enabled=bool(self._cfg("enable")),
                has_tool_calls_result=has_tool_result,
                system_prompt=getattr(req, "system_prompt", "") or "",
                block=block,
                last_inject_ts=self._last_inject.get(sess, 0.0),
                min_interval_sec=float(self._cfg("min_interval_sec") or 0),
            )
            if not ok:
                if reason == "in_tool_loop":
                    self.stats["skip_tool_loop"] += 1
                elif reason == "dup_marker":
                    self.stats["skip_dup"] += 1
                elif reason == "rate_limited":
                    self.stats["skip_rate"] += 1
                elif reason == "empty":
                    self.stats["skip_empty"] += 1
                return
            req.system_prompt = injector.append_to_system_prompt(
                getattr(req, "system_prompt", "") or "", block
            )
            self._last_inject[sess] = time.time()
            self.stats["inject_ok"] += 1
        except Exception as e:
            self.stats["errors"] += 1
            logger.debug(f"[memory_keeper] on_llm_request 跳过（异常已吞掉）: {e}")

    # ------------------------------------------------------------ 钩子：自动采集

    @filter.on_llm_request(priority=50)
    async def auto_capture(self, event: AstrMessageEvent, req: ProviderRequest):
        """自动采集：任务目标、长期指令、日常闲聊记忆；并定期自动归档旧看板。"""
        try:
            if not self._cfg("enable"):
                return
            if bool(getattr(req, "tool_calls_result", None)):
                return  # 工具循环里不做任何写操作
            await self._maybe_sweep(getattr(event, "unified_msg_origin", None))
            text = (getattr(event, "message_str", "") or "").strip()
            if not text or text.startswith("/"):
                return
            sess = self._session_key(event)

            # 1) 任务看板目标（保守：不覆盖 24 小时内的已有目标）
            if self._cfg("auto_goal_capture") and len(text) >= 8 and _TASK_HINT_RE.search(text):
                task = self.store.task_get(sess)
                goal = task.get("goal")
                stale = (time.time() - float(task.get("updated", 0))) > 86400
                if not goal or stale:
                    self.store.task_set(sess, goal=text[:200])

            # 2) 长期指令 / 约定
            if len(text) <= 160 and _DIRECTIVE_RE.search(text):
                self.store.add(
                    text[:160],
                    scope="global",
                    tags=["directive"],
                    importance=4,
                    source="auto_directive",
                    session_key=sess,
                    kind="directive",
                )

            # 3) 日常对话记忆（低重要度、会话内）
            if self._cfg("chat_memory"):
                lo = int(self._cfg("chat_min_len") or 8)
                hi = int(self._cfg("chat_max_len") or 300)
                if lo <= len(text) <= hi and not _DIRECTIVE_RE.search(text):
                    self.store.add(
                        text,
                        scope=sess,
                        tags=["chat"],
                        importance=int(self._cfg("chat_importance") or 2),
                        source="auto_chat",
                        session_key=sess,
                        kind="chat",
                    )

            if self._cfg("auto_save_user") and len(text) >= 12:
                self.store.add(
                    text[:400],
                    scope=sess,
                    tags=["auto"],
                    importance=int(self._cfg("save_importance") or 2),
                    source="auto_user",
                    session_key=sess,
                )
        except Exception as e:
            self.stats["errors"] += 1
            logger.debug(f"[memory_keeper] auto_capture 跳过: {e}")

    # ------------------------------------------------------------ 命令：/mem

    @filter.command("mem", alias={"记忆", "memory"})
    async def mem(self, event: AstrMessageEvent):
        """记忆管家命令面板。用法：/mem [help|task|project|add|search|directives|chat|note|list|del|export|on|off|inject|status]"""
        try:
            raw = (getattr(event, "message_str", "") or "").strip()
            parts = raw.split()
            if parts and re.match(r"^/?\s*(mem|memory|记忆)$", parts[0]):
                parts = parts[1:]
            sub = parts[0].lower() if parts else "help"
            args = parts[1:]
            sess = self._session_key(event)
            umo = getattr(event, "unified_msg_origin", None)

            if sub in ("help", "帮助"):
                yield event.plain_result(self._help_text())

            elif sub in ("task", "看板", "任务"):
                if args and args[0] in ("clear", "reset", "清空"):
                    ok = self.store.task_clear(sess)
                    yield event.plain_result("任务看板已清空。" if ok else "当前会话没有任务看板。")
                else:
                    text = self.store.render_task(sess)
                    yield event.plain_result(
                        text or "当前会话没有任务看板。可用 `/mem task set <目标>` 或让我调用 task_update。"
                    )

            elif sub in ("project", "projects", "项目"):
                arg0 = args[0].lower() if args else ""
                if not args or arg0 in ("list", "列表"):
                    recs = self.store.project_list()
                    if not recs:
                        yield event.plain_result(
                            "还没有归档项目。用 `/mem project finish <名称>` 可把当前项目归档成摘要。"
                        )
                    else:
                        lines = [f"已归档项目 {len(recs)} 个："]
                        for r in recs[:15]:
                            lines.append(
                                f"· {r.get('name')}｜{fmt_ts(r.get('updated', 0))}｜{(r.get('summary') or '')[:80]}"
                            )
                        yield event.plain_result(NL.join(lines))
                elif arg0 in ("finish", "done", "归档", "结束"):
                    name = " ".join(args[1:]).strip() or (self.store.task_get(sess).get("goal") or "")[:24]
                    if not name:
                        yield event.plain_result("用法：/mem project finish <名称>")
                    else:
                        board = self.store.task_get(sess)
                        if not board:
                            yield event.plain_result("当前会话没有任务看板，无法归档。")
                        else:
                            summary = await self._llm_summarize(
                                system=(
                                    "你是记忆整理助手。把给定的任务看板压缩成一段简洁的中文项目摘要，包含："
                                    "做了什么、结论或结果、未完成项。不要寒暄，不要编造，200 字以内。"
                                ),
                                prompt=self.store.board_summary(board),
                                umo=umo,
                            )
                            rec = self._archive_board(sess, name, board=board, llm_summary=summary)
                            yield event.plain_result(
                                f"已归档项目「{rec['name']}」："
                                + NL
                                + str(rec.get("summary", ""))
                                + NL
                                + "（看板已清空；以后你提起它时会被自动想起）"
                            )
                elif arg0 in ("del", "删除"):
                    name = " ".join(args[1:]).strip()
                    ok = self.store.project_delete(name) if name else False
                    yield event.plain_result("已删除该项目归档。" if ok else "没找到该项目。")
                else:
                    name = " ".join(args).strip()
                    text = self.store.render_project(name) if name else ""
                    yield event.plain_result(text or f"没找到项目「{name}」，可用 /mem projects 看列表。")

            elif sub in ("directives", "指令", "约定"):
                items = self.store.directives(limit=20)
                if not items:
                    yield event.plain_result("还没有长期指令。你说「以后…」「记住…」这类话时我会自动记下。")
                else:
                    lines = [f"长期指令/约定 {len(items)} 条："]
                    for e in items:
                        lines.append(f"· `{e['id']}` ★{e.get('importance', 3)}｜{e.get('content', '')[:100]}")
                    yield event.plain_result(NL.join(lines))

            elif sub in ("chat", "闲聊"):
                if args and args[0] in ("clear", "清空"):
                    n = self.store.clear(scope=sess)
                    yield event.plain_result(f"已清空当前会话的 {n} 条记忆。")
                else:
                    items = self.store.chat_recent(limit=10)
                    if not items:
                        yield event.plain_result("还没有闲聊记忆。")
                    else:
                        lines = [f"最近 {len(items)} 条闲聊记忆："]
                        for e in items:
                            lines.append(f"· {fmt_ts(e.get('ts', 0))}｜{e.get('content', '')[:100]}")
                        yield event.plain_result(NL.join(lines))

            elif sub in ("note", "记一句"):
                if not args:
                    yield event.plain_result("用法：/mem note <要记的一句>")
                else:
                    self.store.add(" ".join(args), scope=sess, tags=["chat"], importance=3, kind="chat")
                    yield event.plain_result("已记下这句。")

            elif sub in ("add", "记", "添加"):
                if not args:
                    yield event.plain_result("用法：/mem add <要记住的内容>")
                else:
                    e = self.store.add(" ".join(args), scope="global", tags=["manual"], importance=4)
                    yield event.plain_result(f"已记住（id={e['id']}）。")

            elif sub in ("search", "查", "搜索"):
                if not args:
                    yield event.plain_result("用法：/mem search <关键词>")
                else:
                    hits = self.store.search(" ".join(args), k=5, touch=True)
                    if not hits:
                        yield event.plain_result("没找到相关记忆。")
                    else:
                        lines = [f"找到 {len(hits)} 条："]
                        for h in hits:
                            lines.append(
                                f"· `{h['id']}` ★{h.get('importance', 3)} {fmt_ts(h.get('ts', 0))}｜{h.get('content', '')[:120]}"
                            )
                        yield event.plain_result(NL.join(lines))

            elif sub in ("list", "列表", "全部"):
                items = sorted(self.store.all_entries(), key=lambda x: -float(x.get("ts", 0)))[:20]
                if not items:
                    yield event.plain_result("还没有任何记忆。")
                else:
                    lines = [f"最近 {len(items)} 条记忆："]
                    for e in items:
                        lines.append(
                            f"· `{e['id']}` [{e.get('kind', 'fact')}] ★{e.get('importance', 3)}｜{e.get('content', '')[:100]}"
                        )
                    yield event.plain_result(NL.join(lines))

            elif sub in ("del", "删", "删除"):
                if not args:
                    yield event.plain_result("用法：/mem del <id>")
                else:
                    ok = self.store.delete(args[0])
                    yield event.plain_result("已删除。" if ok else "没找到该 id。")

            elif sub in ("export", "导出"):
                p = self.store.dir / "export.json"
                p.write_text(self.store.export_json(), encoding="utf-8")
                yield event.plain_result(f"已导出到：{p}")

            elif sub in ("on", "off", "开关"):
                self.config["enable"] = sub == "on"
                yield event.plain_result("记忆注入已开启。" if sub == "on" else "记忆注入已关闭（数据仍保留）。")

            elif sub in ("inject", "预览"):
                block = ""
                try:
                    block = self._compose_block(event, ProviderRequest(prompt=getattr(event, "message_str", "")))
                except Exception:
                    pass
                yield event.plain_result("【注入预览】" + NL + preview(block))

            elif sub in ("debug", "状态", "status"):
                yield event.plain_result(self._status_text())

            else:
                yield event.plain_result(self._help_text())
        except Exception as e:
            self.stats["errors"] += 1
            logger.warning(f"[memory_keeper] /mem 执行异常: {e}")
            yield event.plain_result(f"执行出错：{e}")
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    def _help_text(self) -> str:
        return NL.join(
            [
                "【记忆管家】",
                "— 任务与项目 —",
                "/mem task —— 当前会话任务看板　/mem task clear —— 清空看板",
                "/mem project finish [名称] —— 把项目归档成摘要（以后你提起它我会想起）",
                "/mem projects —— 已归档项目列表　/mem project <名称> —— 看某项目详情",
                "/mem project del <名称> —— 删除归档",
                "— 记忆 —",
                "/mem add <内容> —— 写入长期记忆　/mem search <词> —— 检索",
                "/mem directives —— 长期指令/约定　/mem chat —— 最近闲聊记忆",
                "/mem note <一句> —— 手动记一句　/mem list —— 最近记忆　/mem del <id>",
                "— 运维 —",
                "/mem inject —— 预览本次注入内容　/mem status —— 运行统计",
                "/mem export —— 导出 JSON　/mem on | /mem off —— 开关注入",
                "AI 工具：memory_save / memory_search / task_update / task_read / project_archive / project_recall / chat_note",
            ]
        )

    def _status_text(self) -> str:
        s = self.store.stats()
        st = self.stats
        return NL.join(
            [
                "【记忆管家 · 状态】",
                f"记忆条目：{s['total']}｜按类型 {s.get('by_kind', {})}",
                f"任务看板：{s['tasks']} 个｜已归档项目：{s.get('projects', 0)} 个",
                f"数据目录：{s['dir']}",
                f"注入成功 {st['inject_ok']}｜跳过：工具链 {st['skip_tool_loop']}、重复 {st['skip_dup']}、"
                f"限频 {st['skip_rate']}、空 {st['skip_empty']}｜异常 {st['errors']}",
                f"开关：{'开' if self._cfg('enable') else '关'}｜预算 {self._cfg('inject_budget')} 字符"
                f"｜指令槽 {'开' if self._cfg('inject_directives') else '关'}"
                f"｜项目召回 {'开' if self._cfg('project_recall') else '关'}"
                f"｜闲聊记忆 {'开' if self._cfg('chat_memory') else '关'}",
            ]
        )

    # ------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="memory_save")
    async def memory_save(
        self,
        event: AstrMessageEvent,
        content: str,
        tags: str = "",
        importance: int = 3,
        scope: str = "global",
        kind: str = "fact",
    ) -> str:
        """把一条值得长期记住的内容写进记忆库。

        Args:
            content(string): 要记住的内容，一句话说清（不要长篇）。
            tags(string): 可选标签，逗号分隔，例如“项目,偏好”。
            importance(number): 重要度 1-5，默认 3。
            scope(string): `global` 表示跨会话长期记忆；`session` 表示仅当前会话。
            kind(string): 类型：`fact` 事实/偏好（默认）；`directive` 用户给的行为指令/约定（会被优先展示，务必遵守）；`chat` 日常闲聊里值得留一句的内容。
        """
        try:
            sess = self._session_key(event)
            real_scope = sess if str(scope).lower() in ("session", "当前", "会话") else "global"
            e = self.store.add(
                content=content,
                scope=real_scope,
                tags=tags,
                importance=int(importance or 3),
                source="llm_tool",
                session_key=sess,
                kind=kind,
            )
            return f"已记住（id={e['id']}, scope={e['scope']}, kind={e.get('kind')}, ★{e['importance']}）"
        except Exception as e:
            return f"保存失败：{e}"

    @filter.llm_tool(name="memory_search")
    async def memory_search(self, event: AstrMessageEvent, query: str = "", k: int = 5) -> str:
        """检索记忆库（按关键词 + 重要度 + 时间衰减排序）。

        Args:
            query(string): 检索关键词；留空则返回最近/最重要的记忆。
            k(number): 返回条数，默认 5。
        """
        try:
            hits = self.store.search(query=query, k=int(k or 5), touch=True)
            if not hits:
                return "没有找到相关记忆。"
            out = [f"共 {len(hits)} 条："]
            for h in hits:
                out.append(f"- [{h['id']}|{h.get('kind', 'fact')}|★{h.get('importance', 3)}] {h.get('content', '')}")
            return NL.join(out)
        except Exception as e:
            return f"检索失败：{e}"

    @filter.llm_tool(name="task_update")
    async def task_update(
        self,
        event: AstrMessageEvent,
        goal: str = "",
        progress: str = "",
        decisions: str = "",
        todos: str = "",
        next_step: str = "",
    ) -> str:
        """更新当前会话的「任务看板」，用于跨多轮的长任务续接（防上下文被裁剪后失忆）。

        Args:
            goal(string): 任务目标（只在目标变化时填）。
            progress(string): 最新进度描述（会追加累积）。
            decisions(string): 已确认的结论/决定（会追加累积）。
            todos(string): 待办事项（会追加累积）。
            next_step(string): 下一步要做什么。
        """
        try:
            sess = self._session_key(event)
            fields: dict[str, Any] = {}
            if goal:
                fields["goal"] = goal
            if progress:
                fields["progress"] = [f"{fmt_ts(time.time())} {progress}"]
            if decisions:
                fields["decisions"] = [decisions]
            if todos:
                fields["todos"] = [t.strip() for t in re.split(r"[,，;；\n]+", todos) if t.strip()]
            if next_step:
                fields["next_step"] = next_step
            if not fields:
                return "没有需要更新的字段。"
            self.store.task_set(sess, **fields)
            return "任务看板已更新：" + NL + (self.store.render_task(sess) or "")
        except Exception as e:
            return f"更新失败：{e}"

    @filter.llm_tool(name="task_read")
    async def task_read(self, event: AstrMessageEvent) -> str:
        """读取当前会话的任务看板（不确定自己在做什么、进度到哪时调用）。"""
        try:
            sess = self._session_key(event)
            return self.store.render_task(sess) or "当前会话没有任务看板。"
        except Exception as e:
            return f"读取失败：{e}"

    @filter.llm_tool(name="project_archive")
    async def project_archive_tool(self, event: AstrMessageEvent, name: str, summary: str = "") -> str:
        """把当前项目**归档成一段摘要**（项目收尾/结束、或长期搁置时调用）。
        归档后当前看板会被清空；之后用户再提起这个项目时，摘要会被自动召回，
        不会占用当前任务的上下文。

        Args:
            name(string): 项目名称（简短好认，例如“dy40 逆向”“QQMemoryStudio 开源”）。
            summary(string): 项目摘要：做了什么 / 结论或产物 / 未完成项；留空则由系统按看板自动生成。
        """
        try:
            sess = self._session_key(event)
            board = self.store.task_get(sess)
            auto = ""
            if not summary and board:
                auto = await self._llm_summarize(
                    system=(
                        "你是记忆整理助手。把给定的任务看板压缩成一段简洁的中文项目摘要，包含："
                        "做了什么、结论或结果、未完成项。不要寒暄，不要编造，200 字以内。"
                    ),
                    prompt=self.store.board_summary(board),
                    umo=getattr(event, "unified_msg_origin", None),
                )
            rec = self._archive_board(sess, name, board=board, llm_summary=summary or auto)
            return f"已归档项目「{rec['name']}」" + NL + f"摘要：{rec.get('summary', '')}"
        except Exception as e:
            return f"归档失败：{e}"

    @filter.llm_tool(name="project_recall")
    async def project_recall(self, event: AstrMessageEvent, query: str = "", k: int = 3) -> str:
        """检索**已归档的历史项目**（用户提起以前做过的项目时调用；不要把它当成当前任务）。

        Args:
            query(string): 关键词（项目名或内容关键词）。
            k(number): 返回条数，默认 3。
        """
        try:
            if query:
                recs = self.store.project_search(query, k=int(k or 3))
            else:
                recs = self.store.project_list()[: int(k or 3)]
            if not recs:
                return "没有找到相关的归档项目。"
            out = []
            for r in recs:
                out.append(
                    f"【已归档】{r.get('name')}（{fmt_ts(r.get('updated', 0))}）" + NL + str(r.get("summary", ""))
                )
            return (NL + NL).join(out)
        except Exception as e:
            return f"检索失败：{e}"

    @filter.llm_tool(name="chat_note")
    async def chat_note(self, event: AstrMessageEvent, content: str) -> str:
        """记一句日常对话里值得留下的内容（偏好、近况、随口提到的事），属于低优先级的闲聊记忆。

        Args:
            content(string): 要记的一句话。
        """
        try:
            sess = self._session_key(event)
            self.store.add(content, scope=sess, tags=["chat"], importance=3, source="llm_chat", kind="chat")
            return "已记下。"
        except Exception as e:
            return f"记录失败：{e}"
