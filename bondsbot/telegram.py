from __future__ import annotations

import re
from urllib.parse import urlencode
from urllib.request import urlopen


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, message: str, parse_mode: str | None = None) -> None:
        if not self.enabled:
            return
        payload = {"chat_id": self.chat_id, "text": message[:3900]}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        params = urlencode(payload)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage?{params}"
        try:
            with urlopen(url, timeout=10):
                return
        except OSError:
            if not parse_mode:
                return
            plain_text = re.sub(r"<[^>]+>", "", message)
            params = urlencode({"chat_id": self.chat_id, "text": plain_text[:3900]})
            url = f"https://api.telegram.org/bot{self.token}/sendMessage?{params}"
            try:
                with urlopen(url, timeout=10):
                    return
            except OSError:
                return
