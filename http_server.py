"""服务入口：socket I/O、连接生命周期、请求管线。

本模块是唯一接触网络的地方；解析、会话、业务分别在
http_core / sessions / handlers 中，均可脱离网络单测。

用法：
    python http_server.py            # 监听 127.0.0.1:8080
    python http_server.py --port 0   # 由内核分配端口
"""

import argparse
import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Optional

from config import ServerConfig
from handlers import Router, error_response
from http_core import (
    BadRequest,
    HttpError,
    Request,
    RequestTimeout,
    Response,
    build_request,
    build_set_cookie,
    http_date,
    parse_content_length,
    parse_head,
    text_response,
)
from sessions import SessionStore


logger = logging.getLogger("http_server")

RECV_CHUNK_BYTES = 4096
# 无并发上限时，攻击者可无成本地把 task 与 fd 撑爆
MAX_CONCURRENT_CONNECTIONS = 256


async def read_request(client: socket.socket, config: ServerConfig) -> Request:
    """从 socket 读取并解析一个完整请求。

    与原代码的差异：
      - 头部只解析一次（原代码在读取和解析阶段各解析了一遍，且规则不一致）；
      - 连接提前中断时抛 400，而不是凭空补上客户端从未发送的 CRLFCRLF；
      - body 不足声明长度时抛 400，不静默处理残缺 body；
      - body 超出声明长度时截断到声明长度。
    """
    loop = asyncio.get_running_loop()

    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await loop.sock_recv(client, RECV_CHUNK_BYTES)
        if not chunk:
            raise BadRequest(log_message="connection closed before request head was complete")
        data += chunk
        if len(data) > config.max_header_bytes:
            raise HttpError(
                431,
                "Request Header Fields Too Large",
                "request header fields too large",
                log_message=f"head exceeded {config.max_header_bytes} bytes",
            )

    head, _, body = data.partition(b"\r\n\r\n")
    method, target, version, headers = parse_head(head)
    content_length = parse_content_length(headers, config.max_body_bytes)

    while len(body) < content_length:
        chunk = await loop.sock_recv(client, content_length - len(body))
        if not chunk:
            raise BadRequest(
                log_message=f"body truncated: got {len(body)} of {content_length} bytes"
            )
        body += chunk

    return build_request(method, target, version, headers, body[:content_length])


@dataclass
class Exchange:
    """一次请求/响应往返的结果。

    这些字段必须随调用栈传递，绝不能存成 ConnectionHandler 的实例属性：
    handler 实例被所有并发连接共享，存实例上会让并发请求互相覆盖。
    """

    response: Response
    method: str = "-"
    path: str = "-"
    include_body: bool = True


