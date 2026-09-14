"""EVMD 块级解析与渲染：主入口（格式规范见 docs/EVMD_SPEC.md）。

模块族（前缀 evmd_）：
- evmd_parser.py     —— 块级引擎 + 文档级 API（本模块）
- evmd_header.py     —— "@@@" TOML 文档头解析（EvmdError 定义于此）
- evmd_inline.py     —— 段落内行内语法（转义/code/粗斜体/链接/脚注引用）
- evmd_directive.py  —— 行级指令（@{img} / @{video}，注册表驱动）

解析模型（行级主循环，按优先级依次判定）：
1. 围栏代码块 ```（内容整体转义，内部不做行内解析；开启前先结束当前段落）；
2. 行级指令 @{img}/@{video}（evmd_directive）与复杂方言 @{table}/@{footnote}；
3. 缩进代码块（4 空格或 Tab，须在段落开始处）；
4. Setext 标题（段落后紧跟 === / --- 行）与分隔线（---/***/___）；
5. 空行、ATX 标题、无序/有序列表、引用、普通段落（支持行尾硬换行）。
6. 输出收尾阶段统一解析脚注编号（@{ref} 占位在此被替换为带序号的上标链接）。

文档级 API：
    evmd_to_html(text)                  纯正文 -> HTML
    parse_document(text, require=...)   (元数据 dict, HTML)
    document_meta(text, require=...)    仅解析元数据（文章索引扫描用）
"""
import re

from .evmd_header import EvmdError, split_document
from .evmd_inline import escape_html, parse_inline
from .evmd_directive import match_modern_img, img_row_html, render_single_figure, parse_directive_line

__all__ = ["EvmdError", "evmd_to_html", "parse_document", "document_meta"]

FENCE = "```"                # 围栏代码块标记
TABLE_MARKER = "@{table}"    # 表格方言起始行
HARD_BREAK = "\ue001"        # 硬换行占位符（在行内解析完成后替换为 <br>）

# ATX 标题：1-6 个 # 后跟空格
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)')
# Setext 标题下划线：===（h1）/ ---（h2），数量不限
_SETEXT_RE = re.compile(r'^(-+|=+)$')
# 分隔线：3 个以上相同的 - / * / _（允许中间任意数量空格）
_HR_RE = re.compile(r'^([-*_])(?: *\1){2,}$')
# 有序列表项：数字 + 点 + 空格
_ORDERED_ITEM_RE = re.compile(r'^\d+\.\s')
# 围栏语言标记白名单（防止任意字符进 class 属性）
_LANG_RE = re.compile(r'^[A-Za-z0-9_+.-]{1,20}$')
# 脚注定义方言：@{footnote,id,text}，id 限 1-32 位字母数字/下划线/连字符
_FOOTNOTE_DEF_RE = re.compile(r'^@\{footnote,\s*([A-Za-z0-9_-]{1,32})\s*,\s*(.*?)\s*\}$')
# 脚注引用占位符：\ue300{id}\ue301（evmd_inline 生成，这里统一编号）
_REF_TOKEN_RE = re.compile('\ue300([A-Za-z0-9_-]{1,32})\ue301')
# 表格分隔行单元格：:--- / ---: / :---: / ---
_TABLE_ALIGN_RE = re.compile(r'^:?-+:?$')


def document_meta(text: str, *, require_header: bool = False) -> dict:
    """只解析文档头，返回元数据 dict（无文档头时为 {}）。

    require_header=True 且缺少 "@@@" 文档头时抛 EvmdError，
    用于文章目录等"必须带元数据"的场景（文章索引只需要头部，不渲染正文）。
    """
    meta, _ = split_document(text)
    if require_header and meta is None:
        raise EvmdError('document header is required: the document must start with a line "@@@"')
    return meta or {}


def parse_document(text: str, *, require_header: bool = False):
    """解析整份 EVMD 文档，返回 (元数据 dict, 渲染后的正文 HTML)。"""
    meta, body = split_document(text)
    if require_header and meta is None:
        raise EvmdError('document header is required: the document must start with a line "@@@"')
    return (meta or {}), evmd_to_html(body)


