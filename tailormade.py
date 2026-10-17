#!/usr/bin/env python3
"""Tailormade — tailor a master resume to a job description without inventing
anything.

Pipeline:
  1. Read master resume data (resume_master.json) and a job description (text file).
  2. Ask DeepSeek (via its chat-completions API) to reorder — and, only
     when asked, rephrase — EXISTING content toward the job description. It may
     not invent experience.
  3. Validate the response: employers, titles, dates, certifications, and
     education must be untouched; every bullet must name the master bullet it
     came from, and must stay grounded in that specific bullet.
  4. Render a DOCX (python-docx) and a PDF (reportlab) named after the
     company and role, e.g. "Jane Example - Acme Labs - IT Support Specialist.docx".

Two bullet modes:
  verbatim (default)  Bullet text must match the master character for character.
                      The model may only choose the order. Fabrication in the
                      experience section is structurally impossible.
  --rewrite-bullets   The model may rephrase a bullet, but every bullet is
                      checked against ITS OWN source bullet, not the resume as a
                      whole. Vocabulary from another job, from the competencies
                      list, or from the job description cannot leak in.

Usage:
  python3 tailormade.py path/to/job_description.txt
  pbpaste | python3 tailormade.py -          # paste JD from clipboard
  python3 tailormade.py jd.txt --rewrite-bullets
  python3 tailormade.py jd.txt --master other_master.json --outdir Tailored
"""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from resume_checks import (
    _content_stems,
    _stem,
    check_bullet_provenance,
    check_cert_leak,
    check_grounding,
    check_invented_numbers,
    check_master_schema,
    check_seniority,
    check_unverifiable_claims,
    flatten_bullets,
    held_certs,
    validate,
)
from resume_render import (
    build_docx,
    build_letter_docx,
    build_letter_pdf,
    build_pdf,
    letter_parts,
)

MODEL_TIMEOUT_SECONDS = 300
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
# API alias currently served by DeepSeek-V4.1-Flash-1017.
DEEPSEEK_MODEL = "deepseek-flash"
# Checked in order when DEEPSEEK_API_KEY is not already set in the environment.
ENV_FILES = (
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / ".claude" / ".env",
)
MAX_FILENAME_PART = 60

BULLET_RULE_VERBATIM = """\
BULLET TEXT IS FROZEN. Copy each bullet's "text" from the master character for
character. You may ONLY choose the order bullets appear in within a job. Any
edit — a synonym, a dropped word, a reworded ending — will be rejected. Put the
tailoring effort into ordering, the summary, the tagline, and the cover letter.\
"""

BULLET_RULE_REWRITE = """\
You may rephrase a bullet's "text" to emphasize the aspects this job cares
about, but each bullet is checked against ITS OWN source bullet alone. Words
from another job, from the competencies list, or from the job description are
treated as fabrication. Rephrasing means re-emphasizing what the source bullet
already says — never adding a fact to it. Keep most of the source bullet's
substantive words; a bullet that no longer shares its source's meaning is
rejected.\
"""

