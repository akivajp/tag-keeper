"""アクセス制御（BASIC 認証・同一オリジンの検証・Host ヘッダの検証）。

考え方は btrfs-timeline と同じ。

- BASIC 認証の資格情報はブラウザが自動で送るため、認証だけではクロスサイトからの書き込みを防げない。
  書き込み（POST）では Origin ヘッダが自ホストと一致することも確かめる。
- 認証なしで（ループバックだけで）待ち受ける場合は、DNS リバインディング
  （悪意のあるサイトの名前を 127.0.0.1 に向け直して、ブラウザ経由で API を読む攻撃）を防ぐため、
  Host ヘッダが待ち受けているアドレスか localhost であることを確かめる。
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
from urllib.parse import urlsplit


class Credentials:
    """BASIC 認証の資格情報。"""

    def __init__(self, user: str, password: str) -> None:
        self.user = user
        self.password = password

    @classmethod
    def parse(cls, spec: str) -> Credentials:
        """`user:password` 形式の文字列を解析する（パスワードに : を含めてよい）。

        Raises:
            ValueError: 形式が不正、またはどちらかが空。
        """
        spec = spec.strip()
        if ":" not in spec:
            raise ValueError("資格情報は user:password の形で書いてください")
        user, password = spec.split(":", 1)
        if not user or not password:
            raise ValueError("資格情報のユーザー名とパスワードは空にできません")
        return cls(user, password)

    def verify(self, user: str | None, password: str | None) -> bool:
        """資格情報が一致するかを、比較時間から推測されないように確かめる。"""
        if user is None or password is None:
            return False
        user_ok = hmac.compare_digest(user.encode("utf-8"), self.user.encode("utf-8"))
        password_ok = hmac.compare_digest(password.encode("utf-8"), self.password.encode("utf-8"))
        return user_ok and password_ok


def parse_basic_header(value: str | None) -> tuple[str | None, str | None]:
    """`Authorization: Basic ...` を (user, password) に分解する。解析できなければ (None, None)。"""
    if not value or not value.lower().startswith("basic "):
        return None, None
    try:
        decoded = base64.b64decode(value.split(" ", 1)[1]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, None
    if ":" not in decoded:
        return None, None
    user, password = decoded.split(":", 1)
    return user, password


def is_same_origin(origin: str | None, host: str | None) -> bool:
    """Origin ヘッダが自ホストと一致するかを返す。

    Origin が無ければ、ブラウザ以外（curl など）からのリクエストとみなして許す。
    ブラウザはクロスオリジンの書き込みには必ず Origin を付けるため。
    """
    if not origin:
        return True
    if not host:
        return False
    return urlsplit(origin).netloc == host


def is_loopback(host: str) -> bool:
    """待ち受けるアドレスがループバックかを返す。"""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_allowed(host_header: str | None, bind_host: str) -> bool:
    """Host ヘッダの名前が、待ち受けているアドレスか localhost かを返す。

    すべてのアドレスで待ち受ける（0.0.0.0 / ::）場合は、名前では絞れないので許す（認証が必須になる）。
    """
    if bind_host in ("0.0.0.0", "::", ""):
        return True
    if not host_header:
        return False
    name = urlsplit("//" + host_header).hostname or ""
    return name in ("localhost", "127.0.0.1", "::1", bind_host.strip("[]"))
