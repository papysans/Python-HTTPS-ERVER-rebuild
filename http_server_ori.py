import asyncio
import json
import mimetypes
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit


HOST = "127.0.0.1"
PORT = 8080
STATIC_ROOT = Path(__file__).with_name("static").resolve()
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 1024 * 1024
SESSION_COOKIE = "sid"
SESSION_MAX_AGE_SECONDS = 60 * 60
USERNAME = "admin"
PASSWORD = "password"

SESSIONS: dict[str, dict] = {}


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: dict[str, str]
    body: bytes
    path: str = ""
    query: dict[str, list[str]] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    form: dict[str, list[str]] = field(default_factory=dict)
    session_id: str = ""
    session: dict = field(default_factory=dict)


@dataclass
class Response:
    status: int = 200
    reason: str = "OK"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def to_bytes(self) -> bytes:
        headers = {
            "Server": "asyncio-socket-http",
            "Date": http_date(),
            "Connection": "close",
            "Content-Length": str(len(self.body)),
            **self.headers,
        }
        head = [f"HTTP/1.1 {self.status} {self.reason}"]
        head.extend(f"{name}: {value}" for name, value in headers.items())
        return ("\r\n".join(head) + "\r\n\r\n").encode("iso-8859-1") + self.body


def http_date() -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())


def response_type1(text: str, status: int = 200, reason: str = "OK") -> Response:
    return Response(
        status=status,
        reason=reason,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        body=text.encode("utf-8"),
    )


def response_type2(html: str, status: int = 200, reason: str = "OK") -> Response:
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
    

def do_cookie(do_parse, header, name: str = '', value: str= '', max_age: Optional[int] = None) -> str:
    if do_parse:
        cookies = {}
        for part in header.split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies[name.strip()] = unquote(value.strip())
        return cookies
    else:
        cookie = f"{name}={value}; Path=/; HttpOnly; SameSite=Lax"
        if max_age is not None:
            cookie += f"; Max-Age={max_age}"
        return cookie


def get_session(request: Request) -> tuple[str, dict, bool]:
    sid = request.cookies.get(SESSION_COOKIE, "")
    session = SESSIONS.get(sid)
    if sid and session is not None:
        session["last_seen"] = time.time()
        return sid, session, False

    sid = secrets.token_urlsafe(24)
    session = {"created_at": time.time(), "last_seen": time.time(), "visits": 0}
    SESSIONS[sid] = session
    return sid, session, True


def cleanup_sessions() -> None:
    now = time.time()
    expired = [
        sid
        for sid, session in SESSIONS.items()
        if now - session.get("last_seen", now) > SESSION_MAX_AGE_SECONDS
    ]
    for sid in expired:
        del SESSIONS[sid]


def parse_request(raw: bytes) -> Request:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    if not lines or len(lines[0].split()) != 3:
        raise ValueError("bad request line")

    method, target, version = lines[0].split()
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise ValueError("bad header")
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    split = urlsplit(target)
    request = Request(
        method=method.upper(),
        target=target,
        version=version,
        headers=headers,
        body=body,
        path=split.path or "/",
        query=parse_qs(split.query, keep_blank_values=True),
        cookies=do_cookie(True, headers.get("cookie", "")),
    )

    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if request.body and content_type == "application/x-www-form-urlencoded":
        request.form = parse_qs(
            request.body.decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )

    sid, session, is_new = get_session(request)
    request.session_id = sid
    request.session = session
    request.session["_is_new"] = is_new
    request.session["visits"] = request.session.get("visits", 0) + 1
    return request


