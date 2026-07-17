"""HTTP 报文的数据结构与解析。

本模块只做「字节 <-> 结构体」的转换，不触碰 socket、不读全局状态、
不创建会话，因此每个函数都能脱离网络独立单元测试。
"""

import json
import time
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit


FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


class HttpError(Exception):
    """可直接映射为 HTTP 响应的错误。

    原代码用裸 ValueError 表达所有失败，再在最外层统统转成 400，
    导致服务端内部故障也被伪装成客户端错误。这里让错误自带状态码。
    """

    def __init__(self, status: int, reason: str, client_message: str, log_message: str = ""):
        super().__init__(log_message or client_message)
        self.status = status
        self.reason = reason
        # 回给客户端的文案：不含内部实现细节
        self.client_message = client_message
        # 只进日志：可以包含定位所需的细节
        self.log_message = log_message or client_message


class BadRequest(HttpError):
    def __init__(self, client_message: str = "bad request", log_message: str = ""):
        super().__init__(400, "Bad Request", client_message, log_message)


class PayloadTooLarge(HttpError):
    def __init__(self, client_message: str = "payload too large", log_message: str = ""):
        super().__init__(413, "Payload Too Large", client_message, log_message)


class RequestTimeout(HttpError):
    def __init__(self, client_message: str = "request timeout", log_message: str = ""):
        super().__init__(408, "Request Timeout", client_message, log_message)


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: dict[str, str]
    body: bytes
    path: str = "/"
    query: dict[str, list[str]] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    form: dict[str, list[str]] = field(default_factory=dict)
    # 由服务层在解析之后绑定，解析阶段不产生会话
    session_id: str = ""
    session: dict = field(default_factory=dict)


@dataclass
class Response:
    status: int = 200
    reason: str = "OK"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    # 单独成列表：dict 无法表达多个 Set-Cookie
    cookies: list[str] = field(default_factory=list)

    def to_bytes(self, date: Optional[str] = None, include_body: bool = True) -> bytes:
        """序列化为响应字节。

        include_body=False 用于 HEAD：按 RFC 9110 §9.3.2 必须返回与 GET
        相同的头（含 Content-Length），但不得发送实体内容。

        原代码把 `**self.headers` 展开在 Content-Length 之后，handler 一旦
        设置了同名头就能覆盖真实长度；这里让框架头最后写入，不可被覆盖。
        """
        headers = {
            "Server": "asyncio-socket-http",
            "X-Content-Type-Options": "nosniff",
            **self.headers,
            "Date": date or http_date(),
            "Connection": "close",
            "Content-Length": str(len(self.body)),
        }
        head = [f"HTTP/1.1 {self.status} {self.reason}"]
        head.extend(f"{name}: {value}" for name, value in headers.items())
        head.extend(f"Set-Cookie: {cookie}" for cookie in self.cookies)
        raw_head = ("\r\n".join(head) + "\r\n\r\n").encode("iso-8859-1")
        return raw_head + (self.body if include_body else b"")


def http_date(now: Optional[float] = None) -> str:
    """生成 RFC 7231 要求的 HTTP-date。

    原代码用 time.strftime("%a, %d %b %Y ...")，%a/%b 受进程 locale 影响，
    在非英文 locale 下会生成不合规的星期/月份名。formatdate 恒为英文缩写。
    """
    return formatdate(timeval=now if now is not None else time.time(), usegmt=True)


def text_response(text: str, status: int = 200, reason: str = "OK") -> Response:
    return Response(
        status=status,
        reason=reason,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        body=text.encode("utf-8"),
    )


def html_response(html: str, status: int = 200, reason: str = "OK") -> Response:
    return Response(
        status=status,
        reason=reason,
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=html.encode("utf-8"),
    )


def json_response(data: object, status: int = 200, reason: str = "OK") -> Response:
    return Response(
        status=status,
        reason=reason,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
    )


def redirect(location: str) -> Response:
    return Response(status=303, reason="See Other", headers={"Location": location})


def parse_cookie_header(header: str) -> dict[str, str]:
    """解析 Cookie 请求头。无 '=' 的片段按浏览器惯例忽略。"""
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = unquote(value.strip())
    return cookies


