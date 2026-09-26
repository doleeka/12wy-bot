import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

TOKEN = "123456:TEST-TOKEN"


def sign_init_data(user: dict, token: str = TOKEN, auth_date: int | None = None) -> str:
    """initData так же, как его подписывает Telegram (HMAC-SHA256 с ключом из токена бота)."""
    fields = {
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAH-test",
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def auth(user_id: int = 42, first_name: str = "Анна", **kw) -> dict:
    return {"Authorization": "tma " + sign_init_data({"id": user_id, "first_name": first_name}, **kw)}