PROMPT_TEMPLATE = """\
You are a resume tailoring assistant. Rewrite the resume JSON below so it is
tailored to the job description, following these HARD RULES:

1. NEVER invent work experience, employers, titles, dates, certifications,
   degrees, or accomplishments not present in the master resume.
2. You may ONLY: reorder bullets/competencies/projects by relevance, adjust the
   summary and tagline wording (using only facts already in the resume), and
   mirror the job description's terminology WHEN the underlying experience
   genuinely matches (e.g. "helpdesk ticketing" may be surfaced only if
   ticketing work already exists in the resume). Bullet text itself is governed
   by the BULLET RULE below.
3. Keep every "title", "org_line", certification, and education entry EXACTLY
   as-is, character for character.
4. Keep the same number of bullets per job. Do not add or remove jobs or
   projects. Keep the "experience" entries in the master's exact order; jobs are
   listed chronologically and may not be resequenced by relevance. Bullets
   within a job may be reordered.
5. NEVER exaggerate seniority. If a source bullet says "assisted", "supported",
   or "helped", the tailored bullet may not say "led", "owned", "managed",
   "directed", or "architected". Match the source's level of responsibility
   exactly. Do not claim clearances, degrees, or eligibility not stated.
6. EMPLOYER ATTRIBUTION: never attribute an activity to an employer unless that
   employer's own bullets state it. A skill listed in "competencies" is NOT
   licence to say the candidate did it at a specific job. Example of a
   violation: the resume lists ServiceNow as a tool, so the model writes "at
   the county office I worked ticket queues" — that employer's bullets never
   mention ticketing. That is fabrication, not tailoring.
7. NEVER assert availability, schedule flexibility, shift/on-call willingness,
   physical capacity, lifting ability, PPE or safety-equipment familiarity,
   OSHA training, clearances, or relocation. These are facts about the person
   that no source document contains. If the job description demands them, stay
   silent — the candidate will add them by hand if true.
8. Every phrase describing work performed must be traceable to specific text in
   the master resume. If you cannot point to the source line, do not write it.
   When the candidate's background genuinely does not match a requirement, say
   nothing about it rather than inventing a bridge.
9. NEVER introduce a number that is not already in the master resume. No
   percentages, no dollar amounts, no headcounts, no ticket volumes, no uptime
   figures, no "5+ years". If the master does not state the quantity, the
   quantity does not exist. Numbers already present may be reused unchanged.

BULLET RULE:
{bullet_rule}

BULLET PROVENANCE (required):
Every bullet in "experience" must be an object naming the master bullet it came
from, by its 0-based position in that same job's master bullet list:
  {{"text": "...", "source_index": 0}}
The source_index values within a job must be exactly the numbers 0..N-1, each
used once — you are permuting the master's bullets, not selecting from them.

CERT-ATTESTED SKILLS RULE:
The candidate holds the certifications listed below, and each certification's
official exam objectives attest specific skills. When the job description
requires a skill that is NOT in the work experience but IS covered by a held
certification's objectives, you MAY surface it — but only like this:
- Add it to "competencies" (you may ADD competency lines for this purpose) or
  mention it in the summary, phrased as certified/trained knowledge, e.g.
  "incident response (Security+ certified)" — never phrased as on-the-job
  experience.
- NEVER add cert-derived skills to "experience" bullets. Experience bullets
  describe work actually performed. This is checked mechanically: a term that
  appears in the certification objectives but nowhere in the work history will
  be rejected if it shows up in a bullet.
- Any competency line you ADD must be traceable to the certification objectives
  below and must name the certification inline, e.g.
  "Incident response fundamentals (Security+ certified)". A bare added skill
  line with no cert attribution is rejected.
- NEVER claim specific vendor products the resume does not list (e.g. Siemens
  Teamcenter, Oracle Database, Atlassian Jira). Generic capability is fine when
  the cert covers it (e.g. "ticketing systems" via A+ operational procedures),
  a named product the candidate never used is not.

CERT-ATTESTED SKILLS:
{cert_skills_json}

Also extract from the job description:
- "company": short employer name suitable for a filename, with normal
  word spacing (e.g. "Penn State ARL", not "PennStateARL")
- "role": short job title suitable for a filename, with normal word spacing

Also write "cover_letter": EXACTLY ONE paragraph (no line breaks), 120-170
words, addressed to a hiring manager for this role. EVERY rule above applies to
the cover letter with equal force — it is the easiest place to drift into
invented experience, because its prose style invites embellishment. In
particular: rules 5, 6, 7, 8, and 9 apply verbatim. Do not attribute activities
to employers whose bullets do not state them. Do not assert availability, shift
willingness, or physical capability. Do not inflate seniority and do not invent
a single number. Cert-attested skills may be cited as certified training, never
as job experience; no invented vendor products.
It should connect the candidate's actual experience and certifications to the
job's stated needs, acknowledge nothing falsely, and read naturally — no
buzzword stuffing. Where the candidate's background does not cover a
requirement, leave it out entirely: do not name it, apologize for it, or say the
candidate lacks it. Silence is honest; the resume shows what they have. Name a
tool, platform, or technology from the posting only if the resume or a held
certification already names it.
Do not include the greeting or sign-off in this field; paragraph body only.

Return ONLY a JSON object (no markdown fences, no commentary) with this shape:
{{
  "company": "...",
  "role": "...",
  "cover_letter": "...",
  "resume": {{
    ...same schema as the master resume, except that each job's "bullets" is a
    list of {{"text": "...", "source_index": N}} objects as described above...
  }}
}}

MASTER RESUME JSON:
{master_json}

JOB DESCRIPTION:
{job_description}
"""


