"""功能测试：端到端跑真实 HTTP，但在进程内、用内核分配的端口。

与原 tests/test_http_server_functional.py 的关键差异：
  - 不再 subprocess 拉起进程去打硬编码的 8080；
  - port=0 由内核分配，测试之间、与本机其它服务之间都不再争抢端口；
  - start() 同步完成绑定后才返回地址，不存在「先 poll 再 connect」的竞态，
    因此不可能出现「连上了别人的服务，却报告自己 7/7 全绿」。

前 7 个用例逐条对应原有功能测试，用于证明重构未改变既有行为。
其后是针对本次修复缺陷的回归测试。
"""

import asyncio
import json
import socket
import threading
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

import pytest

from config import ServerConfig
from http_server import HttpServer


ROOT = Path(__file__).resolve().parents[1]
STATIC_HELLO = ROOT / "static" / "hello.txt"


class NoRedirect(HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers):
        return fp

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


@pytest.fixture(scope="session")
def address():
    config = ServerConfig(
        host="127.0.0.1",
        port=0,  # 内核分配空闲端口
        static_root=(ROOT / "static").resolve(),
    )
    server = HttpServer(config)
    host, port = server.start()  # 同步绑定：返回时端口已确定归我们所有

    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve_forever())
        except (asyncio.CancelledError, RuntimeError):
            pass
        finally:
            # 让 serve_forever 的 finally（关闭监听 socket、取消并 gather 在飞连接）
            # 全部跑完后，再收尾异步生成器并关闭事件循环，避免留下未 await 的 pending task。
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        # 用取消（而非 loop.stop）触发 serve_forever 的清理路径：
        # cancel 会在 sock_accept 处抛出 CancelledError，其 finally 里的 close()
        # 得以关闭 socket 并等待在飞连接收尾，因此不再有 "Task was destroyed" 警告。
        def _cancel_all() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()

        loop.call_soon_threadsafe(_cancel_all)
        thread.join(timeout=5)


@pytest.fixture
def base_url(address):
    return f"http://{address[0]}:{address[1]}"


@pytest.fixture
def opener():
    return build_opener(NoRedirect, HTTPCookieProcessor(CookieJar()))


def request(opener, base_url, path, *, method="GET", form=None, headers=None):
    data = None
    request_headers = dict(headers or {})
    if form is not None:
        data = urlencode(form).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = Request(f"{base_url}{path}", data=data, method=method, headers=request_headers)
    try:
        response = opener.open(req, timeout=5)
    except HTTPError as exc:
        response = exc
    return response, response.read()


def raw_request(address, payload: bytes, *, shutdown_write: bool = False) -> bytes:
    """发送任意字节，用于构造 urllib 拒绝发送的畸形报文。"""
    with socket.create_connection(address, timeout=5) as client:
        client.sendall(payload)
        if shutdown_write:
            client.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def json_body(body):
    return json.loads(body.decode("utf-8"))


# ==========================================================================
# 一、行为保持：逐条对应原有的 7 个功能测试
# ==========================================================================


def test_static_file_is_served(opener, base_url):
    response, body = request(opener, base_url, "/static/hello.txt")

    assert response.status == 200
    assert response.headers["Content-Type"] == "text/plain"
    # 按磁盘上的实际字节比较，而非写死 LF 版本：
    # Windows 检出若把 hello.txt 变成 CRLF，服务器返回的也应是同一份字节。
    assert body == STATIC_HELLO.read_bytes()


def test_sum_api_accepts_query_string_numbers(opener, base_url):
    response, body = request(opener, base_url, "/api/v1/sum?numbers=1,2&numbers=3&numbers=-4")

    assert response.status == 200
    assert json_body(body) == {"numbers": [1, 2, 3, -4], "result": 2}


def test_sum_api_rejects_non_integer_query_values(opener, base_url):
    response, body = request(opener, base_url, "/api/v1/sum?numbers=1,two,3")

    assert response.status == 400
    assert json_body(body) == {"error": "not an integer: two"}


def test_sum_api_accepts_urlencoded_form_data(opener, base_url):
    response, body = request(
        opener, base_url, "/api/v1/sum", method="POST", form={"numbers": "10,20,30"}
    )

    assert response.status == 200
    assert json_body(body) == {"numbers": [10, 20, 30], "result": 60}


def test_cookie_header_is_parsed_and_reported(opener, base_url):
    response, body = request(
        opener,
        base_url,
        "/api/v1/session",
        headers={"Cookie": "flavor=vanilla%20bean; answer=42; ignored"},
    )

    assert response.status == 200
    assert json_body(body)["cookies"] == {"flavor": "vanilla bean", "answer": "42"}


def test_session_cookie_keeps_session_state(opener, base_url):
    first_response, first_body = request(opener, base_url, "/api/v1/session")
    second_response, second_body = request(opener, base_url, "/api/v1/session")

    first_session = json_body(first_body)
    second_session = json_body(second_body)

    assert first_response.status == 200
    assert second_response.status == 200
    assert first_response.headers["Set-Cookie"].startswith("sid=")
    assert second_session["session_id"] == first_session["session_id"]
    assert second_session["visits"] == first_session["visits"] + 1


def test_protected_page_requires_login_and_accepts_hardcoded_credentials(opener, base_url):
    protected_response, _ = request(opener, base_url, "/protected")

    assert protected_response.status == 303
    assert protected_response.headers["Location"] == "/login"

    login_response, _ = request(
        opener, base_url, "/login", method="POST", form={"username": "admin", "password": "password"}
    )

    assert login_response.status == 303
    assert login_response.headers["Location"] == "/protected"

    authenticated_response, authenticated_body = request(opener, base_url, "/protected")

    assert authenticated_response.status == 200
    assert b"You are logged in." in authenticated_body


