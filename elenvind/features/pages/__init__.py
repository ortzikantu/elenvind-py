"""Pages Feature 包：自定义页面（`custom_pages/*.md`）与主题偏好入口。

对外只有两件事：
- `logic`：扫描/缓存 `custom_pages_dir` 下的 Markdown，并提供 `get_page(slug)`；
- `routes.register(router, render_not_found=...)`：`/theme` 与 `/<slug>` 兜底路由。

`/<slug>` 声明为 `fallback=True`，因此它只在其它的固定路径都没匹配时才接手；
自定义页面因此不会遮蔽 `/login`、`/article/...` 这类真实路由。
"""
