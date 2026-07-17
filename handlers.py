"""路由与业务处理。

原代码的 route() 是一个 81 行、7 个 elif 的巨型函数，同时承担
会话清理、URL 分发、业务实现（求和/sleep/首页）、Cookie 兜底四种职责，
且内联业务逻辑与委托 handler 两种风格混用。

本模块把它拆成：
  - 一张显式路由表；
  - 每个端点一个职责单一的 handler；
  - 可脱离网络单测的纯函数（参数提取、整数解析、静态路径解析）。
Set-Cookie 的兜底不在这里做，统一由连接层负责（见 http_server.py），
避免原代码里「早退分支绕过尾部兜底导致 Set-Cookie 静默丢失」的缺陷。
"""

import asyncio
import logging
import mimetypes
from http import HTTPStatus
from pathlib import Path
from typing import Awaitable, Callable, Optional

from config import ServerConfig
from http_core import (
    Request,
    Response,
    build_set_cookie,
    html_response,
    json_response,
    redirect,
    text_response,
)
from sessions import SessionStore


logger = logging.getLogger(__name__)

# 原代码 sum 端点静默支持这四个别名，无任何文档说明。
# 保留是为了不破坏既有调用方，但必须显式记录下来。
SUM_PARAM_ALIASES = ("numbers", "nums", "values", "n")

INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>asyncio http server</title></head>
<body>
<h1>asyncio + socket HTTP server</h1>
<p>Session visits: {visits}</p>
<p>Current user: {user}</p>
<ul>
    <li><a href="/api/v1/sum?numbers=1,2,3">/api/v1/sum?numbers=1,2,3</a></li>
    <li><a href="/api/v1/session">/api/v1/session</a></li>
    <li><a href="/login">/login</a></li>
    <li><a href="/protected">/protected</a></li>
    <li><a href="/static/">/static/</a></li>
</ul>
<form method="post" action="/api/v1/sum">
    <label>Numbers <input name="numbers" value="10,20,30"></label>
    <button type="submit">Sum</button>
</form>
</body>
</html>
""".strip()

LOGIN_FORM_TEMPLATE = """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Login</title></head>
<body>
  <h1>Login</h1>
  <form method="post" action="/login">
    <label>Username <input name="username" autocomplete="username"></label><br>
    <label>Password <input name="password" type="password" autocomplete="current-password"></label><br>
    <button type="submit">Login</button>
  </form>
