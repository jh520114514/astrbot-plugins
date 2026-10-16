# -*- coding: utf-8 -*-
"""纯逻辑自测（不需要 AstrBot 运行环境）。

覆盖：存储层（记忆/去重/检索/看板/导出）+ 注入层（预算/边界/跳过规则）。
运行：
    python tests/test_logic.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from store import MemoryStore  # noqa: E402
from injector import (  # noqa: E402
    MARK_BEGIN,
    append_to_system_prompt,
    build_block,
    should_inject,
    trim_text,
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="memkeeper_test_")
    try:
        st = MemoryStore(tmp)
        sess = "test:private:123"

        print("== 存储层：记忆 ==")
        e1 = st.add("姜黄喜欢用 Mac，主力浏览器是 Chrome", tags="偏好,设备", importance=4)
        e2 = st.add("dy40 项目：抖音 X-Perseus 算法还原，核心已 351/351 逐字节一致", tags="项目", importance=5)
        st.add("聊天时说话随意一点，不用 emoji 过量", scope=sess, tags="风格", importance=3)
        check("写入 3 条", len(st.all_entries()) == 3, str(len(st.all_entries())))

        e1b = st.add("姜黄喜欢用 Mac，主力浏览器是 Chrome", tags="偏好", importance=5)
        check("重复内容去重（不新增）", e1b["id"] == e1["id"] and len(st.all_entries()) == 3)
        check("去重时提升重要度", int(e1b["importance"]) >= 4)

        hits = st.search("Chrome", k=3)
        check("关键词检索命中", bool(hits) and "Chrome" in hits[0]["content"], str([h["content"] for h in hits]))
        hits2 = st.search("X-Perseus 算法", k=3)
        check("中文/英文混合检索命中", bool(hits2) and "X-Perseus" in hits2[0]["content"])
        check("无关键词返回最近/最重要", len(st.search("", k=2)) == 2)

        check("按 scope 过滤", len(st.all_entries(scope=sess)) == 1)
        check("删除（标记式）", st.delete(e2["id"]) and e2["id"] not in [e["id"] for e in st.all_entries()])

        print("== 存储层：任务看板 ==")
        st.task_set(sess, goal="把 dy40 纯 Python 化推进到底", progress=["163 轮循环已对齐"], decisions=["只认写序列对齐口径"])
        st.task_set(sess, progress=["loop C 剩 12 字节"], todos="修 pc130 连续 11 字节\n补 worker A", next_step="跑 _o20.py 复验")
        t = st.task_get(sess)
        check("看板 goal 写入", t.get("goal", "").startswith("把 dy40"))
        check("progress 追加累积", len(t.get("progress", [])) == 2, str(t.get("progress")))
        check("todos 拆分", len(t.get("todos", [])) == 2, str(t.get("todos")))
        rt = st.render_task(sess)
        check("看板可渲染", "任务看板" in rt and "下一步" in rt)
        check("看板清空", st.task_clear(sess) and not st.task_get(sess))

        print("== 注入层：内容与边界 ==")
        st.task_set(sess, goal="长任务测试", next_step="继续")
        mems = st.search("Chrome", k=2)
        block = build_block(task_text=st.render_task(sess), memories=mems, budget=1500)
        check("块含开始标记", block.startswith(MARK_BEGIN))
        check("块含声明（不是指令）", "不是用户的新指令" in block)
        check("块含任务看板", "长任务测试" in block)
        check("块含记忆条目", "Chrome" in block)
        check("预算裁剪生效", len(build_block(task_text="x" * 5000, budget=300)) <= 640)

        print("== 注入层：跳过规则（防打断任务的关键）==")
        ok, why = should_inject(enabled=True, has_tool_calls_result=True, system_prompt="", block=block)
        check("工具调用链中不注入", (not ok) and why == "in_tool_loop", why)
        ok, why = should_inject(enabled=True, system_prompt="已有 " + MARK_BEGIN + " 标记", block=block)
        check("已有标记不重复注入", (not ok) and why == "dup_marker", why)
        ok, why = should_inject(enabled=True, system_prompt="", block="", )
        check("无内容不注入", (not ok) and why == "empty", why)
        ok, why = should_inject(enabled=False, system_prompt="", block=block)
        check("总开关关闭不注入", (not ok) and why == "disabled", why)
        ok, why = should_inject(
            enabled=True, system_prompt="", block=block, last_inject_ts=0, min_interval_sec=9999
        )
        check("限频规则生效（首次 ts=0 视为过期，允许）", ok or why == "rate_limited", why)
        ok, why = should_inject(
            enabled=True, system_prompt="", block=block, last_inject_ts=1e18, min_interval_sec=9999
        )
        check("限频生效（刚注入过则跳过）", (not ok) and why == "rate_limited", why)

        print("== 注入层：绝不覆盖 system_prompt ==")
        orig = "你是梦，姜黄的知心朋友。\n工具契约：……（很长很重要的原始提示）"
        merged = append_to_system_prompt(orig, block)
        check("原提示完整保留在开头", merged.startswith(orig))
        check("注入内容在末尾追加", merged.endswith("-->"))
        check("未清空任何内容", orig in merged and len(merged) > len(orig))
        check("空提示时也能单独成立", append_to_system_prompt("", block) == block)

        print("== 其他 ==")
        md = st.rebuild_markdown()
        check("memory.md 生成", "长期记忆" in md and os.path.exists(os.path.join(tmp, "memory.md")))
        js = st.export_json()
        check("导出 JSON 含 entries/tasks", '"entries"' in js and '"tasks"' in js)
        check("trim_text 截断加省略号", trim_text("abcdef", 4).endswith("…"))
        s = st.stats()
        check("统计可用", s["total"] >= 1 and s["tasks"] == 1, str(s))

        print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