def evmd_to_html(text: str) -> str:
    """把 EVMD 正文渲染为 HTML（不含文档头）。"""
    footnotes = []                      # [(id, 原始文本)]，按定义顺序编号
    output = _render_lines(text.splitlines(), footnotes)
    html_text = "\n".join(output)
    return _finalize_footnotes(html_text, footnotes)


# ======================= 段落与行内容器 =======================

def _join_paragraph_lines(lines):
    """把段落原始行拼接成一段文本，识别行尾硬换行（两空格 / 行尾反斜杠）。

    硬换行处以 HARD_BREAK 占位符作为行间分隔；普通行之间以空格连接。
    反斜杠规则：行尾奇数个反斜杠 = 硬换行（去掉一个）；偶数个保留给行内转义。
    """
    parts = []
    for line in lines:
        force_break = False
        if line.endswith("  "):
            line = line.rstrip()
            force_break = True
        else:
            # 统计行尾连续反斜杠数量，奇数个触发硬换行
            count = len(line) - len(line.rstrip("\\"))
            if count % 2 == 1:
                line = line[:-1]
                force_break = True
        parts.append((line, force_break))

    joined = ""
    for index, (line, force_break) in enumerate(parts):
        if index > 0:
            joined += HARD_BREAK if parts[index - 1][1] else " "
        joined += line
    return joined


def _flush_paragraph(output, paragraph, level=None):
    """把缓冲的段落行输出为 <p>；level 为 1/2 时输出 Setext 标题。"""
    if not paragraph:
        return
    text = parse_inline(_join_paragraph_lines(paragraph))
    text = text.replace(HARD_BREAK, "<br>")
    if level:
        output.append(f"<h{level}>{text}</h{level}>")
    else:
        output.append(f"<p>{text}</p>")
    paragraph.clear()


# ======================= 块级渲染主循环 =======================

