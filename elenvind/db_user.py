"""user 表：注册、登录查询、资料修改、逻辑删除与密码升级。

读取约定：
- 邮箱匹配使用 COLLATE NOCASE，登录/查重对大小写不敏感；
  写入侧（view_register / view_user）负责先 normalize_email 再落库。
- 所有“取用户”查询都过滤 is_deleted = 0，已注销账号视为不存在。
- 所有连接统一走 `with closing(...)`，任何返回路径都会关闭连接。
"""
import time
from contextlib import closing

from .db_base import get_connection


def _invalidate_user_count():
    """用户数变化后让页脚缓存立即失效（个人站写操作极少，直接清空）。"""
    global _user_count_cache
    _user_count_cache = (0.0, _user_count_cache[1])


def create_user(nickname: str, email: str, password_hash: str) -> int:
    """写入新用户，返回自增 id。

    email 列的 UNIQUE 约束是查重的最终权威：并发注册由 SQLite 抛
    sqlite3.IntegrityError，调用方（view_register）据此返回"已存在"提示，
    而不是 500。这里不做 SELECT 预检查，避免 TOCTOU 竞态。
    """
    with closing(get_connection()) as conn:
        cursor = conn.execute(
            "INSERT INTO user (nickname, email, password, created_at, nickname_changed_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (nickname, email, password_hash, time.strftime("%Y-%m-%dT%H:%M:%S"))
        )
        conn.commit()
        _invalidate_user_count()
        return cursor.lastrowid


def get_user_by_email(email: str):
    """按邮箱查活跃用户（大小写不敏感），用于登录与注册查重。"""
    with closing(get_connection()) as conn:
        return conn.execute(
            "SELECT * FROM user WHERE email = ? COLLATE NOCASE AND is_deleted = 0", (email,)
        ).fetchone()


def get_user_by_id(user_id: int):
    """按 id 查活跃用户，用于每次请求的会话鉴权。"""
    with closing(get_connection()) as conn:
        return conn.execute(
            "SELECT * FROM user WHERE id = ? AND is_deleted = 0", (user_id,)
        ).fetchone()


def update_user_nickname(user_id: int, new_nickname: str):
    """更新昵称并记录更改时间（供“一年只能改一次”策略计时）。"""
    with closing(get_connection()) as conn:
        conn.execute(
            "UPDATE user SET nickname = ?, nickname_changed_at = ? WHERE id = ?",
            (new_nickname, time.strftime("%Y-%m-%dT%H:%M:%S"), user_id)
        )
        conn.commit()


def update_user_email(user_id: int, new_email: str):
    with closing(get_connection()) as conn:
        conn.execute("UPDATE user SET email = ? WHERE id = ?", (new_email, user_id))
        conn.commit()


def update_user_password(user_id: int, new_password_hash: str):
    """覆盖密码哈希（改密与新格式渐进式 rehash 共用）。"""
    with closing(get_connection()) as conn:
        conn.execute("UPDATE user SET password = ? WHERE id = ?", (new_password_hash, user_id))
        conn.commit()


def delete_user(user_id: int):
    """逻辑删除：昵称置为 Ghost、邮箱与密码清空，is_deleted=1。

    email 列有 UNIQUE 约束，因此使用按 id 生成的占位邮箱避免与将来新注册冲突；
    已注销账号的评论仍保留在站内（评论处显示占位昵称）。
    """
    with closing(get_connection()) as conn:
        try:
            placeholder_email = f"deleted_{user_id}@example.com"
            cursor = conn.execute(
                "UPDATE user SET nickname = 'Ghost', email = ?, password = '', is_deleted = 1 "
                "WHERE id = ?",
                (placeholder_email, user_id)
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ValueError(f"User {user_id} does not exist or is already deleted")
            _invalidate_user_count()
        except Exception:
            conn.rollback()
            raise


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
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT COUNT(*) FROM user WHERE is_deleted = 0").fetchone()
    value = row[0] if row else 0
    _user_count_cache = (now, value)
    return value
