"""Isolated text extraction for an uploaded CV (run as a subprocess, never imported by the API).

    python -I -c "<bootstrap>" application/pdf < document.bin > {"text": ..., "warnings": [...]}

The parent (`app.services.cv_text`) runs this in a separate interpreter with a wall-clock
timeout, so a hostile or malformed document can stall or crash only this process. Limits:
at most `MAX_PAGES` PDF pages, `MAX_TEXT_CHARS` characters of output, and for DOCX a single
`word/document.xml` of at most `MAX_DOCX_XML_BYTES` uncompressed (a zip bomb is refused
before it is inflated). Where the platform supports it, the address space is capped too.
Nothing is written to disk, and nothing is fetched.
"""

import contextlib
import json
import sys
import zipfile
from io import BytesIO
from xml.etree import ElementTree

MAX_PAGES = 20
MAX_TEXT_CHARS = 200_000
MAX_DOCX_XML_BYTES = 10_000_000
MAX_DOCX_ENTRIES = 2_000
MEMORY_LIMIT_BYTES = 1_024 * 1_024 * 1_024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _limit_memory() -> None:
    try:
        import resource  # POSIX only.
    except ImportError:
        return
    setrlimit = getattr(resource, "setrlimit", None)
    limit = getattr(resource, "RLIMIT_AS", None)
    if setrlimit is not None and limit is not None:
        with contextlib.suppress(ValueError, OSError):
            setrlimit(limit, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))


def _pdf_text(content: bytes, warnings: list[str]) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content), strict=False)
    if reader.is_encrypted:
        warnings.append("pdf_encrypted")
        return ""
    pages = reader.pages
    if len(pages) > MAX_PAGES:
        warnings.append("pdf_pages_truncated")
    parts: list[str] = []
    total = 0
    for index in range(min(len(pages), MAX_PAGES)):
        text = pages[index].extract_text() or ""
        parts.append(text)
        total += len(text)
        if total > MAX_TEXT_CHARS:
            break
    return "\n".join(parts)


def _docx_text(content: bytes, warnings: list[str]) -> str:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_DOCX_ENTRIES:
            raise ValueError("docx_too_many_entries")
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise ValueError("docx_missing_document") from exc
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise ValueError("docx_document_too_large")
        with archive.open(info) as handle:
            data = handle.read(MAX_DOCX_XML_BYTES + 1)
        if len(data) > MAX_DOCX_XML_BYTES:
            raise ValueError("docx_document_too_large")
    # The bundled expat refuses entity expansion attacks; DTDs are not fetched.
    root = ElementTree.fromstring(data)
    lines: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        pieces = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t" and node.text:
                pieces.append(node.text)
            elif node.tag in {f"{_W}tab"}:
                pieces.append("\t")
            elif node.tag in {f"{_W}br", f"{_W}cr"}:
                pieces.append("\n")
        lines.append("".join(pieces))
    return "\n".join(lines)


def extract(content: bytes, content_type: str) -> dict[str, object]:
    warnings: list[str] = []
    if content_type == "application/pdf":
        text = _pdf_text(content, warnings)
    elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        text = _docx_text(content, warnings)
    else:
        raise ValueError("unsupported_type")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        warnings.append("text_truncated")
    return {"text": text, "warnings": warnings}


def main() -> int:
    _limit_memory()
    content_type = sys.argv[1] if len(sys.argv) > 1 else ""
    content = sys.stdin.buffer.read()
    try:
        result = extract(content, content_type)
    except Exception as exc:  # Report the failure class only, never document content.
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
            ("docx_", "unsupported_")
        ) else type(exc).__name__
        sys.stdout.write(json.dumps({"error": code[:64]}))
        return 1
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
