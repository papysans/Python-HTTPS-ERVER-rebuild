"""服务端配置。

原代码把 HOST/PORT/凭据/各类上限散落成模块级全局常量，测试无法替换，
本模块把它们收敛成一个可构造、可注入的值对象。
"""

from dataclasses import dataclass
from pathlib import Path


DEFAULT_STATIC_ROOT = (Path(__file__).with_name("static")).resolve()


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    static_root: Path = DEFAULT_STATIC_ROOT

    max_header_bytes: int = 16 * 1024
    max_body_bytes: int = 1024 * 1024

    # 无此超时则连接可被永久占用（slowloris）
    read_timeout_seconds: float = 15.0

    session_cookie: str = "sid"
    session_max_age_seconds: int = 60 * 60
    # 原代码的 SESSIONS 无上限，匿名请求可无限撑大内存
    max_sessions: int = 10_000
    # 原代码每个请求全量扫描一次会话表，改为按间隔触发
    session_cleanup_interval_seconds: float = 60.0

    username: str = "admin"
    password: str = "password"

    # 原代码 sleep 时长完全由客户端指定且无校验
    max_sleep_seconds: int = 10
