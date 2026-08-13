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
    library_root下(如果library_root这次真的变了的话)。只在进程启动时跑一次,
    不做运行中途的热切换——跟routers/backup.py"导入备份需重启生效"是同一个思路。

    完全信任library_root这个配置值本身,不再判断它"此刻是否可达"——之前这里用
    Path(candidate_root).is_dir()做可达性判断,网络共享/移动硬盘这类库目录开机时
    经常比这个服务本身启动得慢,服务先于共享连上的那次开机就会被误判成"库没配置
    过",转而使用本地的另一个空数据库,还顺带把指针文件覆盖掉——用户看到的是
    "数据全没了",其实真实数据库原封不动地留在没连上的库目录里。库目录这一刻
    到底连没连上,交给services/library_health.py的定时探测,只用来决定要不要在
    界面上提示用户,不影响数据库实际指向哪。
    """
    env_override = os.getenv("DATABASE_PATH")
    if env_override:
        return env_override

    legacy_default = get_data_dir() / "anime_hub.db"

    # 用config_store.read_ini()的合并结果(没存过就落到DEFAULTS里的默认值D:\AnimeLibrary)
    # 而不是"必须显式在设置页保存过"——很多用户从来没打开过设置页,一直用着默认库目录,
    # 这种情况下settings.ini可能压根不存在,但库目录本身早就是真实、在用的。
    candidate_root = config_store.read_ini().get("library_root")
    desired = Path(candidate_root) / ".anime_hub.db" if candidate_root else legacy_default

    pointer_file = get_db_location_pointer_file()
    if pointer_file.exists():
        current_actual = Path(pointer_file.read_text(encoding="utf-8").strip())
    else:
        current_actual = legacy_default

    if desired != current_actual:
        # library_root跟上次记录的不一样,说明用户主动改了设置(或者这是第一次
        # 真正配置库目录)——这才是需要"搬家"的场景,但搬家这几步(exists探测/
        # 建目录/move)全都可能因为desired或current_actual此刻不可达而失败,
        # 包一层try/except:失败就跳过这次搬家/指针更新,直接沿用desired返回
        # 出去,不退回legacy_default,不会静默换成另一个不相关的数据库。
        try:
            if desired.exists():
                # 恢复场景:目标位置已经有一份数据库(比如重装系统前迁移过),校验通过就直接采用,不覆盖
                if not _looks_like_anime_hub_db(desired):
                    print(f"[DB] {desired} 存在但不像合法的anime-hub数据库,继续使用 {current_actual}")
                    desired = current_actual
            elif current_actual.exists():
                # 迁移场景:把当前数据库搬到新的期望位置
                desired.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(current_actual), str(desired))
            # 都不存在:可能是全新安装,也可能只是desired这一刻还没连上——两种
            # 情况都不在这里瞎猜,原样把desired返回去,等它哪天变得可达,
            # SQLAlchemy自然会在那里新建/打开真实的schema。

            pointer_file.parent.mkdir(parents=True, exist_ok=True)
            pointer_file.write_text(str(desired), encoding="utf-8")
        except OSError as e:
            print(f"[DB] 处理数据库路径迁移时出错(desired={desired} current_actual={current_actual} 可能暂时不可达): {e}")

    return str(desired)


DB_PATH = _resolve_db_path()
try:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)  # 全新安装时数据目录可能还不存在
except OSError as e:
    print(f"[DB] 无法创建数据库所在目录 {Path(DB_PATH).parent}(可能暂时不可达,等库目录可用后会自动补建): {e}")

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