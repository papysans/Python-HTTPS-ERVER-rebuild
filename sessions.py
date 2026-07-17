"""会话存储。

原代码用模块级 SESSIONS 字典 + 三个自由函数管理会话，问题是：
  - 全局状态，单测之间互相污染，必须手工清表；
  - 无容量上限，匿名请求可无限撑大内存；
  - time.time() 直接写死，无法测试过期逻辑；
  - get_session 同时做「查 / 建 / 续期」三件事。
本模块把它收敛成一个可注入时钟、有容量上限的存储对象。
"""

import logging
import secrets
from typing import Callable, Optional


logger = logging.getLogger(__name__)

SESSION_ID_ENTROPY_BYTES = 24


class SessionStore:
    def __init__(
        self,
        *,
        max_age_seconds: int,
        max_sessions: int,
        clock: Callable[[], float],
        id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(SESSION_ID_ENTROPY_BYTES),
    ):
        self._sessions: dict[str, dict] = {}
        self._max_age_seconds = max_age_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._id_factory = id_factory
        self._last_cleanup_at = 0.0

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, session_id: str) -> Optional[dict]:
        """只查，不建、不续期。已过期的会话视为不存在并就地删除。

        原代码的 get_session 命中后立刻刷新 last_seen，而全表清理在
        route() 里更晚才执行，于是过期会话在被判定过期之前就已被续期——
        只要客户端一直带着旧 sid 访问，会话就永不过期，过期策略形同虚设。
        判定过期必须先于刷新。
        """
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if self._is_expired(session):
            del self._sessions[session_id]
            logger.info("session expired on access, removed")
            return None
        return session

    def _is_expired(self, session: dict) -> bool:
        now = self._clock()
        return now - session.get("last_seen", now) > self._max_age_seconds

    def touch(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session["last_seen"] = self._clock()

    def create(self) -> tuple[str, dict]:
        """新建会话。超出容量上限时淘汰最久未访问的一条。"""
        self._evict_if_full()
        now = self._clock()
        session_id = self._id_factory()
        session = {"created_at": now, "last_seen": now, "visits": 0}
        self._sessions[session_id] = session
        return session_id, session

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def rotate_id(self, old_session_id: str) -> str:
        """更换 session id 并保留会话内容（防会话固定）。

        原代码登录成功后沿用登录前的 sid，攻击者可预先种一个 sid
        诱导受害者登录，随后凭同一个 sid 冒用其已认证会话。
        """
        session = self._sessions.pop(old_session_id, None)
        # 旧会话不存在或已过期时，不应把它的旧 last_seen 带进新 id
        # （否则返回的新 id 下一次 get 就立即过期）；直接建一个全新会话。
        if session is None or self._is_expired(session):
            session_id, _ = self.create()
            return session_id
        self._evict_if_full()
        new_session_id = self._id_factory()
        self._sessions[new_session_id] = session
        return new_session_id

    def cleanup_expired(self, *, force: bool = False, interval_seconds: float = 0.0) -> int:
        """清理过期会话，返回清理条数。

        原代码在每个请求里做一次全表扫描；这里按时间间隔节流，
        把 O(n) 的开销从「每请求」摊薄到「每 interval」。
        """
        now = self._clock()
        if not force and now - self._last_cleanup_at < interval_seconds:
            return 0
        self._last_cleanup_at = now

        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.get("last_seen", now) > self._max_age_seconds
        ]
        for session_id in expired:
            del self._sessions[session_id]
        if expired:
            logger.info("cleaned up %d expired sessions, %d remain", len(expired), len(self._sessions))
        return len(expired)

    def _evict_if_full(self) -> None:
        if len(self._sessions) < self._max_sessions:
            return
        self.cleanup_expired(force=True)
        while len(self._sessions) >= self._max_sessions:
            oldest_id = min(
                self._sessions,
                key=lambda sid: self._sessions[sid].get("last_seen", 0.0),
            )
            del self._sessions[oldest_id]
            logger.warning(
                "session store full (max=%d), evicted least-recently-used session",
                self._max_sessions,
            )
