from __future__ import annotations

import hashlib
import mimetypes
import re
import zipfile
from collections.abc import Callable, Iterable
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from openpyxl import load_workbook
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from tenderguard.config import Settings
from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import Severity


class IntakeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IntakeFinding(IntakeModel):
    code: str
    severity: Severity
    archive_path: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SheetInspection(IntakeModel):
    name: str
    state: str
    max_row: int
    max_column: int
    hidden_row_count: int
    hidden_column_count: int
    formula_cell_count: int
    formula_without_cached_value_count: int


class FileInspection(IntakeModel):
    entry_id: str
    archive_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str
    nested_archive: bool = False
    corrupt: bool = False
    protected: bool = False
    unsupported: bool = False
    page_count: int | None = None
    embedded_file_count: int = 0
    sheets: tuple[SheetInspection, ...] = ()
    findings: tuple[IntakeFinding, ...] = ()


class IntakeManifest(IntakeModel):
    root_filename: str
    root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[FileInspection, ...]
    findings: tuple[IntakeFinding, ...]
    all_files_processed: bool


class _Budget:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.files = 0
        self.unpacked_bytes = 0

    def consume(self, size: int) -> None:
        self.files += 1
        self.unpacked_bytes += size
        if self.files > self.settings.max_archive_files:
            raise ValueError("Archive file-count limit exceeded")
        if self.unpacked_bytes > self.settings.max_archive_unpacked_bytes:
            raise ValueError("Archive uncompressed-size limit exceeded")


_ARCHIVE_SUFFIXES = {".zip"}
_UNSUPPORTED_ARCHIVE_SUFFIXES = {".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
_OFFICE_CONTAINER_SUFFIXES = {".docx", ".xlsx", ".xlsm", ".pptx"}
_SUPPORTED_SUFFIXES = {
    ".csv",
    ".docx",
    ".dwg",
    ".dxf",
    ".ifc",
    ".jpeg",
    ".jpg",
    ".json",
    ".pdf",
    ".png",
    ".pptx",
    ".tif",
    ".tiff",
    ".txt",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".xml",
    *_ARCHIVE_SUFFIXES,
    *_UNSUPPORTED_ARCHIVE_SUFFIXES,
}
_IMAGE_SUFFIXES = {".jpeg", ".jpg", ".png", ".tif", ".tiff"}


def _safe_archive_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not re.match(r"^[A-Za-z]:", normalized)
    )


def _media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _finding(
    code: str,
    severity: Severity,
    archive_path: str,
    message: str,
    **details: Any,
) -> IntakeFinding:
    return IntakeFinding(
        code=code,
        severity=severity,
        archive_path=archive_path,
        message=message,
        details=details,
    )


def _inspect_pdf(path: str, content: bytes) -> tuple[int | None, int, bool, list[IntakeFinding]]:
    findings: list[IntakeFinding] = []
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        protected = bool(reader.is_encrypted)
        if protected and reader.decrypt("") == 0:
            return (
                None,
                0,
                True,
                [
                    _finding(
                        "PROTECTED_PDF",
                        Severity.BLOCKER,
                        path,
                        "PDF is encrypted and cannot be inspected",
                    )
                ],
            )
        page_count = len(reader.pages)
        attachments = reader.attachments
        embedded_count = len(attachments)
        if embedded_count:
            findings.append(
                _finding(
                    "PDF_EMBEDDED_FILES",
                    Severity.BLOCKER,
                    path,
                    "PDF contains embedded files that require separate manifest entries",
                    count=embedded_count,
                )
            )
        return page_count, embedded_count, protected, findings
    except Exception as error:
        return (
            None,
            0,
            False,
            [
                _finding(
                    "CORRUPT_PDF",
                    Severity.BLOCKER,
                    path,
                    "PDF parser failed",
                    error_type=type(error).__name__,
                )
            ],
        )


