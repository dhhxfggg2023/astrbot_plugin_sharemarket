"""日志基建自检：验证轮转文件真的写出、AstrBot 桥接真的转发、级别真的生效。

不依赖 astrbot（桥接目标用假对象），因此可离线跑。
注意：文件写在插件根目录（工作区根可写），跑完自行清理。
"""
import importlib
import logging
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PKG = os.path.basename(PLUGIN_DIR)

logmod = importlib.import_module(f"{PKG}.log")
get_logger, set_level = logmod.get_logger, logmod.set_level
bind_astrbot_logger, setup_file_logging = (logmod.bind_astrbot_logger,
                                           logmod.setup_file_logging)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


TOKEN = "落盘自检-9f3a"
FAKE_LEVEL = 0


class FakeAstrBotLogger:
    """模拟 astrbot.api.logger 的三个方法。"""

    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(("info", msg))

    def warning(self, msg):
        self.lines.append(("warning", msg))

    def error(self, msg):
        self.lines.append(("error", msg))


def main():
    print("== 1. 文件日志 ==")
    # 只加载模块不重复挂 handler：先清掉可能存在的旧 handler
    root = logging.getLogger(logmod.ROOT)
    for h in list(root.handlers):
        if h.__class__.__name__ == "RotatingFileHandler":
            root.removeHandler(h)
    path = setup_file_logging(PLUGIN_DIR, "DEBUG")
    check("setup_file_logging 返回路径", path is not None, str(path))
    if path is None:
        return FAIL
    logger = get_logger("selftest")
    logger.info("中文与 emoji 一起写 📋 token=%s", TOKEN)
    logger.warning("警告行 token=%s", TOKEN)
    for h in root.handlers:
        h.flush()
    check("日志文件已创建", os.path.exists(path), str(path))
    content = open(path, encoding="utf-8").read()
    check("INFO 行写入文件", "中文与 emoji 一起写 📋" in content)
    check("WARNING 行写入文件", "警告行" in content)
    check("token 可检索", content.count(TOKEN) >= 2)
    check("含命名空间", f"{logmod.ROOT}.selftest" in content)
    check("重复调用不重复挂 handler",
          setup_file_logging(PLUGIN_DIR, "DEBUG") is not None
          and sum(1 for h in root.handlers
                  if h.__class__.__name__ == "RotatingFileHandler") == 1)

    print("== 2. AstrBot 桥接 ==")
    fake = FakeAstrBotLogger()
    bind_astrbot_logger(fake)
    get_logger("bridge").info("桥接 info")
    get_logger("bridge").warning("桥接 warning")
    get_logger("bridge").error("桥接 error")
    kinds = [k for k, _ in fake.lines]
    check("info 转发", "info" in kinds, str(kinds))
    check("warning 转发", "warning" in kinds, str(kinds))
    check("error 转发", "error" in kinds, str(kinds))
    check("转发内容带前缀", any("[股市]" in m for _, m in fake.lines),
          str(fake.lines[:1]))
    before = len(fake.lines)
    bind_astrbot_logger(fake)
    get_logger("bridge").info("二次绑定测试")
    check("重复绑定不重复转发", len(fake.lines) == before + 1,
          f"{before}→{len(fake.lines)}")

    print("== 3. 级别控制 ==")
    set_level("ERROR")
    n = len(fake.lines)
    get_logger("level").info("这条不该出现")
    get_logger("level").warning("这条也不该出现")
    get_logger("level").error("这条应该出现")
    check("ERROR 级别下屏蔽 info/warning", len(fake.lines) == n + 1,
          f"{n}→{len(fake.lines)}")
    check("ERROR 级别下 error 仍输出",
          fake.lines[-1][1].endswith("这条应该出现"), str(fake.lines[-1]))
    set_level("DEBUG")
    n = len(fake.lines)
    get_logger("level").debug("DEBUG 可见")
    check("DEBUG 级别恢复可见", len(fake.lines) == n + 1)

    print("== 4. 异常堆栈 ==")
    n = len(fake.lines)
    try:
        raise ValueError("自检异常")
    except ValueError:
        get_logger("selftest").exception("捕获到异常")
    check("exception 输出堆栈", len(fake.lines) > n
          and "Traceback" in fake.lines[-1][1], str(fake.lines[-1])[:80])

    print(f"\n==== 结果: {PASS} 通过, {FAIL} 失败 ====")
    # 清理
    for h in list(root.handlers):
        if h.__class__.__name__ == "RotatingFileHandler":
            h.close()
            root.removeHandler(h)
    for p in [path] + [f"{path}.{i}" for i in range(1, 6)]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            print(f"（保留 {os.path.basename(p)}：删除被拒）")
    return FAIL


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
