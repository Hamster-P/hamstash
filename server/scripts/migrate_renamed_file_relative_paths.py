"""
一次性脚本:把RenamedFile表里历史遗留的绝对路径(target_full_path)转换成
相对library_root的相对路径,写入target_relative_path列。

必须在还没有改library_root设置、盘符还是"迁移前"的那个值时跑——脚本用
当前AppSetting里的library_root去反推每条历史记录该转成什么相对路径。
迁移设置之后再跑,前缀就对不上了,历史记录会被跳过、留空。

默认只打印会改哪些行(dry-run),确认没问题后加 --apply 才会真的写入。
用法:
    venv\\Scripts\\python.exe -m scripts.migrate_renamed_file_relative_paths
    venv\\Scripts\\python.exe -m scripts.migrate_renamed_file_relative_paths --apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_store
from database import SessionLocal
from models import RenamedFile
from services.common import get_setting


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="真的写入数据库,不加这个参数只打印会改哪些行",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        library_root = get_setting(db, "library_root", config_store.DEFAULTS["library_root"])
        # 统一成不带结尾反斜杠的形式,方便拼前缀比较
        library_root_norm = library_root.rstrip("\\").rstrip("/")
        prefix = library_root_norm + "\\"

        print(f"当前 library_root: {library_root_norm}")

        rows = (
            db.query(RenamedFile)
            .filter(
                RenamedFile.target_full_path.isnot(None),
                RenamedFile.target_relative_path.is_(None),
            )
            .all()
        )
        print(f"待处理记录数: {len(rows)}")

        converted = 0
        skipped = []
        for row in rows:
            full_path = row.target_full_path
            if full_path.lower().startswith(prefix.lower()):
                relative = full_path[len(prefix):]
                print(f"  [{'将' if not args.apply else '已'}转换] id={row.id} {full_path!r} -> {relative!r}")
                if args.apply:
                    row.target_relative_path = relative
                converted += 1
            else:
                skipped.append((row.id, full_path))

        if skipped:
            print(f"\n跳过(前缀跟当前library_root对不上,不猜测,原样保留): {len(skipped)} 条")
            for row_id, full_path in skipped:
                print(f"  id={row_id} {full_path!r}")

        if args.apply:
            db.commit()
            print(f"\n已提交:转换 {converted} 条,跳过 {len(skipped)} 条。")
        else:
            print(f"\n[dry-run] 会转换 {converted} 条,跳过 {len(skipped)} 条。确认无误后加 --apply 重新运行以真正写入。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
