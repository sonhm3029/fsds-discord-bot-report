"""Deliver the reminder text: Telegram (HTTP Bot API) or a Discord DM."""

from __future__ import annotations

import requests

TELEGRAM_LIMIT = 4000  # API maximum is 4096 characters
DISCORD_LIMIT = 1900  # API maximum is 2000 characters


class NotifyError(RuntimeError):
    pass


def split_message(text: str, limit: int) -> list[str]:
    """Split on line boundaries so no chunk exceeds `limit` (very long lines are cut)."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, session=None) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._session = session or requests.Session()

    def send(self, text: str) -> None:
        for chunk in split_message(text, TELEGRAM_LIMIT):
            try:
                resp = self._session.post(
                    self._url,
                    json={"chat_id": self._chat_id, "text": chunk, "disable_web_page_preview": True},
                    timeout=20,
                )
            except requests.RequestException as exc:
                # Do not include str(exc): it contains the URL, which contains the bot token.
                raise NotifyError(f"Telegram unreachable ({type(exc).__name__})") from None
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code != 200 or not body.get("ok"):
                raise NotifyError(f"Telegram rejected the message: HTTP {resp.status_code} {body.get('description', '')}".strip())
