"""独立可执行文件的入口:被PyInstaller打包+NSSM注册为Windows服务时用这个启动,
而不是`python -m uvicorn main:app`那种CLI方式(打包成exe以后没有CLI可用)。
直接传app对象而不是"main:app"字符串——字符串形式的模块导入在PyInstaller冻结后不可靠。"""
import uvicorn

from main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