@dataclass
class Failure:
    """A rejected draft, in a form both the operator and the model can read."""

    header: str
    items: list[str]
    advice: str = ""

    def as_feedback(self) -> str:
        lines = "\n".join(f"- {item}" for item in self.items)
        return (
            "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED. Fix these exact problems and\n"
            "return the corrected JSON. Every rule above still applies.\n"
            f"{self.header}\n{lines}\n"
            "Where a job requirement is simply absent from the resume, drop the\n"
            "wording entirely rather than rephrasing around it — silence passes,\n"
            "clever paraphrase does not."
        )


def deepseek_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    for path in ENV_FILES:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "DEEPSEEK_API_KEY":
                return value.strip().strip("\"'")
    raise RuntimeError(
        "DEEPSEEK_API_KEY not set — export it or put DEEPSEEK_API_KEY=... in "
        + " or ".join(str(path) for path in ENV_FILES)
    )


# deepseek-flash prices in USD per 1M tokens: (cache hit, cache miss, output).
# Off-peak is half price; peak is 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri.
DEEPSEEK_PRICE_PEAK = (0.006, 0.30, 1.20)
DEEPSEEK_PEAK_HOURS = set(range(1, 4)) | set(range(6, 10))


def deepseek_cost(usage: dict, when: datetime) -> float:
    """Estimated USD cost of one call from its token counts."""
    hit, miss, out = DEEPSEEK_PRICE_PEAK
    peak = when.weekday() < 5 and when.hour in DEEPSEEK_PEAK_HOURS
    cached = usage.get("prompt_cache_hit_tokens", 0)
    uncached = usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - cached)
    cost = (cached * hit + uncached * miss + usage.get("completion_tokens", 0) * out) / 1e6
    return cost if peak else cost / 2


