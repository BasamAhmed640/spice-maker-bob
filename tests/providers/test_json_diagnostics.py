"""A diagnostic excerpt must never expose a sliced credential fragment."""

from __future__ import annotations

import pytest

from boardmodeler.providers.base import ProviderError
from boardmodeler.providers.http_inference import extract_json_object


@pytest.mark.parametrize("side", ["before", "after"])
def test_secret_is_redacted_before_error_window_is_sliced(side):
    secret = "test-credential-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"
    for offset in range(260):
        if side == "before":
            text = '{"value":"' + "x" * 400 + secret + "y" * offset + '", BAD}'
        else:
            text = '{"bad":@,"value":"' + "x" * offset + secret + '"}'
        with pytest.raises(ProviderError) as error:
            extract_json_object(text, secrets=(secret,))
        detail = error.value.detail
        assert "line 1, column" in detail
        for start in range(len(secret) - 7):
            assert secret[start : start + 8] not in detail, (side, offset)
