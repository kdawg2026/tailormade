# Tailormade

Tailors a master resume to a job description — and mechanically refuses to invent experience while doing it.

Every AI resume tool rewrites your resume to match a job posting. The problem is that "rewrite to match" and "make things up" are the same operation from the model's point of view: the posting asks for ticketing experience, your resume mentions ServiceNow in a skills list, and the output now says you worked ticket queues at a job where you did no such thing. You find out in the interview.

Tailormade treats that as the central problem rather than a footnote. The model is allowed to re-emphasize what you already did. Everything else is blocked by checks that run on its output before a single document is rendered.

## What it does

1. Reads your master resume (`resume_master.json`) and a job description.
2. Asks Claude — via the `claude` CLI in headless mode — to reorder and, optionally, rephrase **existing** content toward that posting.
3. Runs seven independent checks against the response. Any failure aborts before rendering.
4. Renders a DOCX and a PDF of both the resume and a cover letter, named for the company and role:
   `Jane Example - Penn State ARL - IT Support Specialist.pdf`

## The guarantees

### Bullet text is frozen by default

In the default **verbatim** mode the model may only choose the *order* bullets appear in. The text must match your master character for character. Fabrication inside the work-experience section is not discouraged — it is structurally impossible.

Ordering still carries most of the tailoring value: the bullet that matches the posting moves to the top of each job.

### Rewriting is scoped to a single source bullet

`--rewrite-bullets` lets the model rephrase, but every bullet must declare which master bullet it came from:

```json
{"text": "...", "source_index": 2}
```

The indices within a job must be a 1:1 permutation of the master's bullets — the model is permuting your resume, not selecting from it. Each rewritten bullet is then checked against **its own source bullet alone**, not the resume as a whole. Vocabulary from another employer, from your competencies list, or from the job description cannot leak in, because it isn't in that bullet's source text.

This is the check that matters most. Grounding a bullet against the whole document is what lets one job's vocabulary migrate to a different employer, which is the most common way a "tailored" resume ends up claiming work that never happened.

A rewritten bullet must also retain at least 50% of its source bullet's substantive words, so "rephrasing" cannot quietly become "describing different work."

### Seniority cannot be inflated

Verbs are ranked in tiers. If your source bullet says *assisted*, *supported*, or *helped*, the output may not say *led*, *owned*, *directed*, or *architected*. Every individual word can be sourced and the claim still be false; this catches that.

### Numbers cannot be invented

Any quantity claim absent from your master resume is rejected, with no override flag. Invented metrics — "reduced downtime 30%", "supported 250 endpoints", "5+ years" — are the most attractive thing for a model to add and the hardest to catch on a read-through.

The check targets quantity *claims*, not every digit. "Tier 1 support" measures nothing and passes; "Tier 1 for 400 users" trips on the number that actually makes a claim.

### Certification knowledge stays out of your work history

A certification proves you were tested on something. It does not prove you did it at a job. Tailormade computes the vocabulary that appears in your certs' exam objectives but nowhere in your work history, and rejects it if it shows up in an experience bullet.

That knowledge is still usable — in competencies or the summary, with the certification named inline:

> Incident response fundamentals (Security+ certified)

An added competency line citing cert-only skills without naming the cert is rejected. So is a cover-letter sentence that presents them as experience.

### Claims about you that no document can support

Availability, shift and on-call willingness, lifting capacity, PPE familiarity, OSHA training, clearances, relocation — no resume and no certification contains these facts. The model has no basis for asserting them even when the posting explicitly asks, so it is required to stay silent and let you add them by hand once you've confirmed they're true.

A pattern only counts as a violation when your master resume doesn't already use that wording, so a bullet that legitimately mentions shift work doesn't fail every run.

### Everything else stays inside your own vocabulary

The summary, tagline, competencies, project descriptions, and cover letter are checked word-by-word against your resume and cert objectives. Anything novel is surfaced for review.

Harmless connective phrasing gets accepted permanently:

```bash
python3 tailormade.py jd.txt --approve-wording pragmatic,collaborative
```

That list persists in `approved_wording.json`, so the noise shrinks over time rather than being switched off wholesale. There is deliberately no "ignore everything" flag — a check people bypass by reflex is a check that isn't running.

### Protected fields

Employers, job titles, dates, certifications, education, your name, contact line, and project titles are compared against the master and must be untouched. Competency lines may be reordered but not reworded or dropped.

## Requirements

- Python 3.10+
- The [`claude` CLI](https://claude.com/claude-code), authenticated and on your `PATH`
- `pip install python-docx reportlab`

## Setup

```bash
git clone https://github.com/kdawg2026/tailormade.git
cd tailormade
pip install python-docx reportlab

cp resume_master.example.json resume_master.json
$EDITOR resume_master.json
```

`resume_master.json` is your real resume as structured data — it is gitignored, and should stay that way. It holds your phone number, email, and address.

`cert_skills.json` maps each certification you hold to the skills its official exam objectives attest. Ships populated with CompTIA A+, Network+, Security+, CIOS, and CSIS. Edit it to match your own credentials.

## Usage

```bash
# Default: bullet text frozen, model chooses order only
python3 tailormade.py job_description.txt

# Paste the posting from your clipboard
pbpaste | python3 tailormade.py -

# Allow rephrasing, still scoped per source bullet
python3 tailormade.py job_description.txt --rewrite-bullets

# Accept reviewed connective wording permanently
python3 tailormade.py job_description.txt --approve-wording pragmatic

# Other options
python3 tailormade.py jd.txt --master other_master.json --outdir Applications
```

Output lands in `Tailored/`: resume DOCX + PDF, cover letter DOCX + PDF.

## When it fails

A failed run is the tool doing its job. The message names the check, quotes the offending text, and shows the master line it should have come from:

```
Validation failed — bullets are not traceable to the master:
  - Administrative Support Technician II: bullet imports wording absent from
    its source bullet ['queue', 'servicenow', 'ticket']
      source:   Maintained legal databases ensuring accuracy and compliance
      returned: Worked ServiceNow ticket queues for county staff
```

Re-run it. Models are non-deterministic, and a run that drifts once usually doesn't drift the same way twice. Loosen a check only when you've read the specific wording and know it's true.

## Layout

| File | Role |
|------|------|
| `tailormade.py` | Prompt construction, CLI, pipeline |
| `resume_checks.py` | Every anti-fabrication check |
| `resume_render.py` | DOCX and PDF rendering |
| `resume_master.json` | Your resume as data (gitignored) |
| `cert_skills.json` | Certification exam objectives |
| `approved_wording.json` | Wording you've reviewed and accepted (gitignored) |

## A note on what this can't do

These checks are lexical. They verify that wording traces back to a source, that numbers exist, that verbs don't escalate — they do not understand meaning. A model determined to imply something false can still do it with entirely sourced words.

What they remove is the *casual* fabrication that happens by default when you ask a model to make a resume match a posting. Read the output before you send it. The tool narrows what you have to check; it doesn't replace checking.
