"""user 表：注册、登录查询、资料修改、逻辑删除。

读取约定：
- 邮箱匹配使用 COLLATE NOCASE，登录/查重对大小写不敏感；
  写入侧（view_register / view_user）负责先 normalize_email 再落库。
- 所有“取用户”查询都过滤 is_deleted = 0，已注销账号视为不存在。
"""
import time

from .db_base import get_connection


def create_user(nickname: str, email: str, password_hash: str) -> int:
    """写入新用户（email/password_hash 须由调用方完成规范化与哈希），返回自增 id。"""
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO user (nickname, email, password, created_at, nickname_changed_at) VALUES (?, ?, ?, ?, NULL)",
        (nickname, email, password_hash, time.strftime("%Y-%m-%dT%H:%M:%S"))
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    return user_id


def get_user_by_email(email: str):
    """按邮箱查活跃用户（大小写不敏感），用于登录与注册查重。"""
    conn = get_connection()
    user = conn.execute(
        "SELECT * FROM user WHERE email = ? COLLATE NOCASE AND is_deleted = 0", (email,)
    ).fetchone()
    conn.close()
    return user


def get_user_by_id(user_id: int):
    """按 id 查活跃用户，用于每次请求的会话鉴权。"""
    conn = get_connection()
    user = conn.execute(
        "SELECT * FROM user WHERE id = ? AND is_deleted = 0", (user_id,)
    ).fetchone()
    conn.close()
    return user


def update_user_nickname(user_id: int, new_nickname: str):
    """更新昵称并记录更改时间（供“一年只能改一次”策略计时）。"""
    conn = get_connection()
    conn.execute(
        "UPDATE user SET nickname = ?, nickname_changed_at = ? WHERE id = ?",
        (new_nickname, time.strftime("%Y-%m-%dT%H:%M:%S"), user_id)
    )
    conn.commit()
    conn.close()


def update_user_email(user_id: int, new_email: str):
    conn = get_connection()
    conn.execute("UPDATE user SET email = ? WHERE id = ?", (new_email, user_id))
    conn.commit()
    conn.close()


def update_user_password(user_id: int, new_password_hash: str):
    conn = get_connection()
    conn.execute("UPDATE user SET password = ? WHERE id = ?", (new_password_hash, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    """逻辑删除：昵称置为 Ghost、邮箱与密码清空，is_deleted=1。

    email 列有 UNIQUE 约束，因此使用按 id 生成的占位邮箱避免与将来新注册冲突；
    已注销账号的评论仍保留在站内（评论处显示“Journeyed On”）。
    """
    conn = get_connection()
    try:
        placeholder_email = f"deleted_{user_id}@example.com"
        cur = conn.execute(
            "UPDATE user SET nickname = 'Ghost', email = ?, password = '', is_deleted = 1 WHERE id = ?",
            (placeholder_email, user_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"User {user_id} does not exist or is already deleted")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_user_number():
    """活跃用户总数（用于页脚展示），已注销用户不计入。"""
    conn = get_connection()
    user_number = conn.execute("SELECT COUNT(*) FROM user WHERE is_deleted = 0;").fetchone()[0]
    return user_number
