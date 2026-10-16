# -*- coding: utf-8 -*-
"""memory_keeper 存储层。

设计目标（针对"长任务失忆 / 注入打断任务"两个痛点）：
- **追加式**：所有记忆写在 append-only 的 JSONL 里，不覆盖、不删除历史（删除只标记）。
- **两级**：scope=global（跨会话长期记忆）+ scope=session（会话内记忆）；
  另有独立的「任务看板」(tasks/*.json) 用于长任务续接。
- **零依赖**：纯标准库，可脱离 AstrBot 单测。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

IMPORTANCE_MIN, IMPORTANCE_MAX = 1, 5

# 列表类字段的字符串拆分规则（换行/逗号/分号）
SPLIT_RE = re.compile("[,，;；\n]+")
DEFAULT_IMPORTANCE = 3
NL = chr(10)  # 换行符（避免转义歧义）


# ---------------------------------------------------------------- 工具


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def safe_key(s: str, limit: int = 80) -> str:
    s = (s or "").strip() or "default"
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", s)[:limit]


def normalize(text: str) -> str:
    """归一化用于去重比较：折叠空白、去掉常见标点差异。"""
    text = re.sub(r"\s+", "", text or "")
    return text.strip().lower()


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> set[str]:
    """粗分词：英文/数字按词切，中文按 单字 + 相邻双字 切。够用且无依赖。"""
    text = text or ""
    toks: set[str] = set(w.lower() for w in _WORD_RE.findall(text))
    zh = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i, ch in enumerate(zh):
        toks.add(ch)
        if i + 1 < len(zh):
            toks.add(zh[i : i + 2])
    return {t for t in toks if t}


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def trim_to(text: str, limit: int) -> str:
    """按字符数裁剪（用于注入预算控制）。"""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------- 存储


class MemoryStore:
    """记忆 + 任务看板的本地存储。线程安全（RLock）。"""

    TASK_FIELDS = ("goal", "progress", "decisions", "todos", "next_step", "notes")
    LIST_FIELDS = ("progress", "decisions", "todos")

    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mem_file = self.dir / "memory.jsonl"
        self.md_file = self.dir / "memory.md"
        self.tasks_dir = self.dir / "tasks"
        self.tasks_dir.mkdir(exist_ok=True)
        self.projects_dir = self.dir / "projects"
        self.projects_dir.mkdir(exist_ok=True)
        self.state_file = self.dir / "state.json"
        self._lock = threading.RLock()
        self._entries: list[dict] | None = None

    # ---------------- 记忆读写 ----------------

    def _load(self) -> list[dict]:
        if self._entries is not None:
            return self._entries
        items: list[dict] = []
        if self.mem_file.exists():
            for line in self.mem_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("content"):
                    obj.setdefault("deleted", False)
                    obj.setdefault("hits", 0)
                    items.append(obj)
        self._entries = items
        return items

    def _append(self, entry: dict) -> None:
        with self.mem_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _rewrite_all(self) -> None:
        items = self._load()
        atomic_write(
            self.mem_file,
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in items),
        )

    @staticmethod
    def make_id(content: str) -> str:
        return hashlib.sha1(
            (content + str(time.time_ns())).encode("utf-8")
        ).hexdigest()[:12]

    def add(
        self,
        content: str,
        scope: str = "global",
        tags: Iterable[str] | str | None = None,
        importance: int = DEFAULT_IMPORTANCE,
        source: str = "manual",
        session_key: str = "",
        kind: str = "fact",
    ) -> dict:
        """写入一条记忆。

        kind 取值：
          `fact`      —— 事实/偏好（默认）
          `chat`      —— 日常对话里值得留一句的内容（低重要度）
          `directive` —— 用户给的行为指令/约定（注入时优先展示）
          `project`   —— 已归档项目的摘要索引
        """
        content = (content or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        kind = (kind or "fact").strip().lower()
        if kind not in ("fact", "chat", "directive", "project"):
            kind = "fact"
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,，;；\s]+", tags) if t.strip()]
        tags = list(tags or [])
        importance = max(IMPORTANCE_MIN, min(IMPORTANCE_MAX, int(importance or DEFAULT_IMPORTANCE)))

        with self._lock:
            items = self._load()
            norm = normalize(content)
            for e in items:
                if e.get("deleted"):
                    continue
                if e.get("scope") == scope and normalize(e.get("content", "")) == norm:
                    # 重复记忆 → 提升重要度/时间，不再新增
                    e["ts"] = now_ts()
                    e["importance"] = max(int(e.get("importance", 3)), importance)
                    e["tags"] = sorted(set(e.get("tags", [])) | set(tags))
                    e["dup_count"] = int(e.get("dup_count", 1)) + 1
                    self._rewrite_all()
                    self.rebuild_markdown()
                    return e
            entry = {
                "id": self.make_id(content),
                "ts": now_ts(),
                "scope": scope,
                "kind": kind,
                "content": content,
                "tags": tags,
                "importance": importance,
                "source": source,
                "session": session_key,
                "hits": 0,
                "dup_count": 1,
                "deleted": False,
            }
            items.append(entry)
            self._append(entry)
            self.rebuild_markdown()
            return entry

    def all_entries(self, scope: str | None = None, include_deleted: bool = False) -> list[dict]:
        with self._lock:
            items = self._load()
        out = [e for e in items if include_deleted or not e.get("deleted")]
        if scope:
            out = [e for e in out if e.get("scope") == scope]
        return out

    def score(self, entry: dict, q_tokens: set[str], half_life_days: float = 14.0) -> float:
        """打分：关键词命中 + 重要度 + 时间衰减。"""
        base = float(entry.get("importance", 3)) * 1.0
        if q_tokens:
            e_tokens = tokenize(entry.get("content", "") + " " + " ".join(entry.get("tags", [])))
            overlap = len(q_tokens & e_tokens)
            if overlap == 0:
                return -1.0
            base += overlap * 2.0
            base += 0.5 * min(overlap, 5)
        age_days = max(0.0, (now_ts() - float(entry.get("ts", 0))) / 86400.0)
        base += 2.0 * (0.5 ** (age_days / half_life_days))
        base += 0.2 * min(int(entry.get("dup_count", 1)), 10)
        return base

    def search(
        self,
        query: str = "",
        scope: str | None = None,
        k: int = 5,
        touch: bool = False,
        kinds: Iterable[str] | None = None,
    ) -> list[dict]:
        q_tokens = tokenize(query) if query else set()
        items = self.all_entries(scope=scope)
        if kinds:
            wanted = {str(x).lower() for x in kinds}
            items = [e for e in items if str(e.get("kind", "fact")).lower() in wanted]
        scored: list[tuple[float, dict]] = []
        for e in items:
            s = self.score(e, q_tokens)
            if s >= 0:
                scored.append((s, e))
        scored.sort(key=lambda x: (-x[0], -float(x[1].get("ts", 0))))
        picked = [e for _, e in scored[: max(1, k)]]
        if touch and picked:
            with self._lock:
                for e in picked:
                    e["hits"] = int(e.get("hits", 0)) + 1
                self._rewrite_all()
        return picked

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            for e in self._load():
                if e.get("id") == entry_id and not e.get("deleted"):
                    e["deleted"] = True
                    e["ts_deleted"] = now_ts()
                    self._rewrite_all()
                    self.rebuild_markdown()
                    return True
        return False

    def clear(self, scope: str | None = None) -> int:
        n = 0
        with self._lock:
            for e in self._load():
                if e.get("deleted"):
                    continue
                if scope is None or e.get("scope") == scope:
                    e["deleted"] = True
                    n += 1
            self._rewrite_all()
            self.rebuild_markdown()
        return n

    def stats(self) -> dict:
        items = self.all_entries()
        by_scope: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for e in items:
            by_scope[e.get("scope", "?")] = by_scope.get(e.get("scope", "?"), 0) + 1
            by_kind[e.get("kind", "fact")] = by_kind.get(e.get("kind", "fact"), 0) + 1
        return {
            "total": len(items),
            "by_scope": by_scope,
            "by_kind": by_kind,
            "tasks": len(list(self.tasks_dir.glob("*.json"))),
            "projects": len(list(self.projects_dir.glob("*.json"))),
            "dir": str(self.dir),
        }

    # ---------------- 任务看板 ----------------

    def _task_path(self, session_key: str) -> Path:
        return self.tasks_dir / f"{safe_key(session_key)}.json"

    def task_get(self, session_key: str) -> dict:
        p = self._task_path(session_key)
        if not p.exists():
            return {}
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def task_set(self, session_key: str, **fields: Any) -> dict:
        """合并式更新。

        - 列表类字段（progress / decisions / todos）：值可以是 list，也可以是
          用换行/逗号/分号分隔的字符串 —— 一律**追加去重**，不覆盖历史。
        - 其它字段（goal / next_step / notes）：字符串**覆盖**。
        """
        with self._lock:
            task = self.task_get(session_key)
            task.setdefault("session", session_key)
            task.setdefault("created", now_ts())
            for key, value in fields.items():
                if value is None:
                    continue
                merge = key in self.LIST_FIELDS
                if merge:
                    cur = task.get(key) or []
                    if not isinstance(cur, list):
                        cur = [str(cur)]
                    if isinstance(value, (list, tuple)):
                        new_items = [str(v).strip() for v in value]
                    else:
                        new_items = [
                            x.strip()
                            for x in SPLIT_RE.split(str(value))
                        ]
                    for v in new_items:
                        if v and v not in cur:
                            cur.append(v)
                    task[key] = cur[-40:]
                else:
                    s = str(value).strip()
                    if s:
                        task[key] = s
            task["updated"] = now_ts()
            task["updates"] = int(task.get("updates", 0)) + 1
            atomic_write(self._task_path(session_key), json.dumps(task, ensure_ascii=False, indent=1))
            return task

    def task_clear(self, session_key: str) -> bool:
        p = self._task_path(session_key)
        if p.exists():
            p.unlink()
            return True
        return False

    def render_task(self, session_key: str) -> str:
        t = self.task_get(session_key)
        if not t:
            return ""
        labels = {
            "goal": "目标",
            "progress": "进度",
            "decisions": "已确认结论",
            "todos": "待办",
            "next_step": "下一步",
            "notes": "备注",
        }
        lines: list[str] = []
        for key, label in labels.items():
            if key not in t:
                continue
            val = t[key]
            if isinstance(val, list):
                if not val:
                    continue
                lines.append(f"- {label}：")
                lines.extend(f"    · {v}" for v in val)
            else:
                lines.append(f"- {label}：{val}")
        if not lines:
            return ""
        head = f"【任务看板】（会话 {safe_key(session_key)} · 更新于 {fmt_ts(t.get('updated', now_ts()))}）"
        return head + "\n" + "\n".join(lines)

    # ---------------- 分类检索 ----------------

    def directives(self, limit: int = 6) -> list[dict]:
        """行为指令/约定（注入时优先展示）。"""
        items = [e for e in self.all_entries() if str(e.get("kind", "")).lower() == "directive"]
        items.sort(key=lambda e: (-int(e.get("importance", 3)), -float(e.get("ts", 0))))
        return items[: max(1, limit)]

    def chat_recent(self, limit: int = 3) -> list[dict]:
        """最近的日常对话记忆。"""
        items = [e for e in self.all_entries() if str(e.get("kind", "")).lower() == "chat"]
        items.sort(key=lambda e: -float(e.get("ts", 0)))
        return items[: max(1, limit)]

    # ---------------- 项目归档 ----------------

    def _project_path(self, name: str) -> Path:
        return self.projects_dir / f"{safe_key(name)}.json"

    def project_archive(
        self,
        name: str,
        summary: str = "",
        keywords: Iterable[str] | str | None = None,
        extra: dict | None = None,
    ) -> dict:
        """归档一个项目：写 projects/<name>.json，并在记忆库里留一条 kind=project 的索引。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("项目名不能为空")
        if isinstance(keywords, str):
            keywords = [k.strip() for k in re.split(r"[,，;；\s]+", keywords) if k.strip()]
        keywords = list(keywords or [])
        with self._lock:
            old = self.project_get(name)
            new_summary = (summary or "").strip()
            history = list(old.get("history", []))
            if new_summary and old.get("summary") and new_summary != old.get("summary"):
                # 保留旧摘要，避免重复归档时丢历史
                history.append({"ts": old.get("updated", now_ts()), "summary": old.get("summary")})
                history = history[-3:]
            rec = {
                "name": name,
                "summary": new_summary or old.get("summary", ""),
                "history": history,
                "keywords": sorted(set(keywords) | set(old.get("keywords", []))),
                "created": old.get("created", now_ts()),
                "updated": now_ts(),
                "archives": int(old.get("archives", 0)) + 1,
            }
            if extra:
                rec.update({k: v for k, v in extra.items() if v not in (None, "")})
            atomic_write(self._project_path(name), json.dumps(rec, ensure_ascii=False, indent=1))
            # 记忆库索引（便于统一检索）
            self.add(
                f"[已归档项目] {name}：{rec['summary'][:200]}",
                scope="global",
                tags=["project", name] + rec["keywords"][:6],
                importance=4,
                source="archive",
                kind="project",
            )
            return rec

    def project_get(self, name: str) -> dict:
        p = self._project_path(name)
        if not p.exists():
            return {}
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def project_list(self) -> list[dict]:
        out = []
        for p in sorted(self.projects_dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        out.sort(key=lambda r: -float(r.get("updated", 0)))
        return out

    def project_delete(self, name: str) -> bool:
        p = self._project_path(name)
        if p.exists():
            p.unlink()
            return True
        return False

    def project_search(self, query: str, k: int = 2) -> list[dict]:
        """按项目名/关键词/摘要命中检索归档项目（用于"提起某项目时才召回"）。"""
        q = tokenize(query) if query else set()
        if not q:
            return []
        scored: list[tuple[float, dict]] = []
        for rec in self.project_list():
            hay = " ".join(
                [str(rec.get("name", ""))]
                + list(rec.get("keywords", []))
                + [str(rec.get("summary", ""))]
            )
            toks = tokenize(hay)
            overlap = len(q & toks)
            if overlap <= 0:
                continue
            name_tokens = tokenize(str(rec.get("name", "")))
            score = overlap + (2.0 if q & name_tokens else 0.0)
            scored.append((score, rec))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[: max(1, k)]]

    def render_project(self, name_or_rec: str | dict, limit: int = 300) -> str:
        rec = name_or_rec if isinstance(name_or_rec, dict) else self.project_get(name_or_rec)
        if not rec:
            return ""
        head = f"【历史项目·已归档】{rec.get('name', '?')}（更新于 {fmt_ts(rec.get('updated', 0))}）"
        body = str(rec.get("summary", "")).strip()
        if rec.get("keywords"):
            body += NL + "　关键词：" + "、".join(str(k) for k in rec["keywords"][:8])
        return (head + NL + trim_to(body, limit)) if body else head

    # ---------------- 自动归档扫描 ----------------

    def stale_boards(self, days: float = 3.0) -> list[tuple[str, dict]]:
        """返回超过 N 天没更新的任务看板（用于自动归档）。"""
        cutoff = now_ts() - float(days) * 86400.0
        out: list[tuple[str, dict]] = []
        for p in sorted(self.tasks_dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(rec, dict) or not rec:
                continue
            if float(rec.get("updated", 0)) < cutoff:
                out.append((p.stem, rec))
        return out

    def board_summary(self, rec: dict) -> str:
        """把看板压成一段人可读的项目摘要（无需 LLM）。"""
        parts = []
        if rec.get("goal"):
            parts.append(f"目标：{rec['goal']}")
        if rec.get("progress"):
            parts.append("进度：" + "；".join(str(x) for x in rec["progress"][-4:]))
        if rec.get("decisions"):
            parts.append("关键结论：" + "；".join(str(x) for x in rec["decisions"][-4:]))
        if rec.get("todos"):
            parts.append("未完成：" + "；".join(str(x) for x in rec["todos"][:6]))
        if rec.get("next_step"):
            parts.append(f"下一步：{rec['next_step']}")
        return NL.join(parts) or "（无内容）"

    # ---------------- 项目归档 END ----------------

    # ---------------- 渲染/导出 ----------------

    def rebuild_markdown(self) -> str:
        """生成人可读的 memory.md（按 scope 分组、按重要度+时间排序）。"""
        items = self.all_entries()
        global_items = sorted(
            [e for e in items if e.get("scope") == "global"],
            key=lambda e: (-int(e.get("importance", 3)), -float(e.get("ts", 0))),
        )
        session_items = sorted(
            [e for e in items if e.get("scope") != "global"],
            key=lambda e: -float(e.get("ts", 0)),
        )
        out = ["# 记忆管家 · 长期记忆", "", f"> 更新时间：{fmt_ts(now_ts())}　条目：{len(items)}", ""]
        out.append("## 全局记忆")
        if not global_items:
            out.append("_（空）_")
        for e in global_items:
            tags = ("　#" + " #".join(e.get("tags", []))) if e.get("tags") else ""
            out.append(
                f"- `{e.get('id')}` ★{e.get('importance', 3)}　{fmt_ts(e.get('ts', 0))}{tags}\n  {e.get('content', '')}"
            )
        out.append("")
        out.append("## 会话记忆")
        if not session_items:
            out.append("_（空）_")
        for e in session_items[:200]:
            out.append(
                f"- `{e.get('id')}` [{e.get('scope')}] ★{e.get('importance', 3)}　{fmt_ts(e.get('ts', 0))}\n  {e.get('content', '')}"
            )
        text = "\n".join(out) + "\n"
        atomic_write(self.md_file, text)
        return text

    def export_json(self) -> str:
        return json.dumps(
            {"entries": self.all_entries(), "tasks": self._all_tasks()},
            ensure_ascii=False,
            indent=1,
        )

    def _all_tasks(self) -> dict:
        out: dict[str, Any] = {}
        for p in sorted(self.tasks_dir.glob("*.json")):
            try:
                out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
        return out
