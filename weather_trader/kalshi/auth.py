"""Kalshi request signing.

Every authenticated request carries three headers.  The signature is the base64 of a
signature over ``timestamp_ms + METHOD + path`` where ``path`` is the full URL path
(``/trade-api/v2/...``) *without* the query string.  RSA keys use RSA-PSS/SHA-256 with
salt length = digest length; Ed25519 keys sign the text directly.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes | str, password: bytes | None = None):
        if isinstance(private_key_pem, str):
            private_key_pem = private_key_pem.encode()
        key = serialization.load_pem_private_key(private_key_pem, password=password)
        if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
            raise ValueError("Kalshi keys must be RSA or Ed25519")
        self.key_id = key_id
        self._key = key

    @classmethod
    def from_file(cls, key_id: str, path: str | Path) -> "KalshiSigner":
        return cls(key_id, Path(path).expanduser().read_bytes())

    def sign(self, text: str) -> str:
        msg = text.encode()
        if isinstance(self._key, Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?")[0]
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts + method.upper() + path),
        }
