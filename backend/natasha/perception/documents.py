"""Document reading: PDF, DOCX, XLSX, PPTX, images (OCR), plain text and code."""

from __future__ import annotations

import csv
import io
import json
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import NotFoundError
from ..security.injection import ContentTrust, ExternalContent, get_injection_guard

MAX_BYTES = 40 * 1024 * 1024

#: Extensions Natasha can extract text from with the standard library + installed optional deps.
TEXT_SUFFIXES = {".txt", ".md", ".rst", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                 ".csv", ".tsv", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c",
                 ".h", ".cpp", ".rb", ".php", ".sh", ".sql", ".html", ".htm", ".xml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}


@dataclass
class DocumentExtract:
    path: str
    ok: bool
    text: str = ""
    kind: str = "text"
    pages: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    truncated: bool = False
    suspicious: bool = False

    def content(self) -> ExternalContent:
        return ExternalContent(text=self.text, source=f"document:{self.path}",
                               trust=ContentTrust.EXTERNAL, metadata={"kind": self.kind})

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "ok": self.ok, "kind": self.kind, "pages": self.pages,
                "bytes": len(self.text.encode()), "truncated": self.truncated,
                "suspicious": self.suspicious, "error": self.error,
                "preview": self.text[:400]}


class DocumentReader:
    """Extracts text; never executes anything it finds inside a document."""

    def __init__(self, *, max_chars: int = 400_000) -> None:
        self.max_chars = max_chars

    def read(self, path: str | Path) -> DocumentExtract:
        target = Path(path).expanduser()
        if not target.is_file():
            raise NotFoundError(f"document not found: {target}")
        size = target.stat().st_size
        if size > MAX_BYTES:
            return DocumentExtract(str(target), False, error=f"file too large ({size} bytes)", kind="unknown")
        suffix = target.suffix.lower()
        try:
            if suffix == ".pdf":
                extract = self._read_pdf(target)
            elif suffix == ".docx":
                extract = self._read_docx(target)
            elif suffix == ".xlsx":
                extract = self._read_xlsx(target)
            elif suffix == ".pptx":
                extract = self._read_pptx(target)
            elif suffix in IMAGE_SUFFIXES:
                extract = self._read_image(target)
            elif suffix == ".csv":
                extract = self._read_csv(target)
            elif suffix == ".tsv":
                extract = self._read_csv(target, delimiter="\t")
            elif suffix in TEXT_SUFFIXES or not suffix:
                extract = self._read_text(target)
            else:
                extract = self._read_text(target)  # best effort
        except Exception as exc:  # extraction failures are reported, never hidden
            return DocumentExtract(str(target), False, kind=suffix.lstrip("."),
                                   error=f"{type(exc).__name__}: {exc}")
        self._finish(extract, target)
        return extract

    # ------------------------------------------------------------------ formats
    def _read_text(self, target: Path) -> DocumentExtract:
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = target.read_text(encoding="utf-8", errors="replace")
        return DocumentExtract(str(target), True, text=text, kind="text")

    def _read_csv(self, target: Path, delimiter: str = ",") -> DocumentExtract:
        rows: list[str] = []
        with target.open(newline="", encoding="utf-8", errors="replace") as handle:
            for index, row in enumerate(csv.reader(handle, delimiter=delimiter)):
                if index > 5_000:
                    break
                rows.append(" | ".join(cell.strip() for cell in row))
        return DocumentExtract(str(target), True, text="\n".join(rows), kind="csv",
                              metadata={"rows": len(rows)})

    def _read_pdf(self, target: Path) -> DocumentExtract:
        from pypdf import PdfReader

        reader = PdfReader(str(target))
        pages = []
        for page in reader.pages[:200]:
            pages.append(page.extract_text() or "")
        meta = dict(getattr(reader, "metadata", {}) or {})
        return DocumentExtract(str(target), True, text="\n\n".join(pages), kind="pdf",
                               pages=len(reader.pages), metadata={k: str(v)[:200] for k, v in meta.items()})

    def _read_docx(self, target: Path) -> DocumentExtract:
        import docx

        document = docx.Document(str(target))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                paragraphs.append(" | ".join(cell.text for cell in row.cells))
        return DocumentExtract(str(target), True, text="\n".join(paragraphs), kind="docx",
                               metadata={"paragraphs": len(paragraphs)})

    def _read_xlsx(self, target: Path) -> DocumentExtract:
        import openpyxl

        workbook = openpyxl.load_workbook(str(target), read_only=True, data_only=True)
        chunks: list[str] = []
        for sheet in workbook.worksheets:
            chunks.append(f"# sheet: {sheet.title}")
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index > 2_000:
                    chunks.append("... (truncated)")
                    break
                chunks.append(" | ".join("" if cell is None else str(cell) for cell in row))
        return DocumentExtract(str(target), True, text="\n".join(chunks), kind="xlsx",
                               metadata={"sheets": len(workbook.worksheets)})

    def _read_pptx(self, target: Path) -> DocumentExtract:
        from pptx import Presentation

        presentation = Presentation(str(target))
        chunks: list[str] = []
        for index, slide in enumerate(presentation.slides, start=1):
            chunks.append(f"# slide {index}")
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    chunks.append(shape.text.strip())
        return DocumentExtract(str(target), True, text="\n".join(chunks), kind="pptx",
                               metadata={"slides": len(presentation.slides)})

    def _read_image(self, target: Path) -> DocumentExtract:
        """Images go through OCR (if available) and are otherwise handed to the vision engine."""
        text, engine = "", "none"
        try:  # optional local OCR
            import pytesseract  # type: ignore
            from PIL import Image

            text = pytesseract.image_to_string(Image.open(target))
            engine = "tesseract"
        except Exception:
            text = ""
        if not text:
            return DocumentExtract(str(target), True, text="", kind="image", metadata={
                "ocr": engine, "note": "no local OCR available; use describe_image for vision analysis"})
        return DocumentExtract(str(target), True, text=text, kind="image", metadata={"ocr": engine})

    def _read_zip(self, target: Path) -> DocumentExtract:
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()[:200]
        return DocumentExtract(str(target), True, text=json.dumps(names, indent=2), kind="zip",
                               metadata={"entries": len(names)})

    # ------------------------------------------------------------------ helpers
    def _finish(self, extract: DocumentExtract, target: Path) -> None:
        if len(extract.text) > self.max_chars:
            extract.text = extract.text[: self.max_chars]
            extract.truncated = True
        guard = get_injection_guard()
        verdict = guard.inspect(extract.text, source=f"document:{target.name}")
        extract.suspicious = verdict.suspicious
        extract.metadata["injection_risk"] = verdict.risk.name
        if verdict.suspicious:
            extract.metadata["injection_findings"] = verdict.findings


_READER: DocumentReader | None = None
_LOCK = threading.Lock()


def get_document_reader(**kwargs: Any) -> DocumentReader:
    global _READER
    with _LOCK:
        if _READER is None:
            _READER = DocumentReader(**kwargs)
        return _READER


def reset_document_reader() -> None:
    """Drop the cached engine. The runtime builds its own; this exists for tests and reloads."""
    global _READER
    with _LOCK:
        _READER = None
