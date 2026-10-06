"""Core Auth：认证与授权。

认证（谁是你）：
    verify_credentials(email, password) -> (user_row | None, reason)
    login_user() 之后由 session Core 颁发会话。
    密码哈希/校验/渐进式 rehash 全部复用 security.py，不在这里重写。

授权（你能做什么）——最小语义，只四档，不建权限框架：

    public         任何人（含匿名）
    authenticated  已登录
    admin          config.toml 的 admin_user_id
    owner          "资源属于你"，由业务显式调用 require_owner()

Route 通过声明使用：

    @route("/settings", methods=["GET", "POST"], auth="required")
    @route("/admin", methods=["GET"], permission="admin")

`auth=` 与 `permission=` 最终走**同一套**判定（`check_permission`）：
`auth="required"` 只是 `auth="authenticated"` 的别名。两者的区别仅在语义命名：
`auth` 回答"要不要登录"，`permission` 回答"要哪个档位"。
声明了未知取值会在**路由注册时**直接报错，而不是留到运行时失效开放。
"""
from __future__ import annotations

from .db_user import get_user_by_email, update_user_password
from .security import (
    admin_id,
    hash_password,
    is_admin,
    password_needs_rehash,
    verify_password,
)

#: 权限档位（字符串常量，避免魔法值散落）
PUBLIC = "public"
AUTHENTICATED = "authenticated"
ADMIN = "admin"

#: `auth=` 的别名：路由里写 `auth="required"` 表达"必须已登录"。
#: 保留它是为了可读性（"这个路由需要认证"比 `auth="authenticated"` 更直白），
#: 但它必须与 AUTHENTICATED 等价，否则又会变成"声明了却没人认识"。
REQUIRED = "required"

#: 已知档位。**不在这个集合里的值一律拒绝**（失效关闭）：
#: 把 `permission="amin"` 这类拼写错误当成"无限制"是最危险的失效方式。
KNOWN_PERMISSIONS = frozenset({PUBLIC, AUTHENTICATED, ADMIN})

#: `auth=` 允许的取值（`None`/`""` 表示未声明 = public）。
#: **不含 owner**：资源归属不是路由级声明，必须由业务显式调用 require_owner()，
#: 因为 Core 无从知道"哪个资源"属于谁。
KNOWN_AUTH = frozenset({PUBLIC, AUTHENTICATED, ADMIN, REQUIRED})

#: `auth=` 里这些值表示"要求已登录或更强"
AUTH_REQUIRED_VALUES = frozenset({AUTHENTICATED, REQUIRED, ADMIN})

#: 这些值表示"不限制"（None / 空串来自未声明的路由）
NO_REQUIREMENT = (None, "")


def normalize_auth(value):
    """把 `auth=` 的取值规范化成权限档位；未声明返回 None。

    `"required"` 是 `"authenticated"` 的别名 —— 两者必须走**同一条**判定路径，
    否则又会出现"声明了但闸门不认识"的失效开放（历史上正是这个 bug）。
    """
    if value in NO_REQUIREMENT:
        return None
    return AUTHENTICATED if value == REQUIRED else value

#: 认证结果原因（供模块映射成提示文案，不直接暴露给用户）
OK = "ok"
BAD_CREDENTIALS = "bad_credentials"
NEEDS_REHASH = "needs_rehash"


def verify_credentials(email: str, password: str):
    """校验邮箱+密码。

    返回 (user_row | None, ok: bool, rehashed: bool)。
    - 账号不存在与密码错误返回同样的 (None, False, False)，调用方应给一致提示；
    - 登录成功且哈希需要升级时**透明 rehash**，不影响本次登录结果；
    - 计时侧信道：账号不存在时也执行一次等量哈希校验（见 view 层调用点）。
    """
    user = get_user_by_email(email)
    if user is None:
        return None, False, False
    stored = user["password"]
    if not verify_password(password, stored):
        return None, False, False
    rehashed = False
    if password_needs_rehash(stored):
        try:
            update_user_password(user["id"], hash_password(password))
            rehashed = True
        except Exception:      # noqa: BLE001 - 升级失败不影响本次登录
            rehashed = False
    return user, True, rehashed


def check_permission(request, requirement: str) -> bool:
    """按声明式权限档位判断是否放行。

    失效关闭：未知档位（拼写错误、未来新增但未实现的值）一律 **拒绝**，
    绝不"不认识就放行"。
    """
    if requirement in NO_REQUIREMENT or requirement == PUBLIC:
        return True
    if requirement not in KNOWN_PERMISSIONS:
        return False
    # 用户对象是 sqlite3.Row（不支持 getattr），用下标访问
    try:
        user = request.user
    except AttributeError:
        user = None
    if requirement == AUTHENTICATED:
        return user is not None
    if requirement == ADMIN:
        return is_admin(user)
    return False


def require_owner(user, resource) -> bool:
    """资源归属判断：业务显式调用（例如"只有文章/评论作者可操作"）。

    Core 只提供判定能力，不替业务定义"谁是 owner"。
    """
    if user is None or resource is None:
        return False
    try:
        return int(resource["user_id"]) == int(user["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def is_admin_user(user) -> bool:
    """给模板/视图用的管理员判定（委托 security.is_admin）。"""
    return is_admin(user)