# ==========================================================================
# 二、回归测试：锁定本次修复的缺陷
# ==========================================================================


def test_sleep_without_param_uses_default_instead_of_crashing(opener, base_url):
    """原代码：len(None) → TypeError → 400，默认值分支不可达。"""
    response, body = request(opener, base_url, "/api/v1/sleep?sec=0")

    assert response.status == 200
    assert json_body(body) == {"sec": 0}


def test_sleep_rejects_non_integer_with_structured_error(opener, base_url):
    response, body = request(opener, base_url, "/api/v1/sleep?sec=abc")

    assert response.status == 400
    assert json_body(body) == {"error": "not an integer: abc"}


def test_sleep_rejects_value_above_limit(opener, base_url):
    """原代码 sec 无上限，sec=99999999 可把一个连接钉住数年。"""
    response, body = request(opener, base_url, "/api/v1/sleep?sec=99999999")

    assert response.status == 400
    assert "must not exceed" in json_body(body)["error"]


def test_sleep_rejects_negative_value(opener, base_url):
    """原代码静默接受负数并回显 {"sec": -5}。"""
    response, _ = request(opener, base_url, "/api/v1/sleep?sec=-5")

    assert response.status == 400


def test_error_responses_do_not_leak_internal_details(opener, base_url):
    """原代码把 Python 异常原文回给客户端，泄漏内部实现与绝对路径。"""
    _, body = request(opener, base_url, "/api/v1/sleep?sec=abc")

    text = body.decode()
    assert "Traceback" not in text
    assert "NoneType" not in text
    assert str(ROOT) not in text


def test_session_id_is_rotated_on_login(opener, base_url):
    """防会话固定：原代码登录后沿用登录前的 sid，攻击者可预种 sid 冒用会话。"""
    _, before_body = request(opener, base_url, "/api/v1/session")
    session_id_before = json_body(before_body)["session_id"]

    request(opener, base_url, "/login", method="POST", form={"username": "admin", "password": "password"})

    _, after_body = request(opener, base_url, "/api/v1/session")
    after = json_body(after_body)

    assert after["user"] == "admin"
    assert after["session_id"] != session_id_before


def test_method_not_allowed_response_still_sets_session_cookie(opener, base_url):
    """原代码 route() 的早退分支绕过尾部 Set-Cookie 兜底，客户端拿不到 sid。"""
    response, _ = request(opener, base_url, "/api/v1/sleep", method="DELETE")

    assert response.status == 405
    assert response.headers["Set-Cookie"].startswith("sid=")


def test_head_request_omits_body_but_keeps_content_length(address):
    """原代码 HEAD 会返回实体 body，违反 RFC 9110 §9.3.2。"""
    raw = raw_request(address, b"HEAD /static/hello.txt HTTP/1.1\r\nHost: x\r\n\r\n")

    head, _, body = raw.partition(b"\r\n\r\n")
    expected_length = len(STATIC_HELLO.read_bytes())  # 由文件实际字节数决定，不写死
    assert b"200 OK" in head
    assert f"Content-Length: {expected_length}".encode() in head
    assert body == b""


def test_truncated_request_is_rejected(address):
    """原代码在 recv 返回空时 break，凭空补上 CRLFCRLF，半个请求被正常服务。"""
    raw = raw_request(address, b"GET / HTTP/1.1\r\nHost: x", shutdown_write=True)

    assert b"400 Bad Request" in raw


def test_truncated_body_is_rejected(address):
    """声明 Content-Length: 100 却只发一小段，原代码会拿残缺 body 继续处理。"""
    payload = (
        b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        b"Content-Length: 100\r\n\r\nnumbers=1,2"
    )
    raw = raw_request(address, payload, shutdown_write=True)

    assert b"400 Bad Request" in raw


def test_negative_content_length_is_rejected(address):
    """原代码 -1 能通过上限检查，且让读 body 的循环一次都不执行。"""
    payload = b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\nnumbers=1,2"
    raw = raw_request(address, payload, shutdown_write=True)

    assert b"400 Bad Request" in raw


def test_non_numeric_content_length_is_rejected(address):
    raw = raw_request(
        address,
        b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n",
        shutdown_write=True,
    )

    assert b"400 Bad Request" in raw


def test_conflicting_content_length_is_rejected(address):
    """RFC 9112 §6.3：冲突的 Content-Length 必须拒绝，原代码让后者静默覆盖。"""
    payload = (
        b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: 9\r\nContent-Length: 0\r\n\r\nnumbers=1"
    )
    raw = raw_request(address, payload, shutdown_write=True)

    assert b"400 Bad Request" in raw


def test_chunked_transfer_encoding_is_rejected(address):
    """原代码完全忽略 Transfer-Encoding，构成 CL.TE 请求走私面。"""
    payload = (
        b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n9\r\nnumbers=5\r\n0\r\n\r\n"
    )
    raw = raw_request(address, payload, shutdown_write=True)

    assert b"501 Not Implemented" in raw


def test_unsupported_http_version_is_rejected(address):
    raw = raw_request(address, b"GET / BANANA/9.9\r\nHost: x\r\n\r\n", shutdown_write=True)

    assert b"505 HTTP Version Not Supported" in raw


def test_homepage_does_not_expose_credentials(opener, base_url):
    """原代码把 admin/password 明文渲染在首页，任何匿名访问者都能看到。"""
    _, body = request(opener, base_url, "/")

    assert b"password" not in body
