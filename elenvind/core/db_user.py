"""user 表：注册、登录查询、资料修改、逻辑删除与密码升级。

读取约定：
- 邮箱匹配使用 COLLATE NOCASE，登录/查重对大小写不敏感；
  写入侧（`modules/auth/routes.py`、`modules/users/routes.py`）负责先
  `normalize_email` 再落库。
- 所有"取用户"查询都过滤 is_deleted = 0，已注销账号视为不存在。
- **读走 `connect()`，写走 `write_tx()`**：写事务由 Core 的唯一写入口提供
  跨进程 flock 与 BEGIN IMMEDIATE（见 db_base.write_tx）。
"""
import time
import uuid

from .db_base import connect, write_tx


def _invalidate_user_count():
    """用户数变化后让页脚缓存立即失效（个人站写操作极少，直接清空）。"""
    global _user_count_cache
    _user_count_cache = (0.0, _user_count_cache[1])


def create_user(nickname: str, email: str, password_hash: str) -> int:
    """写入新用户，返回自增 id。

    email 列的 UNIQUE 约束是查重的最终权威：并发注册由 SQLite 抛
    sqlite3.IntegrityError，调用方（`modules/auth/routes.py`）据此返回
    "已存在"提示，而不是 500。这里不做 SELECT 预检查，避免 TOCTOU 竞态。

    **邮箱在这里统一规范化**（而不是依赖调用方先转小写）：
    大小写不敏感现在靠"数据规范化"实现（查询改用 BINARY 比较以命中索引，
    见 `get_user_by_email`），如果写入侧漏了一处，就会存进大写邮箱并导致
    查不到 —— 把不变式收在唯一的写入原语里，调用方就不可能漏。
    """
    from .utils import normalize_email

    with write_tx() as conn:
        cursor = conn.execute(
            "INSERT INTO user (nickname, email, password, created_at, nickname_changed_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (nickname, normalize_email(email), password_hash,
             time.strftime("%Y-%m-%dT%H:%M:%S"))
        )
        new_id = cursor.lastrowid
    # 提交成功之后才让缓存失效（提交失败不该影响缓存语义）
    _invalidate_user_count()
    return new_id


def get_user_by_email(email: str):
    """按邮箱查活跃用户（大小写不敏感），用于登录与注册查重。

    **不用 `COLLATE NOCASE`**：`user.email` 的隐式 UNIQUE 索引是 BINARY 排序规则，
    而 `email = ? COLLATE NOCASE` 的排序规则与之不匹配 -> 索引用不上 ->
    **每次登录/注册/改邮箱都全表扫描 user 表**（EXPLAIN QUERY PLAN 实测 `SCAN user`）。

    大小写不敏感靠"数据规范化"而不是"查询时比较"：写入侧一律经
    `normalize_email()` 转小写，v4 迁移也把历史数据统一成小写，
    因此这里的入参先转小写、再用 BINARY 比较即可命中索引
    （实测 `SEARCH user USING INDEX sqlite_autoindex_user_1 (email=?)`）。
    """
    from .utils import normalize_email

    with connect() as conn:
        return conn.execute(
            "SELECT * FROM user WHERE email = ? AND is_deleted = 0",
            (normalize_email(email),)
        ).fetchone()


def get_user_by_id(user_id: int):
    """按 id 查活跃用户，用于每次请求的会话鉴权。"""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM user WHERE id = ? AND is_deleted = 0", (user_id,)
        ).fetchone()


def update_user_profile(user_id: int, nickname=None, email=None):
    """**原子地**更新昵称与/或邮箱（一个事务，要么都成功要么都不变）。

    为什么需要它：昵称与邮箱各自一个 `UPDATE` 会开两个连接、两个事务，
    于是"同时改昵称和邮箱"可能只成功一半 —— 典型场景是并发注册者抢走了
    新邮箱：邮箱更新抛 IntegrityError 回滚，而昵称那条**已经提交**，
    用户看到"邮箱已被占用"，昵称却已经悄悄改了，页面显示的还是旧行。

    传 `None` 表示"这一项不改"。返回是否实际更新了行。
    """
    from .utils import normalize_email

    fields = []
    params = []
    if nickname is not None:
        fields.append("nickname = ?")
        params.append(nickname)
        fields.append("nickname_changed_at = ?")
        params.append(time.strftime("%Y-%m-%dT%H:%M:%S"))
    if email is not None:
        fields.append("email = ?")
        params.append(normalize_email(email))
    if not fields:
        return False
    params.append(user_id)
    with write_tx() as conn:
        cursor = conn.execute(
            f"UPDATE user SET {', '.join(fields)} WHERE id = ? AND is_deleted = 0",
            params)
        updated = bool(cursor.rowcount)
    return updated


def update_user_password(user_id: int, new_password_hash: str):
    """覆盖密码哈希（改密与新格式渐进式 rehash 共用）。"""
    with write_tx() as conn:
        conn.execute("UPDATE user SET password = ? WHERE id = ?", (new_password_hash, user_id))


def delete_user(user_id: int):
    """逻辑删除：昵称置为 Ghost、邮箱与密码清空，is_deleted=1。

    email 列有 UNIQUE 约束，因此用占位地址避免与新注册冲突。
    占位地址必须**不可预测、也不可注册**：

    - 旧实现是 `deleted_<user_id>@example.com` —— user_id 是公开的
      （评论区渲染 `(#id)`）且连续递增，任何人都能在受害者删号**之前**
      抢注 `deleted_<id>@example.com`；于是受害者删号时撞 UNIQUE 约束、
      请求 500，账号被永久锁死删不掉。
    - 现在用随机 UUID + `.invalid`（RFC 2606 保留、永不解析的顶级域）：
      攻击者无法预知 UUID，`.invalid` 也不可能有真实邮箱与之冲突。

    已注销账号的评论仍保留在站内（评论区显示占位昵称）。
    """
    with write_tx() as conn:
        placeholder_email = f"deleted+{uuid.uuid4().hex}@deleted.invalid"
        # WHERE 里带 is_deleted = 0：否则重复删号会再次命中同一行，
        # rowcount 恒为 1，下面那句"already deleted"的报错永远不会触发
        # （行会被反复改写，占位邮箱也跟着变，属于无意义写入）。
        cursor = conn.execute(
            "UPDATE user SET nickname = 'Ghost', email = ?, password = '', is_deleted = 1 "
            "WHERE id = ? AND is_deleted = 0",
            (placeholder_email, user_id)
        )
        if cursor.rowcount == 0:
            # 抛出去 → write_tx() 回滚整个事务，调用方拿到 ValueError
            raise ValueError(f"User {user_id} does not exist or is already deleted")
    _invalidate_user_count()


# 页脚用户数缓存：每个页面渲染都要用，但没必要每个请求都查一次库
_USER_COUNT_TTL = 30.0
_user_count_cache = (0.0, 0)


def get_user_number():
    """活跃用户总数（用于页脚展示），已注销用户不计入；结果缓存 30 秒。"""
    global _user_count_cache
    cached_at, cached_value = _user_count_cache
    now = time.time()
    if cached_at and now - cached_at < _USER_COUNT_TTL:
        return cached_value
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM user WHERE is_deleted = 0").fetchone()
    value = row[0] if row else 0
    _user_count_cache = (now, value)
    return value
