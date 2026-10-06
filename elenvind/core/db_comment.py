"""comment 表：文章评论的读取、软删除/恢复与计数。

设计说明：
- 业务上的“删除”是**软删除**（is_deleted=1）：评论仍留在库里以便审计与恢复，
  页面展示层负责把已删评论对普通访客打码、对管理员划线显示。
  **没有物理删除入口**：评论一旦产生就永久保留（这也是审计前提）。
- `parent_id` 实现楼中楼回复；一次取回整篇文章评论后在内存里组树（树算法见
  modules/blog/logic.py 的 build_comment_rows），避免 N+1 次查询。
  数据库层 `parent_id` 是 ON DELETE SET NULL，因此即便父行在库外被删除，
  子评论也会自动升级为顶层而不是消失。
- **本模块所有返回评论行的查询都带 `nickname` / `user_deleted`**（JOIN user），
  保证"单条"与"整篇"两种取法的行形状一致；调用方可以放心按同一套键读取。
- 评论的“写入 + 限流判定”在 db_comment_rate.try_post_comment 中原子完成。
- **读走 `connect()`，写走 `write_tx()`**（跨进程 flock + BEGIN IMMEDIATE）。
"""
from datetime import datetime

from .db_base import connect, write_tx


def get_comments_by_article(article_slug: str):
    """取回某文章全部评论（含软删除），按发表顺序排序。

    作者可能是已注销用户（user.is_deleted=1），展示层据此显示占位昵称。
    """
    with connect() as conn:
        return conn.execute("""
            SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at, c.parent_id,
                   c.is_deleted,
                   u.nickname, u.is_deleted AS user_deleted
            FROM comment c
            JOIN user u ON c.user_id = u.id
            WHERE c.article_slug = ?
            ORDER BY c.created_at ASC, c.id ASC
        """, (article_slug,)).fetchall()


def get_comment_by_id(comment_id: int):
    """按 id 取单条评论（含软删除），用于回复目标、删除/恢复前的归属与权限校验。

    **返回的行与 `get_comments_by_article()` 同形**（即带上 `nickname` /
    `user_deleted`）。这一点很重要：调用方既要用 `user_id` 判权限，
    也要用作者信息渲染"回复 @某某"提示，两处若形状不一致就会在补评论树时
    因缺列而 500（详见 tests/test_comments.py 的回归用例）。
    """
    with connect() as conn:
        return conn.execute("""
            SELECT c.id, c.article_slug, c.user_id, c.content, c.created_at,
                   c.parent_id, c.is_deleted,
                   u.nickname, u.is_deleted AS user_deleted
            FROM comment c
            JOIN user u ON c.user_id = u.id
            WHERE c.id = ?
        """, (comment_id,)).fetchone()




def soft_delete_comment(comment_id: int):
    with write_tx() as conn:
        conn.execute("UPDATE comment SET is_deleted = 1 WHERE id = ?", (comment_id,))


def restore_comment(comment_id: int):
    with write_tx() as conn:
        conn.execute("UPDATE comment SET is_deleted = 0 WHERE id = ?", (comment_id,))


def comment_depth(comment, *, max_depth: int, conn=None) -> int:
    """沿 parent_id 回溯计算评论层级（顶层 = 1）。

    这是**层级计算的唯一定义**：渲染侧（modules/blog/logic.comment_depth）
    与写入侧校验（db_comment_rate.try_post_comment）都走这里，
    避免"两处各算一遍、迟早算出不同结果"。

    - **防环**：用访问集合记录走过的 id，数据成环时立即停止（不会死循环）；
    - **limit 兜底**：最多向上走 max_depth 层，超深/被篡改的链也不会走太久；
      返回上限为 max_depth + 1（比"已知的合法最大深度"再多一层，
      以便调用方判断"是否已经超过"）。

    `conn` 给出时复用该连接（写事务内必须这样，才能与 INSERT 处于同一事务）。
    未给出时**自己开一个连接走完整条链**，而不是每个祖先各开一次 ——
    后者在深链上是 N 次连接 + N 次查询（实测深度 10 就是 9 次连接），
    而本函数在每次评论 POST 的路由预检里都会被调用。
    """
    depth = 1
    parent_id = comment["parent_id"]
    seen = {comment["id"]}
    limit = max_depth + 1
    if parent_id is None or depth >= limit:
        return depth

    if conn is not None:
        return _walk_depth(conn, parent_id, depth, seen, limit)

    with connect() as own_conn:
        return _walk_depth(own_conn, parent_id, depth, seen, limit)