class ConnectionHandler:
    """处理单条连接的完整生命周期。无每连接可变状态，可被并发共享。"""

    def __init__(self, config: ServerConfig, router: Router, session_store: SessionStore):
        self._config = config
        self._router = router
        self._session_store = session_store

    async def handle(self, client: socket.socket, address: tuple[str, int]) -> None:
        started_at = time.monotonic()
        try:
            exchange = await self._build_exchange(client, address)
        except Exception:
            logger.exception("unhandled error while serving %s", address)
            exchange = Exchange(
                error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")
            )

        try:
            payload = exchange.response.to_bytes(http_date(), include_body=exchange.include_body)
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_sendall(client, payload),
                timeout=self._config.read_timeout_seconds,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("failed to send response to %s: %s", address, exc)
        finally:
            client.close()
            logger.info(
                "%s %s %s -> %d (%.1f ms)",
                address[0],
                exchange.method,
                exchange.path,
                exchange.response.status,
                (time.monotonic() - started_at) * 1000,
            )

    async def _build_exchange(self, client: socket.socket, address: tuple[str, int]) -> Exchange:
        try:
            request = await asyncio.wait_for(
                read_request(client, self._config),
                timeout=self._config.read_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # 原代码读 socket 无任何超时，半开连接可永久占用 task 与 fd
            logger.warning("read timeout from %s", address)
            return Exchange(self._to_response(RequestTimeout()))
        except HttpError as exc:
            logger.warning("rejected request from %s: %s", address, exc.log_message)
            return Exchange(self._to_response(exc))

        exchange = Exchange(
            response=Response(),
            method=request.method,
            path=request.path,
            include_body=request.method != "HEAD",
        )
        try:
            self._bind_session(request)
            exchange.response = await self._router.dispatch(request)
        except HttpError as exc:
            logger.warning("request from %s failed: %s", address, exc.log_message)
            exchange.response = self._to_response(exc)
        except Exception:
            # 原代码把所有异常统统映射为 400 并回显异常原文，
            # 既把服务端 bug 谎报成客户端错误，又泄漏内部实现细节。
            logger.exception("handler crashed on %s %s", request.method, request.path)
            exchange.response = error_response(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error"
            )

        self._attach_session_cookie(exchange.response, request)
        return exchange

    @staticmethod
    def _to_response(error: HttpError) -> Response:
        return text_response(error.client_message, error.status, error.reason)

    def _bind_session(self, request: Request) -> None:
        """把会话绑定到请求上。

        原代码在 parse_request（一个签名承诺是纯函数的解析器）里创建会话
        并递增 visits，导致任何解析测试都会污染全局会话表。会话是请求
        管线的职责，不是解析器的职责。
        """
        self._session_store.cleanup_expired(
            interval_seconds=self._config.session_cleanup_interval_seconds
        )
        session_id = request.cookies.get(self._config.session_cookie, "")
        session = self._session_store.get(session_id)
        if session is None:
            session_id, session = self._session_store.create()
        else:
            self._session_store.touch(session_id)

        request.session_id = session_id
        request.session = session
        session["visits"] = session.get("visits", 0) + 1

    def _attach_session_cookie(self, response: Response, request: Request) -> None:
        """统一在出口挂 session cookie。

        原代码把兜底放在 route() 末尾，于是两个 `return` 早退分支绕过了它，
        客户端在那两条路径上拿不到 sid。放在管线出口就没有分支能绕开。
        """
        already_set = any(
            cookie.split("=", 1)[0] == self._config.session_cookie for cookie in response.cookies
        )
        if not already_set:
            response.cookies.append(
                build_set_cookie(self._config.session_cookie, request.session_id)
            )


class HttpServer:
    """可编程启停的服务器。

    原代码只有一个 serve() 把「建 socket + 绑定 + accept 循环」焊死，
    端口写在模块常量里，于是测试只能 subprocess 拉起真进程去打 8080。
    实测这会让测试信号与代码脱钩：8080 被别的进程占用时，子进程带着
    EADDRINUSE 崩溃，而测试连上了占位者，照样 7/7 全绿。

    把 start() 与 serve_forever() 分开、并允许 port=0 由内核分配端口，
    测试就能在进程内起一个独占实例，不再争抢固定端口。
    """

    def __init__(self, config: ServerConfig, clock=time.time):
        self._config = config
        self.session_store = SessionStore(
            max_age_seconds=config.session_max_age_seconds,
            max_sessions=config.max_sessions,
            clock=clock,
        )
        self.router = Router(config, self.session_store)
        self._handler = ConnectionHandler(config, self.router, self.session_store)
        self._socket: Optional[socket.socket] = None
        self._pending: set[asyncio.Task] = set()

    def start(self) -> tuple[str, int]:
        """绑定并监听，返回实际监听地址。port=0 时由内核分配。"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self._config.host, self._config.port))
        except OSError as exc:
            # 原代码在此直接抛 traceback，运维只看到一堆 asyncio 栈帧
            server.close()
            raise RuntimeError(
                f"cannot bind {self._config.host}:{self._config.port} — {exc}"
            ) from exc
        server.listen()
        server.setblocking(False)
        self._socket = server
        host, port = server.getsockname()
        logger.info("serving on http://%s:%d", host, port)
        return host, port

    async def serve_forever(self) -> None:
        assert self._socket is not None, "call start() first"
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
        loop = asyncio.get_running_loop()

        async def serve_one(client: socket.socket, address: tuple[str, int]) -> None:
            async with semaphore:
                await self._handler.handle(client, address)

        try:
            while True:
                client, address = await loop.sock_accept(self._socket)
                try:
                    client.setblocking(False)
                except OSError:
                    client.close()
                    continue
                # asyncio 只持弱引用，不留强引用的 task 可能被 GC 中途回收
                task = asyncio.create_task(serve_one(client, address))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        except asyncio.CancelledError:
            raise
        finally:
            await self.close()

    async def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


async def serve(config: ServerConfig) -> None:
    server = HttpServer(config)
    server.start()
    await server.serve_forever()


def parse_args(argv: Optional[list[str]] = None) -> ServerConfig:
    parser = argparse.ArgumentParser(description="asyncio + socket HTTP server")
    parser.add_argument("--host", default=ServerConfig.host)
    parser.add_argument("--port", type=int, default=ServerConfig.port)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return ServerConfig(host=args.host, port=args.port)


def main() -> None:
    config = parse_args()
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
