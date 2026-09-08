"""独立可执行文件的入口:被PyInstaller打包+NSSM注册为Windows服务时用这个启动,
而不是`python -m uvicorn main:app`那种CLI方式(打包成exe以后没有CLI可用)。
直接传app对象而不是"main:app"字符串——字符串形式的模块导入在PyInstaller冻结后不可靠。"""
import os

import uvicorn

import config_store
from main import app


def _resolve_port() -> int:
    """监听端口:环境变量 SERVER_PORT 优先(Docker 用),否则读 settings.ini 的 server_port,
    都没有就回落到默认值。改端口在设置页操作,重启本服务后经由这里生效。"""
    raw = os.getenv("SERVER_PORT") or config_store.read_ini().get("server_port")
    try:
        port = int(raw)
        if 1024 <= port <= 65535:
            return port
    except (TypeError, ValueError):
        pass
    return int(config_store.DEFAULTS["server_port"])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=_resolve_port())