def _inspect_excel(
    path: str, content: bytes
) -> tuple[tuple[SheetInspection, ...], list[IntakeFinding]]:
    findings: list[IntakeFinding] = []
    formulas = load_workbook(BytesIO(content), data_only=False, read_only=False, keep_links=True)
    cached = load_workbook(BytesIO(content), data_only=True, read_only=False, keep_links=True)
    sheets: list[SheetInspection] = []
    try:
        for worksheet in formulas.worksheets:
            cached_sheet = cached[worksheet.title]
            formula_count = 0
            missing_cached = 0
            for row in worksheet.iter_rows():
                for cell in row:
                    if cell.data_type == "f":
                        formula_count += 1
                        if cached_sheet[cell.coordinate].value is None:
                            missing_cached += 1
            hidden_rows = sum(
                1 for dimension in worksheet.row_dimensions.values() if dimension.hidden
            )
            hidden_columns = sum(
                1 for dimension in worksheet.column_dimensions.values() if dimension.hidden
            )
            inspection = SheetInspection(
                name=worksheet.title,
                state=worksheet.sheet_state,
                max_row=worksheet.max_row,
                max_column=worksheet.max_column,
                hidden_row_count=hidden_rows,
                hidden_column_count=hidden_columns,
                formula_cell_count=formula_count,
                formula_without_cached_value_count=missing_cached,
            )
            sheets.append(inspection)
            if worksheet.sheet_state != "visible":
                findings.append(
                    _finding(
                        "HIDDEN_EXCEL_SHEET",
                        Severity.WARNING,
                        path,
                        f"Workbook contains {worksheet.sheet_state} sheet",
                        sheet=worksheet.title,
                    )
                )
            if hidden_rows or hidden_columns:
                findings.append(
                    _finding(
                        "HIDDEN_EXCEL_DIMENSIONS",
                        Severity.WARNING,
                        path,
                        "Workbook contains hidden rows or columns",
                        sheet=worksheet.title,
                        hidden_rows=hidden_rows,
                        hidden_columns=hidden_columns,
                    )
                )
            if missing_cached:
                findings.append(
                    _finding(
                        "EXCEL_FORMULA_CACHE_MISSING",
                        Severity.BLOCKER,
                        path,
                        "Formula cells lack cached calculated values",
                        sheet=worksheet.title,
                        count=missing_cached,
                    )
                )
        external_links = getattr(formulas, "_external_links", [])
        if external_links:
            findings.append(
                _finding(
                    "EXCEL_EXTERNAL_LINKS",
                    Severity.BLOCKER,
                    path,
                    "Workbook contains external links whose values may be stale or unavailable",
                    count=len(external_links),
                )
            )
        return tuple(sheets), findings
    finally:
        formulas.close()
        cached.close()


def _inspect_image(path: str, content: bytes) -> list[IntakeFinding]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        return []
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        return [
            _finding(
                "CORRUPT_OR_UNSAFE_IMAGE",
                Severity.BLOCKER,
                path,
                "Image could not be safely decoded",
                error_type=type(error).__name__,
            )
        ]


def _inspect_office_container(path: str, content: bytes) -> tuple[int, list[IntakeFinding]]:
    findings: list[IntakeFinding] = []
    try:
        with zipfile.ZipFile(BytesIO(content)) as package:
            bad_member = package.testzip()
            if bad_member:
                findings.append(
                    _finding(
                        "CORRUPT_OFFICE_PACKAGE",
                        Severity.BLOCKER,
                        path,
                        "Office package contains a corrupt member",
                        member=bad_member,
                    )
                )
            embedded = tuple(name for name in package.namelist() if "/embeddings/" in name)
            if embedded:
                findings.append(
                    _finding(
                        "OFFICE_EMBEDDED_OBJECTS",
                        Severity.BLOCKER,
                        path,
                        "Office file contains embedded objects requiring separate inspection",
                        count=len(embedded),
                    )
                )
            return len(embedded), findings
    except zipfile.BadZipFile as error:
        return 0, [
            _finding(
                "CORRUPT_OR_PROTECTED_OFFICE_FILE",
                Severity.BLOCKER,
                path,
                "Office package is corrupt, legacy binary, or encrypted",
                error_type=type(error).__name__,
            )
        ]


