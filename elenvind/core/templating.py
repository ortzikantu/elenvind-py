"""Core 模板渲染：Jinja2 的唯一入口。

    模块 ──► render_template("blog/article.html", {...}) ──► Jinja2

规则（Core Contract 的一部分，由 tests/test_core_contract.py 静态守卫）：
- 业务代码**不得**自行创建 Jinja `Environment` / `FileSystemLoader`，
  也不得 import jinja2；模板只能通过本模块渲染。
- 自动转义默认开启：模板里 `{{ value }}` 永远被转义，只有显式 `|safe`
  才能输出原样 HTML（Markdown 渲染结果已由 render_markdown 标注为可安全输出）。
- 模板目录来自运行时配置 `templates_dir`（相对项目根），默认 `elenvind/templates`。
- 环境是进程级单例（Jinja 官方推荐做法：Environment 应复用，模板本身有缓存）。
  配置文件变更不热生效，与其它配置一致。
"""
from pathlib import Path

from jinja2 import (
    ChainableUndefined,
    Environment,
    FileSystemLoader,
    select_autoescape,
)

from .config import config, resolve_path

#: 默认模板目录（包内），可被 config.toml 的 templates_dir 覆盖
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_environment = None
_environment_dir = None


def templates_dir() -> Path:
    """模板目录（相对项目根解析），默认 elenvind/templates。"""
    return resolve_path(config.get("templates_dir"), DEFAULT_TEMPLATES_DIR)


def get_environment() -> Environment:
    """返回 Jinja2 Environment 单例（目录变化时重建，供测试与配置切换）。

    - autoescape：`.html` / `.xml` / `.j2` 默认转义，其余（纯文本邮件等）不转义。
    - undefined：用 `ChainableUndefined` 而不是 `StrictUndefined`。
      理由：模板里 `{% if optional %}` 这种"可选变量"是正常写法，
      StrictUndefined 会让它直接 500；而 ChainableUndefined 只允许
      "未定义 -> 假值/可继续取属性"，一旦真的把未定义变量**输出**到页面上
      仍然会渲染成空串（明显可见），足以在测试里被发现。
      模板拼写错误仍由 Core Contract 测试（渲染全部页面）兜住。
    - trim_blocks / lstrip_blocks：模板可读性（控制语句不产生多余空行）。
    - auto_reload：开发时改模板即时生效；生产同样安全（按 mtime 判断）。
    """
    global _environment, _environment_dir
    directory = templates_dir()
    if _environment is None or _environment_dir != str(directory):
        if not directory.is_dir():
            raise RuntimeError(f"templates directory not found: {directory}")
        _environment = Environment(
            loader=FileSystemLoader(str(directory)),
            autoescape=select_autoescape(
                enabled_extensions=("html", "xml", "j2", "svg"),
                default_for_string=True,
                default=True,
            ),
            undefined=ChainableUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=True,
            keep_trailing_newline=True,
        )
        _install_globals(_environment)
        _environment_dir = str(directory)
    return _environment


def _install_globals(environment: Environment) -> None:
    """注册 Core 提供的模板全局（模板作者不需要了解内部实现）。

    - `csrf_input()`：输出 CSRF 隐藏域；令牌取自当前请求，无请求时返回空串。
    - `t(key, **kw)`：取词（语言取自当前请求，无请求时用配置 locale）。
    - `static_url(kind, default)`：静态资源 URL（来自 [static] 配置）。
    - `is_admin_user(user)`：管理员判定（模板里做徽章显示等）。
    - `theme_icons`：主题切换图标（内联 SVG，站点零 JS 的一部分）。
    """
    from .auth import is_admin_user
    from .csrf import csrf_input_html
    from .i18n import t as _translate
    from .version import get_version

    def translate(key, **kwargs):
        from .context import current_lang
        return _translate(current_lang(), key, **kwargs)

    environment.globals.update(
        csrf_input=csrf_input_html,
        t=translate,
        static_url=_static_url,
        css_url=stylesheet_url,
        icon_url=icon_url,
        is_admin_user=is_admin_user,
        elenvind_version=get_version(),
        theme_icons=THEME_ICONS,
    )
    environment.filters["datetime"] = _format_datetime
    environment.filters["date"] = _format_date


