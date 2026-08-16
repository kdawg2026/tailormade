#!/usr/bin/env python3
"""Tailormade — tailor a master resume to a job description without inventing
anything.

Pipeline:
  1. Read master resume data (resume_master.json) and a job description (text file).
  2. Ask Claude (via the `claude` CLI in headless mode) to reorder — and, only
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
import subprocess
import sys
from pathlib import Path

from resume_checks import (
    _stem,
    check_bullet_provenance,
    check_cert_leak,
    check_grounding,
    check_invented_numbers,
    check_seniority,
    check_unverifiable_claims,
    flatten_bullets,
    validate,
)
from resume_render import (
    build_docx,
    build_letter_docx,
    build_letter_pdf,
    build_pdf,
    letter_parts,
)

CLAUDE_TIMEOUT_SECONDS = 300
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
buzzword stuffing. Where the candidate's background genuinely does not cover a
core requirement, it is better to name that honestly than to paper over it.
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


def run_claude(prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", "--output-format", "text"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def parse_model_json(raw: str) -> dict:
    # Strip markdown fences if the model added them despite instructions.
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model output:\n{raw[:500]}")
    return json.loads(text[start : end + 1])



def safe_filename_part(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "", text)
    cleaned = re.sub(r"[-_]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MAX_FILENAME_PART].rstrip(" .")



# ---------------------------------------------------------------- main ----

def load_approved_wording(path: Path) -> set[str]:
    """Words the operator has personally checked and accepted in past runs."""
    if not path.exists():
        return set()
    return {_stem(w.lower()) for w in json.loads(path.read_text()).get("approved", [])}


def save_approved_wording(path: Path, approved: set[str]) -> None:
    path.write_text(json.dumps({"approved": sorted(approved)}, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tailormade",
        description="Tailor a master resume to a job description without inventing experience.",
    )
    parser.add_argument("job_description", help="Path to JD text file, or '-' for stdin")
    parser.add_argument("--master", default=str(Path(__file__).parent / "resume_master.json"))
    parser.add_argument("--certs", default=str(Path(__file__).parent / "cert_skills.json"))
    parser.add_argument("--outdir", default=str(Path(__file__).parent / "Tailored"))
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
        "--approve-wording",
        default="",
        help="Comma-separated words to accept as sourced, now and in future runs",
    )
    args = parser.parse_args()

    if args.job_description == "-":
        jd_text = sys.stdin.read()
    else:
        jd_text = Path(args.job_description).read_text()
    if not jd_text.strip():
        print("Job description is empty.", file=sys.stderr)
        return 1

    master = json.loads(Path(args.master).read_text())
    certs_path = Path(args.certs)
    cert_skills = json.loads(certs_path.read_text()) if certs_path.exists() else {"certs": []}

    approved_path = Path(args.approved_wording)
    approved = load_approved_wording(approved_path)
    newly_approved = {
        _stem(w.strip().lower()) for w in args.approve_wording.split(",") if w.strip()
    }
    if newly_approved:
        approved |= newly_approved
        save_approved_wording(approved_path, approved)
        print(f"Recorded {len(newly_approved)} approved word(s) in {approved_path.name}.")

    mode = "rephrase" if args.rewrite_bullets else "verbatim"
    print(f"Tailoring resume with Claude (bullet mode: {mode})...")
    prompt = PROMPT_TEMPLATE.format(
        master_json=json.dumps(master, indent=2),
        cert_skills_json=json.dumps(cert_skills["certs"], indent=2),
        bullet_rule=BULLET_RULE_REWRITE if args.rewrite_bullets else BULLET_RULE_VERBATIM,
        job_description=jd_text,
    )
    response = parse_model_json(run_claude(prompt))
    tailored = response["resume"]
    letter_body = response.get("cover_letter", "").strip()

    def fail(header: str, items: list[str], advice: str = "") -> int:
        print(header, file=sys.stderr)
        for item in items:
            print(f"  - {item}", file=sys.stderr)
        if advice:
            print(f"\n{advice}", file=sys.stderr)
        return 1

    problems = validate(master, tailored)
    if problems:
        return fail("Validation failed — tailored output altered protected fields:", problems)

    # Each bullet must be a traceable transformation of one specific master
    # bullet. This is the check that makes cross-employer fabrication impossible
    # rather than merely discouraged.
    provenance, notes = check_bullet_provenance(master, tailored, args.rewrite_bullets)
    if provenance:
        return fail(
            "Validation failed — bullets are not traceable to the master:",
            provenance,
            "In verbatim mode bullet text may not change at all. Use\n"
            "--rewrite-bullets to allow rephrasing within each bullet's own source.",
        )

    inflated = check_seniority(master, tailored)
    if inflated:
        return fail("Validation failed — seniority was inflated above the source:", inflated)

    invented = check_invented_numbers(master, tailored, letter_body)
    if invented:
        return fail(
            "Validation failed — output invented quantities:",
            invented,
            "Metrics not in the master resume are fabrications. There is no override.",
        )

    leaks = check_cert_leak(master, cert_skills, tailored, letter_body)
    if leaks:
        return fail(
            "Validation failed — certification knowledge presented as work experience:",
            leaks,
            "A certification proves the candidate was tested on something, not that\n"
            "they did it on the job. Keep it in competencies with the cert named.",
        )

    # The resume and the cover letter are held to the same standard: nothing in
    # either may originate from the job description instead of the candidate.
    claims = check_unverifiable_claims(master, tailored, letter_body)
    if claims:
        return fail(
            "Validation failed — output asserts things it cannot know:",
            claims,
            "These may well be true, but only you can confirm them. Add them\n"
            "by hand after verifying; the model must not assert them unprompted.",
        )

    unsourced = check_grounding(master, cert_skills, tailored, letter_body, approved)
    if unsourced:
        return fail(
            "Unsourced wording — not found in the resume or cert objectives:",
            unsourced,
            "Review each word above. Harmless connective phrasing can be accepted\n"
            "permanently with --approve-wording word1,word2; anything describing\n"
            "work performed must not be accepted — re-run instead.",
        )

    for note in notes:
        print(note)
    tailored = flatten_bullets(master, tailored)

    company = safe_filename_part(response.get("company", "Company"))
    role = safe_filename_part(response.get("role", "Role"))
    person = master["name"].title().replace("D. ", "")
    base = f"{person} - {company} - {role}"

    outdir = Path(args.outdir)
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
        print("Warning: model returned no cover letter.", file=sys.stderr)

    print("Done:")
    for output in outputs:
        print(f"  {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
