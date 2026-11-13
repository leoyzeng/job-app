"""Sample-data mode for the profile screen: a made-up person and stand-in agents.

Sample-data mode must not use Claude or touch your files, so in that mode
the two agents are replaced by the simple functions below. They are also what
the automated tests use in place of Claude.
"""

from __future__ import annotations

import asyncio
import json

from ..agents.runner import AgentReply
from ..core.jsonfile import write_json_atomic
from ..core.models import now_iso
from ..paths import Paths
from .models import ProfileFile, ReadingFile, SearchReading
from .requirements import fingerprint, reconcile, write_requirements

SAMPLE_FACTS = {
    "name": {"first": "Sam", "last": "Rivera", "full": "Sam Rivera"},
    "contact": {"email": "sam.rivera@example.com", "phone": "555-0142", "city": "Pittsburgh", "state": "PA",
                "country": "United States"},
    "links": {"linkedin": "linkedin.com/in/sam-rivera-example", "github": "github.com/samrivera-example",
              "website": "samrivera.example.com"},
    "education": [
        {"school": "Three Rivers University", "degree": "Master of Science", "field": "Electrical and Computer Engineering",
         "start": "August 2026", "end": "December 2027", "end_is_expected": True, "gpa": "", "honors": []},
        {"school": "Lakeshore Institute of Technology", "degree": "Bachelor of Science", "field": "Computer Engineering",
         "start": "September 2022", "end": "May 2026", "end_is_expected": False, "gpa": "3.8/4.0",
         "honors": ["Dean's List"]},
    ],
    "experience": [
        {"employer": "Halden Robotics", "title": "Robotics Software Intern", "location": "Boston, MA",
         "start": "May 2025", "end": "August 2025", "current": False,
         "highlights": ["Wrote a ROS 2 node that cut arm calibration time from 40 minutes to 6.",
                        "Added hardware-in-the-loop tests for the gripper controller."]},
        {"employer": "Lakeshore Autonomy Lab", "title": "Undergraduate Research Assistant", "location": "Chicago, IL",
         "start": "September 2024", "end": "April 2025", "current": False,
         "highlights": ["Trained a PyTorch model to detect curbs from lidar scans."]},
    ],
    "projects": [
        {"name": "Line-following rover", "summary": "A small rover that follows tape lines using a camera and a PID loop.",
         "tools": ["Raspberry Pi", "Python", "OpenCV"]},
    ],
    "skills": ["Python", "C++", "ROS 2", "PyTorch", "Linux", "Git", "STM32", "MATLAB"],
    "certifications": [],
}

SAMPLE_HARD = ["Must be an internship or entry level", "Must be in the United States"]
SAMPLE_SOFT = ["Prefer the job to be related to robotics", "Prefer pay of at least $35 an hour",
               "Would be nice if it is hybrid or on site"]

_WISH_WORDS = ("prefer", "ideally", "would like", "nice to have", "would be nice", "hopefully")
_RULE_WORDS = ("must", "only", "required", "have to", "need to")


def _fake_reading(request: dict) -> dict:
    """What the requirements reader returns, done by simple rules instead of Claude."""
    hard, soft, settled = request.get("hard", []), request.get("soft", []), request.get("settled", [])
    text = " ".join(hard + soft).lower()
    ambiguous = []
    for line in hard:
        if any(word in line.lower() for word in _WISH_WORDS) and line not in settled:
            ambiguous.append({"text": line, "question": "This is in your hard requirements but reads like a preference. "
                              "Should jobs that miss it be hidden, or only ranked lower?"})
    for line in soft:
        if any(f" {word} " in f" {line.lower()} " for word in _RULE_WORDS) and line not in settled:
            ambiguous.append({"text": line, "question": "This is in your soft preferences but reads like a rule. "
                              "Should jobs that miss it be hidden, or only ranked lower?"})
    titles, keywords, types = [], [], []
    if "intern" in text:
        types.append("internship")
    if "entry level" in text or "new grad" in text:
        types.append("entry level")
    for word in ("robotics", "embedded", "controls", "machine learning", "firmware"):
        if word in text:
            keywords.append(word)
            titles.append(f"{word.title()} {'Intern' if 'intern' in text else 'Engineer'}")
    pay = next((line for line in hard + soft if "$" in line), "")
    return {
        "hard": [{"text": line, "test": f'The posting states something that satisfies "{line}".'} for line in hard],
        "soft": [{"text": line, "weight": 1 if "nice" in line.lower() else 2} for line in soft],
        "titles": titles, "keywords": keywords,
        "locations": ["United States"] if "united states" in text else [],
        "remote_ok": True if "remote" in text else None,
        "pay_floor": pay[pay.index("$"):].split(",")[0] if pay else "",
        "types": types, "ambiguous": ambiguous,
    }


