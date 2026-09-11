"""Telegram Bot API client (stdlib only) plus message rendering.

Only the methods this tool needs: getMe, getUpdates, sendMessage,
editMessageText, editMessageReplyMarkup, answerCallbackQuery, sendChatAction,
setMyCommands.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.telegram.org/bot%s/%s"


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token, timeout=35):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self.token = token
        self.timeout = timeout

    def call(self, method, **payload):
        url = API_ROOT % (self.token, method)
        data = json.dumps(payload).encode("utf-8") if payload else None
        request = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8", "replace"))
                if not body.get("ok"):
                    raise TelegramError("%s: %s" % (method, body.get("description")))
                return body.get("result")
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                last_error = "HTTP %s %s" % (exc.code, detail)
                if exc.code == 429 or exc.code >= 500:
                    retry_after = 3
                    try:
                        retry_after = int(exc.headers.get("Retry-After") or 3)
                    except (TypeError, ValueError):
                        pass
                    if attempt < 2:
                        time.sleep(min(retry_after, 20))
                        continue
                raise TelegramError("%s: %s" % (method, last_error))
            except Exception as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise TelegramError("%s: %s" % (method, last_error))

    # ------------------------------------------------------------------ basics
    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset=None, timeout=25):
        payload = {"timeout": timeout, "allowed_updates":
                   ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", **payload)

    def send_message(self, chat_id, text, keyboard=None, preview=False,
                     silent=False):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
            "disable_notification": silent,
        }
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard)
        return self.call("sendMessage", **payload)

    def edit_text(self, chat_id, message_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard is not None:
            payload["reply_markup"] = json.dumps(keyboard)
        return self.call("editMessageText", **payload)

    def edit_keyboard(self, chat_id, message_id, keyboard=None):
        return self.call("editMessageReplyMarkup",
                         chat_id=chat_id, message_id=message_id,
                         reply_markup=json.dumps(keyboard or {"inline_keyboard": []}))

    def answer_callback(self, callback_id, text=None, alert=False):
        payload = {"callback_query_id": callback_id, "show_alert": alert}
        if text:
            payload["text"] = text[:190]
        try:
            return self.call("answerCallbackQuery", **payload)
        except TelegramError:
            return None

    def set_commands(self, commands):
        return self.call("setMyCommands", commands=[
            {"command": c, "description": d} for c, d in commands])
