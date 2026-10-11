"""Telegram Bot API client.

Only the standard library is used so the dispatcher has no extra dependencies
beyond :pypi:`psutil`. The client is intentionally small: it wraps
``sendMessage`` (and a couple of helpers) and raises :class:`TelegramError`
on API failures so the dispatcher can react without parsing HTTP details.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

API_BASE = "https://api.telegram.org/bot"
_DEFAULT_TIMEOUT = 30  # seconds


class TelegramError(Exception):
    """A Telegram API call failed."""

    def __init__(self, method: str, code: Optional[int], description: Optional[str]):
        self.method = method
        self.code = code
        self.description = description
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.description:
            return f"Telegram {self.method}: {self.description}"
        return f"Telegram {self.method}: HTTP {self.code}"


@dataclass
class SendMessageResult:
    """The result of a successful ``sendMessage`` call."""
    message_id: Optional[int]
    chat_id: str
    date: str

    @classmethod
    def from_api(cls, payload: Dict[str, Any]) -> "SendMessageResult":
        result = payload.get("result", {})
        chat = result.get("chat", {})
        return cls(
            message_id=result.get("message_id"),
            chat_id=str(chat.get("id", result.get("chat_id", ""))),
            date=result.get("date", ""),
        )


class TelegramBot:
    """Thin wrapper around the Telegram Bot API.

    Parameters
    ----------
    token :
        The bot token from ``@BotFather``.
    chat_id :
        The destination chat. Numeric ids are used as-is; ``@username``
        strings are resolved to their numeric id on first use.
    parse_mode :
        ``"MarkdownV2"`` (default) or ``"HTML"``.
    """

    def __init__(self, token: str, chat_id: Any, *, parse_mode: str = "MarkdownV2"):
        self.token = token
        self.chat_id = self._normalize_chat_id(chat_id)
        self.parse_mode = parse_mode
        self.request_count = 0

    @property
    def api_url(self) -> str:
        return f"{API_BASE}{self.token}"

    @staticmethod
    def _normalize_chat_id(chat_id: Any) -> str:
        """Convert ``@username`` to a numeric id when possible.

        A raw ``@`` prefix triggers a lightweight :meth:`resolve` lookup; a bare
        numeric id or ``@``-less name is passed through unchanged.
        """
        raw = str(chat_id).strip()
        if raw.startswith("@") and not re.fullmatch(r"^-?\d+$", raw):
            return raw
        return raw

    def send_message(
        self,
        text: str,
        *,
        thread_id: Optional[int] = None,
        disable_notification: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> SendMessageResult:
        """Send a text message to the configured chat.

        Parameters
        ----------
        text :
            The message body. It is MarkdownV2-escaped automatically when
            ``parse_mode`` is ``MarkdownV2``.
        thread_id :
            Forum topic id (``message_thread_id``).
        disable_notification :
            ``True`` to send silently.
        reply_markup :
            Optional inline keyboard / reply markup dict.
        """
        params: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": self._escape(text),
            "parse_mode": self.parse_mode,
        }
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        if disable_notification:
            params["disable_notification"] = disable_notification
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        return SendMessageResult.from_api(self._request("sendMessage", params))

    def send_photo(
        self,
        photo: str,
        caption: Optional[str] = None,
        *,
        thread_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a photo (URL or file_id) with an optional caption."""
        params: Dict[str, Any] = {"chat_id": self.chat_id, "photo": photo}
        if caption is not None:
            params["caption"] = self._escape(caption)
            params["parse_mode"] = self.parse_mode
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        return self._request("sendPhoto", params)

    def get_chat(self) -> Dict[str, Any]:
        """Resolve ``@username`` to a numeric chat id."""
        return self._request("getChat", {"chat_id": self.chat_id})

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.api_url}/{method}"
        data = urllib.parse.urlencode(params or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise TelegramError(method, exc.code, f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TelegramError(method, None, f"network error: {exc.reason}") from exc

        payload = json.loads(raw)
        if not payload.get("ok"):
            raise TelegramError(method, None, payload.get("description"))
        return payload

    @staticmethod
    def _escape(text: str) -> str:
        """MarkdownV2 safe-escape user controlled content."""
        for ch in "\\_*[]()~>`#+-|=!.{}":
            text = text.replace(ch, "\\" + ch)
        return text


def build_alert_markup(alert: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Build a small inline keyboard for an alert.

    The ``view`` button opens the raw JSON payload; the ``close`` button
    dismisses the message.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "View JSON", "callback_data": "view"},
                {"text": "Close", "callback_data": "close"},
            ],
        ],
    }