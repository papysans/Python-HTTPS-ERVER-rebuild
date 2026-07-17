"""单元测试：全程不起进程、不绑端口、不碰全局状态。

原代码这一层测试根本写不出来——核心逻辑全部耦合模块级全局
（SESSIONS / time.time / STATIC_ROOT / USERNAME），只能从 8080
这个唯一入口去打黑盒 e2e。本文件的存在本身就是解耦的验收标准。
"""

import asyncio
import json
import logging
import runpy
import socket
import sys

import pytest

from config import ServerConfig
from handlers import (
    Router,
    error_response,
    extract_params,
    parse_int_list,
    parse_sleep_seconds,
    resolve_static_path,
)
from http_core import (
    BadRequest,
    HttpError,
    PayloadTooLarge,
    Request,
    Response,
    build_set_cookie,
    parse_content_length,
    parse_cookie_header,
    parse_head,
    parse_request,
)
import http_server
from http_server import ConnectionHandler, HttpServer, main, parse_args, read_request, serve
from sessions import SessionStore


# --------------------------------------------------------------------------
# 报文解析
# --------------------------------------------------------------------------


def test_parse_request_extracts_method_path_query_and_form():
    raw = (
        b"POST /api/v1/sum?a=1 HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        b"\r\n"
        b"numbers=1,2"
    )
    request = parse_request(raw)

    assert request.method == "POST"
    assert request.path == "/api/v1/sum"
    assert request.query == {"a": ["1"]}
    assert request.form == {"numbers": ["1,2"]}


def test_parse_request_has_no_side_effects_on_sessions():
    """解析器必须是纯函数。

    原代码的 parse_request 会创建 session、递增 visits，导致任何解析
    测试都会污染全局会话表。这里断言解析后请求上没有绑定任何会话。
    """
    request = parse_request(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")

    assert request.session_id == ""
    assert request.session == {}


def test_parse_request_rejects_head_without_terminator():
    """截断的请求不得被当作完整请求。

    原代码在 recv 返回空时直接 break，然后凭空补上客户端从未发送的
    CRLFCRLF，半个请求会被正常服务。
    """
    with pytest.raises(BadRequest):
        parse_request(b"GET / HTTP/1.1\r\nHost: x")


def test_parse_head_rejects_unknown_http_version():
    with pytest.raises(HttpError) as excinfo:
        parse_head(b"GET / BANANA/9.9\r\nHost: x")
    assert excinfo.value.status == 505


def test_parse_headers_rejects_conflicting_content_length():
    """重复且冲突的 Content-Length 必须拒绝（RFC 9112 §6.3）。

    原代码用 dict 让后者静默覆盖前者，构成请求走私面。
    """
    with pytest.raises(BadRequest):
        parse_head(b"POST / HTTP/1.1\r\nContent-Length: 9\r\nContent-Length: 0\r\n")


@pytest.mark.parametrize("raw", ["abc", "-1", "+10", "1.5"])
def test_parse_content_length_rejects_non_digit_values(raw):
    """原代码 int() 裸转：'abc' 抛未捕获 ValueError；'-1' 更危险——
    它能通过 `-1 > MAX_BODY` 的上限检查，且让读 body 的循环一次都不执行。
    """
    with pytest.raises(BadRequest):
        parse_content_length({"content-length": raw}, max_body_bytes=1024)


def test_parse_content_length_rejects_oversized_body():
    with pytest.raises(PayloadTooLarge):
        parse_content_length({"content-length": "2048"}, max_body_bytes=1024)


def test_parse_content_length_rejects_transfer_encoding():
    """原代码完全忽略 Transfer-Encoding，chunked 报文会被错误解析。"""
    with pytest.raises(HttpError) as excinfo:
        parse_content_length({"transfer-encoding": "chunked"}, max_body_bytes=1024)
    assert excinfo.value.status == 501


# --------------------------------------------------------------------------
# Cookie
# --------------------------------------------------------------------------


def test_parse_cookie_header_unquotes_and_skips_malformed_parts():
    cookies = parse_cookie_header("flavor=vanilla%20bean; answer=42; ignored")

    assert cookies == {"flavor": "vanilla bean", "answer": "42"}


def test_build_set_cookie_includes_max_age_only_when_given():
    assert build_set_cookie("sid", "abc") == "sid=abc; Path=/; HttpOnly; SameSite=Lax"
    assert build_set_cookie("sid", "", max_age=0).endswith("; Max-Age=0")


def test_build_set_cookie_rejects_crlf_injection():
    """cookie 值若能塞进 CRLF 就能伪造响应头。原代码对此不做任何校验。"""
    with pytest.raises(ValueError):
        build_set_cookie("sid", "abc\r\nX-Injected: yes")


# --------------------------------------------------------------------------
# 响应序列化
# --------------------------------------------------------------------------


def test_content_length_cannot_be_overridden_by_handler():
    """原代码把 **self.headers 展开在 Content-Length 之后，
    handler 设置同名头即可让声明长度与实际 body 不符（响应走私/挂起）。
    """
    raw = Response(headers={"Content-Length": "999"}, body=b"hello").to_bytes()

    assert b"Content-Length: 5" in raw
    assert b"Content-Length: 999" not in raw


def test_multiple_set_cookie_headers_are_all_emitted():
    """原代码 headers 是 dict，结构上无法表达多个 Set-Cookie，会静默丢失。"""
    response = Response(cookies=["a=1", "b=2"])

    raw = response.to_bytes()

    assert b"Set-Cookie: a=1" in raw
    assert b"Set-Cookie: b=2" in raw


def test_head_response_keeps_content_length_but_omits_body():
    raw = Response(body=b"hello").to_bytes(include_body=False)

    assert b"Content-Length: 5" in raw
    assert raw.endswith(b"\r\n\r\n")


# --------------------------------------------------------------------------
# 业务纯函数
# --------------------------------------------------------------------------


def test_parse_int_list_flattens_comma_separated_values():
    assert parse_int_list(["1,2", "3", "-4"]) == ([1, 2, 3, -4], None)


def test_parse_int_list_reports_first_invalid_item():
    numbers, invalid = parse_int_list(["1,two,3"])

    assert invalid == "two"
    assert numbers == []


def test_parse_sleep_seconds_defaults_to_one_when_absent():
    """原代码这里是真 bug：source.get("sec") 缺参时返回 None，
    len(None) 直接抛 TypeError，使得它上一行的默认值永远不可达。
    """
    assert parse_sleep_seconds([], max_seconds=10) == (1, None)


@pytest.mark.parametrize(
    "raw, expected_error_fragment",
    [("abc", "not an integer"), ("-5", "must not be negative"), ("99999", "must not exceed")],
)
def test_parse_sleep_seconds_rejects_invalid_values(raw, expected_error_fragment):
    seconds, error = parse_sleep_seconds([raw], max_seconds=10)

    assert error is not None and expected_error_fragment in error


def test_extract_params_prefers_form_only_for_post_with_body():
    get_request = Request("GET", "/", "HTTP/1.1", {}, b"", query={"a": ["1"]}, form={"b": ["2"]})
    post_request = Request("POST", "/", "HTTP/1.1", {}, b"", query={"a": ["1"]}, form={"b": ["2"]})

    assert extract_params(get_request) == {"a": ["1"]}
    assert extract_params(post_request) == {"b": ["2"]}


# --------------------------------------------------------------------------
# 静态路径解析（纯函数，可密集测穿越向量）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_path",
    [
        "/static/../secret.txt",
        "/static/%2e%2e/secret.txt",
        "/static/../../etc/passwd",
        "/static//etc/passwd",
    ],
)
def test_resolve_static_path_blocks_escapes(tmp_path, url_path):
    root = tmp_path / "static"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("secret")

    assert resolve_static_path(root.resolve(), url_path) is None


