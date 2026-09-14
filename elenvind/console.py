"""简洁的彩色控制台输出工具，仅使用 ANSI 转义序列"""

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"

def _format(color: str, tag: str, msg: str) -> str:
    return f"{BOLD}\033[92mELENVIND: {color}[{tag}]{RESET} {msg}"

def success(msg: str):
    print(_format(GREEN, "SUCCESS", msg))

def error(msg: str):
    print(_format(RED, "ERROR", msg))

def warning(msg: str):
    print(_format(YELLOW, "WARNING", msg))

def info(msg: str):
    print(_format(BLUE, "INFO", msg))

def banner(msg: str):
    """打印醒目的加粗标题横幅"""
    print(f"{CYAN}{BOLD}{msg}{RESET}")
