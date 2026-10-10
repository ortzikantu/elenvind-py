"""comment 表：文章评论的读取、软删除/恢复与计数。

设计说明：
- 业务上的“删除”是**涂黑**（`is_deleted=1` 且正文被黑块替换）：行本身保留，
  这样评论树不会散架、审计线索不断；正文在同一个 UPDATE 里被覆盖，数据库里
  不再有可恢复的副本。
  **没有物理删除入口**：评论行一旦产生就永久保留。
  注意：涂黑会**释放**文章配额（`db/comment_rate.try_post_comment` 只统计
  `is_deleted = 0`），否则"打满上限"会变成该文章永久拒评。
- `parent_id` 实现楼中楼回复；一次取回整篇文章评论后在内存里组树
  （树算法见 `db/comment.flatten_comment_tree`），避免 N+1 次查询。
  数据库层 `parent_id` 是 ON DELETE SET NULL，因此即便父行在库外被删除，
  子评论也会自动升级为顶层而不是消失。
- **本模块所有返回评论行的查询都带 `nickname` / `user_deleted`**（JOIN user），
  保证"单条"与"整篇"两种取法的行形状一致；调用方可以放心按同一套键读取。
- 评论的“写入 + 限流判定”在 db_comment_rate.try_post_comment 中原子完成。
- **读走 `connect()`，写走 `write_tx()`**（跨进程 flock + BEGIN IMMEDIATE）。
"""
from .connection import connect
from .transaction import write_tx


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




#: 涂黑字符：U+2588 FULL BLOCK。删除后正文被它替换并**写回数据库**。
REDACTION_BLOCK = "\u2588"
#: 涂黑上限：超长评论也只存这么多黑块（正文长度信息仍保留在等长前缀里，
#: 但不允许一条评论把库撑大 —— 这是"删除"语义，不是"保留副本"）。
REDACTION_MAX_BLOCKS = 400


def redact_comment(comment_id: int, *, max_blocks: int = REDACTION_MAX_BLOCKS) -> int:
    """**永久涂黑**：把正文替换成等长（有上限）的黑块，并标记 `is_deleted = 1`。

    为什么不是"只改标志位、渲染时打码"：那样原文一直留在库里，
    "已删除"的承诺不成立（数据库副本、备份、任何拿到库文件的人都读得到）。
    现在原文在同一个 UPDATE 里被覆盖 —— 数据库里不再有可恢复的副本，
    而评论行本身仍在（`parent_id` 链完整，评论树不会散架）。

    保留长度是为了视觉信息量：读者能看出"这里原本有一段话"，
    但内容不可复原。返回受影响行数（0 = 评论不存在）。
    """
    with write_tx() as conn:
        row = conn.execute("SELECT content FROM comment WHERE id = ?",
                           (comment_id,)).fetchone()
        if row is None:
            return 0
        normalized = (row["content"] or "").replace("\r\n", "\n").replace("\r", "\n")
        blocks = REDACTION_BLOCK * max(min(len(normalized), max_blocks), 1)
        conn.execute("UPDATE comment SET is_deleted = 1, content = ? WHERE id = ?",
                     (blocks, comment_id))
    return 1


def update_comment_content(comment_id: int, content: str) -> int:
    """编辑评论正文。返回受影响行数（0 = 不存在或已涂黑）。

    `is_deleted = 0` 是 WHERE 的一部分：已涂黑的评论**不可编辑**
    （原文已经不存在，编辑它只会把黑块换掉，等于伪造历史）。
    """
    with write_tx() as conn:
        cursor = conn.execute(
            "UPDATE comment SET content = ? WHERE id = ? AND is_deleted = 0",
            (content, comment_id))
        return cursor.rowcount


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
