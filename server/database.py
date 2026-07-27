import os
import shutil
import sqlite3
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

import config_store
from paths import get_data_dir, get_db_location_pointer_file


def _looks_like_anime_hub_db(path: Path) -> bool:
    """轻量校验:是不是一个合法的、带anime-hub关键表的SQLite文件——
    避免"库目录里恰好有个同名文件"被误当成历史数据库直接采用。
    """
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='app_setting'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def _resolve_db_path() -> str:
    """引导阶段(engine创建之前)决定数据库文件实际路径,顺便把它从老位置搬到
    library_root下(如果用户已经配置过库目录的话)。只在进程启动时跑一次,
    不做运行中途的热切换——跟routers/backup.py"导入备份需重启生效"是同一个思路。
    """
    env_override = os.getenv("DATABASE_PATH")
    if env_override:
        return env_override

    legacy_default = get_data_dir() / "anime_hub.db"

    # 用config_store.read_ini()的合并结果(没存过就落到DEFAULTS里的默认值D:\AnimeLibrary)
    # 而不是"必须显式在设置页保存过"——很多用户从来没打开过设置页,一直用着默认库目录,
    # 这种情况下settings.ini可能压根不存在,但库目录本身早就是真实、在用的。
    # 用"这个目录是否已经真实存在于磁盘上"来判断"库目录是不是已经配置好了",
    # 避免在压根没有对应盘符的机器上凭空建目录/建数据库。
    candidate_root = config_store.read_ini().get("library_root")
    desired = legacy_default
    if candidate_root and Path(candidate_root).is_dir():
        desired = Path(candidate_root) / ".anime_hub.db"

    pointer_file = get_db_location_pointer_file()
    if pointer_file.exists():
        current_actual = Path(pointer_file.read_text(encoding="utf-8").strip())
    else:
        current_actual = legacy_default

    if desired != current_actual:
        if desired.exists():
            # 恢复场景:目标位置已经有一份数据库(比如重装系统前迁移过),校验通过就直接采用,不覆盖
            if not _looks_like_anime_hub_db(desired):
                print(f"[DB] {desired} 存在但不像合法的anime-hub数据库,继续使用 {current_actual}")
                desired = current_actual
        elif current_actual.exists():
            # 迁移场景:把当前数据库搬到新的期望位置
            desired.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current_actual), str(desired))
        # 都不存在:全新安装,交给SQLAlchemy在desired处新建即可

        pointer_file.parent.mkdir(parents=True, exist_ok=True)
        pointer_file.write_text(str(desired), encoding="utf-8")

    return str(desired)


DB_PATH = _resolve_db_path()
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)  # 全新安装时数据目录可能还不存在

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()