</body>
</html>
""".strip()


def error_response(status: HTTPStatus, message: str) -> Response:
    """所有错误走同一个出口，避免原代码 text/JSON/HTML 三套错误格式并存。"""
    return text_response(message, status.value, status.phrase)


def extract_params(request: Request) -> dict[str, list[str]]:
    """POST 且带表单时取表单，否则取查询串。

    原代码把这条规则以一模一样的 93 字符三元表达式复制在两个分支里。
    """
    if request.method == "POST" and request.form:
        return request.form
    return request.query


def parse_int_list(raw_values: list[str]) -> tuple[list[int], Optional[str]]:
    """把 ["1,2", "3"] 解析成 [1, 2, 3]。

    返回 (整数列表, 第一个非法项)。非法项非 None 时整数列表无意义。
    纯函数，可脱离 HTTP 单测。
    """
    numbers: list[int] = []
    for value in raw_values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                numbers.append(int(item))
            except ValueError:
                return [], item
    return numbers, None


def parse_sleep_seconds(raw_values: list[str], max_seconds: int) -> tuple[int, Optional[str]]:
    """解析 sleep 秒数，返回 (秒数, 错误信息)。

    原代码 `sec = source.get("sec"); if len(sec) > 0` 在缺参时对 None
    调用 len() 直接抛 TypeError，使得它上一行的默认值 `sleep_sec = 1`
    永远不可达；且对 sec 既不校验类型也不校验上界。
    """
    if not raw_values:
        return 1, None
    raw = raw_values[0].strip()
    try:
        seconds = int(raw)
    except ValueError:
        return 0, f"not an integer: {raw}"
    if seconds < 0:
        return 0, f"must not be negative: {seconds}"
    if seconds > max_seconds:
        return 0, f"must not exceed {max_seconds} seconds: {seconds}"
    return seconds, None


def resolve_static_path(static_root: Path, url_path: str) -> Optional[Path]:
    """把 URL 路径映射到静态目录下的真实文件；越界返回 None。

    static_root 必须是已 resolve 的绝对路径，否则 relative_to 的比较不成立。
    """
    if url_path == "/static/":
        candidate = static_root / "index.html"
    else:
        from urllib.parse import unquote

        relative = unquote(url_path.removeprefix("/static/"))
        candidate = static_root / relative

    resolved = candidate.resolve()
    try:
        resolved.relative_to(static_root)
    except ValueError:
        return None
    return resolved


class Router:
    """按路径分发请求。

    依赖（配置、会话存储、sleep 实现）全部通过构造函数注入，
    因此单测可以传入假时钟和假 sleep，不需要真的等待、也不碰全局状态。
    """

    def __init__(
        self,
        config: ServerConfig,
        session_store: SessionStore,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._config = config
        self._session_store = session_store
        self._sleep = sleep
        self._routes: dict[str, Callable[[Request], Awaitable[Response]]] = {
            "/": self._handle_index,
            "/api/v1/sum": self._handle_sum,
            "/api/v1/sleep": self._handle_sleep,
            "/api/v1/session": self._handle_session_api,
            "/login": self._handle_login,
            "/logout": self._handle_logout,
            "/protected": self._handle_protected,
        }

    async def dispatch(self, request: Request) -> Response:
        handler = self._routes.get(request.path)
        if handler is not None:
            return await handler(request)
        if request.path.startswith("/static/"):
            return self._handle_static(request)
        logger.info("no route for %s %s", request.method, request.path)
        return error_response(HTTPStatus.NOT_FOUND, "not found")

    async def _handle_index(self, request: Request) -> Response:
        return html_response(
            INDEX_TEMPLATE.format(
                visits=request.session.get("visits", 0),
                user=request.session.get("user") or "anonymous",
            )
        )

    async def _handle_sum(self, request: Request) -> Response:
        params = extract_params(request)
        raw_values: list[str] = []
        for alias in SUM_PARAM_ALIASES:
            if params.get(alias):
                raw_values = params[alias]
                break

        numbers, invalid_item = parse_int_list(raw_values)
        if invalid_item is not None:
            logger.info("sum rejected invalid integer %r from %s", invalid_item, request.path)
            return json_response(
                {"error": f"not an integer: {invalid_item}"},
                HTTPStatus.BAD_REQUEST.value,
                HTTPStatus.BAD_REQUEST.phrase,
            )
        return json_response({"numbers": numbers, "result": sum(numbers)})

    async def _handle_sleep(self, request: Request) -> Response:
        if request.method not in {"GET", "POST"}:
            return error_response(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

        params = extract_params(request)
        seconds, error = parse_sleep_seconds(
            params.get("sec", []),
            self._config.max_sleep_seconds,
        )
        if error is not None:
            logger.info("sleep rejected invalid sec: %s", error)
            return json_response(
                {"error": error},
                HTTPStatus.BAD_REQUEST.value,
                HTTPStatus.BAD_REQUEST.phrase,
            )

        await self._sleep(seconds)
        return json_response({"sec": seconds})

    async def _handle_session_api(self, request: Request) -> Response:
        return json_response(
            {
                "session_id": request.session_id,
                "visits": request.session.get("visits", 0),
                "user": request.session.get("user"),
                "cookies": request.cookies,
            }
        )

    async def _handle_login(self, request: Request) -> Response:
        if request.method == "GET":
            return html_response(LOGIN_FORM_TEMPLATE)
        if request.method != "POST":
            return error_response(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        return self._authenticate(request)

    def _authenticate(self, request: Request) -> Response:
        import secrets as _secrets

        username = request.form.get("username", [""])[0]
        password = request.form.get("password", [""])[0]
        # compare_digest 避免按字符提前返回的时序差异。但它对 str 只支持 ASCII，
        # 客户端提交中文等非 ASCII 凭据会抛 TypeError（被顶层伪装成 500）。
        # 先统一编码为 UTF-8 bytes 再比较：bytes 版对任意字节都成立。
        def _const_eq(a: str, b: str) -> bool:
            return _secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

        username_ok = _const_eq(username, self._config.username)
        password_ok = _const_eq(password, self._config.password)
        if not (username_ok and password_ok):
            logger.warning("failed login attempt for username=%r", username)
            return html_response(
                "<h1>Login failed</h1><p>Invalid username or password.</p>",
                HTTPStatus.UNAUTHORIZED.value,
                HTTPStatus.UNAUTHORIZED.phrase,
            )

        # 认证成功后更换 session id，防止会话固定攻击
        new_session_id = self._session_store.rotate_id(request.session_id)
        request.session_id = new_session_id
        request.session["user"] = self._config.username
        logger.info("login succeeded for username=%r, session id rotated", username)

        response = redirect("/protected")
        response.cookies.append(build_set_cookie(self._config.session_cookie, new_session_id))
        return response

    async def _handle_logout(self, request: Request) -> Response:
        self._session_store.delete(request.session_id)
        logger.info("logout, session destroyed")
        response = redirect("/")
        response.cookies.append(build_set_cookie(self._config.session_cookie, "", max_age=0))
        return response

    async def _handle_protected(self, request: Request) -> Response:
        if request.session.get("user") != self._config.username:
            return redirect("/login")
        return html_response("<h1>Protected</h1><p>You are logged in.</p>")

    def _handle_static(self, request: Request) -> Response:
        path = resolve_static_path(self._config.static_root, request.path)
        if path is None:
            logger.warning("blocked static path escape attempt: %r", request.path)
            return error_response(HTTPStatus.FORBIDDEN, "forbidden")

        if path.is_dir():
            # 追加目录 index 后必须再次 resolve + 校验归属：resolve_static_path
            # 只验证了目录本身在根内，若该目录下的 index.html 是指向根外文件的
            # 符号链接，is_file()/read_bytes() 会跟随它逃出静态根目录。
            index = (path / "index.html").resolve()
            try:
                index.relative_to(self._config.static_root)
            except ValueError:
                logger.warning("blocked static index symlink escape: %r", request.path)
                return error_response(HTTPStatus.FORBIDDEN, "forbidden")
            path = index
        if not path.is_file():
            return error_response(HTTPStatus.NOT_FOUND, "not found")

        try:
            body = path.read_bytes()
        except OSError as exc:
            # 原代码此处未捕获，PermissionError 会被顶层伪装成 400
            logger.error("failed to read static file %s: %s", path, exc)
            return error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(headers={"Content-Type": content_type}, body=body)