def _walk_depth(conn, parent_id, depth, seen, limit) -> int:
    """在给定连接上沿 parent_id 向上走（`comment_depth` 的实现细节）。"""
    while parent_id is not None and depth < limit:
        if parent_id in seen:
            break
        seen.add(parent_id)
        row = conn.execute(
            "SELECT id, parent_id FROM comment WHERE id = ?",
            (parent_id,)).fetchone()
        if row is None:
            break
        depth += 1
        parent_id = row["parent_id"]
    return depth


def flatten_comment_tree(comments):
    """把评论行展开成 [(row, depth)]，深度优先、父在子前、每条恰好一次。

    这是评论树展开的**唯一定义**：渲染侧（`modules/blog/logic.build_comment_rows`）
    与测试都调用它，不允许再有一份副本。

    为什么必须防环与"救回不可达评论"：`parent_id` 只是普通外键，写入侧虽然有
    深度校验，但库外维护脚本、手工 SQL、或历史遗留数据都可能造出环。旧实现是

        if parent_id is None or parent_id not in by_id:   # 视为根
            roots.append(row)
        else:
            children.setdefault(parent_id, []).append(row)

    于是"父存在但不可达"的节点（即环上的节点）**既不是根、也没有从根进来的边**，
    会连同它的整棵子树**从页面上彻底消失** —— 用户看不到、也删不掉。
    实测：root(1) + 环 2<->3 时，旧实现只渲染出 [1]，评论 2 与 3 凭空不见。

    现在的规则：
    - 父为 `None` 或父不在集合里（孤儿）-> 真根；
    - 从真根深度优先展开，每条只出栈一次；
    - 展开完仍有没访问到的节点 -> 说明它们在一个（或多个）闭环里，
      把它们**按 id 顺序提升为根**再展开，保证"库里有的一条都不少"。
    """
    by_id = {row["id"]: row for row in comments}
    children = {}
    roots = []
    for row in comments:
        parent_id = row["parent_id"]
        if parent_id is None or parent_id not in by_id:
            roots.append(row)
        else:
            children.setdefault(parent_id, []).append(row)

    ordered = []
    seen = set()

    def walk(start_rows):
        # 显式栈（不用递归）：深链也不能爆栈
        stack = [(row, 1) for row in reversed(start_rows)]
        while stack:
            row, depth = stack.pop()
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            ordered.append((row, depth))
            for child in reversed(children.get(row["id"], ())):
                stack.append((child, depth + 1))

    walk(roots)
    # 收尾：把不可达的（环上的）节点补成根，避免评论消失
    leftovers = [row for row in comments if row["id"] not in seen]
    if leftovers:
        walk(sorted(leftovers, key=lambda row: row["id"]))
    return ordered


def create_comment(article_slug: str, user_id: int, content: str, parent_id: int = None) -> int:
    """写入一条评论，返回新 id（parent_id 为空表示顶层评论）。

    注意：Web 流程请用 db_comment_rate.try_post_comment（带原子限流 + 深度校验）；
    本函数保留给脚本与测试使用，**不校验深度**——它是"底层写入原语"，
    绕开它写出的超深链由渲染侧的 `comment_depth` limit 兜底。
    """
    with write_tx() as conn:
        cursor = conn.execute(
            "INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (article_slug, user_id, content, datetime.now().isoformat(), parent_id)
        )
        new_id = cursor.lastrowid
    return new_id
