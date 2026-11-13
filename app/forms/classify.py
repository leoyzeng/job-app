"""What kind of question each field asks, which fields are never filled, and what each button does.

The rules here are enforced by the filler itself, whoever asks it to fill:

* never filled: passwords, payment details, government ID numbers;
* left for you: consent, certification and signature fields;
* sensitive (filled only from your own saved answers, never guessed; the
  application agent's rule): work authorization, sponsorship,
  salary, demographics, criminal record, clearance, age, citizenship;
* certain (filled by plain code from your confirmed profile): name, email,
  phone, links, city, country, resume.
"""

from __future__ import annotations

import re

from .model import Button, Field, Reading

NEVER = [
    ("password", re.compile(r"\bpass ?(word|code|phrase)\b|\bpin\b")),
    ("payment", re.compile(r"\b(card number|credit card|debit card|cvv|cvc|security code|card expir\w*|expiration date"
                           r"|iban|routing number|bank account|account number|sort code|swift)\b")),
    ("government ID", re.compile(r"\b(social security|ssn|social insurance|sin number|national insurance|national id"
                                 r"(entity)? (number|card)|passport number|driver'?s? licen[cs]e (number|no\b|#)|tax (id|identification)"
                                 r"|taxpayer|\btin\b|aadhaa?r)\b")),
]
CONSENT = re.compile(
    r"\b(i (hereby )?(agree|certify|acknowledge|consent|attest|confirm|understand|authori[sz]e|accept)"
    r"|by (checking|clicking|submitting|signing|ticking)|terms (of|and) (use|service|conditions)|privacy (policy|notice|statement)"
    r"|attest|consent|e-?signature|electronic signature)\b")
SIGNATURE = re.compile(r"\b(signature|sign (here|your name)|type your (full |legal )?name to sign|initials)\b")
SENSITIVE = [
    ("work_authorization", re.compile(r"authori[sz]ed to work|work authori[sz]ation|eligib(le|ility) to work|legally (authori[sz]ed|eligible|able)"
                                      r"|right to work|work permit")),
    ("sponsorship", re.compile(r"sponsor")),
    ("salary", re.compile(r"salary|compensation (expectation|requirement)|expected (pay|compensation|salary|rate)|desired (pay|salary|rate)"
                          r"|pay expectation|rate expectation")),
    ("demographic", re.compile(r"\b(gender|sex|race|racial|ethnic\w*|hispanic|latin[oax]|veteran|disabilit\w*|sexual orientation"
                               r"|transgender|lgbt\w*|pronouns?)\b")),
    ("criminal", re.compile(r"criminal|convicted|conviction|felony|misdemeanou?r")),
    ("clearance", re.compile(r"security clearance|clearance level|active clearance|\bclearance\b")),
    ("age", re.compile(r"date of birth|birth ?date|\bage\b|how old|at least 1[68]\b|1[68] years")),
    ("citizenship", re.compile(r"citizen(ship)?|nationality|permanent resident|green card|visa status|immigration")),
]
CERTAIN = [
    ("preferred_name", re.compile(r"preferred (first )?name|nickname")),
    ("first_name", re.compile(r"^(legal )?first name$|^given name$|^first$|\bfirst_?name\b|firstname")),
    ("last_name", re.compile(r"^(legal )?last name$|surname|family name|^last$|\blast_?name\b|lastname")),
    ("full_name", re.compile(r"^(full |legal |your |candidate )?name$|^full legal name$")),
    ("email", re.compile(r"e-?mail")),
    ("phone", re.compile(r"phone( number)?$|mobile( number)?$|^cell|telephone|phone$|phone-number")),
    ("linkedin", re.compile(r"linked ?in")),
    ("github", re.compile(r"git ?hub")),
    ("website", re.compile(r"website|portfolio|personal (site|url|page)|other (url|link)")),
    ("country", re.compile(r"^country( of residence)?$")),
    ("city", re.compile(r"^(current )?(city|location)\b|location \(city\)|where are you (located|based)|^city$")),
    ("resume", re.compile(r"resume|résumé|\bcv\b|curriculum")),
    ("cover_letter", re.compile(r"cover letter")),
    ("school", re.compile(r"^school|university|college|institution")),
    ("degree", re.compile(r"^degree")),
    ("discipline", re.compile(r"discipline|major|field of study")),
]
TEXTLIKE = {"text", "email", "tel", "url", "number", "textarea", "date", "month"}
RESUME = re.compile(r"resume|résumé|\bcv\b|curriculum")


