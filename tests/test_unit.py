"""单元测试：全程不起进程、不绑端口、不碰全局状态。

原代码这一层测试根本写不出来——核心逻辑全部耦合模块级全局
（SESSIONS / time.time / STATIC_ROOT / USERNAME），只能从 8080
这个唯一入口去打黑盒 e2e。本文件的存在本身就是解耦的验收标准。
"""

import asyncio

import pytest

from config import ServerConfig
from handlers import (
    Router,
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