async def demo_ask(*, name: str, instructions: str, prompt: str, schema: dict) -> AgentReply:
    # Long enough to see the "reading" state; the search agents answer faster.
    await asyncio.sleep(0.6 if name in ("profile_builder", "requirements_reader") else 0.15)
    if name == "profile_builder":
        data = dict(SAMPLE_FACTS, to_confirm=[
            {"field": "contact.phone", "question": "Is 555-0142 still the best number to reach you?"},
        ])
    elif name == "requirements_reader":
        data = _fake_reading(json.loads(prompt))
    elif name == "search_planner":
        from ..search.demo import fake_plan

        data = fake_plan(json.loads(prompt))
    elif name == "fit_evaluator":
        from ..search.demo import fake_verdicts

        data = fake_verdicts(json.loads(prompt))
    elif name == "question_matcher":
        from ..apply.questions import demo_match

        data = demo_match(json.loads(prompt))
    elif name == "writing_agent":
        from ..apply.writer import demo_draft

        data = demo_draft(json.JSONDecoder().raw_decode(prompt)[0])
    else:
        raise ValueError(f"no stand-in for agent {name}")
    return AgentReply(data=data, path="demo", model="stand-in", turns=1, cost_usd=0.0, duration_ms=600)


SAMPLE_RESUME_LINES = [
    "Sam Rivera",
    "555-0142   sam.rivera@example.com   github.com/samrivera-example   linkedin.com/in/sam-rivera-example",
    "EDUCATION",
    "Three Rivers University   August 2026 - Expected December 2027",
    "Master of Science in Electrical and Computer Engineering",
    "Lakeshore Institute of Technology   September 2022 - May 2026",
    "Bachelor of Science in Computer Engineering   GPA: 3.8/4.0   Dean's List",
    "EXPERIENCE",
    "Halden Robotics - Robotics Software Intern   Boston, MA   May 2025 - August 2025",
    "Wrote a ROS 2 node that cut arm calibration time from 40 minutes to 6.",
    "Added hardware-in-the-loop tests for the gripper controller.",
    "Lakeshore Autonomy Lab - Undergraduate Research Assistant   Chicago, IL   September 2024 - April 2025",
    "Trained a PyTorch model to detect curbs from lidar scans.",
    "SKILLS",
    "Python, C++, ROS 2, PyTorch, Linux, Git, STM32, MATLAB",
]


def simple_pdf(lines: list[str]) -> bytes:
    """A small, valid one-page PDF with the given lines of text (Helvetica, no images)."""
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = "BT /F1 11 Tf 50 760 Td 14 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def seed_demo_profile(paths: Paths, *, with_resume: bool = False) -> None:
    """Start sample-data mode from a finished, confirmed profile (and, if asked, a made-up resume)."""
    folder = paths.profile
    folder.mkdir(parents=True, exist_ok=True)
    if with_resume:
        (folder / "resume.pdf").write_bytes(simple_pdf(SAMPLE_RESUME_LINES))
        (folder / "resume.md").write_text("\n".join(SAMPLE_RESUME_LINES) + "\n", encoding="utf-8", newline="\n")
    profile = ProfileFile.model_validate({
        "source": {"file": "", "sha256": "", "read_at": now_iso()},
        "confirmed": True, "confirmed_at": now_iso(), "facts": SAMPLE_FACTS,
    })
    write_json_atomic(folder / "profile.json", profile.model_dump(mode="json"), backup=False)
    write_requirements(folder / "requirements.md", SAMPLE_HARD, SAMPLE_SOFT)
    request = {"hard": SAMPLE_HARD, "soft": SAMPLE_SOFT, "settled": []}
    reading = reconcile(SearchReading.model_validate(_fake_reading(request)), SAMPLE_HARD, SAMPLE_SOFT, [])
    saved = ReadingFile(source_sha256=fingerprint(SAMPLE_HARD, SAMPLE_SOFT), read_at=now_iso(),
                        confirmed=True, confirmed_at=now_iso(), reading=reading)
    write_json_atomic(folder / "search_profile.json", saved.model_dump(mode="json"), backup=False)
