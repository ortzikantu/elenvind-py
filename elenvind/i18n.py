"""多语言支持：加载 i18n/*.toml 文案表，提供取词与语言规范化。

约定：
- 站点界面语言由 config.toml 的 locale 字段设定（en / zh / ja），不做浏览器自动检测；
- 语言代码固定为 en / zh / ja（可扩展：往 i18n/ 加文件并在 SUPPORTED_LANGS 登记）；
- 文案文件位于项目根目录 i18n/ 下，键名即代码里的引用标识；
- 取词回退链：目标语言 -> en -> 键名本身（不会抛 KeyError）。
- 本模块**导入时不读磁盘**：文案在 lifespan 启动阶段由 load() 统一加载。
  加载失败（文件缺失、TOML 语法错误、值不是字符串）会直接抛异常，
  让服务启动失败而不是运行到某个页面才发现文案坏了。
"""
import logging
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_LANGS = ("en", "zh", "ja")

_TABLES = {}          # {lang: {key: text}}
_I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"


class I18nError(ValueError):
    """文案表加载失败（缺失/语法错误/类型错误）。"""


def _validate_table(lang: str, table) -> dict:
    if not isinstance(table, dict):
        raise I18nError(f"i18n/{lang}.toml must contain a TOML table")
    cleaned = {}
    for key, value in table.items():
        if not isinstance(value, str):
            raise I18nError(f"i18n/{lang}.toml: value of {key!r} must be a string")
        cleaned[key] = value
    return cleaned


def load() -> None:
    """重新加载全部语言文案表（启动时调用）。

    文件缺失只记 warning（此时界面会退回 en 或键名，不至于整站不可用），
    但文件存在却解析失败 / 值类型不对会抛 I18nError，让启动阶段暴露问题。
    """
    global _TABLES
    tables = {}
    for lang in SUPPORTED_LANGS:
        path = _I18N_DIR / f"{lang}.toml"
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError:
            logger.warning("i18n file missing: %s", path)
            tables[lang] = {}
            continue
        except tomllib.TOMLDecodeError as e:
            raise I18nError(f"invalid TOML in {path}: {e}") from e
        tables[lang] = _validate_table(lang, raw)

    if not tables.get("en"):
        raise I18nError(f"i18n/{'en'}.toml is required but missing or empty")
    _TABLES = tables


def is_loaded() -> bool:
    return bool(_TABLES)


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