def _render_lines(lines, footnotes):
    output = []
    i = 0
    in_code = False            # 围栏代码块缓冲状态
    code_lines = []
    code_lang = ""             # 围栏声明的语言（如 ```py）
    paragraph = []             # 段落缓冲（保留行尾空白以识别硬换行）

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ----- 1) 围栏代码块（开/关/收集） -----
        if in_code:
            if stripped.startswith(FENCE):
                output.append(f"<pre><code{class_attr(code_lang)}>{escape_html('\n'.join(code_lines))}</code></pre>")
                in_code = False
                code_lines = []
                code_lang = ""
                i += 1
                continue
            code_lines.append(line)
            i += 1
            continue
        if stripped.startswith(FENCE):
            # 开启围栏前先结束当前段落，避免代码块前后文字被错误拼进同一段
            _flush_paragraph(output, paragraph)
            in_code = True
            code_lines = []
            # 围栏后的语言信息串（如 py）经白名单校验后作为 class 输出
            lang = stripped[len(FENCE):].strip()
            code_lang = lang if _LANG_RE.match(lang) else ""
            i += 1
            continue

        # ----- 2) 现代图片行 @{img,url[,NN%]}：连续行收集后按百分比并排 -----
        # 收集规则：连续的新式图片行合并处理；无百分比（默认单张居中）的图片
        # 会打断并排行，自己单独成行；空行/其它内容同样打断（行循环天然保证）。
        modern_img = match_modern_img(line)
        if modern_img is not None:
            _flush_paragraph(output, paragraph)
            items = [modern_img]
            i += 1
            while i < len(lines):
                nxt = match_modern_img(lines[i])
                if nxt is None:
                    break
                items.append(nxt)
                i += 1
            # 连续百分比图进 .img-row（flex 自动折行，3 x 30% 即同一行）；
            # 默认单张（pct=None）输出为居中的 figure.img-single
            row_items = []
            for url, pct in items:
                if pct is None:
                    if row_items:
                        output.append(img_row_html(row_items))
                        row_items = []
                    output.append(render_single_figure(url))
                else:
                    row_items.append((url, pct))
            if row_items:
                output.append(img_row_html(row_items))
            continue

        # ----- 3a) 表格方言 @{table} + 后续 | 行 -----
        if stripped == TABLE_MARKER:
            _flush_paragraph(output, paragraph)
            table_html, next_i = _try_parse_table(lines, i)
            if table_html is not None:
                output.append(table_html)
                i = next_i
                continue
            # 没有合法表格内容时降级为普通段落继续处理

        # ----- 3b) 脚注定义方言 @{footnote,id,text} -----
        footnote_match = _FOOTNOTE_DEF_RE.match(stripped)
        if footnote_match:
            _flush_paragraph(output, paragraph)
            fid, text = footnote_match.group(1), footnote_match.group(2)
            if not any(existing_id == fid for existing_id, _ in footnotes):
                footnotes.append((fid, text))
            i += 1
            continue

        # ----- 3c) 视频指令 @{video,url}（图片已在上方 2 分支处理） -----
        directive_html = parse_directive_line(line)
        if directive_html is not None:
            _flush_paragraph(output, paragraph)
            output.append(directive_html)
            i += 1
            continue

        # ----- 4) 缩进代码块（4 空格或 Tab，且处于段落起点） -----
        if not paragraph and (line.startswith("    ") or line.startswith("\t")) and stripped:
            code_lines = []
            while i < len(lines) and lines[i].strip() != "":
                raw = lines[i]
                if not (raw.startswith("    ") or raw.startswith("\t")):
                    break
                code_lines.append(raw[4:] if raw.startswith("    ") else raw[1:])
                i += 1
            output.append(f"<pre><code>{escape_html('\n'.join(code_lines))}</code></pre>")
            continue

        # ----- 5) Setext 标题 / 分隔线 -----
        setext_match = _SETEXT_RE.match(stripped)
        if setext_match and paragraph:
            level = 1 if stripped.startswith("=") else 2
            _flush_paragraph(output, paragraph, level=level)
            i += 1
            continue
        if _HR_RE.match(stripped) and not paragraph:
            output.append("<hr>")
            i += 1
            continue

        # ----- 6) 空行：结束当前段落 -----
        if not stripped:
            _flush_paragraph(output, paragraph)
            i += 1
            continue

        # ----- 7) ATX 标题 #..###### -----
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            _flush_paragraph(output, paragraph)
            level = len(heading_match.group(1))
            output.append(f"<h{level}>{parse_inline(heading_match.group(2))}</h{level}>")
            i += 1
            continue

        # ----- 8) 无序列表 / 有序列表 / 引用（吞掉连续同类行） -----
        if stripped.startswith(("- ", "* ")):
            _flush_paragraph(output, paragraph)
            items = []
            while i < len(lines) and lines[i].strip().startswith(("- ", "* ")):
                items.append(f"<li>{parse_inline(lines[i].strip()[2:])}</li>")
                i += 1
            output.append("<ul>" + "".join(items) + "</ul>")
            continue

        if _ORDERED_ITEM_RE.match(stripped):
            _flush_paragraph(output, paragraph)
            items = []
            while i < len(lines) and _ORDERED_ITEM_RE.match(lines[i].strip()):
                item_text = _ORDERED_ITEM_RE.sub("", lines[i].strip())
                items.append(f"<li>{parse_inline(item_text)}</li>")
                i += 1
            output.append("<ol>" + "".join(items) + "</ol>")
            continue

        if stripped.startswith("> "):
            _flush_paragraph(output, paragraph)
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(lines[i].strip()[2:])
                i += 1
            output.append(f"<blockquote>{parse_inline(' '.join(quote_lines))}</blockquote>")
            continue

        # ----- 9) 普通段落行：缓冲（保留行尾空白/反斜杠用于硬换行识别） -----
        paragraph.append(line.lstrip())
        i += 1

    # ----- 收尾：残留段落 / 未闭合代码块 -----
    _flush_paragraph(output, paragraph)
    if in_code:
        output.append(f"<pre><code{class_attr(code_lang)}>{escape_html('\n'.join(code_lines))}</code></pre>")

    return output


