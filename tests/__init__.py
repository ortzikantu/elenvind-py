"""Elenvind 测试包。

运行（在项目根目录）：

    .venv/bin/python -m unittest discover -s tests -v

不依赖任何第三方测试框架：只用标准库 `unittest`，与项目"标准库为核心"的
约束一致。测试**不会**读写仓库里的 config.toml / sqlite.db / logs/ ——
配置由 `tests/support.py` 注入，数据库、日志与内容都落在临时目录里。
"""
