#!/usr/bin/env python3
"""管理软删除的评论：列出全部软删除评论，并可永久清除选中的条目。

在项目根目录运行：
    python purge.py

这是一个人工审核辅助脚本：软删除的评论会保留在数据库中以便审计/恢复，
只有本脚本会将其物理删除。
"""
from elenvind.db_comment import get_all_soft_deleted_comments, physical_delete_comments


def main():
    comments = get_all_soft_deleted_comments()
    if not comments:
        print("No soft-deleted comments.")
        return

    print("Soft-deleted comments:")
    for c in comments:
        print(f"ID: {c['id']} | User: {c['nickname']} | Article: {c['article_slug']} | Time: {c['created_at']}")
        print(f"Content: {c['content'][:80]}...")
        print("-" * 50)

    # 非交互环境（管道/CI/EOF/Ctrl-C）下不吐 traceback，静默退出
    try:
        raw_ids = input("IDs to permanently delete (comma separated, Enter to exit): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return
    if not raw_ids:
        return
    try:
        id_list = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
    except ValueError:
        print("Invalid input: expected comma-separated numbers.")
        return

    confirm = input(f"Permanently delete comments {id_list}? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Cancelled.")
        return

    physical_delete_comments(id_list)
    print("Deleted.")


if __name__ == "__main__":
    main()
