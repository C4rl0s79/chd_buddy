"""Core data models for CHD Buddy.

Wszystkie enumy i dataclassy w jednym miejscu, żeby uniknąć cyklicznych
importów między modułami backendu (chdman/detector/audit/fixer).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --- Stałe fizyczne nośników -------------------------------------------------

# Rozmiar sektora użytkownika ISO9660 (Mode1 data) i DVD.
DVD_SECTOR = 2048
# Ramka CD z podkodem (2352 danych + 96 subchannel) używana w metadanych CHD.
CD_FRAME_WITH_SUB = 2448
# Ramka CD bez podkodu.
CD_FRAME = 2352

# Powyżej tej wielkości logicznej obraz CD-typed jest podejrzany jako DVD.
# Redump potrafi mieć CD do ~870 MB (overburn), dlatego próg z zapasem.
CD_MAX_LOGICAL_BYTES = 950 * 1024 * 1024

# Kodeki charakterystyczne dla obrazów CD (createcd).
CD_CODECS = frozenset({"cdlz", "cdzl", "cdfl", "cdzs", "cdfl"})
# Tagi metadanych CHD świadczące o strukturze ścieżek CD.
CD_METADATA_TAGS = frozenset({"CHTR", "CHT2", "CHCD", "CHGT", "CHGD"})


class MediaType(enum.Enum):
    CD = "cd"
    DVD = "dvd"
    HD = "hd"
    RAW = "raw"
    LD = "ld"
    UNKNOWN = "unknown"

    @property
    def create_cmd(self) -> str:
        return {
            MediaType.CD: "createcd",
            MediaType.DVD: "createdvd",
            MediaType.HD: "createhd",
            MediaType.RAW: "createraw",
            MediaType.LD: "createld",
        }.get(self, "")

    @property
    def extract_cmd(self) -> str:
        return {
            MediaType.CD: "extractcd",
            MediaType.DVD: "extractdvd",
            MediaType.HD: "extracthd",
            MediaType.RAW: "extractraw",
            MediaType.LD: "extractld",
        }.get(self, "")


class SourceType(enum.Enum):
    CUE = "cue"
    GDI = "gdi"
    TOC = "toc"
    NRG = "nrg"
    CDR = "cdr"
    ISO = "iso"
    IMG = "img"
    RAW = "raw"
    CHD = "chd"
    ARCHIVE = "archive"
    UNKNOWN = "unknown"


class Operation(enum.Enum):
    CREATE = "create"        # utwórz nowy CHD ze źródła
    VERIFY = "verify"        # sprawdź integralność kontenera
    EXTRACT = "extract"      # wypakuj CHD do obrazu źródłowego
    RECOMPRESS = "recompress"  # chdman copy: ten sam typ, inna kompresja
    RETYPE = "retype"        # extract + recreate właściwym poleceniem
    AUDIT = "audit"          # analiza bez modyfikacji
    DEEPCHECK = "deepcheck"  # próbuj metod ekstrakcji aż do trafienia w DAT


class JobStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_DISK = "blocked_disk"
    SKIPPED = "skipped"
    QUARANTINED = "quarantined"


class AuditVerdict(enum.Enum):
    OK = "ok"
    SUSPECT_WRONG_TYPE = "suspect_wrong_type"  # np. DVD spakowane jako CD
    VERIFY_FAILED = "verify_failed"
    UNREADABLE = "unreadable"
    NOT_CHD = "not_chd"
    UNKNOWN = "unknown"


# --- Dataclassy --------------------------------------------------------------

@dataclass
class CHDInfo:
    """Metadane odczytane z ``chdman info`` (lub chd-rs-py)."""
    path: Path
    version: int = 0
    logical_bytes: int = 0
    hunk_bytes: int = 0
    unit_bytes: int = 0
    total_hunks: int = 0
    compression: list[str] = field(default_factory=list)
    chd_bytes: int = 0            # rozmiar skompresowany raportowany przez chdman
    file_bytes: int = 0           # faktyczny os.path.getsize
    sha1: str = ""
    data_sha1: str = ""
    metadata_tags: list[str] = field(default_factory=list)
    raw_info: str = ""            # surowy output info (do debugowania)

    @property
    def is_cd_typed(self) -> bool:
        """Czy CHD ma strukturę CD (utworzony przez createcd)."""
        if any(c.lower() in CD_CODECS for c in self.compression):
            return True
        if self.unit_bytes in (CD_FRAME, CD_FRAME_WITH_SUB):
            return True
        if any(t in CD_METADATA_TAGS for t in self.metadata_tags):
            return True
        return False

    @property
    def detected_media(self) -> MediaType:
        if self.is_cd_typed:
            return MediaType.CD
        if self.unit_bytes == DVD_SECTOR:
            return MediaType.DVD
        if self.unit_bytes in (512, 4096):
            return MediaType.HD
        return MediaType.UNKNOWN

    @property
    def compression_ratio(self) -> float:
        if self.logical_bytes <= 0 or self.chd_bytes <= 0:
            return 0.0
        return self.chd_bytes / self.logical_bytes


@dataclass
class SourceItem:
    """Element wykryty przez skaner, gotowy do konwersji."""
    path: Path
    source_type: SourceType
    media_type: MediaType = MediaType.UNKNOWN
    companions: list[Path] = field(default_factory=list)  # .bin dla .cue itp.
    confidence: float = 0.0
    detect_reason: str = ""


@dataclass
class AuditResult:
    path: Path
    verdict: AuditVerdict
    info: Optional[CHDInfo] = None
    detected_media: MediaType = MediaType.UNKNOWN
    expected_media: MediaType = MediaType.UNKNOWN
    message: str = ""
    verify_ok: Optional[bool] = None

    @property
    def needs_fix(self) -> bool:
        return self.verdict in (
            AuditVerdict.SUSPECT_WRONG_TYPE,
            AuditVerdict.VERIFY_FAILED,
        )


@dataclass
class DiskBudget:
    """Wynik preflightu miejsca dla pojedynczego pliku."""
    free_bytes: int
    required_peak_bytes: int
    strategy: str = ""
    breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def fits(self) -> bool:
        return self.free_bytes >= self.required_peak_bytes

    @property
    def margin_bytes(self) -> int:
        return self.free_bytes - self.required_peak_bytes


@dataclass
class Job:
    """Pojedyncze zadanie w kolejce."""
    src: Path
    operation: Operation
    dst_dir: Optional[Path] = None
    media_type: MediaType = MediaType.UNKNOWN
    compression: Optional[str] = None      # None => domyślne chdman dla typu
    threads: int = 0                        # 0 => auto
    verify_after: bool = True
    delete_source: bool = False
    # stan runtime
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0                    # 0..100
    status_text: str = ""
    dst_path: Optional[Path] = None
    error: str = ""
    audit: Optional[AuditResult] = None
    budget: Optional[DiskBudget] = None
    deep: Optional[object] = None  # DeepResult z deepcheck (unikamy importu cyklicznego)
