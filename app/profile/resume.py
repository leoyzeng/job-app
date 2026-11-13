"""Your resume: keeping the file you gave, and reading its text.

The original stays in ``profile/`` untouched. Its text is written beside it as
``resume.md``, which is what the agents read.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("jobapp.resume")

ALLOWED = (".pdf", ".docx")
MAX_BYTES = 10 * 1024 * 1024
MIN_USEFUL_CHARS = 200


class BadResume(ValueError):
    """The file cannot be used. The message is written for the user."""


@dataclass
class ResumeText:
    text: str
    pages: int = 0
    links: list[str] = field(default_factory=list)

    def as_markdown(self) -> str:
        out = self.text.strip() + "\n"
        if self.links:
            out += "\nLinks in the document:\n" + "".join(f"- {link}\n" for link in self.links)
        return out


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def current_resume(profile_dir: Path) -> Optional[Path]:
    """The resume in use: ``resume.pdf`` or ``resume.docx``, else the newest PDF or Word file."""
    named = [profile_dir / f"resume{ext}" for ext in ALLOWED]
    named = [path for path in named if path.is_file()]
    if named:
        return max(named, key=lambda path: path.stat().st_mtime)
    others = [
        path for path in profile_dir.glob("*")
        if path.is_file() and path.suffix.lower() in ALLOWED and not path.name.startswith("resume.previous")
    ]
    return max(others, key=lambda path: path.stat().st_mtime) if others else None


def save_upload(profile_dir: Path, filename: str, data: bytes) -> Path:
    """Store an uploaded resume as ``resume.pdf`` or ``resume.docx``."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise BadResume("Upload a PDF or a Word (.docx) file.")
    if not data:
        raise BadResume("That file is empty.")
    if len(data) > MAX_BYTES:
        raise BadResume("That file is larger than 10 MB. Export a smaller copy and try again.")
    looks_right = data.startswith(b"%PDF") if ext == ".pdf" else data.startswith(b"PK")
    if not looks_right:
        kind = "PDF" if ext == ".pdf" else "Word"
        raise BadResume(f"That file is named like a {kind} file but is not one. Export it again and retry.")

    profile_dir.mkdir(parents=True, exist_ok=True)
    target = profile_dir / f"resume{ext}"
    # One current resume: an older one of the other kind is kept, set aside.
    for other in ALLOWED:
        old = profile_dir / f"resume{other}"
        if other != ext and old.exists():
            os.replace(old, profile_dir / f"resume.previous{other}")
    tmp = target.with_name(target.name + ".uploading")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    log.info("Resume saved: %s (%d bytes)", target.name, len(data))
    return target


def _pdf_text(path: Path) -> ResumeText:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise BadResume("That PDF is password-protected. Save a copy without a password and upload it.")
    parts, links = [], []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
        # Text like "Personal Website" often hides the real address in a link.
        try:
            for ref in page.get("/Annots") or []:
                action = ref.get_object().get("/A")
                uri = action.get_object().get("/URI") if action is not None else None
                if uri and str(uri) not in links:
                    links.append(str(uri))
        except Exception as exc:  # noqa: BLE001 - links are a bonus, never a failure
            log.debug("Could not read links on a page: %s", exc)
    return ResumeText(text="\n".join(parts), pages=len(reader.pages), links=links)


def _docx_text(path: Path) -> ResumeText:
    import docx

    document = docx.Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            parts.append("  ".join(cell for cell in cells if cell))
    links = []
    for rel in document.part.rels.values():
        if "hyperlink" in rel.reltype and rel.is_external and rel.target_ref not in links:
            links.append(rel.target_ref)
    return ResumeText(text="\n".join(parts), pages=0, links=links)


def extract_text(path: Path) -> ResumeText:
    """Read the resume's text. Raises BadResume with a message for the user."""
    try:
        result = _pdf_text(path) if path.suffix.lower() == ".pdf" else _docx_text(path)
    except BadResume:
        raise
    except Exception as exc:  # noqa: BLE001 - any parser failure means "cannot read this file"
        log.warning("Could not read %s: %s: %s", path.name, type(exc).__name__, exc)
        raise BadResume(
            f"{path.name} could not be opened. Export it again from the program you wrote it in and upload that copy."
        ) from exc
    # Tidy: no trailing spaces, no runs of blank lines.
    lines = [line.rstrip() for line in result.text.replace("\r", "").split("\n")]
    tidy: list[str] = []
    for line in lines:
        if line.strip() or (tidy and tidy[-1].strip()):
            tidy.append(line)
    result.text = "\n".join(tidy).strip()
    if len(result.text) < MIN_USEFUL_CHARS:
        raise BadResume(
            f"{path.name} has almost no selectable text. It may be a scan or an image. "
            "Upload a Word file, or a PDF exported from the program you wrote it in."
        )
    return result


def write_resume_text(profile_dir: Path, extracted: ResumeText) -> Path:
    target = profile_dir / "resume.md"
    target.write_text(extracted.as_markdown(), encoding="utf-8", newline="\n")
    return target
