from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, model_validator

from tenderguard.domain.enums import Severity
from tenderguard.domain.models import DomainModel

ALLOWED_INTAKE_SUFFIXES = frozenset(
    {
        ".7z",
        ".bz2",
        ".csv",
        ".docx",
        ".dwg",
        ".dxf",
        ".gz",
        ".ifc",
        ".jpeg",
        ".jpg",
        ".json",
        ".pdf",
        ".png",
        ".pptx",
        ".rar",
        ".tar",
        ".tif",
        ".tiff",
        ".txt",
        ".xls",
        ".xlsm",
        ".xlsx",
        ".xml",
        ".xz",
        ".zip",
    }
)


def normalize_upload_filename(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    filename = PurePosixPath(normalized).name
    if (
        not filename
        or filename in {".", ".."}
        or "\x00" in filename
        or len(filename) > 500
        or re.search(r"[\r\n]", filename)
    ):
        raise ValueError("Upload filename is invalid")
    suffix = PurePosixPath(filename.casefold()).suffix
    if suffix not in ALLOWED_INTAKE_SUFFIXES:
        raise ValueError("File type is outside the document-intake allowlist")
    return filename


class IntakeFinding(DomainModel):
    code: str
    severity: Severity
    archive_path: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SheetInspection(DomainModel):
    name: str
    state: str
    max_row: int
    max_column: int
    hidden_row_count: int
    hidden_column_count: int
    formula_cell_count: int
    formula_without_cached_value_count: int
    formula_error_cell_count: int = 0
    non_formula_error_cell_count: int = 0


class FileInspection(DomainModel):
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
    external_hyperlink_count: int = 0
    external_dependency_count: int = 0
    sheets: tuple[SheetInspection, ...] = ()
    findings: tuple[IntakeFinding, ...] = ()


class IntakeManifest(DomainModel):
    root_filename: str
    root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[FileInspection, ...]
    findings: tuple[IntakeFinding, ...]
    all_files_processed: bool

    @model_validator(mode="after")
    def validate_processing_outcome(self) -> IntakeManifest:
        has_blocker = any(finding.severity is Severity.BLOCKER for finding in self.findings) or any(
            finding.severity is Severity.BLOCKER
            for entry in self.entries
            for finding in entry.findings
        )
        expected = (
            bool(self.entries)
            and not has_blocker
            and not any(
                entry.corrupt or entry.protected or entry.unsupported for entry in self.entries
            )
        )
        if self.all_files_processed is not expected:
            raise ValueError("all_files_processed contradicts intake findings or file health")
        return self
