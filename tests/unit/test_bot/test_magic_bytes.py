"""Tests for ``src.bot.features.magic_bytes`` (M6 from upgrade.md).

Telegram's ``file_name`` / ``mime_type`` are attacker-controllable
because the uploading client sets both and MIME is typically
inferred from the extension. These tests pin the magic-byte
detector that backs the new magic-based file-upload policy.
"""

import pytest

from src.bot.features.magic_bytes import (
    detect_type,
    extension_matches_type,
    is_archive,
    is_executable,
)

# --------------------------------------------------------------------
# detect_type — binary signatures
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "jpeg"),
        (b"GIF87a" + b"\x00" * 50, "gif"),
        (b"GIF89a" + b"\x00" * 50, "gif"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 50, "webp"),
        (b"BM" + b"\x00" * 100, "bmp"),
        (b"%PDF-1.4\n" + b"\x00" * 100, "pdf"),
        (b"PK\x03\x04" + b"\x00" * 50, "zip"),
        (b"PK\x05\x06" + b"\x00" * 50, "zip"),
        (b"\x1f\x8b\x08\x00" + b"\x00" * 50, "gzip"),
        (b"BZh9" + b"\x00" * 50, "bzip2"),
        (b"\xfd7zXZ\x00\x00" + b"\x00" * 50, "xz"),
        (b"7z\xbc\xaf\x27\x1c" + b"\x00" * 50, "7z"),
    ],
)
def test_binary_signatures_detected(data, expected):
    assert detect_type(data) == expected


def test_tar_detected_at_non_zero_offset():
    # Tar signature lives at byte 257 inside a 512-byte header.
    header = b"\x00" * 257 + b"ustar" + b"\x00" * 250
    assert detect_type(header) == "tar"


@pytest.mark.parametrize(
    "data,label",
    [
        (b"MZ\x90\x00" + b"\x00" * 100, "pe_exe"),
        (b"\x7fELF\x02\x01\x01" + b"\x00" * 100, "elf"),
        (b"\xfe\xed\xfa\xce" + b"\x00" * 100, "macho_32"),
        (b"\xfe\xed\xfa\xcf" + b"\x00" * 100, "macho_64"),
        (b"\xca\xfe\xba\xbe" + b"\x00" * 100, "macho_universal"),
    ],
)
def test_every_executable_variant_collapses_to_executable(data, label):
    """All PE / ELF / Mach-O variants must report as ``executable`` so
    policy treats them identically."""
    assert detect_type(data) == "executable"
    assert is_executable(detect_type(data)) is True


def test_riff_without_webp_is_unknown():
    """RIFF is also used by AVI and WAV; without the WEBP subtag we
    must not claim it is an image."""
    assert detect_type(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 50) == "unknown"


# --------------------------------------------------------------------
# detect_type — text fallback
# --------------------------------------------------------------------


class TestTextFallback:
    def test_plain_utf8_is_text(self):
        assert detect_type(b"Hello, world!\nThis is plain text.") == "text"

    def test_utf8_with_multibyte_is_text(self):
        assert detect_type("résumé — café".encode("utf-8")) == "text"

    def test_nul_byte_disqualifies_text(self):
        assert detect_type(b"plain text\x00 with NUL") == "unknown"

    def test_empty_is_unknown(self):
        assert detect_type(b"") == "unknown"

    def test_invalid_utf8_without_known_signature_is_unknown(self):
        # Bytes that don't match any signature AND don't decode as
        # UTF-8 should be classified ``unknown``. ``0xff 0xfe 0xfd
        # 0xfc`` starts a 4-byte UTF-8 codepoint that's out of range.
        assert detect_type(b"\xff\xfe\xfd\xfc") == "unknown"


# --------------------------------------------------------------------
# extension_matches_type
# --------------------------------------------------------------------


class TestExtensionMatching:
    def test_matching_png(self):
        assert extension_matches_type("photo.png", "png") is True

    def test_mismatched_extension_flagged(self):
        """The whole point — a PE binary renamed ``.png`` must not
        slip through."""
        assert extension_matches_type("malware.png", "executable") is False

    def test_zip_renamed_jar_is_accepted(self):
        """JAR files ARE zips on disk — the table permits both."""
        assert extension_matches_type("app.jar", "zip") is True

    def test_unknown_extension_is_permissive(self):
        """We do not want to reject rare legitimate types. An
        extension we have never heard of gets the benefit of the
        doubt (other layers still apply)."""
        assert extension_matches_type("data.xyz", "unknown") is True

    def test_no_extension_is_permissive(self):
        assert extension_matches_type("README", "text") is True

    def test_text_extension_matches_text_fallback(self):
        assert extension_matches_type("src/hello.py", "text") is True

    def test_text_extension_rejects_executable_content(self):
        assert extension_matches_type("hello.py", "executable") is False


# --------------------------------------------------------------------
# Archive / executable predicates
# --------------------------------------------------------------------


class TestPredicates:
    @pytest.mark.parametrize(
        "detected",
        ["zip", "gzip", "bzip2", "xz", "7z", "tar"],
    )
    def test_is_archive_true(self, detected):
        assert is_archive(detected) is True

    @pytest.mark.parametrize(
        "detected",
        ["png", "jpeg", "text", "unknown", "executable"],
    )
    def test_is_archive_false(self, detected):
        assert is_archive(detected) is False
