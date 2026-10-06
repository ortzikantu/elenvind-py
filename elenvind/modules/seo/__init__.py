"""seo 模块：`/robots.txt` 与 `/sitemap.xml`。

公开入口：`routes.register(router, articles=...)`。
`articles` 是装配层注入的公开数据源（`blog.sitemap_articles`），因此本模块
**不 import 任何其它业务模块**。内容全部由 config 与注入的数据推导，
不读数据库、也没有独立的状态，因此没有 `logic` 模块。
"""
