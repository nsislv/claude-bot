"""Magic-byte file-type detection (M6 from upgrade.md).

File uploads arriving from Telegram carry a ``file_name`` and
``mime_type`` — both attacker-controllable because the extension is
set by the uploading client and the MIME is often inferred from the
extension. A zip bomb renamed ``notes.txt`` will have
``mime_type="text/plain"``; an ``.exe`` renamed ``image.png`` will
have ``mime_type="image/png"``.

This module inspects the **actual file bytes** against a small set
of magic-byte signatures and returns a normalised type name. It
deliberately uses only the stdlib (no ``python-magic`` /
``libmagic`` dep) — the signatures listed here cover every format
the bot currently cares about, and the failure mode for unknown
types is a conservative "let the filename/MIME path decide" rather
than a crash.

The detector is separate from policy. Callers (``validate_file_upload``
in the security middleware) combine detected-type + claimed-extension
+ claimed-MIME to decide whether to accept or reject.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# Human-readable name -> (offset, signature_bytes).
# ``offset`` is where the signature starts in the file; most formats
# sit at 0, a few (TAR) need a non-zero offset.
#
# The type names are the canonical ones we return from ``detect_type``
# — they intentionally do NOT match MIME types verbatim so a caller
# cannot accidentally use this as a MIME lookup and bypass the
# policy layer.
_SIGNATURES: Dict[str, Tuple[int, bytes]] = {
    # Images
    "png": (0, b"\x89PNG\r\n\x1a\n"),
    "jpeg": (0, b"\xff\xd8\xff"),
    "gif87a": (0, b"GIF87a"),
    "gif89a": (0, b"GIF89a"),
    "webp_riff": (0, b"RIFF"),  # narrowed further inside detect_type
    "bmp": (0, b"BM"),
    # Documents
    "pdf": (0, b"%PDF-"),
    # Archives
    "zip": (0, b"PK\x03\x04"),
    "zip_empty": (0, b"PK\x05\x06"),
    "zip_spanned": (0, b"PK\x07\x08"),
    "gzip": (0, b"\x1f\x8b"),
    "bzip2": (0, b"BZh"),
    "xz": (0, b"\xfd7zXZ\x00"),
    "7z": (0, b"7z\xbc\xaf\x27\x1c"),
    "tar_ustar": (257, b"ustar"),
    # Executables
    "pe_exe": (0, b"MZ"),
    "elf": (0, b"\x7fELF"),
    "macho_32": (0, b"\xfe\xed\xfa\xce"),
    "macho_64": (0, b"\xfe\xed\xfa\xcf"),
    "macho_universal": (0, b"\xca\xfe\xba\xbe"),
}


def detect_type(data: bytes) -> str:
    """Return a canonical type name for ``data`` based on magic bytes.

    Returns the narrowest sensible identifier from the set below, or
    ``"unknown"`` if nothing matches:

    - ``"png" | "jpeg" | "gif" | "webp" | "bmp"`` — image formats.
    - ``"pdf"`` — Portable Document Format.
    - ``"zip" | "gzip" | "bzip2" | "xz" | "7z" | "tar"`` — archives
      / compressed streams.
    - ``"executable"`` — any PE / ELF / Mach-O binary. We collapse
      the sub-variants because policy treats them identically
      (reject).
    - ``"text"`` — a best-effort guess for "looks like UTF-8 text"
      used as a fallback when no binary signature matches.

    Unknown / truncated inputs return ``"unknown"``; callers should
    fall back to filename + MIME for those.
    """
    if not data:
        return "unknown"

    # Check every binary signature at its stated offset.
    for name, (offset, sig) in _SIGNATURES.items():
        if len(data) >= offset + len(sig) and data[offset : offset + len(sig)] == sig:
            # Collapse sub-variants to the canonical group name.
            if name.startswith("gif"):
                return "gif"
            if name.startswith("zip"):
                return "zip"
            if name.startswith("pe_") or name == "elf" or name.startswith("macho"):
                return "executable"
            if name == "tar_ustar":
                return "tar"
            if name == "webp_riff":
                # RIFF is shared with AVI / WAV — narrow to WebP by
                # checking the four-byte format id at offset 8.
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    return "webp"
                return "unknown"
            return name

    # Best-effort text detection: first 1KB must decode as UTF-8 and
    # contain no NUL bytes. Not cryptographic — just enough to stop
    # obviously-binary payloads from being labelled "text".
    sample = data[:1024]
    if b"\x00" in sample:
        return "unknown"
    try:
        sample.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "unknown"


# Mapping of claimed extensions (lowercase, without leading dot) to
# the magic-byte types we'd EXPECT to see. Used by
# ``extension_matches_type`` to flag mismatches.
_EXTENSION_EXPECTED_TYPES: Dict[str, Tuple[str, ...]] = {
    "png": ("png",),
    "jpg": ("jpeg",),
    "jpeg": ("jpeg",),
    "gif": ("gif",),
    "webp": ("webp",),
    "bmp": ("bmp",),
    "pdf": ("pdf",),
    "zip": ("zip",),
    "jar": ("zip",),  # JARs are ZIP archives
    "gz": ("gzip",),
    "tgz": ("gzip", "tar"),
    "bz2": ("bzip2",),
    "xz": ("xz",),
    "7z": ("7z",),
    "tar": ("tar",),
    # Plain-text code/config files.
    "txt": ("text",),
    "md": ("text",),
    "py": ("text",),
    "js": ("text",),
    "ts": ("text",),
    "tsx": ("text",),
    "jsx": ("text",),
    "json": ("text",),
    "yml": ("text",),
    "yaml": ("text",),
    "toml": ("text",),
    "csv": ("text",),
    "xml": ("text",),
    "html": ("text",),
    "css": ("text",),
    "scss": ("text",),
    "sh": ("text",),
    "bash": ("text",),
    "sql": ("text",),
    "c": ("text",),
    "h": ("text",),
    "cpp": ("text",),
    "hpp": ("text",),
    "java": ("text",),
    "go": ("text",),
    "rs": ("text",),
    "rb": ("text",),
    "php": ("text",),
    "swift": ("text",),
    "kt": ("text",),
    "cs": ("text",),
}


def extension_matches_type(filename: Optional[str], detected: str) -> bool:
    """Return True if the claimed extension matches the detected type.

    Conservative contract:

    - If the filename has no extension we cannot compare and return
      ``True`` (the upstream MIME / magic checks still apply).
    - If the extension is unknown to our table, we also return
      ``True`` — we do not want to reject legitimate uncommon types
      the bot never learnt about.
    - If the extension IS known and the detected type is different,
      return ``False``. This is the "extension mismatch" path.
    """
    if not filename or "." not in filename:
        return True

    ext = filename.rsplit(".", 1)[-1].lower()
    expected = _EXTENSION_EXPECTED_TYPES.get(ext)
    if expected is None:
        return True
    return detected in expected


def is_executable(detected: str) -> bool:
    """Convenience predicate for policy layers."""
    return detected == "executable"


def is_archive(detected: str) -> bool:
    """Archive-style detection — zip/gzip/bzip2/xz/7z/tar all count.

    Used by the upload policy to decide whether decompression-
    bomb limits apply.
    """
    return detected in {"zip", "gzip", "bzip2", "xz", "7z", "tar"}


__all__ = (
    "detect_type",
    "extension_matches_type",
    "is_executable",
    "is_archive",
)
