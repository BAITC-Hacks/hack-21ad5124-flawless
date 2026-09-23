"""Signed cart snapshots for stateless hosting such as Vercel Functions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from agent import ShopTools

MAX_TOKEN_LENGTH = 8192
TOKEN_AGE = timedelta(days=7)


class InvalidCartToken(ValueError):
    pass


class CartStateCodec:
    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ValueError("CART_SIGNING_KEY must contain at least 32 characters")
        self.key = secret.encode("utf-8")

    def encode(self, tools: ShopTools, session_id: str) -> str:
        with tools.lock:
            cart = tools.carts.get(session_id, {}).copy()
            processed = sorted(
                [article, qty, turn]
                for sid, article, qty, turn in tools.processed_additions
                if sid == session_id
            )
        payload = {"v": 2, "sid": session_id, "cart": cart, "processed": processed, "iat": int(datetime.now(timezone.utc).timestamp())}
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(data).rstrip(b"=")
        signature = hmac.new(self.key, encoded, hashlib.sha256).hexdigest().encode("ascii")
        token = (encoded + b"." + signature).decode("ascii")
        if len(token) > MAX_TOKEN_LENGTH:
            raise ValueError("Cart state is too large")
        return token

    def decode(self, token: str, session_id: str | None = None) -> dict:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidCartToken("Invalid cart token")
        try:
            encoded, signature = token.encode("ascii").split(b".", 1)
            expected = hmac.new(self.key, encoded, hashlib.sha256).hexdigest().encode("ascii")
            if not hmac.compare_digest(signature, expected):
                raise InvalidCartToken("Invalid cart token signature")
            data = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
            payload = json.loads(data)
            issued = datetime.fromtimestamp(payload["iat"], timezone.utc)
        except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, binascii.Error) as exc:
            raise InvalidCartToken("Invalid cart token") from exc
        now = datetime.now(timezone.utc)
        if (payload.get("v") != 2 or not isinstance(payload.get("sid"), str) or not payload["sid"]
                or (session_id is not None and payload["sid"] != session_id)
                or issued > now + timedelta(minutes=5) or now - issued > TOKEN_AGE
                or not isinstance(payload.get("cart"), dict) or not isinstance(payload.get("processed"), list)):
            raise InvalidCartToken("Invalid or expired cart token")
        return payload

    def restore(self, tools: ShopTools, token: str, session_id: str | None = None) -> str:
        payload = self.decode(token, session_id)
        session_id = payload["sid"]
        cart: dict[str, int] = {}
        for article, qty in payload["cart"].items():
            if not isinstance(article, str) or type(qty) is not int or qty < 1:
                raise InvalidCartToken("Invalid cart item")
            product = tools.catalog.get(article)
            if not product or product["stock"] is None or qty > product["stock"]:
                raise InvalidCartToken("Cart item is no longer available")
            cart[product["article"]] = qty
        processed: OrderedDict[tuple[str, str, int, str], None] = OrderedDict()
        if len(payload["processed"]) > 50:
            raise InvalidCartToken("Cart history is too large")
        for item in payload["processed"]:
            if (not isinstance(item, list) or len(item) != 3 or not isinstance(item[0], str)
                    or type(item[1]) is not int or not 1 <= item[1] <= 10_000
                    or not isinstance(item[2], str) or len(item[2]) != 64):
                raise InvalidCartToken("Invalid cart history")
            processed[(session_id, item[0], item[1], item[2])] = None
        with tools.lock:
            tools.carts[session_id] = cart
            tools.processed_additions = processed
        return session_id