async def read_http_request(client: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await asyncio.get_running_loop().sock_recv(client, 4096)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_HEADER_BYTES:
            raise ValueError("headers too large")

    head, _, body = data.partition(b"\r\n\r\n")
    headers = {}
    for line in head.decode("iso-8859-1", errors="replace").split("\r\n")[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

    content_length = int(headers.get("content-length", "0") or "0")
    if content_length > MAX_BODY_BYTES:
        raise ValueError("body too large")

    while len(body) < content_length:
        chunk = await asyncio.get_running_loop().sock_recv(client, content_length - len(body))
        if not chunk:
            break
        body += chunk

    return head + b"\r\n\r\n" + body


def is_authenticated(request: Request) -> bool:
    return request.session.get("user") == USERNAME


def require_auth(request: Request) -> Optional[Response]:
    if is_authenticated(request):
        return None
    response = redirect("/login")
    response.headers["Set-Cookie"] = do_cookie(False, None, SESSION_COOKIE, request.session_id)
    return response
    

def handle_login(request: Request) -> Response:
    if request.method == "GET":
        return response_type2(
            """
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
        )

    if request.method != "POST":
        return response_type1("method not allowed", 405, "Method Not Allowed")

    username = request.form.get("username", [""])[0]
    password = request.form.get("password", [""])[0]
    if username == USERNAME and password == PASSWORD:
        request.session["user"] = USERNAME
        response = redirect("/protected")
        response.headers["Set-Cookie"] = do_cookie(False, None, SESSION_COOKIE, request.session_id)
        return response

    return response_type2("<h1>Login failed</h1><p>Invalid username or password.</p>", 401, "Unauthorized")


def handle_logout(request: Request) -> Response:
    SESSIONS.pop(request.session_id, None)
    response = redirect("/")
    response.headers["Set-Cookie"] = do_cookie(False, None, SESSION_COOKIE, "", 0)
    return response

 
def handle_session_api(request: Request) -> Response:
    return json_response(
        {
            "session_id": request.session_id,
            "visits": request.session.get("visits", 0),
            "user": request.session.get("user"),
            "cookies": request.cookies,
        }
    )


def safe_static_path(url_path: str) -> Optional[Path]:
    if url_path == "/static/":
        candidate = STATIC_ROOT / "index.html"
    else:
        relative = unquote(url_path.removeprefix("/static/"))
        candidate = STATIC_ROOT / relative

    resolved = candidate.resolve()
    try:
        resolved.relative_to(STATIC_ROOT)
    except ValueError:
        return None
    return resolved


def serve_static(request: Request) -> Response:
    path = safe_static_path(request.path)
    if path is None:
        return response_type1("forbidden", 403, "Forbidden")
    if not path.exists():
        return response_type1("not found", 404, "Not Found")
    if path.is_dir():
        path = path / "index.html"
        if not path.exists():
            return response_type1("not found", 404, "Not Found")

    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return Response(headers={"Content-Type": content_type}, body=path.read_bytes())


async def route(request: Request) -> Response:
    cleanup_sessions()

    if request.path == "/":
        user = request.session.get("user") or "anonymous"
        response = response_type2(
            f"""
            <!doctype html>
            <html lang="en">
            <head><meta charset="utf-8"><title>asyncio http server</title></head>
            <body>
            <h1>asyncio + socket HTTP server</h1>
            <p>Session visits: {request.session.get("visits", 0)}</p>
            <p>Current user: {user}</p>
            <ul>
                <li><a href="/api/v1/sum?numbers=1,2,3">/api/v1/sum?numbers=1,2,3</a></li>
                <li><a href="/api/v1/session">/api/v1/session</a></li>
                <li><a href="/login">/login</a> ({USERNAME}/{PASSWORD})</li>
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
        )
    elif request.path == "/api/v1/sum":
        source = request.form if request.method == "POST" and request.form else request.query
        raw_values = (
            source.get("numbers")
            or source.get("nums")
            or source.get("values")
            or source.get("n")
            or []
        )
        numbers = []
        for value in raw_values:
            for item in value.split(","):
                item = item.strip()
                if item:
                    try:
                        numbers.append(int(item))
                    except ValueError:
                        return json_response(
                            {"error": f"not an integer: {item}"},
                            400,
                            "Bad Request",
                        )
        response = json_response({"numbers": numbers, "result": sum(numbers)})
    elif request.path == "/api/v1/sleep":
        if request.method not in {"GET", "POST"}:
            return response_type1("method not allowed", 405, "Method Not Allowed")
        source = request.form if request.method == "POST" and request.form else request.query
        sec = source.get("sec")
        sleep_sec = 1
        if len(sec) > 0:
            sleep_sec = int(sec[0])
        await asyncio.sleep(sleep_sec)
        response = json_response({"sec": sleep_sec}) 
    elif request.path == "/api/v1/session":
        response = handle_session_api(request)
    elif request.path == "/login":
        response = handle_login(request)
    elif request.path == "/logout":
        response = handle_logout(request)
    elif request.path == "/protected":
        auth_response = require_auth(request)
        response = auth_response or response_type2("<h1>Protected</h1><p>You are logged in.</p>")
    elif request.path.startswith("/static/"):
        response = serve_static(request)
    else:
        response = response_type1("not found", 404, "Not Found")

    if SESSION_COOKIE not in response.headers.get("Set-Cookie", ""):
        response.headers.setdefault("Set-Cookie", do_cookie(False, None, SESSION_COOKIE, request.session_id))
    return response


async def do_client(client: socket.socket, address: tuple[str, int]) -> None:
    try:
        raw = await read_http_request(client)
        request = parse_request(raw)
        response = await route(request)
    except Exception as exc:
        response = response_type1(f"bad request: {exc}", 400, "Bad Request")

    try:
        await asyncio.get_running_loop().sock_sendall(client, response.to_bytes())
    finally:
        client.close()


async def serve(host: str = HOST, port: int = PORT) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    server.setblocking(False)

    def on_client_done(task: asyncio.Task[None]) -> None:
        print("task end", task.get_name())
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    print(f"Serving on http://{host}:{port}")
    try:
        while True:
            client, address = await asyncio.get_running_loop().sock_accept(server)
            client.setblocking(False)
            task = asyncio.create_task(do_client(client, address))
            task.add_done_callback(on_client_done)
    finally:
        server.close()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        print("\nStopped.")
