import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from weather_trader.kalshi.auth import KalshiSigner

TS = 1_700_000_000_000


def _pem(key, fmt):
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())


def test_rsa_pss_signature_verifies_and_ignores_query_string():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner("key-1", _pem(key, serialization.PrivateFormat.TraditionalOpenSSL))
    h = signer.headers("get", "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders?limit=5", TS)
    assert h["KALSHI-ACCESS-KEY"] == "key-1"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == str(TS)
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        f"{TS}GET/trade-api/v2/portfolio/orders".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signs_the_text_directly():
    key = ed25519.Ed25519PrivateKey.generate()
    signer = KalshiSigner("key-2", _pem(key, serialization.PrivateFormat.PKCS8))
    h = signer.headers("POST", "/trade-api/v2/portfolio/events/orders", TS)
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            f"{TS}POST/trade-api/v2/portfolio/events/orders".encode())