#: 主题切换图标（内联 SVG，避免额外请求；站点零 JS）
THEME_ICONS = {
    "dark": ('<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" '
             'viewBox="0 0 24 24"><path fill="var(--text)" d="M10 2c-1.82 0-3.53.5-5 '
             '1.35C8 5.08 10 8.3 10 12s-2 6.92-5 8.65C6.47 21.5 8.18 22 10 22a10 10 0 0 0 '
             '10-10A10 10 0 0 0 10 2" /></svg>'),
    "light": ('<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" '
              'viewBox="0 0 24 24"><path fill="var(--text)" d="M12 18a6 6 0 0 1-6-6a6 6 0 0 1 '
              '6-6a6 6 0 0 1 6 6a6 6 0 0 1-6 6m8-2.69L23.31 12L20 8.69V4h-4.69L12 .69L8.69 4H4v4.69'
              'L.69 12L4 15.31V20h4.69L12 23.31L15.31 20H20z"/></svg>'),
}


def _static_url(kind: str, default: str = "") -> str:
    """取 [static] 配置里的 URL：static_url("css", "/style.css")。"""
    static_cfg = config.get("static") or {}
    value = str(static_cfg.get(kind) or "").strip()
    return value or default


def stylesheet_url() -> str:
    """样式表地址，按"配置优先、缺省兜底"解析。

    规则（模板里用 `{{ css_url() }}`，不要在模板里写这套判断）：

    1. `[static].css` 有值 -> 用它（站内路径或 CDN 绝对地址）；
    2. 为空且 `use_builtin_css = true`（默认）-> 用应用发出的缺省样式表
       `/css/style.css`（磁盘上是 `elenvind/static/css/style.css`）；
    3. 为空且 `use_builtin_css = false` -> 返回空串，模板不输出 `<link>`
       （给"我有自己的样式方案"留的出口）。

    刻意不做的事：不去探测 `config.toml` 里那个路径对应的文件是否存在。
    配置就是声明，探测文件会让行为随部署环境的目录状态漂移。
    """
    configured = str(((config.get("static") or {}).get("css")) or "").strip()
    if configured:
        return configured
    if config.get("use_builtin_css", True) is False:
        return ""
    from .assets import DEFAULT_CSS_PATH
    return DEFAULT_CSS_PATH


def icon_url() -> str:
    """站点图标地址，同样"配置优先、缺省兜底"。

    1. `[static].favicon` 有值 -> 用它；
    2. 为空且 `elenvind/static/imgs/favicon.ico`（或 .png）存在 -> 用缺省图标；
    3. 都没有 -> 返回空串，模板**不输出** `<link rel="icon">`。

    与 CSS 不同的一点：这里要探测缺省文件是否存在。因为图标是浏览器会主动
    去取的资源，输出一个不存在的地址只会换来一条 404；而样式表有明确开关，
    不存在时页面本来就"没样式"，语义一致。
    """
    configured = str(((config.get("static") or {}).get("favicon")) or "").strip()
    if configured:
        return configured
    from .assets import default_icon_path
    return default_icon_path() or ""


def _format_datetime(value) -> str:
    from .utils import format_datetime
    return format_datetime(value)


def _format_date(value) -> str:
    from .utils import format_date
    return format_date(value)


def render_template(name: str, context=None) -> str:
    """渲染模板并返回 HTML 字符串。

    context 必须包含模板需要的全部数据；Core 会自动注入：
    `request` / `user` / `csrf_token` / `lang` / `config`（若调用方未提供）。
    """
    from .context import build_render_context

    merged = build_render_context(context or {})
    template = get_environment().get_template(name)
    return template.render(**merged)


def reset_environment() -> None:
    """丢弃缓存的 Environment（测试夹具在切换 templates_dir 时使用）。"""
    global _environment, _environment_dir
    _environment = None
    _environment_dir = None
