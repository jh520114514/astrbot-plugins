
import sys, os, importlib
sys.path.insert(0, r"C:\Users\Administrator\.astrbot_launcher\instances\f3436f89-9e75-4ef3-a1f9-6b6507c1994f\core")
sys.path.insert(0, r"C:\Users\Administrator\.astrbot_launcher\instances\f3436f89-9e75-4ef3-a1f9-6b6507c1994f\core\data\workspaces\default_1904368419_FriendMessage_51CE673CC0EFBBEAC4BA1D54A8BBD400")
os.environ.setdefault("ASTRBOT_TEST_IMPORT", "1")  # 仅导入检查，不启动核心
try:
    mod = importlib.import_module("astrbot_plugin_memory_keeper.main")
    print("导入成功:", mod.__file__)
    cls = getattr(mod, "MemoryKeeperPlugin", None)
    print("插件类:", cls.__name__ if cls else "未找到")
    # 列出被注册的钩子
    import inspect
    hooks = [n for n, f in vars(cls).items() if callable(f) and not n.startswith("__")]
    print("方法:", ", ".join(sorted(hooks)))
    # 检查装饰器是否挂上
    for n in ("on_llm_request", "auto_capture", "mem", "memory_save", "memory_search", "task_update", "task_read"):
        f = getattr(cls, n, None)
        print("   %-16s %s" % (n, "存在" if f else "缺失"))
except Exception as e:
    import traceback; traceback.print_exc()
