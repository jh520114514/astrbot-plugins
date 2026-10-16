# -*- coding: utf-8 -*-
"""v1.1 新功能自测：项目归档召回 / 指令记忆 / 闲聊记忆 / 自动归档 / 注入槽位。

运行：python tests/test_v11.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from injector import build_block  # noqa: E402
from store import MemoryStore  # noqa: E402

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
    tmp = tempfile.mkdtemp(prefix="memkeeper_v11_")
    try:
        st = MemoryStore(tmp)
        sess = "test:private:abc"

        print("== 指令 / 闲聊 分类 ==")
        st.add("以后回复短一点，别长篇大论", scope="global", importance=4, kind="directive")
        st.add("今晚想早点睡，明天有事", scope=sess, importance=2, kind="chat")
        st.add("姜黄用的是 Windows 11", scope="global", importance=3, kind="fact")
        check("指令可单独取出", len(st.directives()) == 1 and "以后" in st.directives()[0]["content"])
        check("闲聊可单独取出", len(st.chat_recent()) == 1 and "早点睡" in st.chat_recent()[0]["content"])
        check("按类型检索（只 chat）", all(e.get("kind") == "chat" for e in st.search("早睡", kinds=("chat",))))
        check("按类型检索（排除 chat）", all(e.get("kind") != "chat" for e in st.search("", kinds=("fact", "directive"))))
        s = st.stats()
        check("统计含按类型分布", s.get("by_kind", {}).get("directive") == 1 and s.get("by_kind", {}).get("chat") == 1, str(s.get("by_kind")))

        print("== 项目归档 ==")
        rec = st.project_archive("dy40 逆向", summary="把抖音 X-Perseus 算法还原成纯 Python；核心 351/351 逐字节一致；剩余 CF 层未做。",
                                 keywords=["dy40", "X-Perseus", "逆向"])
        check("归档记录写入", rec["name"] == "dy40 逆向" and rec["archives"] == 1)
        check("归档同时建立记忆索引", any(str(e.get("kind")) == "project" for e in st.all_entries()))
        check("项目列表可读", len(st.project_list()) == 1)
        check("按项目名检索命中", bool(st.project_search("dy40")))
        check("按内容关键词检索命中", bool(st.project_search("X-Perseus 算法")))
        check("无关词不命中（不抢占）", st.project_search("今天晚饭吃什么") == [])
        rp = st.render_project("dy40 逆向")
        check("渲染含已归档标记", "已归档" in rp and "351/351" in rp)
        rec2 = st.project_archive("dy40 逆向", summary="补充：文档已整理成成果报告。")
        check("重复归档保留历史摘要", rec2["archives"] == 2 and "成果报告" in rec2["summary"]
              and any("351/351" in h.get("summary", "") for h in rec2.get("history", [])))
        check("删除归档", st.project_delete("dy40 逆向") and st.project_list() == [])

        print("== 自动归档（看板长期未更新）==")
        st.task_set(sess, goal="做一个记忆插件", progress=["写完 store"], todos="补测试")
        # 手动把看板时间往前拨 5 天
        import json
        from pathlib import Path
        tp = Path(tmp) / "tasks" / f"{sess}.json".replace(":", "_")
        board = json.loads(tp.read_text(encoding="utf-8"))
        board["updated"] = time.time() - 5 * 86400
        tp.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
        stale = st.stale_boards(days=3)
        check("识别出过期看板", len(stale) == 1 and stale[0][0] == sess.replace(":", "_"), str(stale))
        summary = st.board_summary(stale[0][1])
        check("看板可压成摘要", "记忆插件" in summary and "补测试" in summary)
        check("3 天内不误伤", st.stale_boards(days=10) == [])

        print("== 注入槽位：归档项目不抢当前任务 ==")
        st2 = MemoryStore(tempfile.mkdtemp(prefix="memkeeper_v11b_"))
        task_txt = "【任务看板】目标：做记忆插件"
        dirs = [{"content": "以后回复短一点", "importance": 4, "kind": "directive"}]
        mems = [{"content": "姜黄用 Windows", "importance": 3, "kind": "fact"},
                {"content": "今晚想早点睡", "importance": 2, "kind": "chat"}]
        proj = [{"name": "dy40 逆向", "summary": "算法已完全还原，剩余 CF 层未做", "keywords": ["dy40"]}]

        b_plain = build_block(task_text=task_txt, directives=dirs, memories=mems, budget=1500)
        check("常规注入含看板", "做记忆插件" in b_plain)
        check("常规注入含指令槽", "长期指令" in b_plain and "回复短一点" in b_plain)
        check("常规注入含闲聊标记", "闲聊" in b_plain)
        check("未提起项目时不注入该项目内容", "dy40 逆向" not in b_plain)

        b_proj = build_block(task_text=task_txt, directives=dirs, memories=mems, projects=proj, budget=1500)
        check("提起项目时注入归档槽", "历史项目" in b_proj and "dy40 逆向" in b_proj)
        check("归档槽有免责说明", "不是当前任务" in b_proj)
        check("归档排在记忆之后（不压住当前任务）", b_proj.find("任务看板") < b_proj.find("历史项目"))
        check("预算仍然生效", len(build_block(task_text="x" * 3000, memories=mems, budget=400)) < 700)
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    code = main()
    print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(code)