def run_model(prompt: str, on_call=None) -> str:
    """Call DeepSeek once. on_call, if given, receives a record of the call
    (tokens, estimated cost, duration, success) whether it succeeds or fails."""
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        DEEPSEEK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {deepseek_api_key()}",
        },
    )
    started_at, started = datetime.now(timezone.utc), time.monotonic()
    usage: dict = {}
    error = None
    try:
        try:
            with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT_SECONDS) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"DeepSeek API returned HTTP {exc.code}:\n{detail}")
        except TimeoutError:
            raise RuntimeError(f"DeepSeek API timed out after {MODEL_TIMEOUT_SECONDS}s")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach DeepSeek API: {exc.reason}")
        usage = payload.get("usage") or {}
        try:
            return payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError(f"Unexpected DeepSeek API response:\n{json.dumps(payload)[:500]}")
    except RuntimeError as exc:
        error = str(exc).splitlines()[0]
        raise
    finally:
        if on_call is not None:
            on_call({
                "model": DEEPSEEK_MODEL,
                "ok": error is None,
                "error": error,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "cached_tokens": usage.get("prompt_cache_hit_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
                "cost_usd": deepseek_cost(usage, started_at),
                "duration_ms": int((time.monotonic() - started) * 1000),
            })


def parse_model_json(raw: str) -> dict:
    # Strip markdown fences if the model added them despite instructions.
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model output:\n{raw[:500]}")
    response = json.loads(text[start : end + 1])
    if not isinstance(response, dict) or not isinstance(response.get("resume"), dict):
        raise ValueError('Model output has no "resume" object')
    return response



def safe_filename_part(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "", text)
    cleaned = re.sub(r"[-_]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MAX_FILENAME_PART].rstrip(" .")


def display_name(name: str) -> str:
    """'JANE Q. EXAMPLE' -> 'Jane Example': title case, middle initials dropped."""
    words = name.title().split()
    return " ".join(
        w for i, w in enumerate(words)
        if not (0 < i < len(words) - 1 and re.fullmatch(r"\w\.?", w))
    )



# ---------------------------------------------------------------- main ----

def load_approved_wording(path: Path) -> set[str]:
    """Words the operator has personally checked and accepted in past runs."""
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {_stem(w.lower()) for w in data.get("approved", [])}


def parse_word_list(text: str) -> set[str]:
    """'pragmatic, Collaborative' -> stems comparable with the grounding check."""
    return {_stem(w.strip().lower()) for w in text.split(",") if w.strip()}


def save_approved_wording(path: Path, approved: set[str]) -> None:
    path.write_text(
        json.dumps({"approved": sorted(approved)}, indent=2) + "\n", encoding="utf-8"
    )


class TailorError(Exception):
    """The model could not produce a draft that passes every check."""

    def __init__(self, failure: Failure):
        super().__init__(failure.header)
        self.failure = failure


def run_checks(
    master: dict,
    cert_skills: dict,
    tailored: dict,
    letter_body: str,
    rewrite_bullets: bool,
    approved: set[str],
    letter_names: set[str] = frozenset(),
    job_description: str = "",
) -> Failure | None:
    """Run every anti-fabrication check, returning the first failure."""
    # A malformed response would otherwise crash a check with a TypeError;
    # report it to the model like any other rejection.
    try:
        return _run_checks(
            master, cert_skills, tailored, letter_body, rewrite_bullets, approved,
            letter_names, job_description,
        )
    except (AttributeError, KeyError, TypeError) as exc:
        return Failure(
            "Validation failed — response does not match the resume schema:",
            [f"{type(exc).__name__}: {exc}"],
        )


def _run_checks(
    master: dict,
    cert_skills: dict,
    tailored: dict,
    letter_body: str,
    rewrite_bullets: bool,
    approved: set[str],
    letter_names: set[str],
    job_description: str,
) -> Failure | None:
    problems = validate(master, tailored)
    if problems:
        return Failure(
            "Validation failed — tailored output altered protected fields:",
            problems,
        )

    # Each bullet must be a traceable transformation of one specific master
    # bullet. This is the check that makes cross-employer fabrication
    # impossible rather than merely discouraged.
    provenance, _ = check_bullet_provenance(master, tailored, rewrite_bullets)
    if provenance:
        return Failure(
            "Validation failed — bullets are not traceable to the master:",
            provenance,
            "In verbatim mode bullet text may not change at all. Enable bullet\n"
            "rewriting to allow rephrasing within each bullet's own source.",
        )

    inflated = check_seniority(master, tailored)
    if inflated:
        return Failure(
            "Validation failed — seniority was inflated above the source:",
            inflated,
        )

    invented = check_invented_numbers(master, tailored, letter_body)
    if invented:
        return Failure(
            "Validation failed — output invented quantities:",
            invented,
            "Metrics not in the master resume are fabrications. There is no override.",
        )

    leaks = check_cert_leak(master, cert_skills, tailored, letter_body)
    if leaks:
        return Failure(
            "Validation failed — certification knowledge presented as work experience:",
            leaks,
            "A certification proves the candidate was tested on something, not that\n"
            "they did it on the job. Keep it in competencies with the cert named.",
        )

    # The resume and the cover letter are held to the same standard: nothing
    # in either may originate from the job description instead of the
    # candidate.
    claims = check_unverifiable_claims(master, tailored, letter_body)
    if claims:
        return Failure(
            "Validation failed — output asserts things it cannot know:",
            claims,
            "These may well be true, but only you can confirm them. Add them\n"
            "by hand after verifying; the model must not assert them unprompted.",
        )

    unsourced = check_grounding(
        master, cert_skills, tailored, letter_body, approved, job_description, letter_names
    )
    if unsourced:
        return Failure(
            "Unsourced wording — not found in the resume or cert objectives:",
            unsourced,
            "Review each word above. Harmless connective phrasing can be approved\n"
            "(--approve-wording, or the approve field on the website); anything\n"
            "describing work performed must not be accepted — re-run instead.",
        )
    return None


def tailor(
    master: dict,
    cert_skills: dict,
    jd_text: str,
    outdir: Path,
    *,
    rewrite_bullets: bool = False,
    attempts: int = 4,
    approved: set[str] = frozenset(),
    company: str = "",
    log=print,
    on_model_call=None,
) -> list[Path]:
    """Tailor, check, and render. Returns the files written to outdir.

    company, when given, is the employer name the candidate typed. It names
    the files and the letter instead of the name the model reads from the
    posting, which can be missing or wrong (staffing agencies, unnamed firms).

    Raises TailorError when the master is malformed or no attempt passes every
    check, and RuntimeError when the model API call itself fails.
    """
    problems = check_master_schema(master)
    if problems:
        raise TailorError(Failure("Master resume is malformed:", problems))
    cert_skills = held_certs(master, cert_skills)

    mode = "rephrase" if rewrite_bullets else "verbatim"
    log(f"Tailoring resume with DeepSeek (bullet mode: {mode})...")
    base_prompt = PROMPT_TEMPLATE.format(
        master_json=json.dumps(master, indent=2),
        cert_skills_json=json.dumps(cert_skills["certs"], indent=2),
        bullet_rule=BULLET_RULE_REWRITE if rewrite_bullets else BULLET_RULE_VERBATIM,
        job_description=jd_text,
    )
    company = " ".join(company.split())[:MAX_FILENAME_PART]
    if company:
        base_prompt += (
            f"\nEMPLOYER NAME (entered by the candidate): {company}\n"
            'Use exactly this name for "company" and wherever the cover letter names the employer.\n'
        )

    # The checks are strict by design, and a single generation often trips one
    # of them. Rather than making the operator re-run by hand, feed the exact
    # failure back to the model and let it correct itself.
    failure: Failure | None = None
    for attempt in range(1, attempts + 1):
        prompt = base_prompt if failure is None else base_prompt + failure.as_feedback()
        label = "correcting last failure" if failure else "first draft"
        log(f"Attempt {attempt}/{attempts} ({label}) — calling DeepSeek...")
        started = time.monotonic()
        raw = run_model(prompt, on_model_call)
        log(f"  model replied in {time.monotonic() - started:.0f}s")
        try:
            response = parse_model_json(raw)
        except ValueError as exc:  # includes json.JSONDecodeError
            failure = Failure(
                "Response was not a valid JSON object:", [str(exc).splitlines()[0]]
            )
        else:
            tailored = response["resume"]
            letter_body = str(response.get("cover_letter") or "").strip()
            # The letter is addressed to this employer and role, so it may name
            # them -- but only words the posting itself uses, so the model can't
            # widen what the letter may say by inventing a "role". A company the
            # candidate typed is theirs to vouch for, so it is allowed as is.
            letter_names = (_content_stems(
                f"{response.get('company') or ''} {response.get('role') or ''}"
            ) & _content_stems(jd_text)) | _content_stems(company)
            failure = run_checks(
                master, cert_skills, tailored, letter_body, rewrite_bullets, approved,
                letter_names, jd_text,
            )
            if failure is None:
                break
        log(f"  rejected: {failure.header}")
        for item in failure.items:
            log(f"    - {item}")

    if failure is not None:
        raise TailorError(failure)

    for note in check_bullet_provenance(master, tailored, rewrite_bullets)[1]:
        log(note)
    tailored = flatten_bullets(master, tailored)

    company = safe_filename_part(company or str(response.get("company") or "")) or "Company"
    role = safe_filename_part(str(response.get("role") or "")) or "Role"
    base = f"{display_name(master['name'])} - {company} - {role}"

    outdir.mkdir(parents=True, exist_ok=True)
    docx_path = outdir / f"{base}.docx"
    pdf_path = outdir / f"{base}.pdf"
    build_docx(tailored, docx_path)
    build_pdf(tailored, pdf_path)
    outputs = [docx_path, pdf_path]

    if letter_body:
        parts = letter_parts(master, letter_body, company, role)
        letter_docx = outdir / f"{base} - Cover Letter.docx"
        letter_pdf = outdir / f"{base} - Cover Letter.pdf"
        build_letter_docx(parts, letter_docx)
        build_letter_pdf(parts, letter_pdf)
        outputs += [letter_docx, letter_pdf]
    else:
        log("Warning: model returned no cover letter.")
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tailormade",
        description="Tailor a master resume to a job description without inventing experience.",
    )
    parser.add_argument("job_description", help="Path to JD text file, or '-' for stdin")
    parser.add_argument("--master", default=str(Path(__file__).parent / "resume_master.json"))
    parser.add_argument("--certs", default=str(Path(__file__).parent / "cert_skills.json"))
    parser.add_argument("--outdir", default=str(Path(__file__).parent / "Tailored"))
    parser.add_argument("--company", default="",
                        help="Employer name for the file names and letter (default: read from the posting)")
    parser.add_argument(
        "--approved-wording",
        default=str(Path(__file__).parent / "approved_wording.json"),
        help="Words previously reviewed and accepted as safe connective phrasing",
    )
    parser.add_argument(
        "--rewrite-bullets",
        action="store_true",
        help="Let the model rephrase experience bullets (default: reorder only, "
             "text frozen). Each rewrite is still checked against its own source bullet.",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=4,
        help="How many times to let the model correct a rejected draft (default 4)",
    )
    parser.add_argument(
        "--approve-wording",
        default="",
        help="Comma-separated words to accept as sourced, now and in future runs",
    )
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")

    # Model output can contain any character; a cp1252 console must not turn a
    # rejection report into a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    if args.job_description == "-":
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        jd_text = sys.stdin.read()
    else:
        jd_text = Path(args.job_description).read_text(encoding="utf-8")
    if not jd_text.strip():
        print("Job description is empty.", file=sys.stderr)
        return 1

    # Explicit UTF-8 everywhere: the Windows default codepage decodes "–" as
    # "â€“", which then reaches the prompt and the rendered documents.
    master = json.loads(Path(args.master).read_text(encoding="utf-8"))
    certs_path = Path(args.certs)
    cert_skills = json.loads(certs_path.read_text(encoding="utf-8")) if certs_path.exists() else {"certs": []}

    approved_path = Path(args.approved_wording)
    approved = load_approved_wording(approved_path)
    newly_approved = parse_word_list(args.approve_wording)
    if newly_approved:
        approved |= newly_approved
        save_approved_wording(approved_path, approved)
        print(f"Recorded {len(newly_approved)} approved word(s) in {approved_path.name}.")

    try:
        outputs = tailor(
            master,
            cert_skills,
            jd_text,
            Path(args.outdir),
            rewrite_bullets=args.rewrite_bullets,
            attempts=args.attempts,
            approved=approved,
            company=args.company,
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    except TailorError as exc:
        failure = exc.failure
        print(failure.header, file=sys.stderr)
        for item in failure.items:
            print(f"  - {item}", file=sys.stderr)
        if failure.advice:
            print(f"\n{failure.advice}", file=sys.stderr)
        return 1

    print("Done:")
    for output in outputs:
        print(f"  {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
