"""Shared error classification + clean-message helpers (app.utils.errors), used by BOTH the email
pipeline (processing_service) and the upload pipeline (upload_ingest_service).

Run: python -m tests.test_error_messages
"""
from app.utils.errors import clean_error_message, is_permanent_error


def main() -> None:
    # The real Anthropic 400 blob for a password-protected PDF.
    pw = Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'messages.0.content.1.pdf.source.base64.data: The PDF specified is password protected.'}, "
        "'request_id': 'req_x'}"
    )
    assert is_permanent_error(pw) is True
    msg = clean_error_message(pw)
    assert "password protected" in msg.lower(), msg
    assert "Error code" not in msg and "{" not in msg, msg  # provider noise stripped

    # Corrupt / invalid PDF -> permanent, clean.
    bad = Exception("The PDF is invalid and could not be read")
    assert is_permanent_error(bad) is True
    assert "corrupt" in clean_error_message(bad).lower() or "unreadable" in clean_error_message(bad).lower()

    # Genuinely transient -> NOT permanent (still retryable).
    t = Exception("Error code: 529 - {'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}")
    assert is_permanent_error(t) is False
    # Fallback still extracts the human 'message' and drops the envelope.
    assert clean_error_message(t) == "Overloaded", clean_error_message(t)

    # A plain error with no envelope passes through (truncated).
    plain = Exception("Some unexpected failure")
    assert clean_error_message(plain) == "Some unexpected failure"
    assert is_permanent_error(plain) is False

    print("error messages: all assertions passed")


if __name__ == "__main__":
    main()
