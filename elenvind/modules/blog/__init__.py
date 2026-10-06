"""blog 模块：文章索引、正文渲染、文章页与评论。

公开入口（装配层只从这两处取能力）：
- `logic`：业务操作与模板数据组装；
- `routes.register(router, render_not_found=...)`：把路由声明装到 router 上。

给其它模块用的数据通过公开函数暴露（`logic.sitemap_articles`），
由装配层注入，而不是让别的模块 import 本模块内部实现。
只依赖 Core。
"""
