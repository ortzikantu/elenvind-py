"""EVMD 行级指令：图片 @{img,...}（v2 语法）与视频 @{video,...}。

图片语法（v2，完整定义见 docs/EVMD_SPEC.md）：

    @{img, url}                单张图：默认整行居中，按原始比例自适应
    @{img, url, 30%}           指定宽度份额（5%~100%）

份额换算与同行规则（重要）：
- NN% 表示的是"含间隙的份额"，不是图片本身的像素级宽度：同一行内各图
  的总视觉宽度 = 份额之和（例如 2 x 50% 的总视觉宽度 = 1 x 100%，都占满整行）。
- 具体换算（由 img_row_html 生成 calc 宽度）：同一行的 N 张图，先把
  (N-1) 处图间间隙（--img-gap）从行宽预算中扣除，再按份额比例分配：
      实际宽_i = ( 份额和% - (N-1) * var(--img-gap) ) * 份额_i / 份额和
  例如两张 50%：每张 = (100% - 1*gap) / 2，两张加间隙正好等于整行宽度，
  与单张 100% 的"表现宽度"一致；三张 30%：每张 = (90% - 2*gap) / 3，
  整行视觉宽度为 90%（右侧留白 10%）。
- 折行规则：份额之和超过 100% 的行会被切成多个子行（例如 34% x 3 = 102%
  -> "2 张一行 + 1 张一行"），每子行仍按上述公式换算；子行不足一行时居中。
- 移动端（<=700px）由样式表以 flex-basis:100% !important 覆盖，每张图
  独占一行（纵向排列）。

安全性：
- 图片/视频指令必须独占一行；URL 拼入属性前做 HTML 转义 + 协议白名单。
- 排版只依赖 .img-row 的 CSS 与行内 flex-basis，响应式覆盖简单可靠。
"""
import re

from .evmd_inline import escape_html, safe_url

# @{img,url,NN%}：NN 为 5~100 的整数
_IMG_PCT_RE = re.compile(r'^@\{img,\s*([^,}]+?)\s*,\s*(\d{1,3})\s*%\s*\}$')
# @{img,url}：默认单张居中
_IMG_PLAIN_RE = re.compile(r'^@\{img,\s*([^,}]+?)\s*\}$')
# @{video,url}
_VIDEO_RE = re.compile(r'^@\{video,\s*([^,}]+?)\s*\}$')

# 单行指令的长度上限：真实指令行只有几十字符，超长行直接不匹配，
# 避免在畸形超长输入上做无意义的正则回溯。
MAX_DIRECTIVE_LINE = 2048


def _esc_url(url: str):
    """URL 协议白名单校验 + 属性转义；不合法返回 None。

    全项目唯一的指令 URL 转义实现：`@{img}` / `@{video}` 都走这里，
    scheme 白名单与属性转义只在一处维护。
    """
    if not safe_url(url):
        return None
    return escape_html(url)


# ---------- 匹配与渲染 ----------

def match_modern_img(line: str):
    """识别图片行，返回 (url, 宽度百分比 int 或 None) 或 None。

    pct 为 None 表示 "@{img,url}"（默认单张居中，不进并排行）。
    旧式对齐参数（left/right/center 等）不再受支持，按普通文本处理。
    """
    line = line.strip()
    if len(line) > MAX_DIRECTIVE_LINE:
        return None
    m = _IMG_PCT_RE.match(line)
    if m:
        pct = int(m.group(2))
        if 5 <= pct <= 100:
            return m.group(1).strip(), pct
        return None  # 百分比越界：按普通文本处理
    m = _IMG_PLAIN_RE.match(line)
    if m:
        return m.group(1).strip(), None
    return None


def img_row_html(items) -> str:
    """把一组 (url, pct) 渲染为若干 .img-row 容器（含份额->实际宽度换算）。

    换算（详见模块 docstring）：先按"份额和 <= 100"把连续图切成若干子行，
    每子行把 (N-1) 处间隙从预算中扣除后按份额比例分配实际宽度，
    因此两张 50% 的总表现宽度等于一张 100%。
    """
    # 1) 贪心切片：份额和 > 100 即另起一行（34% x 3 -> 2+1）
    slices = []
    current = []
    total = 0
    for url, pct in items:
        if current and total + pct > 100:
            slices.append(current)
            current = []
            total = 0
        current.append((url, pct))
        total += pct
    if current:
        slices.append(current)

    # 2) 每子行换算实际宽度并输出
    rows = []
    for slice_items in slices:
        figures = []
        share_sum = sum(pct for _, pct in slice_items)
        count = len(slice_items)
        for url, pct in slice_items:
            escaped = _esc_url(url)
            if escaped is None:
                continue
            if count == 1:
                # 单张独占一行：不扣除间隙，份额即宽度
                basis = f"{pct}%"
            else:
                ratio = pct / share_sum
                # (份额和% - (N-1)处间隙) 按份额比例分配；var(--img-gap) 见 style.css
                basis = (
                    f"calc(({share_sum}% - {count - 1} * var(--img-gap)) "
                    f"* {ratio:.5f})"
                )
            figures.append(
                f'<figure style="flex-basis:{basis}">'
                f'<img src="{escaped}" alt=""></figure>'
            )
        if figures:
            rows.append('<div class="img-row">' + "".join(figures) + "</div>")
    return "".join(rows)


def render_single_figure(url: str) -> str:
    """渲染默认单张图（@{img,url}）：figure.img-single，居中、宽度自适应。"""
    escaped = _esc_url(url)
    if escaped is None:
        return '<p>Invalid image URL</p>'
    return f'<figure class="img-single"><img src="{escaped}" alt=""></figure>'


def _render_video(url: str) -> str:
    escaped = _esc_url(url)
    if escaped is None:
        return '<p>Invalid video URL</p>'
    return f'<video src="{escaped}" controls></video>'


def parse_directive_line(line: str):
    """解析视频指令行；非视频指令返回 None（图片由 match_modern_img 先行处理）。"""
    line = line.strip()
    if len(line) > MAX_DIRECTIVE_LINE:
        return None
    m = _VIDEO_RE.match(line)
    if m:
        return _render_video(m.group(1).strip())
    return None