def _inspect_single(path: str, content: bytes, *, nested_archive: bool = False) -> FileInspection:
    suffix = PurePosixPath(path.casefold()).suffix
    digest = hashlib.sha256(content).hexdigest()
    findings: list[IntakeFinding] = []
    page_count: int | None = None
    embedded_count = 0
    protected = False
    corrupt = False
    unsupported = False
    sheets: tuple[SheetInspection, ...] = ()

    if suffix == ".pdf":
        page_count, embedded_count, protected, pdf_findings = _inspect_pdf(path, content)
        findings.extend(pdf_findings)
        corrupt = any(item.code == "CORRUPT_PDF" for item in findings)
    elif suffix in {".xlsx", ".xlsm"}:
        try:
            sheets, excel_findings = _inspect_excel(path, content)
            findings.extend(excel_findings)
        except Exception as error:
            corrupt = True
            findings.append(
                _finding(
                    "CORRUPT_OR_PROTECTED_EXCEL",
                    Severity.BLOCKER,
                    path,
                    "Excel workbook could not be opened",
                    error_type=type(error).__name__,
                )
            )
    elif suffix in {".docx", ".pptx"}:
        embedded_count, office_findings = _inspect_office_container(path, content)
        findings.extend(office_findings)
        corrupt = any(item.code.startswith("CORRUPT_") for item in office_findings)
    elif suffix in _IMAGE_SUFFIXES:
        image_findings = _inspect_image(path, content)
        findings.extend(image_findings)
        corrupt = bool(image_findings)
    elif suffix == ".xls":
        unsupported = True
        findings.append(
            _finding(
                "LEGACY_EXCEL_ADAPTER_REQUIRED",
                Severity.BLOCKER,
                path,
                "Legacy XLS requires a qualified conversion/parser adapter",
            )
        )
    elif suffix in _UNSUPPORTED_ARCHIVE_SUFFIXES:
        unsupported = True
        findings.append(
            _finding(
                "ARCHIVE_ADAPTER_REQUIRED",
                Severity.BLOCKER,
                path,
                f"Archive format {suffix} requires a qualified adapter",
            )
        )
    elif suffix not in _SUPPORTED_SUFFIXES:
        unsupported = True
        findings.append(
            _finding(
                "UNSUPPORTED_FILE_TYPE",
                Severity.BLOCKER,
                path,
                "File type is not in the document-intake allowlist",
                suffix=suffix,
            )
        )

    return FileInspection(
        entry_id=f"file-{content_hash({'path': path, 'sha256': digest})[:24]}",
        archive_path=path,
        sha256=digest,
        size_bytes=len(content),
        media_type=_media_type(path),
        nested_archive=nested_archive,
        corrupt=corrupt,
        protected=protected,
        unsupported=unsupported,
        page_count=page_count,
        embedded_file_count=embedded_count,
        sheets=sheets,
        findings=tuple(findings),
    )


