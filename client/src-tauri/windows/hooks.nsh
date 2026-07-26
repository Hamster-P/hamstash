; 安装/卸载时注册Windows服务(用NSSM包装打包好的后端exe)。
; 数据目录(数据库/设置)由后端自己在检测到打包运行(PyInstaller frozen)时
; 自动指向%ProgramData%\hamstash\,不需要这里传环境变量,详见server/paths.py。

!macro NSIS_HOOK_PREINSTALL
  ; 升级安装场景:旧版本服务可能还在跑,占用着即将被覆盖的exe/dll文件,
  ; 复制新文件之前先停掉它释放文件锁。全新安装时这里还没有旧的nssm.exe/服务,
  ; 这条命令会失败但不影响继续安装,不用特地判断。
  ExecWait '"$INSTDIR\backend\nssm.exe" stop HamStashServer'
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; 下载暂存目录/媒体库根目录的默认值(server/config_store.py::DEFAULTS,通过
  ; paths.py::get_install_dir()算出来,固定就是这里的$INSTDIR)——提前建好,
  ; 首次启动不用用户自己手动建目录。CreateDirectory对已存在的目录是幂等的,
  ; 重装/升级场景不会报错。
  CreateDirectory "$INSTDIR\AnimeDownloads"
  CreateDirectory "$INSTDIR\animelibrary"

  ; 幂等处理:先尝试停掉/删掉同名旧服务(如果存在,比如重装/升级场景),
  ; 找不到服务时这两条会报错但不影响后面继续执行,不用特地判断。
  ExecWait '"$INSTDIR\backend\nssm.exe" stop HamStashServer'
  ExecWait '"$INSTDIR\backend\nssm.exe" remove HamStashServer confirm'

  ExecWait '"$INSTDIR\backend\nssm.exe" install HamStashServer "$INSTDIR\backend\hamstash-server\hamstash-server.exe"'
  ExecWait '"$INSTDIR\backend\nssm.exe" set HamStashServer AppDirectory "$INSTDIR\backend\hamstash-server"'
  ExecWait '"$INSTDIR\backend\nssm.exe" set HamStashServer Start SERVICE_AUTO_START'
  ExecWait '"$INSTDIR\backend\nssm.exe" set HamStashServer AppStdout "$INSTDIR\backend\service.log"'
  ExecWait '"$INSTDIR\backend\nssm.exe" set HamStashServer AppStderr "$INSTDIR\backend\service.log"'

  ; 本机专属:服务默认注册成LocalSystem,访问不了这台机器配置的网络共享媒体库
  ; (LocalSystem没有对应的登录凭据,服务一启动就在database.py::_resolve_db_path
  ; 检查库路径时崩溃退出)。如果存在本机专属的服务账户配置文件(不进安装包/
  ; 不进版本库,%ProgramData%\hamstash\跟server/paths.py::get_data_dir()是
  ; 同一套约定,装/卸载都不会清理),启动前就直接把服务账户切过去;文件不存在时
  ; (绝大多数用户的正常安装场景)跳过这一步,服务照旧用默认的LocalSystem。
  IfFileExists "$%ProgramData%\hamstash\service-account.local.ini" ApplyServiceAccount SkipServiceAccount
  ApplyServiceAccount:
    ReadINIStr $0 "$%ProgramData%\hamstash\service-account.local.ini" "service" "username"
    ReadINIStr $1 "$%ProgramData%\hamstash\service-account.local.ini" "service" "password"
    ExecWait '"$INSTDIR\backend\nssm.exe" set HamStashServer ObjectName "$0" "$1"'
  SkipServiceAccount:

  ExecWait '"$INSTDIR\backend\nssm.exe" start HamStashServer'
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; 只停止/删除服务本身,不清理%ProgramData%\hamstash\里的数据库/设置——
  ; 卸载重装/升级时用户的观看记录、订阅规则等要保留。
  ExecWait '"$INSTDIR\backend\nssm.exe" stop HamStashServer'
  ExecWait '"$INSTDIR\backend\nssm.exe" remove HamStashServer confirm'
!macroend
