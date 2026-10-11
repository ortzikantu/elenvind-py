"""版本号（页脚与 <meta name="generator"> 的唯一来源）。

`stage` 为空串表示正式发布版；开发/候选版写 "dev" / "rc1" 等，会拼成
`v1.0.0-dev` 这种形式。改这里就等于改对外可见的版本（模板里读 `elenvind_version`）。
"""
major = 1
minor = 0
patch = 0
stage = ""


def get_version() -> str:
    base = f"v{major}.{minor}.{patch}"
    return f"{base}-{stage}" if stage else base
