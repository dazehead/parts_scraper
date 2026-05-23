"""Regression tests for the HMAC-derived final filename.

We don't want the key derivation to silently change shape, because the
URL is persisted in dbo.parts.final_tag and downstream systems will be
broken if it does.
"""
import hashlib
import hmac
import base64

from image_proc.image_processing import Img_Proc


def _expected_key(name: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), name.encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"final/{encoded}.png"


def test_hash_key_matches_hmac_sha256():
    proc = Img_Proc.__new__(Img_Proc)  # bypass __init__ (it needs a db)
    keep_value = "images/AB123_brake_disc_0.png"
    secret = "test-secret-do-not-use-in-prod"

    out = proc.hash_key([keep_value], secret)
    assert out == _expected_key("AB123_brake_disc_0", secret)


def test_hash_key_stable_across_calls():
    proc = Img_Proc.__new__(Img_Proc)
    out_a = proc.hash_key(["images/XYZ_pad_set_3.png"], "secret-a")
    out_b = proc.hash_key(["images/XYZ_pad_set_3.png"], "secret-a")
    assert out_a == out_b


def test_hash_key_differs_when_secret_rotated():
    proc = Img_Proc.__new__(Img_Proc)
    out_old = proc.hash_key(["images/A_b_0.png"], "old-secret")
    out_new = proc.hash_key(["images/A_b_0.png"], "new-secret")
    assert out_old != out_new


def test_hash_key_is_url_safe():
    proc = Img_Proc.__new__(Img_Proc)
    out = proc.hash_key(["images/A_b_0.png"], "secret")
    # urlsafe_b64encode never produces + or /; we also strip =
    body = out[len("final/"):-len(".png")]
    assert "+" not in body and "/" not in body and "=" not in body