def _walk_zip(
    *,
    archive_path: str,
    content: bytes,
    depth: int,
    budget: _Budget,
    on_member: Callable[[str, bytes], None] | None = None,
) -> tuple[list[FileInspection], list[IntakeFinding]]:
    entries: list[FileInspection] = []
    findings: list[IntakeFinding] = []
    if depth > budget.settings.max_archive_depth:
        findings.append(
            _finding(
                "ARCHIVE_DEPTH_EXCEEDED",
                Severity.BLOCKER,
                archive_path,
                "Nested archive depth exceeds configured safety limit",
            )
        )
        return entries, findings
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                child_path = f"{archive_path}!/{info.filename}"
                if not _safe_archive_path(info.filename):
                    findings.append(
                        _finding(
                            "ARCHIVE_PATH_TRAVERSAL",
                            Severity.BLOCKER,
                            child_path,
                            "Archive member path is unsafe",
                        )
                    )
                    continue
                budget.consume(info.file_size)
                if info.compress_size == 0 and info.file_size > 0:
                    ratio = float(budget.settings.max_archive_compression_ratio + 1)
                else:
                    ratio = info.file_size / max(info.compress_size, 1)
                if ratio > budget.settings.max_archive_compression_ratio:
                    findings.append(
                        _finding(
                            "ARCHIVE_COMPRESSION_RATIO_EXCEEDED",
                            Severity.BLOCKER,
                            child_path,
                            "Archive member exceeds compression-ratio safety limit",
                            ratio=str(ratio),
                        )
                    )
                    continue
                if info.flag_bits & 0x1:
                    findings.append(
                        _finding(
                            "PROTECTED_ARCHIVE_MEMBER",
                            Severity.BLOCKER,
                            child_path,
                            "Archive member is encrypted",
                        )
                    )
                    entries.append(
                        FileInspection(
                            entry_id=f"file-{content_hash(child_path)[:24]}",
                            archive_path=child_path,
                            sha256="0" * 64,
                            size_bytes=info.file_size,
                            media_type=_media_type(info.filename),
                            protected=True,
                        )
                    )
                    continue
                child = archive.read(info)
                if on_member is not None:
                    on_member(child_path, child)
                suffix = PurePosixPath(info.filename.casefold()).suffix
                is_archive = (
                    suffix in _ARCHIVE_SUFFIXES and suffix not in _OFFICE_CONTAINER_SUFFIXES
                )
                inspected = _inspect_single(child_path, child, nested_archive=is_archive)
                entries.append(inspected)
                findings.extend(inspected.findings)
                if is_archive:
                    nested_entries, nested_findings = _walk_zip(
                        archive_path=child_path,
                        content=child,
                        depth=depth + 1,
                        budget=budget,
                        on_member=on_member,
                    )
                    entries.extend(nested_entries)
                    findings.extend(nested_findings)
    except (zipfile.BadZipFile, RuntimeError) as error:
        findings.append(
            _finding(
                "CORRUPT_ARCHIVE",
                Severity.BLOCKER,
                archive_path,
                "Archive could not be completely enumerated",
                error_type=type(error).__name__,
            )
        )
    except ValueError as error:
        findings.append(
            _finding(
                "ARCHIVE_LIMIT_EXCEEDED",
                Severity.BLOCKER,
                archive_path,
                str(error),
            )
        )
    return entries, findings


def inspect_intake(
    filename: str,
    content: bytes,
    settings: Settings,
    *,
    on_member: Callable[[str, bytes], None] | None = None,
) -> IntakeManifest:
    if len(content) > settings.max_upload_bytes:
        raise ValueError(f"Upload exceeds configured limit of {settings.max_upload_bytes} bytes")
    root_hash = hashlib.sha256(content).hexdigest()
    budget = _Budget(settings)
    budget.consume(len(content))
    suffix = PurePosixPath(filename.casefold()).suffix
    if suffix in _ARCHIVE_SUFFIXES:
        entries, findings = _walk_zip(
            archive_path=filename,
            content=content,
            depth=0,
            budget=budget,
            on_member=on_member,
        )
    else:
        entry = _inspect_single(filename, content)
        entries = [entry]
        findings = list(entry.findings)
    all_processed = (
        bool(entries)
        and not any(item.corrupt or item.protected or item.unsupported for item in entries)
        and not any(item.severity is Severity.BLOCKER for item in findings)
    )
    return IntakeManifest(
        root_filename=filename,
        root_sha256=root_hash,
        entries=tuple(entries),
        findings=tuple(findings),
        all_files_processed=all_processed,
    )


def missing_referenced_documents(
    *,
    extracted_references: Iterable[str],
    available_logical_keys: Iterable[str],
) -> tuple[str, ...]:
    available = {" ".join(item.casefold().split()) for item in available_logical_keys}
    return tuple(
        sorted(
            reference
            for reference in extracted_references
            if " ".join(reference.casefold().split()) not in available
        )
    )
