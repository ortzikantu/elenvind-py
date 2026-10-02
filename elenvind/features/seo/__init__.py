"""Seo Feature 包：`/robots.txt`、`/sitemap.xml` 与 Open Graph 元数据。

对外只有 `routes.register(router)`。内容全部由 config 与已加载的文章索引推导，
不读数据库、也没有独立的状态，因此没有 `logic` 模块。
"""
