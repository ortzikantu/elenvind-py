"""多语言支持：加载 i18n/*.toml 文案表，提供取词与语言规范化。

约定：
- 站点界面语言由 config.toml 的 locale 字段设定（en / zh / ja），不做浏览器自动检测；
- 语言代码固定为 en / zh / ja（可扩展：往 i18n/ 加文件并在 SUPPORTED_LANGS 登记）；
- 文案文件位于项目根目录 i18n/ 下，键名即代码里的引用标识；
- 取词回退链：目标语言 -> en -> 键名本身（不会抛 KeyError）。
"""
import logging
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_LANGS = ("en", "zh", "ja")

_TABLES = {}          # {lang: {key: text}}
_I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"


def load() -> None:
    """重新加载全部语言文案表（启动时调用；文件缺失/损坏时回退空表）。"""
    global _TABLES
    _TABLES = {}
    for lang in SUPPORTED_LANGS:
        path = _I18N_DIR / f"{lang}.toml"
        try:
            with open(path, "rb") as f:
                _TABLES[lang] = tomllib.load(f)
        except FileNotFoundError:
            logger.warning("i18n file missing: %s", path)
            _TABLES[lang] = {}
        except Exception as e:
            logger.error("Failed to load i18n file %s: %s", path, e)
            _TABLES[lang] = {}


def t(lang: str, key: str, **kwargs) -> str:
    """取词并做 {name} 格式化；回退链：目标语言 -> en -> 键名。"""
    table = _TABLES.get(lang) or {}
    text = table.get(key)
    if text is None and lang != "en":
        text = _TABLES.get("en", {}).get(key)
    if text is None:
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            # 文案缺参/参数不匹配时原样返回，避免页面 500
            return text
    return text


def normalize(lang) -> str:
    """把配置值规整为受支持语言；无法识别回退 "en"。

    兼容 "zh-CN"/"zh_TW"/"ja-JP" 这类带区域后缀的写法，统一取其主语言。
    """
    value = str(lang or "").lower().strip()
    if value in SUPPORTED_LANGS:
        return value
    base = value.split("-")[0].split("_")[0]
    return base if base in SUPPORTED_LANGS else "en"


# 模块导入即加载（与 config 一致：文件变更需重启生效）
load()
