"""Авторизация Mini App: проверка подписи initData от Telegram.

Telegram подписывает initData токеном бота (HMAC-SHA256), поэтому по нему бэкенд
надёжно узнаёт, кто открыл приложение, — без паролей и логинов.
Фронтенд шлёт initData в заголовке: Authorization: tma <initData>
"""
from __future__ import annotations

import time

from aiogram.utils.web_app import WebAppInitData, safe_parse_webapp_init_data
from fastapi import Header, HTTPException, Request

# initData живёт, пока открыто приложение; старше суток — просим открыть заново
MAX_AGE_SECONDS = 24 * 3600


def validate_init_data(token: str, init_data: str, now: float | None = None) -> WebAppInitData:
    try:
        data = safe_parse_webapp_init_data(token, init_data)
    except ValueError:
        raise HTTPException(status_code=401, detail="bad_signature") from None
    if data.user is None:
        raise HTTPException(status_code=401, detail="no_user")
    age = (now or time.time()) - data.auth_date.timestamp()
    if age > MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="expired")
    return data


async def telegram_user(request: Request, authorization: str = Header(default="")) -> WebAppInitData:
    scheme, _, init_data = authorization.partition(" ")
    if scheme.lower() != "tma" or not init_data:
        raise HTTPException(status_code=401, detail="no_init_data")
    return validate_init_data(request.app.state.settings.bot_token, init_data)
