from app.adapters.base import NormalizedStatus
from app.adapters.cpay import compute_checksum, format_amount, map_confirm_response, map_status_response


def test_matches_real_payment_example():
    # MOCK-417ED9940B /payment call, otp="" — accepted by C-Pay UAT.
    checksum = compute_checksum(
        ext_transaction_id="MOCK-417ED9940B",
        client_code="LESOLE_LTD8972",
        amount="5.00",
        msisdn="62221503",
        otp="",
        secret="aoi5On",
    )
    assert checksum == "f6093857304ee51e2a2ff0c4b9fdfd0265048dc073a78695e23ab1f93f857d29"


def test_matches_real_confirm_example():
    # Same transaction's /confirm call, otp="4425" — accepted by C-Pay UAT.
    checksum = compute_checksum(
        ext_transaction_id="MOCK-417ED9940B",
        client_code="LESOLE_LTD8972",
        amount="5.00",
        msisdn="62221503",
        otp="4425",
        secret="aoi5On",
    )
    assert checksum == "af2672ea34292dc35d058e6b80421f5f601f60bffcadaeca9629199d9f62f636"


def test_format_amount_matches_what_was_sent():
    assert format_amount(5) == "5.00"
    from decimal import Decimal

    assert format_amount(Decimal("1200")) == "1200.00"


def test_status_mapping_matches_real_resolved_response():
    # Real response from GET /transaction-status?requestReference=MOCK-417ED9940B
    # for a transaction that was actually confirmed via /confirm.
    real_response = {
        "StatusCode": "200",
        "Description": "MOCK-417ED9940B",
        "ExtTransactionId": "MOCK-417ED9940B",
        "CPayTransactionId": "CPN0000900294",
        "PaymentRequestStatus": "processed",
        "additionalData": {
            "Amount": 5,
            "Customer": "Lesole Polilane",
            "Date": "2026-10-02T09:58:14.859+02:00",
        },
        "ReasonCode": "paymentComplete",
    }
    assert map_status_response(real_response) == NormalizedStatus.CONFIRMED


def test_status_mapping_does_not_confuse_confirm_endpoint_vocabulary():
    # "paymentSuccessful" is /confirm's success code, NOT this endpoint's
    # ("paymentComplete" is). Guard against silently reverting to the wrong
    # vocabulary in a future edit.
    wrong_vocab = {"StatusCode": "200", "PaymentRequestStatus": "processed", "ReasonCode": "paymentSuccessful"}
    assert map_status_response(wrong_vocab) != NormalizedStatus.CONFIRMED


def test_confirm_mapping_matches_real_success_example():
    # Real /confirm response for a transaction confirmed with the correct OTP.
    real_response = {
        "StatusCode": "200",
        "ExtTransactionId": "MOCK-417ED9940B",
        "CPayTransactionId": "CPN0000900294",
        "PaymentRequestStatus": None,
        "ReasonCode": "paymentSuccessful",
    }
    assert map_confirm_response(real_response) == NormalizedStatus.CONFIRMED


def test_confirm_mapping_matches_real_wrong_otp_example():
    # Real response from POST /confirm with a deliberately wrong otp — HTTP
    # 422, this is the body inside it. CONFIRMED against real C-Pay UAT,
    # closing the "decline shape is unseen" gap noted in cpay.py.
    real_wrong_otp_response = {
        "StatusCode": "422",
        "Description": "Payment Failed. Operation failed due to invalid password! Please verify OTP is correct",
        "ExtTransactionId": None,
        "CPayTransactionId": None,
        "PaymentRequestStatus": None,
        "additionalData": None,
        "ReasonCode": "invalidPassword",
    }
    assert map_confirm_response(real_wrong_otp_response) == NormalizedStatus.DECLINED


def test_status_mapping_pending_when_not_yet_processed():
    # INFERRED, not a real response — PaymentRequestStatus null before
    # resolution is consistent with /payment and /confirm both showing null
    # PaymentRequestStatus prior to completion.
    unresolved = {"StatusCode": "200", "PaymentRequestStatus": None, "ReasonCode": "otpSent"}
    assert map_status_response(unresolved) == NormalizedStatus.PENDING
