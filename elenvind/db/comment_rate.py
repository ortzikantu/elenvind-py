"""评论发布限流（comment_rate 表）：短窗口计数，防刷屏。

策略与 db_login 同款（逐条流水 + 窗口统计），维度为：
- 单用户窗口内评论数（正常读者连发很少超过阈值）；
- 单 IP 窗口内评论数（防同一出口批量刷屏）。
窗口都很短（默认 60 秒），误伤概率低。

流水清理分两层（见 `db.maintenance`）：
- **启动时**全量清一次（`cleanup_old_comment_attempts`）；
- **运行期**每次发布提交之后，机会式清理（每小时最多一次），
  这样长跑进程不会无界增长，又不会把 O(表大小) 的 DELETE 放进写事务里。

并发说明：限流判定、父评论校验与评论写入必须在一个事务里完成才真正原子，
因此对外主入口是 try_post_comment() —— 由它统一取号、计数、校验、插入。
该事务由 Core 的唯一写入口 `write_tx()` 提供（跨进程 flock + BEGIN IMMEDIATE +
提交/回滚/关闭）。

与清理的关系（重要）：`prune()` 自己也要写数据库（取同一把 flock），
因此**必须在本函数返回之后、`write_tx()` 块之外**调用 —— 块内调用会触发
重入守卫（`write_tx()` 不可嵌套），否则就是自死锁。
"""
import time

from .comment import comment_depth
from .maintenance import prune
from .transaction import write_tx

RETENTION_DAYS = 7


def cleanup_old_comment_attempts(days: int = RETENTION_DAYS):
    """启动时删除指定天数之前的流水，控制表体积。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM comment_rate WHERE attempted_at < ?",
                     (time.time() - days * 86400,))


def try_post_comment(article_slug: str, user_id: int, ip: str, content: str,
                     parent_id=None, *, max_per_user: int, max_per_ip: int,
                     window_seconds: int, max_per_article: int,
                     created_at: str, max_depth: int = 0) -> str:
    """原子地校验父评论与深度、判定限流并写入评论。

    返回 "ok" / "rate_user" / "rate_ip" / "too_many" / "bad_parent" / "too_deep"；
    被拒绝时不写流水、不写评论（避免把攻击流量放大成表增长）。

    父评论校验（parent_id 非空时）：
    - 父评论必须存在；
    - 父评论必须属于**同一篇文章**（防在 A 文章下引用 B 文章的评论 id）；
    - **不校验 is_deleted**：本项目的"删除"是软删除、可恢复，
      已删除评论仍可被回复（对访客打码展示，但关系链保持完整）。

    深度校验（parent_id 非空且 max_depth > 0 时）：
    - 父评论层级 + 1 不得超过 `max_depth`，否则返回 "too_deep"。
    - 模板只在未到顶时渲染回复按钮，但 `reply_to` 是表单字段、可以被直接构造，
      因此**必须在这里校验**——这是唯一的权威判定点。
    - 放在事务内还有一个原因：路由层的预检在写事务**之前**，
      属于 TOCTOU——两个并发请求可以同时看到"还没到顶"然后各写一层。
      放进写事务后，深度判定与 INSERT 之间不会再被并发插入插队。

    全部校验都在 `write_tx()` 的 BEGIN IMMEDIATE 之后、INSERT 之前，因此限流计数、
    深度判定与写入处于同一事务：要么全部生效，要么全部回滚。
    被拒绝的分支显式 `rollback()`（本来也没写过行），保持"什么都没发生"。

    事务内**不做**流水清理：清理是 O(表大小) 的 DELETE，放在写锁里会明显拉长
    锁持有时间（高并发时互相排队），而且它是另一个写事务（会嵌套取锁）。
    它只由启动清理 `cleanup_old_comment_attempts()` 与提交后的机会式
    `prune()` 负责，与查询用的时间窗口互不影响——窗口只有几十秒，
    而保留期是 7 天，残留旧流水不会影响限流判定的正确性。
    """
    now = time.time()
    cutoff = now - window_seconds
    with write_tx() as conn:
        user_count = conn.execute(
            "SELECT COUNT(*) FROM comment_rate WHERE user_id = ? AND attempted_at > ?",
            (user_id, cutoff)).fetchone()[0]
        if user_count >= max_per_user:
            conn.rollback()
            return "rate_user"
        ip_count = conn.execute(
            "SELECT COUNT(*) FROM comment_rate WHERE ip = ? AND attempted_at > ?",
            (ip, cutoff)).fetchone()[0]
        if ip_count >= max_per_ip:
            conn.rollback()
            return "rate_ip"
        # 配额只统计**仍然展示中**的评论（涂黑即释放名额）。
        # 涂黑之所以保留行，是为了不拆散评论树；但把已涂黑的行算进配额，
        # 就等于"上限 = 该文章永久停止接受评论"——管理员也救不回来
        # （全项目没有任何 DELETE 评论的路径）。所以这里必须排除 is_deleted=1。
        article_count = conn.execute(
            "SELECT COUNT(*) FROM comment WHERE article_slug = ? AND is_deleted = 0",
            (article_slug,)
        ).fetchone()[0]
        if article_count >= max_per_article:
            conn.rollback()
            return "too_many"
        if parent_id is not None:
            # 只取判定需要的列：parent_id 是外部输入，绝不能带入写入语句。
            parent = conn.execute(
                "SELECT id, article_slug, parent_id FROM comment WHERE id = ?",
                (parent_id,)).fetchone()
            if parent is None or parent["article_slug"] != article_slug:
                conn.rollback()
                return "bad_parent"
            if max_depth > 0:
                # 复用 db.comment.comment_depth（防环 + limit 兜底都在那里），
                # 传 conn 以确保回溯与 INSERT 处于同一事务。
                parent_depth = comment_depth(parent, max_depth=max_depth, conn=conn)
                if parent_depth + 1 > max_depth:
                    conn.rollback()
                    return "too_deep"
        conn.execute(
            "INSERT INTO comment_rate (user_id, ip, attempted_at) VALUES (?, ?, ?)",
            (user_id, ip, now))
        conn.execute(
            "INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (article_slug, user_id, content, created_at, parent_id))
    # 写事务已结束（flock 已释放），才做机会式清理：
    # DELETE 不会占着刚释放的写锁，也不会嵌套取锁。
    prune("comment_rate", "attempted_at", RETENTION_DAYS)
    return "ok"