def class_attr(lang: str) -> str:
    """把校验过的语言名拼成 <code> 的 class 属性；空语言返回空串。"""
    return f' class="language-{lang}"' if lang else ""


# ======================= 表格（@{table} 方言） =======================

def _split_table_row(row: str):
    """把一行 '| a | b |' 拆成单元格列表（允许省略首尾竖线）。"""
    cells = row.strip("|").split("|")
    return [cell.strip() for cell in cells]


def _try_parse_table(lines, i):
    """从 @{table} 行之后收集 | 行并渲染表格。

    结构：表头行 + 可选分隔行（:--- / ---: / :---:）+ 若干数据行。
    返回 (HTML 或 None, 下一行下标)。列数按表头对齐，多余单元格截断、不足留空。
    """
    j = i + 1
    rows = []
    while j < len(lines) and lines[j].strip().startswith("|"):
        rows.append(lines[j].strip())
        j += 1
        if len(rows) >= 500:  # 防御性上限
            break

    if len(rows) < 2:          # 至少需要表头 + 一行内容
        return None, i

    header = _split_table_row(rows[0])
    if not header or not any(header):
        return None, i

    aligns = [None] * len(header)
    data_rows = rows[1:]
    first_cells = _split_table_row(data_rows[0])
    if first_cells and all(_TABLE_ALIGN_RE.match(c) for c in first_cells):
        aligns = [_align_of(cell) for cell in first_cells]
        data_rows = rows[2:]

    def cells_of(row):
        cells = _split_table_row(row)
        # 按表头列数补齐/截断
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        return cells[: len(header)]

    def cell_html(cell, align, tag):
        # 对齐用类实现（cell-left/center/right），不在 HTML 中写内联 style
        cls = f' class="cell-{align}"' if align else ""
        return f"<{tag}{cls}>{parse_inline(cell)}</{tag}>"

    head_html = "".join(cell_html(cell, aligns[k], "th") for k, cell in enumerate(header))
    body_html = ""
    for row in data_rows:
        body_html += "<tr>" + "".join(
            cell_html(cell, aligns[k], "td") for k, cell in enumerate(cells_of(row))) + "</tr>"

    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>", j


def _align_of(cell: str):
    """分隔行单元格 -> 对齐方式：:--- left / ---: right / :---: center / --- 默认。"""
    if cell.startswith(":") and cell.endswith(":"):
        return "center"
    if cell.startswith(":"):
        return "left"
    if cell.endswith(":"):
        return "right"
    return None


# ======================= 脚注收尾（@{footnote} + @{ref}） =======================

def _finalize_footnotes(text: str, footnotes):
    """把正文中的脚注引用占位符替换为带编号的上标链接，并在文末追加脚注列表。"""
    number_of = {fid: index + 1 for index, (fid, _) in enumerate(footnotes)}

    def ref_html(match):
        fid = match.group(1)
        number = number_of.get(fid)
        if number is None:
            return ""  # 引用了未定义的脚注：不渲染
        return (f'<sup class="footnote-ref">'
                f'<a href="#fn-{fid}" id="fnref-{fid}">{number}</a></sup>')

    resolved = _REF_TOKEN_RE.sub(ref_html, text)

    if not footnotes:
        return resolved

    items = []
    for fid, raw_text in footnotes:
        content = _REF_TOKEN_RE.sub(ref_html, parse_inline(raw_text))
        backref = f'<a href="#fnref-{fid}" class="footnote-backref">↩</a>'
        items.append(f'<li id="fn-{fid}">{content} {backref}</li>')
    section = '<section class="footnotes"><h2>Footnotes</h2><ol>' + "".join(items) + "</ol></section>"
    return resolved + "\n" + section
