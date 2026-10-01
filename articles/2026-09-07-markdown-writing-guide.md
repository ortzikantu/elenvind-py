+++
title = "Markdown 写作速查"
date = 2026-09-07T10:00:00+08:00
authors = ["Hao Wu"]
tags = ["markdown", "写作"]
summary = "本站正文格式速查：front matter 字段与本项目启用的 Markdown 扩展。"
+++
# Markdown 写作速查

本站文章是**标准 Markdown**，没有自有标记语言。文件放在 `articles/` 下，
扩展名 `.md`，正文之前是一段 TOML front matter。

## 1. Front matter

用 `+++` 包起来的 TOML 段落，位于文件最开头：

```toml
+++
title = "文章标题"          # 必填
date = 2026-09-07T10:00:00+08:00   # 建议填写，用于首页排序
lastmod = 2026-10-01T09:00:00+08:00 # 可选
authors = ["Hao Wu"]         # 可选
tags = ["python", "web"]     # 可选
summary = "一句话摘要"        # 可选
+++
```

- `title` 必填；缺失或 front matter 未闭合的文章会被索引跳过，并在日志里点名文件。
- `date` 决定首页排序（倒序），也能被 `lastmod` 覆盖更新日期。
- 自定义页面（`custom_pages/`）**不需要** front matter，直接写正文。

## 2. 基础语法

- **加粗**用 `**加粗**`，*斜体* 用 `*斜体*`，~~删除线~~ 用 `~~删除线~~`
- 行内代码：`pip install -r requirements.txt`
- 链接：[Codeberg](https://codeberg.org/ortzikantu/elenvind)
- 一至六级标题用 `#` 到 `######`
- 引用块、无序列表、有序列表、分隔线都是标准写法

> 引用块里同样可以使用**行内语法**与 `代码`。

1. 第一步：写内容
2. 第二步：`git commit`
3. 第三步：部署

## 3. 项目启用的扩展

| 扩展 | 效果 |
| :--- | :--- |
| 表格 | 用 `\|` 分隔的 GFM 表格（本表即是） |
| 脚注 | 正文写 `[^id]`，文末写 `[^id]: 内容` |
| 代码围栏 | 三个反引号加语言名，输出 `class="language-xxx"` |
| 属性列表 | 图片后接 `{: width="50%" }` 等属性 |

脚注示例：这里的说法有出处[^src]。

[^src]: 脚注正文也支持**行内语法**。

## 4. 图片与视频

图片用标准 Markdown 语法，尺寸靠属性列表：

```markdown
![说明文字](https://example.com/photo.png)
![半宽图片](https://example.com/photo.png){: width="50%" }
```

视频用本项目的 `@video(url)` 指令，**单独占一行**，默认带原生控制条：

```markdown
@video(https://example.com/movie.mp4)
```

实测效果：

@video(https://www.w3schools.com/html/movie.mp4)

地址只允许 `http(s)` 与站内相对路径；`javascript:`、`data:`、`file:` 会被拒绝，
并原样显示成文字（方便你一眼看出写错了）。指令写在代码围栏或行内代码里不会被解析。

> 注意：**不要**直接写 `<video>` 原始 HTML —— 正文里的原始 HTML 会被转义成可见文字
> （这是安全默认值），只有上面这个指令才会真正生成播放器。

## 5. 安全边界（写作时值得知道）

正文里的**原始 HTML 会被转义成可见文字**，不会作为标签执行：

    <script>alert(1)</script>   ->  页面上显示为这行文字本身

链接与图片的 URL 只允许 `http` / `https` / `mailto` 以及站内相对路径，
`javascript:`、`data:`、`vbscript:`、`file:` 会被丢弃。
因此正文可以放心写，安全由框架统一负责。