def _haystack(field: Field) -> str:
    parts = [field.label, field.name.replace("_", " "), field.autocomplete, field.automation_id.replace("_", " ")]
    return " ".join(p for p in parts if p).lower()


def classify_field(field: Field) -> Field:
    label = field.label.lower()
    text = _haystack(field)
    field.category, field.rule, field.rule_reason = "other", "", ""
    if field.kind == "password":
        field.category, field.rule, field.rule_reason = "password", "never", "Passwords are never filled."
        return field
    if field.autocomplete.startswith("cc-"):
        field.category, field.rule, field.rule_reason = "payment", "never", "Payment details are never filled."
        return field
    for name, pattern in NEVER:
        if pattern.search(label):
            field.category, field.rule = name.replace(" ", "_"), "never"
            field.rule_reason = f"{name[0].upper()}{name[1:]} details are never filled. You enter them."
            return field
    if field.kind in ("checkbox", "checkbox_group", "choice", "radio") and CONSENT.search(label):
        field.category, field.rule = "consent", "user"
        field.rule_reason = "Agreements and certifications are left for you to read and tick."
        return field
    if field.kind in TEXTLIKE and SIGNATURE.search(label):
        field.category, field.rule, field.rule_reason = "signature", "user", "Signatures are left for you."
        return field
    for name, pattern in SENSITIVE:
        if pattern.search(label):
            field.category = f"sensitive:{name}"
            return field
    if field.kind == "file":
        if RESUME.search(text):
            field.category = "resume"
        elif "cover letter" in text:
            field.category = "cover_letter"
        else:
            field.category = "file"
        return field
    for name, pattern in CERTAIN:
        if name in ("resume", "cover_letter"):
            continue
        if pattern.search(label) or (not label and pattern.search(text)) or (name in ("email",) and field.kind == "email"):
            field.category = name
            break
    if field.category == "other":
        if field.kind == "email":
            field.category = "email"
        elif field.kind == "tel":
            field.category = "phone"
        elif field.autocomplete in ("given-name", "family-name", "name", "email", "tel"):
            field.category = {"given-name": "first_name", "family-name": "last_name", "name": "full_name",
                              "email": "email", "tel": "phone"}[field.autocomplete]
    if field.category == "phone" and field.kind not in ("tel", "text", "number"):
        field.category = "other"   # "Phone device type" and the like
    if field.category == "cover_letter" and field.kind != "textarea":
        field.category = "other"
    return field


SUBMIT = re.compile(r"^(submit( my)?( application)?|submit and apply|send( my)? application|send|finish|complete( application)?"
                    r"|apply now|confirm and submit|submit now)$|\bsubmit\b")
NEXT = re.compile(r"^(next|continue|save (and|&) continue|save and next|proceed|next step|review( application)?|go to review)\b")
BACK = re.compile(r"^(back|previous|go back|prev)\b")
OPEN = re.compile(r"^(apply|apply now|apply for this (job|position|role)|i'?m interested|start (your )?application"
                  r"|apply (on|via) company (site|website)|easy apply)$")
ADD = re.compile(r"^(\+ ?)?add( another| more)?\b")
UPLOAD = re.compile(r"attach|upload|choose file|select file|enter manually|dropbox|google drive|remove file")


def classify_button(button: Button, has_fields: bool) -> Button:
    text = button.text.strip().lower()
    if UPLOAD.search(text):
        button.role = "upload"
    elif BACK.search(text):
        button.role = "back"
    elif NEXT.search(text):
        button.role = "next"
    elif OPEN.search(text):
        # "Apply" on a posting opens the form; on a filled-in form it may send it.
        button.role = "submit" if has_fields else "open_application"
    elif SUBMIT.search(text):
        button.role = "submit"
    elif ADD.search(text):
        button.role = "add"
    elif button.type == "submit":
        button.role = "submit"
    else:
        button.role = "other"
    return button


def classify(reading: Reading) -> Reading:
    for field in reading.fields:
        classify_field(field)
    has_fields = any(f.visible and f.kind not in ("file",) for f in reading.fields)
    for button in reading.buttons:
        classify_button(button, has_fields)
    return reading


def blockers(reading: Reading) -> list[str]:
    """Things on the page that need you before anything can be filled."""
    found = []
    if reading.captcha:
        found.append("captcha")
    visible = [f for f in reading.fields if f.visible]
    if any(f.kind == "password" for f in visible) and len(visible) <= 4:
        found.append("sign_in")
    return found