def _symlink_or_skip(link, target) -> None:
    """创建符号链接；若当前平台/权限不允许则跳过测试。

    Windows 默认账户无建符号链接权限，os.symlink 会抛 OSError；
    开启「开发者模式」或以管理员身份运行后即可执行本用例。
    """
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前环境不支持创建符号链接（Windows 需开启开发者模式）：{exc}")


def test_resolve_static_path_blocks_symlink_escape(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    _symlink_or_skip(root / "link.txt", outside)

    assert resolve_static_path(root.resolve(), "/static/link.txt") is None


def test_resolve_static_path_allows_file_inside_root(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (root / "hello.txt").write_text("hello")

    resolved = resolve_static_path(root.resolve(), "/static/hello.txt")

    assert resolved == (root / "hello.txt").resolve()


# --------------------------------------------------------------------------
# 会话存储（注入假时钟，无需真的等一小时）
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_store(clock, *, max_age=3600, max_sessions=100) -> SessionStore:
    counter = iter(f"sid-{i}" for i in range(10_000))
    return SessionStore(
        max_age_seconds=max_age,
        max_sessions=max_sessions,
        clock=clock,
        id_factory=lambda: next(counter),
    )


def test_expired_session_is_not_revived_on_access():
    """Codex 发现的逻辑 bug：原代码 get_session 命中后立刻刷新 last_seen，
    而全表清理在 route() 里更晚才跑，于是过期会话在被判定过期前就已续期，
    只要客户端一直带着旧 sid 访问，会话就永不过期。
    """
    clock = FakeClock()
    store = make_store(clock)
    session_id, _ = store.create()

    clock.now += 3601  # 超过 max_age

    assert store.get(session_id) is None
    assert len(store) == 0


def test_active_session_is_kept_alive_by_touch():
    clock = FakeClock()
    store = make_store(clock)
    session_id, _ = store.create()

    for _ in range(5):
        clock.now += 1800
        assert store.get(session_id) is not None
        store.touch(session_id)

    assert len(store) == 1


def test_store_evicts_least_recently_used_when_full():
    """原代码 SESSIONS 无容量上限，匿名请求可无限撑大内存。"""
    clock = FakeClock()
    store = make_store(clock, max_sessions=3)

    first_id, _ = store.create()
    clock.now += 1
    store.create()
    clock.now += 1
    store.create()
    clock.now += 1
    store.create()  # 触发淘汰

    assert len(store) == 3
    assert store.get(first_id) is None


def test_rotate_id_changes_id_and_preserves_session_data():
    """防会话固定：登录成功后必须更换 sid。"""
    clock = FakeClock()
    store = make_store(clock)
    old_id, session = store.create()
    session["user"] = "admin"

    new_id = store.rotate_id(old_id)

    assert new_id != old_id
    assert store.get(old_id) is None
    assert store.get(new_id)["user"] == "admin"


def test_cleanup_expired_is_throttled_by_interval():
    """原代码每个请求都全量扫描一次会话表，O(n) 开销压在每次请求上。"""
    clock = FakeClock()
    store = make_store(clock)
    store.create()
    clock.now += 3601

    assert store.cleanup_expired(interval_seconds=60) == 1

    # 再造一个已过期的会话，但只推进 10 秒（不足 60 秒的清理间隔）
    session_id, session = store.create()
    session["last_seen"] = clock.now - 9999
    clock.now += 10

    assert store.cleanup_expired(interval_seconds=60) == 0  # 被节流跳过
    assert len(store) == 1  # 过期会话暂时留在表里，等下一轮清理
    assert store.cleanup_expired(force=True) == 1  # 强制清理才回收
    assert store.get(session_id) is None


# --------------------------------------------------------------------------
# Router（注入假 sleep，无需真的等待）
# --------------------------------------------------------------------------


def make_router(recorded_sleeps: list) -> Router:
    async def fake_sleep(seconds: float) -> None:
        recorded_sleeps.append(seconds)

    clock = FakeClock()
    return Router(ServerConfig(), make_store(clock), sleep=fake_sleep)


def dispatch(router: Router, request: Request) -> Response:
    return asyncio.run(router.dispatch(request))


def test_sleep_endpoint_defaults_to_one_second_without_param():
    """原代码此处返回 400，本用例锁定修复后的行为。"""
    sleeps: list = []
    request = Request("GET", "/api/v1/sleep", "HTTP/1.1", {}, b"", path="/api/v1/sleep")

    response = dispatch(make_router(sleeps), request)

    assert response.status == 200
    assert sleeps == [1]


def test_sleep_endpoint_rejects_value_above_limit_without_sleeping():
    sleeps: list = []
    request = Request(
        "GET", "/api/v1/sleep", "HTTP/1.1", {}, b"",
        path="/api/v1/sleep", query={"sec": ["99999"]},
    )

    response = dispatch(make_router(sleeps), request)

    assert response.status == 400
    assert sleeps == []  # 未真的睡下去


def test_unknown_path_returns_404():
    request = Request("GET", "/nope", "HTTP/1.1", {}, b"", path="/nope")

    assert dispatch(make_router([]), request).status == 404


# ==========================================================================
# 第二轮复审（Codex high）发现并修复的缺陷 —— 回归测试
# ==========================================================================


def test_parse_content_length_rejects_empty_value():
    """R04：头存在但值为空，不能等同于「头不存在」当成 0（否则带 body 的报文被当无 body）。"""
    with pytest.raises(BadRequest):
        parse_content_length({"content-length": ""}, max_body_bytes=1024)


def test_parse_content_length_absent_header_means_zero():
    assert parse_content_length({}, max_body_bytes=1024) == 0


@pytest.mark.parametrize("value", ["²", "³", "٣", "০"])
def test_parse_content_length_rejects_non_ascii_digits(value):
    """R05：str.isdigit() 对上标/他国数字返回 True，但 int() 未必能解析（'²' 抛 ValueError）。"""
    with pytest.raises(BadRequest):
        parse_content_length({"content-length": value}, max_body_bytes=1024)


def test_parse_content_length_rejects_absurdly_long_number():
    """R05：>4300 位数字会让 int() 直接抛异常，应按位数提前判过大而非泄漏 ValueError。"""
    with pytest.raises(PayloadTooLarge):
        parse_content_length({"content-length": "9" * 5000}, max_body_bytes=10**9)


def test_parse_headers_rejects_whitespace_before_colon():
    """R06：'Content-Length : 5' 会被前置代理与本服务不一致地解析，构成走私面。"""
    with pytest.raises(BadRequest):
        parse_head(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length : 5")


def test_response_control_headers_are_case_insensitive():
    """R07：HTTP 头名大小写不敏感，但 dict 键敏感。handler 设小写 content-length
    不得与框架的并存（响应走私），设 X-Content-Type-Options 不得覆盖 nosniff。
    """
    raw = Response(
        headers={"content-length": "999", "X-Content-Type-Options": "off"},
        body=b"hello",
    ).to_bytes()

    assert raw.lower().count(b"content-length:") == 1
    assert b"Content-Length: 5" in raw
    assert b"nosniff" in raw


def test_parse_head_requires_host_for_http_1_1():
    """R14：HTTP/1.1 必须携带 Host。"""
    with pytest.raises(BadRequest):
        parse_head(b"GET / HTTP/1.1\r\n")


def test_parse_head_allows_missing_host_for_http_1_0():
    method, _, version, headers = parse_head(b"GET / HTTP/1.0\r\n")
    assert method == "GET" and version == "HTTP/1.0" and headers == {}


def test_rotate_id_of_expired_session_returns_fresh_session():
    """R12：对已过期会话 rotate 不应把旧 last_seen 带进新 id（否则新 id 立即过期）。"""
    clock = FakeClock()
    store = make_store(clock, max_age=10)
    old_id, _ = store.create()

    clock.now += 11  # 过期
    new_id = store.rotate_id(old_id)

    assert store.get(new_id) is not None  # 新会话有效，不会一取就没


def test_login_with_non_ascii_credentials_returns_401_not_500():
    """R08：secrets.compare_digest 对非 ASCII str 抛 TypeError；改为 UTF-8 bytes 比较后
    中文等凭据应正常判为 401，而不是被顶层伪装成 500。
    """
    router = Router(ServerConfig(), make_store(FakeClock()))
    request = Request(
        "POST", "/login", "HTTP/1.1", {}, b"",
        path="/login", form={"username": ["管理员"], "password": ["密码"]},
    )

    response = asyncio.run(router.dispatch(request))

    assert response.status == 401


def test_static_nested_index_symlink_escape_is_blocked(tmp_path):
    """R09：/static/<dir> 的 index.html 若是指向根外的符号链接，追加后必须再校验归属。"""
    root = tmp_path / "static"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    sub = root / "docs"
    sub.mkdir()
    _symlink_or_skip(sub / "index.html", tmp_path / "secret.txt")

    router = Router(ServerConfig(static_root=root.resolve()), make_store(FakeClock()))
    request = Request("GET", "/static/docs", "HTTP/1.1", {}, b"", path="/static/docs")

    response = asyncio.run(router.dispatch(request))

    assert response.status == 403
    assert b"TOP SECRET" not in response.body


def test_read_request_does_not_miscount_body_as_oversized_header():
    """R03：头与 body 在同一次 recv 到达时，头部上限只应按头部长度判断，不含 body。"""

    async def drive() -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        config = ServerConfig(max_header_bytes=100)  # 故意调小
        head = b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\nContent-Length: 40\r\n\r\n"
        body = b"numbers=" + b"1," * 16  # 40 字节
        right.sendall(head + body[:40])
        right.close()
        try:
            request = await read_request(left, config)
            assert request.path == "/api/v1/sum"
            assert len(request.body) == 40
        finally:
            left.close()

    asyncio.run(drive())


def test_read_request_still_rejects_truly_oversized_header():
    """R03 反向：真正超长且无终止符的头部仍应触发 431。"""

    async def drive() -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        config = ServerConfig(max_header_bytes=100)
        right.sendall(b"X" * 300)  # 无 CRLFCRLF
        right.close()
        try:
            with pytest.raises(HttpError) as excinfo:
                await read_request(left, config)
            assert excinfo.value.status == 431
        finally:
            left.close()

    asyncio.run(drive())


def test_unexpected_handler_error_returns_fixed_500_without_leak():
    """R16.3：真正走通用异常兜底路径——注入一个会抛异常的 handler，
    断言返回固定 500 文案且不泄漏内部异常（原测试只触发了显式 400 校验）。
    """

    class BoomRouter:
        async def dispatch(self, request):
            raise RuntimeError("secret internal detail: /etc/shadow")

    config = ServerConfig()
    store = make_store(FakeClock())
    handler = ConnectionHandler(config, BoomRouter(), store)

    async def drive() -> bytes:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await handler.handle(left, ("127.0.0.1", 12345))
        right.setblocking(True)
        right.settimeout(1)
        data = b""
        while True:
            chunk = right.recv(4096)
            if not chunk:
                break
            data += chunk
        right.close()
        left.close()
        return data

    raw = asyncio.run(drive())

    assert b"500 Internal Server Error" in raw
    assert b"internal server error" in raw
    assert b"secret internal detail" not in raw  # 不泄漏异常原文
    assert b"/etc/shadow" not in raw


# ==========================================================================
# 覆盖率补全
#
# 以下用例覆盖此前只有功能测试（起真服务、打真端口）才触达、或根本没被
# 触达的分支。补齐的意义不在于数字：这些分支里有相当一部分是**故障路径**
# （客户端中途断开、读超时、bind 失败、连接被取消），而故障路径恰恰是
# 功能测试最难稳定构造、线上又最容易出事的地方。全部改为进程内构造后，
# 核心逻辑退化在不起进程、不抢端口的前提下即可被发现。
# ==========================================================================


def make_handler(router=None, *, config=None, store=None) -> ConnectionHandler:
    config = config or ServerConfig()
    store = store or make_store(FakeClock())
    return ConnectionHandler(config, router if router is not None else Router(config, store), store)


async def handle_raw(handler: ConnectionHandler, raw: bytes) -> bytes:
    """把 raw 字节喂给 handler，返回它写回的完整响应。

    用 socketpair 代替真 TCP 连接：不占端口，因而不会撞上本机已占用 8080
    这类环境问题，也不需要 wait_for_server 之类的就绪探测。
    """
    left, right = socket.socketpair()
    left.setblocking(False)
    try:
        if raw:
            right.sendall(raw)
        await handler.handle(left, ("127.0.0.1", 12345))
        right.settimeout(2)
        data = b""
        while True:
            chunk = right.recv(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        right.close()
        left.close()


# --------------------------------------------------------------------------
# 报文解析：剩余拒绝分支
# --------------------------------------------------------------------------


def test_parse_head_rejects_malformed_request_line():
    """请求行必须恰好三段，否则解包会越界。"""
    with pytest.raises(BadRequest):
        parse_head(b"GET /missing-version\r\nHost: x")


def test_parse_head_rejects_header_line_without_colon():
    with pytest.raises(BadRequest):
        parse_head(b"GET / HTTP/1.1\r\nHost: x\r\nBanana")


# --------------------------------------------------------------------------
# 会话存储：删除
# --------------------------------------------------------------------------


def test_delete_removes_session_and_tolerates_unknown_id():
    store = make_store(FakeClock())
    session_id, _ = store.create()

    store.delete(session_id)

    assert store.get(session_id) is None
    assert len(store) == 0
    store.delete("never-existed")  # 不得抛：/logout 可能带着任意 sid 打进来


# --------------------------------------------------------------------------
# 业务纯函数：剩余分支
# --------------------------------------------------------------------------


def test_parse_int_list_skips_empty_items():
    """"1,,2" 与尾随逗号是表单里的常见形态，空片段应跳过而非报错。"""
    assert parse_int_list(["1,,2", "  ", "3,"]) == ([1, 2, 3], None)


def test_parse_sleep_seconds_accepts_value_within_limit():
    assert parse_sleep_seconds(["3"], max_seconds=10) == (3, None)
    assert parse_sleep_seconds(["10"], max_seconds=10) == (10, None)  # 边界值不应被拒
    assert parse_sleep_seconds(["0"], max_seconds=10) == (0, None)


def test_resolve_static_path_maps_directory_root_to_index_html(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<h1>hi</h1>")

    assert resolve_static_path(root.resolve(), "/static/") == (root / "index.html").resolve()


# --------------------------------------------------------------------------
# Router：各端点
# --------------------------------------------------------------------------


def test_index_renders_visit_count_and_user():
    router = make_router([])
    anonymous = Request("GET", "/", "HTTP/1.1", {}, b"", path="/")
    anonymous.session = {"visits": 3}
    logged_in = Request("GET", "/", "HTTP/1.1", {}, b"", path="/")
    logged_in.session = {"visits": 1, "user": "admin"}

    assert b"Session visits: 3" in dispatch(router, anonymous).body
    assert b"anonymous" in dispatch(router, anonymous).body
    assert b"Current user: admin" in dispatch(router, logged_in).body


def test_sum_endpoint_reads_any_documented_alias():
    """SUM_PARAM_ALIASES 是从原代码里考古出来的隐式契约，必须被测试钉住，
    否则下一次重构会以为只有 numbers 一个参数名而悄悄破坏调用方。
    """
    router = make_router([])

    for alias in ("numbers", "nums", "values", "n"):
        request = Request(
            "GET", f"/api/v1/sum?{alias}=1,2,3", "HTTP/1.1", {}, b"",
            path="/api/v1/sum", query={alias: ["1,2,3"]},
        )

        response = dispatch(router, request)

        assert response.status == 200
        assert json.loads(response.body) == {"numbers": [1, 2, 3], "result": 6}


def test_sum_endpoint_without_params_returns_zero():
    request = Request("GET", "/api/v1/sum", "HTTP/1.1", {}, b"", path="/api/v1/sum")

    assert json.loads(dispatch(make_router([]), request).body) == {"numbers": [], "result": 0}


def test_sum_endpoint_rejects_invalid_integer_with_json_400():
    request = Request(
        "POST", "/api/v1/sum", "HTTP/1.1", {}, b"",
        path="/api/v1/sum", form={"numbers": ["1,two"]},
    )

    response = dispatch(make_router([]), request)

    assert response.status == 400
    assert json.loads(response.body) == {"error": "not an integer: two"}


def test_sleep_endpoint_sleeps_requested_seconds():
    sleeps: list = []
    request = Request(
        "GET", "/api/v1/sleep", "HTTP/1.1", {}, b"",
        path="/api/v1/sleep", query={"sec": ["3"]},
    )

    response = dispatch(make_router(sleeps), request)

    assert json.loads(response.body) == {"sec": 3}
    assert sleeps == [3]


def test_sleep_endpoint_rejects_unsupported_method():
    sleeps: list = []
    request = Request("PUT", "/api/v1/sleep", "HTTP/1.1", {}, b"", path="/api/v1/sleep")

    response = dispatch(make_router(sleeps), request)

    assert response.status == 405
    assert sleeps == []


def test_session_api_returns_current_session_snapshot():
    request = Request(
        "GET", "/api/v1/session", "HTTP/1.1", {}, b"",
        path="/api/v1/session", cookies={"sid": "sid-0"},
    )
    request.session_id = "sid-0"
    request.session = {"visits": 2, "user": "admin"}

    payload = json.loads(dispatch(make_router([]), request).body)

    assert payload == {
        "session_id": "sid-0",
        "visits": 2,
        "user": "admin",
        "cookies": {"sid": "sid-0"},
    }


def test_login_get_returns_form():
    request = Request("GET", "/login", "HTTP/1.1", {}, b"", path="/login")

    response = dispatch(make_router([]), request)

    assert response.status == 200
    assert b"<h1>Login</h1>" in response.body


def test_login_rejects_unsupported_method():
    request = Request("DELETE", "/login", "HTTP/1.1", {}, b"", path="/login")

    assert dispatch(make_router([]), request).status == 405


def test_login_success_rotates_session_id_and_redirects():
    """防会话固定：登录成功必须换 sid，且新 sid 要通过 Set-Cookie 下发。

    原代码沿用登录前的 sid，攻击者可预先种一个 sid 诱导受害者登录，
    随后凭同一个 sid 冒用其已认证会话。
    """
    config = ServerConfig()
    store = make_store(FakeClock())
    router = Router(config, store)
    old_id, session = store.create()
    request = Request(
        "POST", "/login", "HTTP/1.1", {}, b"",
        path="/login", form={"username": ["admin"], "password": ["password"]},
    )
    request.session_id, request.session = old_id, session

    response = asyncio.run(router.dispatch(request))

    assert response.status == 303
    assert response.headers["Location"] == "/protected"
    assert request.session_id != old_id
    assert store.get(old_id) is None  # 旧 sid 立即失效
    assert store.get(request.session_id)["user"] == "admin"
    assert any(cookie.startswith(f"{config.session_cookie}={request.session_id}") for cookie in response.cookies)


def test_login_with_wrong_password_returns_401_without_rotating():
    config = ServerConfig()
    store = make_store(FakeClock())
    router = Router(config, store)
    old_id, session = store.create()
    request = Request(
        "POST", "/login", "HTTP/1.1", {}, b"",
        path="/login", form={"username": ["admin"], "password": ["wrong"]},
    )
    request.session_id, request.session = old_id, session

    response = asyncio.run(router.dispatch(request))

    assert response.status == 401
    assert request.session_id == old_id
    assert "user" not in session


def test_logout_destroys_session_and_expires_cookie():
    config = ServerConfig()
    store = make_store(FakeClock())
    router = Router(config, store)
    session_id, session = store.create()
    session["user"] = "admin"
    request = Request("POST", "/logout", "HTTP/1.1", {}, b"", path="/logout")
    request.session_id, request.session = session_id, session

    response = asyncio.run(router.dispatch(request))

    assert response.status == 303
    assert response.headers["Location"] == "/"
    assert store.get(session_id) is None
    assert any(cookie.endswith("; Max-Age=0") for cookie in response.cookies)


def test_protected_redirects_anonymous_to_login():
    request = Request("GET", "/protected", "HTTP/1.1", {}, b"", path="/protected")

    response = dispatch(make_router([]), request)

    assert response.status == 303
    assert response.headers["Location"] == "/login"


def test_protected_serves_page_to_logged_in_user():
    request = Request("GET", "/protected", "HTTP/1.1", {}, b"", path="/protected")
    request.session = {"user": ServerConfig().username}

    response = dispatch(make_router([]), request)

    assert response.status == 200
    assert b"You are logged in" in response.body


# --------------------------------------------------------------------------
# Router：静态文件
# --------------------------------------------------------------------------


def make_static_router(root) -> Router:
    return Router(ServerConfig(static_root=root.resolve()), make_store(FakeClock()))


def static_request(path: str) -> Request:
    return Request("GET", path, "HTTP/1.1", {}, b"", path=path)


def test_static_serves_file_with_guessed_content_type(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (root / "hello.txt").write_text("hello")

    response = asyncio.run(make_static_router(root).dispatch(static_request("/static/hello.txt")))

    assert response.status == 200
    assert response.body == b"hello"
    assert response.headers["Content-Type"].startswith("text/plain")


def test_static_falls_back_to_octet_stream_for_unknown_extension(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (root / "blob.unknownext").write_bytes(b"\x00\x01")

    response = asyncio.run(
        make_static_router(root).dispatch(static_request("/static/blob.unknownext"))
    )

    assert response.headers["Content-Type"] == "application/octet-stream"


def test_static_serves_directory_index(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "index.html").write_text("<h1>docs</h1>")

    response = asyncio.run(make_static_router(root).dispatch(static_request("/static/docs")))

    assert response.status == 200
    assert b"<h1>docs</h1>" in response.body


def test_static_path_escape_returns_403(tmp_path):
    """路径穿越必须在 router 这一层就被挡下（resolve_static_path 返回 None）。"""
    root = tmp_path / "static"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET")

    response = asyncio.run(
        make_static_router(root).dispatch(static_request("/static/../secret.txt"))
    )

    assert response.status == 403
    assert b"TOP SECRET" not in response.body


def test_static_missing_file_returns_404(tmp_path):
    root = tmp_path / "static"
    root.mkdir()

    response = asyncio.run(make_static_router(root).dispatch(static_request("/static/nope.txt")))

    assert response.status == 404


def test_static_unreadable_file_returns_500_not_400(tmp_path):
    """原代码未捕获读文件的 OSError，PermissionError 会被顶层伪装成 400
    ——把服务端的权限配置错误谎报成客户端请求有问题。
    """
    root = tmp_path / "static"
    root.mkdir()
    locked = root / "locked.txt"
    locked.write_text("x")
    locked.chmod(0o000)
    try:
        locked.read_bytes()
    except OSError:
        pass
    else:  # root 用户或 Windows 上 chmod 000 仍可读，无法构造该场景
        locked.chmod(0o644)
        pytest.skip("当前环境下 chmod 000 仍可读，跳过")

    try:
        response = asyncio.run(make_static_router(root).dispatch(static_request("/static/locked.txt")))
    finally:
        locked.chmod(0o644)  # 还原，否则 tmp_path 清理可能受阻

    assert response.status == 500
    assert b"internal server error" in response.body


# --------------------------------------------------------------------------
# read_request：socket 读取边界
# --------------------------------------------------------------------------


def test_read_request_rejects_head_truncated_by_disconnect():
    """R01：原代码 recv 返回空时直接 break，再凭空补上客户端从未发送的
    CRLFCRLF，于是半个请求会被当成完整请求正常服务。
    """

    async def drive() -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.sendall(b"GET / HTTP/1.1\r\nHost: x")  # 无终止符就断开
        right.close()
        try:
            with pytest.raises(BadRequest):
                await read_request(left, ServerConfig())
        finally:
            left.close()

    asyncio.run(drive())


def test_read_request_rejects_complete_but_oversized_head():
    """头部带终止符、但长度超限时仍须 431（与「无终止符」是两条不同分支）。"""

    async def drive() -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        head = b"GET / HTTP/1.1\r\nHost: x\r\nX-Big: " + b"A" * 200 + b"\r\n\r\n"
        right.sendall(head)
        try:
            with pytest.raises(HttpError) as excinfo:
                await read_request(left, ServerConfig(max_header_bytes=100))
            assert excinfo.value.status == 431
        finally:
            right.close()
            left.close()

    asyncio.run(drive())


def test_read_request_waits_for_body_arriving_in_a_later_packet():
    """body 与 head 分包到达是 TCP 的常态，不能只读一次就以为拿全了。"""

    async def drive() -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        task = asyncio.create_task(read_request(left, ServerConfig()))
        head = (
            b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: 11\r\n\r\n"
        )
        right.sendall(head)
        await asyncio.sleep(0.05)  # 让 read_request 先把头部消费掉
        right.sendall(b"numbers=1,2")
        try:
            request = await asyncio.wait_for(task, timeout=2)
            assert request.form == {"numbers": ["1,2"]}
        finally:
            right.close()
            left.close()

    asyncio.run(drive())


def test_read_request_rejects_body_truncated_by_disconnect():
    """body 不足声明长度时必须拒绝，不能静默服务残缺 body。"""

    async def drive() -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.sendall(
            b"POST /api/v1/sum HTTP/1.1\r\nHost: x\r\nContent-Length: 40\r\n\r\nshort"
        )
        right.close()
        try:
            with pytest.raises(BadRequest):
                await read_request(left, ServerConfig())
        finally:
            left.close()

    asyncio.run(drive())


# --------------------------------------------------------------------------
# ConnectionHandler：故障路径
# --------------------------------------------------------------------------


def test_read_timeout_returns_408():
    """原代码读 socket 无任何超时，半开连接可永久占用 task 与 fd（slowloris）。"""
    handler = make_handler(config=ServerConfig(read_timeout_seconds=0.05))

    raw = asyncio.run(handle_raw(handler, b""))  # 连上就不说话

    assert b"408 Request Timeout" in raw


def test_malformed_request_is_answered_with_its_own_status():
    """解析层抛出的 HttpError 必须按自带状态码回，而不是统统 400。"""
    raw = asyncio.run(handle_raw(make_handler(), b"GET / BANANA/9.9\r\nHost: x\r\n\r\n"))

    assert b"505 HTTP Version Not Supported" in raw


def test_http_error_raised_by_router_is_mapped_to_its_status():
    class RaisingRouter:
        async def dispatch(self, request):
            raise PayloadTooLarge(log_message="内部细节不该外泄")

    raw = asyncio.run(
        handle_raw(make_handler(RaisingRouter()), b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    )

    assert b"413 Payload Too Large" in raw
    assert b"payload too large" in raw
    assert "内部细节不该外泄".encode() not in raw  # log_message 只进日志


def test_unexpected_error_while_reading_request_returns_500(monkeypatch):
    """读请求阶段的意外异常（非 HttpError/超时）必须兜成 500 并把响应发出去，
    而不是让异常穿透整个连接处理、让客户端拿到一个空响应。
    """

    async def boom(client, config):
        raise RuntimeError("unexpected read failure")

    monkeypatch.setattr(http_server, "read_request", boom)

    raw = asyncio.run(handle_raw(make_handler(), b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"))

    assert b"500 Internal Server Error" in raw
    assert b"unexpected read failure" not in raw


def test_client_disconnect_before_response_is_logged_not_raised(caplog):
    """客户端发完就走是日常现象，不该让连接处理抛异常。"""

    async def drive() -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        right.close()  # 响应还没写就断开
        try:
            await make_handler().handle(left, ("127.0.0.1", 12345))
        finally:
            left.close()

    with caplog.at_level(logging.WARNING, logger="http_server"):
        asyncio.run(drive())

    assert "failed to send response" in caplog.text


def test_cancelled_connection_still_closes_its_socket():
    """CancelledError 不是 Exception：关停/重启时若 close 只写在 except 里，
    被取消的连接会漏 fd。这里断言 socket 在取消路径上依然被关闭。
    """

    async def drive() -> socket.socket:
        left, right = socket.socketpair()
        left.setblocking(False)
        handler = make_handler(config=ServerConfig(read_timeout_seconds=30))
        task = asyncio.create_task(handler.handle(left, ("127.0.0.1", 12345)))
        await asyncio.sleep(0.05)  # 让它进入读等待
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        right.close()
        return left

    left = asyncio.run(drive())

    assert left.fileno() == -1  # fd 已释放


def test_existing_session_cookie_is_reused_and_touched():
    """带着有效 sid 的请求必须复用会话并续期，而不是每次新建一个。"""
    config = ServerConfig()
    store = make_store(FakeClock())
    handler = ConnectionHandler(config, Router(config, store), store)
    session_id, _ = store.create()
    request = (
        b"GET /api/v1/session HTTP/1.1\r\nHost: x\r\n"
        b"Cookie: sid=" + session_id.encode() + b"\r\n\r\n"
    )

    raw = asyncio.run(handle_raw(handler, request))

    payload = json.loads(raw.partition(b"\r\n\r\n")[2])
    assert payload["session_id"] == session_id
    assert payload["visits"] == 1
    assert len(store) == 1  # 没有凭空多出一个会话


# --------------------------------------------------------------------------
# HttpServer：启停
# --------------------------------------------------------------------------


def test_start_binds_ephemeral_port_and_close_releases_it():
    """port=0 让内核分配端口，是「测试不再争抢固定 8080」的前提。"""

    async def drive() -> None:
        server = HttpServer(ServerConfig(port=0))
        host, port = server.start()
        try:
            assert host == "127.0.0.1"
            assert port != 0
        finally:
            await server.close()
        assert server._socket is None

    asyncio.run(drive())


def test_start_reports_readable_error_when_port_is_taken():
    """这正是 8080 被别的进程占用时的场景：必须给出一句能看懂的 bind 失败，
    而不是原代码那样甩一堆 asyncio 栈帧，让人误判成代码 bug。
    """
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken_port = blocker.getsockname()[1]
    try:
        with pytest.raises(RuntimeError) as excinfo:
            HttpServer(ServerConfig(port=taken_port)).start()
        assert "cannot bind" in str(excinfo.value)
        assert str(taken_port) in str(excinfo.value)
    finally:
        blocker.close()


def test_serve_forever_accepts_connections_and_responds():
    async def drive() -> None:
        server = HttpServer(ServerConfig(port=0))
        host, port = server.start()
        task = asyncio.create_task(server.serve_forever())
        loop = asyncio.get_running_loop()
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.setblocking(False)
        try:
            await loop.sock_connect(client, (host, port))
            await loop.sock_sendall(client, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            data = b""
            while True:
                chunk = await loop.sock_recv(client, 4096)
                if not chunk:
                    break
                data += chunk
            assert b"200 OK" in data
        finally:
            client.close()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert server._socket is None  # 取消后 listening socket 已释放

    asyncio.run(asyncio.wait_for(drive(), timeout=10))


def test_close_cancels_in_flight_connections():
    """关停时必须取消仍在处理中的连接，否则这些 task 与 fd 会残留。"""

    async def drive() -> None:
        server = HttpServer(ServerConfig(port=0, read_timeout_seconds=30))
        host, port = server.start()
        task = asyncio.create_task(server.serve_forever())
        loop = asyncio.get_running_loop()
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.setblocking(False)
        try:
            await loop.sock_connect(client, (host, port))
            for _ in range(200):  # 等服务端 accept 并把 handler 挂进 _pending
                if server._pending:
                    break
                await asyncio.sleep(0.01)
            assert server._pending, "服务端未受理连接"
            in_flight = next(iter(server._pending))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert in_flight.cancelled()
        finally:
            client.close()

    asyncio.run(asyncio.wait_for(drive(), timeout=10))


def test_serve_forever_drops_client_it_cannot_configure(monkeypatch):
    """accept 成功但 setblocking 失败（fd 已失效）时，必须关掉这条连接、
    归还并发许可并继续服务——不能泄漏 fd，更不能让整个 accept 循环挂掉。
    """

    class StopAccepting(Exception):
        pass

    class BrokenClient:
        def __init__(self):
            self.closed = False

        def setblocking(self, flag):
            raise OSError("bad file descriptor")

        def close(self):
            self.closed = True

    broken = BrokenClient()

    async def drive() -> None:
        server = HttpServer(ServerConfig(port=0, max_concurrent_connections=1))
        server.start()
        loop = asyncio.get_running_loop()
        pending_clients = [broken]

        async def fake_accept(sock):
            if pending_clients:
                return pending_clients.pop(), ("127.0.0.1", 5555)
            raise StopAccepting  # 第二轮：证明许可已归还，循环仍在转

        monkeypatch.setattr(loop, "sock_accept", fake_accept)

        with pytest.raises(StopAccepting):
            await server.serve_forever()

    asyncio.run(asyncio.wait_for(drive(), timeout=10))

    assert broken.closed  # fd 未泄漏


def test_serve_starts_a_server_and_stops_on_cancel():
    async def drive() -> None:
        task = asyncio.create_task(serve(ServerConfig(port=0)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(drive(), timeout=10))


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------


def test_parse_args_maps_cli_flags_to_config():
    config = parse_args(["--host", "0.0.0.0", "--port", "0", "--log-level", "warning"])

    assert config.host == "0.0.0.0"
    assert config.port == 0


def test_parse_args_defaults_match_server_config():
    config = parse_args([])

    assert config.host == ServerConfig.host
    assert config.port == ServerConfig.port


def test_parse_args_tolerates_unknown_log_level():
    """未知级别退回 INFO，而不是让进程带着 AttributeError 起不来。"""
    assert parse_args(["--log-level", "banana"]).port == ServerConfig.port


def patch_entrypoint(monkeypatch) -> None:
    """让 main() 走完整条路径但不真的起服务：asyncio.run 立刻抛 KeyboardInterrupt。"""
    monkeypatch.setattr(sys, "argv", ["http_server.py", "--port", "0"])

    def fake_run(coro):
        coro.close()  # 避免 "coroutine was never awaited"
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", fake_run)


def test_main_exits_quietly_on_keyboard_interrupt(monkeypatch, caplog):
    """Ctrl-C 是正常的停服方式，不该给运维甩一段 traceback。"""
    patch_entrypoint(monkeypatch)

    with caplog.at_level(logging.INFO, logger="http_server"):
        main()  # 不得抛

    assert "stopped" in caplog.text


def test_module_runs_as_a_script(monkeypatch):
    """`python http_server.py` 是文档里写的用法，__main__ 入口必须真的可执行。"""
    patch_entrypoint(monkeypatch)

    runpy.run_path(http_server.__file__, run_name="__main__")  # 不得抛
