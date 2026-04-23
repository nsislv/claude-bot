"""Integration tests for M6 in ``validate_file_upload``.

The function gained a ``file_bytes`` kwarg. These tests exercise
the magic-byte branches added in M6 and prove the legacy
metadata-only branches still work for callers that do not pass
bytes.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.middleware.security import validate_file_upload


def _doc(filename="data.png", mime="image/png", size=1024):
    d = MagicMock()
    d.file_name = filename
    d.mime_type = mime
    d.file_size = size
    return d


def _validator(accept_filename=True):
    v = MagicMock()
    v.validate_filename = MagicMock(return_value=(accept_filename, ""))
    return v


def _audit():
    a = AsyncMock()
    a.log_security_violation = AsyncMock()
    a.log_file_access = AsyncMock()
    return a


class TestMetadataOnlyBranchStillWorks:
    """``file_bytes`` defaults to None — legacy callers that just
    vet the Document metadata must keep passing."""

    async def test_legit_png_metadata_accepted(self):
        ok, msg = await validate_file_upload(
            _doc(), _validator(), user_id=1, audit_logger=_audit()
        )
        assert ok is True
        assert msg == ""

    async def test_dangerous_mime_rejected_without_bytes(self):
        ok, msg = await validate_file_upload(
            _doc(filename="bad.exe", mime="application/x-msdownload"),
            _validator(),
            user_id=1,
            audit_logger=_audit(),
        )
        assert ok is False
        assert "not allowed" in msg.lower()

    async def test_oversize_rejected(self):
        ok, msg = await validate_file_upload(
            _doc(size=50 * 1024 * 1024),  # 50MB
            _validator(),
            user_id=1,
            audit_logger=_audit(),
        )
        assert ok is False
        assert "too large" in msg.lower()


class TestMagicByteExecutableBlock:
    """An attacker who mints ``ANTHROPIC_API_KEY=`` renames their
    PE binary ``image.png``. Telegram claims MIME ``image/png``.
    Magic bytes say PE. We must reject."""

    async def test_pe_renamed_png_rejected(self):
        pe_bytes = b"MZ\x90\x00" + b"\x00" * 1024
        audit = _audit()

        ok, msg = await validate_file_upload(
            _doc(filename="image.png", mime="image/png"),
            _validator(),
            user_id=42,
            audit_logger=audit,
            file_bytes=pe_bytes,
        )

        assert ok is False
        assert "executable" in msg.lower()

        # A security-violation audit entry with high severity is
        # written so the operator can see the attempt.
        audit.log_security_violation.assert_awaited()
        call = audit.log_security_violation.call_args
        assert call.kwargs["violation_type"] == "executable_upload"
        assert call.kwargs["severity"] == "high"

    @pytest.mark.parametrize(
        "payload",
        [
            b"\x7fELF\x02\x01\x01" + b"\x00" * 1024,  # ELF
            b"\xfe\xed\xfa\xcf" + b"\x00" * 1024,  # Mach-O 64
        ],
    )
    async def test_other_executable_shapes_also_blocked(self, payload):
        ok, _ = await validate_file_upload(
            _doc(filename="harmless.jpg", mime="image/jpeg"),
            _validator(),
            user_id=1,
            audit_logger=_audit(),
            file_bytes=payload,
        )
        assert ok is False


class TestMagicByteExtensionMismatch:
    """When the extension says one thing and the file bytes say
    another, reject."""

    async def test_zip_labeled_as_png(self):
        zip_bytes = b"PK\x03\x04" + b"\x00" * 500
        audit = _audit()

        ok, msg = await validate_file_upload(
            _doc(filename="photo.png", mime="image/png"),
            _validator(),
            user_id=1,
            audit_logger=audit,
            file_bytes=zip_bytes,
        )

        assert ok is False
        assert "extension" in msg.lower() or "does not match" in msg.lower()
        call = audit.log_security_violation.call_args
        assert call.kwargs["violation_type"] == "mime_extension_mismatch"

    async def test_matching_extension_and_bytes_pass(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024
        ok, msg = await validate_file_upload(
            _doc(filename="photo.png", mime="image/png"),
            _validator(),
            user_id=1,
            audit_logger=_audit(),
            file_bytes=png_bytes,
        )
        assert ok is True
        assert msg == ""

    async def test_text_file_with_txt_extension_passes(self):
        ok, _ = await validate_file_upload(
            _doc(filename="notes.txt", mime="text/plain"),
            _validator(),
            user_id=1,
            audit_logger=_audit(),
            file_bytes=b"Hello notes.\n",
        )
        assert ok is True

    async def test_unknown_extension_is_permissive(self):
        """Uncommon extension + unknown magic bytes — the check
        is permissive so we don't break legitimate weird files."""
        ok, _ = await validate_file_upload(
            _doc(filename="weird.xyz", mime="application/octet-stream"),
            _validator(),
            user_id=1,
            audit_logger=_audit(),
            file_bytes=b"\xde\xad\xbe\xef" * 50,
        )
        assert ok is True
