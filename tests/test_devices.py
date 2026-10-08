import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException

from app.dependencies import check_device_scope
from app.models import Device, DeviceStatus
from app.services import device_service as ds


def test_enrollment_code_shape_and_hash_roundtrip():
    code = ds.new_enrollment_code()
    assert len(code) == 9 and code[4] == "-"
    assert not set(code.replace("-", "")) & set("01OI")
    # The app may send it typed lower-case, with or without the dash.
    assert ds.hash_secret(ds.normalize_code(code.lower())) == ds.hash_secret(ds.normalize_code(code.replace("-", "")))


def test_issue_code_resets_device_and_kills_old_credential():
    d = Device(status=DeviceStatus.ACTIVE, token_hash="oldhash", revoked_reason="lost")
    code = ds.issue_enrollment_code(d)
    assert d.status == DeviceStatus.PENDING
    assert d.token_hash is None and d.revoked_reason is None
    assert d.enrollment_code_hash == ds.hash_secret(ds.normalize_code(code))
    assert not ds.is_code_expired(d)


def test_code_expires():
    d = Device()
    ds.issue_enrollment_code(d)
    d.enrollment_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert ds.is_code_expired(d)
    assert ds.is_code_expired(Device())  # no code at all


def test_token_is_long_and_unique():
    a, b = ds.new_device_token(), ds.new_device_token()
    assert a != b and a.startswith("ppd_") and len(a) > 40


def test_enroll_rate_limit_blocks_after_ten_failures_then_recovers():
    key = "10.0.0.9"
    ds.clear_enroll_failures(key)
    for i in range(10):
        assert not ds.enroll_blocked(key, now=1000.0 + i)
        ds.record_enroll_failure(key, now=1000.0 + i)
    assert ds.enroll_blocked(key, now=1020.0)
    assert not ds.enroll_blocked(key, now=1000.0 + 16 * 60)  # window passed


M, S = uuid.uuid4(), uuid.uuid4()


def dev(**kw):
    return NS(merchant_id=kw.get("merchant_id", M), shop_id=kw.get("shop_id", S))


def code_of(exc: HTTPException) -> str:
    return exc.detail["code"]


def test_scope_rejects_missing_device():
    with pytest.raises(HTTPException) as e:
        check_device_scope(None, merchant_id=M, shop_id=S)
    assert e.value.status_code == 403 and code_of(e.value) == "device_not_registered"


def test_scope_rejects_other_merchant_and_other_shop():
    with pytest.raises(HTTPException) as e:
        check_device_scope(dev(merchant_id=uuid.uuid4()), merchant_id=M, shop_id=None)
    assert code_of(e.value) == "device_wrong_merchant"
    with pytest.raises(HTTPException) as e:
        check_device_scope(dev(shop_id=uuid.uuid4()), merchant_id=M, shop_id=S)
    assert code_of(e.value) == "device_wrong_shop"


def test_scope_allows_owner_with_no_shop_on_a_shop_device():
    d = dev()
    assert check_device_scope(d, merchant_id=M, shop_id=None) is d


def test_routes_are_wired():
    from app.main import app

    paths = {(m, r.path) for r in app.routes if hasattr(r, "methods") for m in r.methods}
    assert ("POST", "/devices/enroll") in paths
    assert ("GET", "/devices/me") in paths
    assert ("POST", "/merchants/{merchant_id}/devices") in paths
    assert ("POST", "/merchants/{merchant_id}/devices/{device_id}/revoke") in paths
