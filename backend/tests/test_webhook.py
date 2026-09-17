"""Webhook signature + idempotency tests."""

from app.github.webhook import _wants_fix, verify_signature


def test_verify_signature_roundtrip():
    import hashlib
    import hmac

    secret = "s3cret"
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, secret) is True
    assert verify_signature(body, "sha256=dead", secret) is False


def test_wants_fix_on_label():
    payload = {
        "action": "labeled",
        "label": {"name": "fixhub-fix"},
        "issue": {"number": 1},
        "repository": {"full_name": "a/b"},
    }
    ok, _ = _wants_fix("issues", payload)
    assert ok is True


def test_ignores_unlabeled_open():
    payload = {
        "action": "opened",
        "issue": {"labels": [], "number": 1},
        "repository": {"full_name": "a/b"},
    }
    ok, _ = _wants_fix("issues", payload)
    assert ok is False


def test_fix_comment_trigger():
    payload = {
        "comment": {"body": "/fix please"},
        "issue": {"number": 2},
        "repository": {"full_name": "a/b"},
    }
    ok, reason = _wants_fix("issue_comment", payload)
    assert ok is True and reason == "fix-comment"
