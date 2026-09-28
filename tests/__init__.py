"""让 tests 目录成为一个可发现测试的包，并提供统一的 unittest 运行入口。

用法（项目根目录）：
    python -m unittest discover -s tests -t . -v
    python -m unittest tests.test_http -v
"""
