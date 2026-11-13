"""本机 TCP 端口的 Windows 保留区间检测 + 推荐空闲端口。

动机:qBittorrent/本服务这类绑定本地端口的程序,在装过 Docker Desktop / WSL2 的
Windows 机器上经常撞上一个很隐蔽的坑——Hyper-V 会在开机时动态划走大段端口
("netsh int ipv4 show excludedportrange protocol=tcp"能看到),落在这段区间里的
端口谁都绑不上,报错也只是笼统的"地址被保护了",用户看不出跟Hyper-V有关系,
以为是程序本身的bug。而且这个区间每次开机可能不一样,今天好用的端口明天可能
又被占。所以连接失败时与其让用户自己去猜,不如直接告诉他"这个端口当前被系统
保留"、顺手推荐一个这一刻确认可用的端口。

只在Windows上有意义(其他平台没有这个机制),非Windows直接返回空结果。
"""
import locale
import os
import re
import socket
import subprocess

_RANGE_LINE_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s*\*?\s*$")

# 候选端口按常见程度排:先试用户大概率认得出、好记的几个,都不行才falls back
# 到让操作系统随便分配一个。
_CANDIDATE_PORTS = [8080, 8081, 8090, 18080, 28080, 38080, 48090]


def _get_excluded_tcp_ranges() -> list[tuple[int, int]]:
    """读取当前Windows保留的TCP端口区间列表。非Windows/命令本身失败都返回
    空列表(当作"没有保留区间"处理)——这只是诊断用的加分信息,查不到不该让
    调用方连带报错或把正常的连接失败诊断流程打断。"""
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    # 不用text=True交给subprocess自己按locale解码——netsh这条命令的实际输出编码
    # 跟Python探测到的locale编码对不上时(实测这台机器上会炸,报
    # UnicodeDecodeError),subprocess内部的读取线程会直接崩溃、stdout变成None。
    # 自己用errors="replace"兜底解码,乱码也比直接炸了拿不到任何保留区间强——
    # 反正真正要抓的只是纯数字的端口号那两列,不会落在被替换掉的字符里。
    try:
        text = result.stdout.decode(locale.getpreferredencoding(False), errors="replace")
    except (LookupError, AttributeError):
        text = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        m = _RANGE_LINE_RE.match(line)
        if m:
            ranges.append((int(m.group(1)), int(m.group(2))))
    return ranges


def is_port_excluded(port: int, ranges: list[tuple[int, int]] | None = None) -> bool:
    ranges = _get_excluded_tcp_ranges() if ranges is None else ranges
    return any(start <= port <= end for start, end in ranges)


def _port_is_bindable(port: int) -> bool:
    """实际尝试bind一下确认这一刻真的空着——只查"在不在保留区间"不够,端口
    也可能被另一个普通程序占用着,两种情况用户都需要换一个。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def suggest_free_port() -> int | None:
    """挑一个这一刻确认可用(不在保留区间里、也bind得上)的端口。
    先试几个好记的候选,都不行就让操作系统随便分配一个空闲端口,
    只要它恰好也不在保留区间里就采用(重试几次,极小概率都落在区间里就放弃)。
    """
    ranges = _get_excluded_tcp_ranges()
    for port in _CANDIDATE_PORTS:
        if not is_port_excluded(port, ranges) and _port_is_bindable(port):
            return port
    for _ in range(10):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        except OSError:
            continue
        if not is_port_excluded(port, ranges):
            return port
    return None


def diagnose_unreachable_port(host: str, port: int) -> dict:
    """连不上host:port时,判断是不是"端口被Windows保留"这种本机环境问题,
    是的话附带一个推荐端口。host不是本机地址(远程qBittorrent)时不做这项检测——
    保留区间是检测本机Hyper-V分配状况,对远程主机没有意义。"""
    if host not in ("127.0.0.1", "localhost", "::1"):
        return {"port_excluded": False, "reason": None, "suggested_port": None}
    if not is_port_excluded(port):
        return {"port_excluded": False, "reason": None, "suggested_port": None}
    return {
        "port_excluded": True,
        "reason": (
            f"端口 {port} 当前被 Windows 保留（常见于装过 Docker Desktop / WSL2 的电脑，"
            "Hyper-V 开机时动态划走了一段端口，这段区间每次开机可能不一样），"
            "qBittorrent 的 Web UI 没法绑定到这个端口上"
        ),
        "suggested_port": suggest_free_port(),
    }