def build_set_cookie(name: str, value: str, max_age: Optional[int] = None) -> str:
    """构造 Set-Cookie 值。

    name/value 会做 CRLF 校验：cookie 值若能注入 \\r\\n 就能伪造响应头。
    """
    for part in (name, value):
        if "\r" in part or "\n" in part:
            raise ValueError("cookie name/value must not contain CR or LF")
    cookie = f"{name}={value}; Path=/; HttpOnly; SameSite=Lax"
    if max_age is not None:
        cookie += f"; Max-Age={max_age}"
    return cookie


def parse_headers(head_lines: list[str]) -> dict[str, str]:
    """解析请求头。

    重复出现且取值冲突的 Content-Length 必须拒绝（RFC 9112 §6.3）：
    原代码用 dict 静默让后者覆盖前者，前置代理与本服务对 body 边界的
    理解就可能不一致，构成请求走私面。
    """
    headers: dict[str, str] = {}
    for line in head_lines:
        if not line:
            continue
        if ":" not in line:
            raise BadRequest(log_message=f"malformed header line: {line!r}")
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if name == "content-length" and name in headers and headers[name] != value:
            raise BadRequest(log_message=f"conflicting Content-Length: {headers[name]!r} vs {value!r}")
        headers[name] = value
    return headers


def parse_head(head: bytes) -> tuple[str, str, str, dict[str, str]]:
    """解析请求行 + 请求头。

    原代码在 read_http_request 和 parse_request 里各写了一份头解析，
    且两份的容错策略已经分叉（一份静默跳过畸形行，一份抛异常）。
    这里只保留这一份实现。
    """
    lines = head.decode("iso-8859-1", errors="strict").split("\r\n")
    request_line = lines[0].split()
    if len(request_line) != 3:
        raise BadRequest(log_message=f"malformed request line: {lines[0]!r}")

    method, target, version = request_line
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        raise HttpError(
            505,
            "HTTP Version Not Supported",
            "http version not supported",
            log_message=f"unsupported version: {version!r}",
        )
    return method.upper(), target, version, parse_headers(lines[1:])


def parse_content_length(headers: dict[str, str], max_body_bytes: int) -> int:
    """解析 Content-Length。

    原代码直接 int(headers.get(...))：
      - "abc" 抛裸 ValueError，被顶层伪装成 400 并回显内部异常文本；
      - "-1" 通过 `-1 > MAX_BODY_BYTES` 的检查，且让读 body 的循环一次都不执行，
        于是声明的长度与实际 body 不符也能被正常处理。
    isdigit() 同时挡住这两种输入。
    """
    if "transfer-encoding" in headers:
        raise HttpError(
            501,
            "Not Implemented",
            "transfer-encoding is not supported",
            log_message=f"unsupported Transfer-Encoding: {headers['transfer-encoding']!r}",
        )

    raw = headers.get("content-length", "0").strip() or "0"
    if not raw.isdigit():
        raise BadRequest(log_message=f"invalid Content-Length: {raw!r}")
    length = int(raw)
    if length > max_body_bytes:
        raise PayloadTooLarge(log_message=f"Content-Length {length} exceeds {max_body_bytes}")
    return length


def build_request(
    method: str,
    target: str,
    version: str,
    headers: dict[str, str],
    body: bytes,
) -> Request:
    """由已解析的头部与 body 组装 Request。纯函数：不建会话、不改全局状态。"""
    split = urlsplit(target)
    request = Request(
        method=method,
        target=target,
        version=version,
        headers=headers,
        body=body,
        path=split.path or "/",
        query=parse_qs(split.query, keep_blank_values=True),
        cookies=parse_cookie_header(headers.get("cookie", "")),
    )

    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if request.body and content_type == FORM_CONTENT_TYPE:
        request.form = parse_qs(
            request.body.decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
    return request


def parse_request(raw: bytes) -> Request:
    """把完整原始报文解析成 Request。供单元测试与非流式场景使用。"""
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise BadRequest(log_message="request head is not terminated by CRLFCRLF")
    method, target, version, headers = parse_head(head)
    return build_request(method, target, version, headers, body)
