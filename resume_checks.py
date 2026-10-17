"""Anti-fabrication checks for a tailored resume.

Every function here answers one question: can this sentence be traced back to
something the candidate's own documents already say? A tailored resume is only
allowed to re-emphasize; the moment it asserts something the master resume and
the certification objectives do not support, it stops being a resume and starts
being a lie the candidate has to defend in an interview.
"""

from __future__ import annotations

import re

MIN_SOURCE_CONTAINMENT = 0.5


# ------------------------------------------------------ grounding checks ----

# Claims about the candidate's availability, physical capacity, eligibility, or
# personal circumstances can never be derived from a resume or a certification.
# The model has no basis for asserting them, so they are always a violation --
# even when the job description explicitly asks for them. The candidate must add
# these by hand after confirming they are true.
UNVERIFIABLE_CLAIM_PATTERNS = [
    (r"\bon[- ]call\b", "on-call availability"),
    (r"\b(rotating |non[- ]standard |night |weekend |holiday |differing )?shifts?\b", "shift availability"),
    (r"\bnon[- ]standard hours\b", "hours availability"),
    (r"\bavailable (for|to)\b", "availability claim"),
    (r"\bwilling to\b", "willingness claim"),
    (r"\bable to lift\b|\b\d+\s*lbs?\b|\bpounds\b", "lifting/physical capacity"),
    (r"\bPPE\b|\bpersonal protective equipment\b", "PPE experience"),
    (r"\bharness(es)?\b|\bladders?\b|\bstep stools?\b|\bwork platforms?\b", "equipment handling"),
    (r"\bOSHA\b", "OSHA training"),
    (r"\bmaterial handling\b", "material handling"),
    (r"\bsecurity clearance\b|\bclearance\b", "security clearance"),
    (r"\brelocat\w*\b", "relocation"),
    (r"\bwhile working full[- ]time\b", "concurrent-employment claim"),
    (r"\bcomfortable with the physical\b", "physical requirements claim"),
]

_STOPWORDS = {
    "and", "the", "for", "with", "that", "this", "from", "have", "has", "had",
    "are", "was", "were", "been", "being", "not", "into", "onto", "any",
    "these", "those",
    "all", "our", "your", "their", "its", "his", "her", "them", "they", "you",
    "who", "which", "when", "where", "what", "how", "why", "can", "will",
    "would", "could", "should", "may", "might", "must", "than", "then", "such",
    "also", "more", "most", "much", "many", "some", "other", "both", "each",
    "same", "own", "very", "just", "only", "over", "under", "about", "across",
    "between", "through", "during", "while", "before", "after", "above",
    "below", "out", "off", "again", "further", "once", "here", "there", "role",
    "team", "work", "working", "job", "position", "candidate", "experience",
    "experienced",
    "including", "include", "includes", "using", "used", "use", "well", "new",
    "within", "toward", "towards", "per", "via", "etc", "eg", "ie",
    "but", "yet", "does", "did", "doing", "done", "need", "needs", "rather",
    "though", "although", "because", "since", "even", "still", "every", "already",
    "current", "currently", "early", "clear", "clearly", "make", "makes", "keep",
    "like", "way", "ways", "part", "able", "whether", "whose", "upon", "among",
}


# Nominalizing suffixes need a longer remainder than the rest, or "document"
# loses its "ment" and stops matching "documentation" -- the exact kind of
# silent mismatch that makes a grounding check flag sourced wording.
_LONG_SUFFIXES = ("ment", "ance", "ence")


def _stem(word: str) -> str:
    """Crude suffix stripping so 'configure'/'configuring' compare as equal."""
    # "reliability" and "reliable" must land together; the generic rules below
    # would leave "reliabil" and "reliabl".
    if word.endswith("bility") and len(word) >= 9:
        word = word[:-5] + "le"
    for suffix in ("ations", "ation", "ingly", "ing", "ies", "ied", "ers", "er",
                   "ed", "es", "s", "ly", *_LONG_SUFFIXES):
        minimum = 5 if suffix in _LONG_SUFFIXES else 4
        if word.endswith(suffix) and len(word) - len(suffix) >= minimum:
            word = word[: -len(suffix)]
            break
    # A trailing "e" is dropped last so 'manage' and 'managing' land together.
    if len(word) >= 5 and word.endswith("e"):
        word = word[:-1]
    return word


# Stopwords are filtered again after stemming, or an inflection slips through:
# "experience" is a stopword and never enters the allowed set, while
# "experienced" is not one, stems to "experienc", and gets flagged as unsourced.
_STOPWORD_STEMS = {_stem(w) for w in _STOPWORDS}


def _content_stems(text: str) -> set[str]:
    # "/" separates words ("cloud/database"), so it is not part of a token.
    words = re.findall(r"[a-zA-Z0-9+#]{3,}", text.lower())
    stems = {_stem(w) for w in words if w not in _STOPWORDS}
    return stems - _STOPWORD_STEMS


def _collect_text(value) -> str:
    """Flatten any nested JSON structure into one searchable string."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_collect_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_collect_text(v) for v in value)
    return ""


def _normalize(text: str) -> str:
    """Collapse whitespace so a verbatim comparison is not defeated by spacing."""
    return " ".join(text.split()).strip()


def check_unverifiable_claims(
    master: dict, tailored: dict, cover_letter: str
) -> list[str]:
    """Flag assertions the model cannot possibly ground in the source documents.

    A pattern only counts as a violation when the master resume does not already
    use that wording. Otherwise a master bullet that legitimately mentions, say,
    "shifts" would fail every run, and the operator would learn to bypass the
    check wholesale — which is worse than not having it.
    """
    master_text = _collect_text(master)
    problems: list[str] = []
    targets = [("resume", _collect_text(tailored)), ("cover letter", cover_letter)]

    for where, text in targets:
        for pattern, label in UNVERIFIABLE_CLAIM_PATTERNS:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            if re.search(pattern, master_text, flags=re.IGNORECASE):
                continue  # the master already says this; not the model's invention
            problems.append(
                f"{where} asserts {label} (unverifiable): ...{match.group(0)!r}..."
            )
    return problems


# ------------------------------------------------ bullet provenance checks ----

# Words a rephrasing may reach for that describe no work of their own. Anything
# outside this set must come from the bullet's own source text.
_NEUTRAL_STEMS = {
    "support", "provid", "perform", "deliver", "handl", "hands", "day",
    "ongo", "regular", "rout", "direct", "close", "consist", "relat",
}


def bullet_entries(job: dict, master_job: dict) -> tuple[list[dict], list[str]]:
    """Normalize a job's bullets to {"text", "source_index"} records.

    Bare strings are tolerated when they match a master bullet exactly, so a
    model that ignores the provenance schema still succeeds in verbatim mode
    instead of throwing away an otherwise-valid run.
    """
    problems: list[str] = []
    entries: list[dict] = []
    master_bullets = master_job["bullets"]

    for position, raw in enumerate(job.get("bullets", [])):
        if isinstance(raw, dict):
            entries.append(
                {"text": raw.get("text", ""), "source_index": raw.get("source_index")}
            )
            continue
        if isinstance(raw, str):
            normalized = _normalize(raw)
            matches = [
                i for i, m in enumerate(master_bullets) if _normalize(m) == normalized
            ]
            if len(matches) == 1:
                entries.append({"text": raw, "source_index": matches[0]})
                continue
            problems.append(
                f"{job.get('title', '?')}: bullet {position} has no source_index "
                f"and does not match a master bullet verbatim"
            )
            entries.append({"text": raw, "source_index": None})
            continue
        problems.append(f"{job.get('title', '?')}: bullet {position} is not text")
        entries.append({"text": "", "source_index": None})

    return entries, problems


def check_bullet_provenance(
    master: dict, tailored: dict, rewrite_allowed: bool
) -> tuple[list[str], list[str]]:
    """Verify every bullet is a traceable transformation of one master bullet.

    Returns (violations, notes). Grounding is scoped to the bullet's own source,
    which is the whole point: checking against the resume as a whole lets one
    job's vocabulary migrate to another employer, and that is the most common
    way a "tailored" resume ends up claiming work that never happened.
    """
    problems: list[str] = []
    notes: list[str] = []

    for master_job, job in zip(master["experience"], tailored.get("experience", [])):
        title = master_job["title"]
        master_bullets = master_job["bullets"]
        entries, parse_problems = bullet_entries(job, master_job)
        problems.extend(parse_problems)

        indices = [e["source_index"] for e in entries]
        if sorted(i for i in indices if isinstance(i, int)) != list(
            range(len(master_bullets))
        ):
            problems.append(
                f"{title}: bullets are not a 1:1 permutation of the master's "
                f"{len(master_bullets)} bullets (got source_index {indices})"
            )
            continue

        for entry in entries:
            source = master_bullets[entry["source_index"]]
            text = entry["text"]

            if not rewrite_allowed:
                if _normalize(text) != _normalize(source):
                    problems.append(
                        f"{title}: bullet text was edited in verbatim mode\n"
                        f"      master:   {source}\n"
                        f"      returned: {text}"
                    )
                continue

            source_stems = _content_stems(source)
            text_stems = _content_stems(text)
            allowed = source_stems | _content_stems(title) | _NEUTRAL_STEMS

            novel = sorted(text_stems - allowed)
            if novel:
                problems.append(
                    f"{title}: bullet imports wording absent from its source "
                    f"bullet {novel}\n      source: {source}\n      returned: {text}"
                )

            if source_stems:
                kept = len(source_stems & text_stems) / len(source_stems)
                if kept < MIN_SOURCE_CONTAINMENT:
                    problems.append(
                        f"{title}: bullet retains only {kept:.0%} of its source "
                        f"bullet's substance (minimum {MIN_SOURCE_CONTAINMENT:.0%})\n"
                        f"      source: {source}\n      returned: {text}"
                    )

    if not rewrite_allowed:
        notes.append("Bullet text verified verbatim against the master.")
    return problems, notes


# ------------------------------------------------------- seniority checks ----

# Claiming a higher level of responsibility than the source bullet states is
# fabrication even when every individual word is sourced.
VERB_TIERS = {
    3: (
        "led", "leads", "leading", "owned", "owns", "owning", "directed",
        "directs", "architected", "spearheaded", "headed", "oversaw",
        "oversees", "overseeing", "supervised", "supervises", "founded",
    ),
    2: (
        "managed", "manages", "managing", "designed", "designs", "designing",
        "implemented", "implements", "built", "builds", "developed", "develops",
        "created", "creates", "established", "engineered", "administered",
    ),
    1: (
        "assisted", "assists", "assisting", "supported", "supports",
        "supporting", "helped", "helps", "helping", "participated",
        "contributed", "aided", "shadowed", "observed",
    ),
}
_VERB_TIER_LOOKUP = {verb: tier for tier, verbs in VERB_TIERS.items() for verb in verbs}


def _seniority_tier(text: str) -> tuple[int, str | None]:
    """Highest responsibility level any verb in the text claims."""
    best, source_word = 0, None
    for word in re.findall(r"[a-zA-Z]+", text.lower()):
        tier = _VERB_TIER_LOOKUP.get(word)
        if tier and tier > best:
            best, source_word = tier, word
    return best, source_word


def check_seniority(master: dict, tailored: dict) -> list[str]:
    """Flag bullets that promote the candidate above what the source bullet says."""
    problems: list[str] = []

    for master_job, job in zip(master["experience"], tailored.get("experience", [])):
        entries, _ = bullet_entries(job, master_job)
        for entry in entries:
            index = entry["source_index"]
            if not isinstance(index, int) or not 0 <= index < len(master_job["bullets"]):
                continue
            source = master_job["bullets"][index]
            source_tier, _ = _seniority_tier(source)
            text_tier, claimed = _seniority_tier(entry["text"])
            # A source with no ranked verb is not a free pass: "Maintained
            # databases" rewritten as "Directed databases" is still a promotion,
            # and "direct" is a neutral stem the provenance check lets through.
            if text_tier > source_tier:
                problems.append(
                    f"{master_job['title']}: seniority inflated to {claimed!r}\n"
                    f"      source: {source}\n      returned: {entry['text']}"
                )
    return problems


# ---------------------------------------------------------- number checks ----

def check_invented_numbers(master: dict, tailored: dict, cover_letter: str) -> list[str]:
    """Reject any quantity claim the master resume does not state.

    Invented metrics are the most attractive thing for a model to add and the
    hardest thing to notice on a read-through, so this has no override. What it
    targets is a quantity CLAIM -- a measured amount of work, people, or
    improvement. A lone small digit inside an ordinary phrase ("Tier 1 support")
    measures nothing, so it is left alone; scaling it up ("Tier 1 for 400 users")
    trips the check on the number that actually makes a claim.
    """
    master_numbers = {n.rstrip(".,") for n in re.findall(r"\d[\d,.]*", _collect_text(master))}
    problems: list[str] = []

    for where, text in (("resume", _collect_text(tailored)), ("cover letter", cover_letter)):
        for match in re.finditer(r"\d[\d,.]*", text):
            number = match.group(0).rstrip(".,")
            if number in master_numbers:
                continue
            if not _is_quantity_claim(text, match):
                continue
            context = _normalize(text[max(0, match.start() - 40) : match.end() + 40])
            problems.append(
                f"{where} states a quantity not in the master: {number!r} in ...{context}..."
            )
    return sorted(set(problems))


# Nouns that turn a number into a measurement of the candidate's work.
_MAGNITUDE_WORDS = (
    r"percent|users?|clients?|customers?|tickets?|systems?|endpoints?|devices?|"
    r"machines?|workstations?|servers?|employees?|staff|people|teams?|sites?|"
    r"locations?|accounts?|years?|months?|weeks?|days?|hours?|minutes?|calls?|"
    r"requests?|incidents?|records?|databases?|applications?|apps?"
)


# Words that make a following number a label rather than a count: "Tier 1",
# "Level 2", "Windows 11". These name a thing; they do not measure work.
_LABEL_WORDS = (
    r"tier|level|line|phase|stage|step|type|class|category|group|core|"
    r"windows|version|v|part|section|option|priority|p"
)


def _is_quantity_claim(text: str, match: re.Match) -> bool:
    """Does this number measure something, or is it incidental to the phrasing?"""
    number = match.group(0).rstrip(".,")
    trailing = text[match.end() : match.end() + 24]
    leading = text[max(0, match.start() - 24) : match.start()]

    if re.match(r"\s*(%|\+|x\b|-fold)", trailing, flags=re.IGNORECASE):
        return True
    if leading.endswith("$"):
        return True
    if re.search(rf"\b({_LABEL_WORDS})[\s\-‑]*$", leading, flags=re.IGNORECASE):
        return False
    if re.match(rf"\s*({_MAGNITUDE_WORDS})\b", trailing, flags=re.IGNORECASE):
        return True
    if re.search(r"\b(over|under|more than|up to|nearly|approximately|about)\s*$", leading,
                 flags=re.IGNORECASE):
        return True
    # Multi-digit numbers are rarely incidental; single digits usually are.
    return len(number.replace(",", "").replace(".", "")) > 1


# ------------------------------------------------------- cert-leak checks ----

_CERT_MARKER_WORDS = ("certified", "certification")


def _cert_marker(cert_skills: dict) -> re.Pattern:
    """Words that attribute a skill to a certification the candidate holds.

    Built from cert_skills.json so that editing that file to match your own
    credentials is enough. A name token counts when it carries a "+" ("A+") or
    is a 3+ letter acronym-style word ("CompTIA", "CISSP"); "IT" is too common
    to mark anything.
    """
    tokens = set(_CERT_MARKER_WORDS)
    for cert in cert_skills.get("certs", []):
        tokens |= _cert_name_tokens(cert.get("name", ""))
    alternation = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    # Trailing \b would never fire after a "+", so the boundary is only on the left.
    return re.compile(rf"\b({alternation})", flags=re.IGNORECASE)


def _cert_name_tokens(name: str) -> set[str]:
    """Distinctive tokens of a certification name: "A+", "CompTIA", "CIOS"."""
    return {
        token
        for token in re.split(r"[\s\-–—/,:]+", name.split("(")[0])
        if "+" in token or (len(token) >= 3 and sum(c.isupper() for c in token) >= 2)
    }


def held_certs(master: dict, cert_skills: dict) -> dict:
    """Only the certifications the master resume actually lists.

    cert_skills.json is a catalogue, not a claim. Without this filter, anyone
    whose resume lacks a CompTIA cert would still have the prompt tell the model
    they hold one -- and have the checks accept skills attributed to it.
    """
    held_text = " ".join(master.get("certifications", []))

    def held(cert: dict) -> bool:
        tokens = _cert_name_tokens(cert.get("name", ""))
        return bool(tokens) and all(
            re.search(rf"(?<!\w){re.escape(t)}(?!\w)", held_text, flags=re.IGNORECASE)
            for t in tokens
        )

    return {**cert_skills, "certs": [c for c in cert_skills.get("certs", []) if held(c)]}

# Ordinary English that happens to appear in exam objectives. Flagging these as
# "certification knowledge" buries the real leaks in noise, and a check nobody
# can read is a check nobody enforces.
_GENERIC_CERT_STEMS = {
    "account", "address", "back", "basic", "best", "chang", "command", "common",
    "communic", "component", "comput", "concept", "consid", "core", "document",
    "effectiv", "establish", "flow", "fundamental", "handl", "high", "install",
    "learn", "least", "lesson", "line", "list", "log", "loss", "mainten",
    "media", "method", "methodolog", "model", "operational", "panel", "party",
    "perform", "physical", "polic", "practic", "precaution", "prem",
    "prevention", "proces", "professionalism", "program", "protection",
    "removal", "requir", "review", "safety", "screen", "selection", "sensitiv",
    "serv", "servic", "set", "sett", "side", "spann", "standard", "structur",
    "suppl", "surfac", "systematic", "techniqu", "technolog", "third", "type",
}


def cert_vocabulary(cert_skills: dict) -> str:
    """The certifications' own words -- names and skills, not file commentary.

    Keys beginning with "_" are notes to human readers of cert_skills.json. If
    they leak into the vocabulary, ordinary words from those notes start
    counting as certification-attested skills.
    """
    return _collect_text(
        [
            {k: v for k, v in cert.items() if not k.startswith("_")}
            for cert in cert_skills.get("certs", [])
        ]
    )


def _cert_only_stems(master: dict, cert_skills: dict) -> set[str]:
    """Technical vocabulary the certs attest but the work history never mentions."""
    cert_only = _content_stems(cert_vocabulary(cert_skills)) - _content_stems(
        _collect_text(master)
    )
    return {s for s in cert_only - _GENERIC_CERT_STEMS - _LETTER_STEMS if len(s) >= 4}


def check_cert_leak(
    master: dict, cert_skills: dict, tailored: dict, cover_letter: str
) -> list[str]:
    """Keep certification-attested knowledge out of the work-experience claims.

    A cert proves the candidate was tested on something. It does not prove they
    did it at a job. Experience bullets are for work performed; cert knowledge
    belongs in competencies or the summary, attributed to the certification.
    """
    cert_only = _cert_only_stems(master, cert_skills)
    marker = _cert_marker(cert_skills)
    problems: list[str] = []

    for master_job, job in zip(master["experience"], tailored.get("experience", [])):
        entries, _ = bullet_entries(job, master_job)
        for entry in entries:
            leaked = sorted(_content_stems(entry["text"]) & cert_only)
            if leaked:
                problems.append(
                    f"{master_job['title']}: bullet presents certification-attested "
                    f"knowledge as work performed {leaked}\n      returned: {entry['text']}"
                )

    master_competencies = {_normalize(c) for c in master["competencies"]}
    for line in tailored.get("competencies", []):
        if _normalize(line) in master_competencies:
            continue
        leaked = sorted(_content_stems(line) & cert_only)
        if leaked and not marker.search(line):
            problems.append(
                f"added competency cites cert-only skills {leaked} without naming "
                f"the certification: {line!r}"
            )

    # In prose, cert knowledge is allowed only where the sentence says it is
    # certification knowledge. The summary is prose too, and the easiest place
    # after the cover letter to state cert knowledge as experience.
    for where, prose in (("summary", tailored.get("summary", "")), ("cover letter", cover_letter)):
        for sentence in re.split(r"(?<=[.!?])\s+", prose):
            leaked = sorted(_content_stems(sentence) & cert_only)
            if leaked and not marker.search(sentence):
                problems.append(
                    f"{where} presents cert-attested skills {leaked} as experience: "
                    f"{sentence.strip()!r}"
                )
    return problems


# ----------------------------------------------------- whole-doc grounding ----

# Ordinary cover-letter phrasing ("I'd welcome the chance to discuss...").
# None of it describes work performed, so no resume could ever source it and
# requiring that only makes every letter fail. Words that could carry a claim
# (tools, duties, "active", "federal", "available") are deliberately absent.
_LETTER_WORDS = """
    apply application applying interest interested excited eager enthusiasm bring
    contribute contribution discuss discussion welcome opportunity thank thanks
    consideration attention careful carefully sincerely hiring manager dear cover
    letter chance hear hearing look looking forward value values fit fits believe
    confident ready glad pleased happy write writing hold focus focused background
    strength strengths reliable dependable motivated mission organization company
    join joining grow growth learn learning quickly committed commitment detail
    details oriented attached enclosed resume review reviewing meet conversation
    combine combines combined approach environment environments prior potential
    formal respect respected appreciate today further additional excellent
    objective objectives earlier offer offers align aligned provide ensure ability
    success successful effective effectively goals priorities impact people
    solution solutions process processes plus practical mix help prepared related
    express request discipline disciplined found
""".split()
_LETTER_STEMS = {_stem(w) for w in _LETTER_WORDS}


def _proper_name_stems(text: str) -> set[str]:
    """Stems of capitalized words that don't start a sentence: product, tool,
    and organization names such as "Kubernetes" or "Okta"."""
    names: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        tokens = re.findall(r"[A-Za-z0-9+#]{3,}", sentence)
        names.update(_content_stems(" ".join(t for t in tokens[1:] if t != t.lower())))
    return names


def check_grounding(
    master: dict,
    cert_skills: dict,
    tailored: dict,
    cover_letter: str,
    approved: set[str],
    job_description: str = "",
    letter_names: set[str] = frozenset(),
) -> list[str]:
    """Flag wording in the free-form fields that the source documents don't support.

    Bullets are excluded here — they get the far stricter per-source check in
    check_bullet_provenance. Tagline, competencies, and projects are dense
    claims, so every word in them must come from the resume or cert topics.

    The summary and cover letter are prose. Holding every word of prose to the
    resume's vocabulary rejects ordinary English ("earlier", "offer") and makes
    most letters fail, so prose is flagged only where fabrication actually
    comes from: wording lifted from the job description, and capitalized
    names (tools, products, organizations) found in no source. The letter may
    also name the employer and role it is addressed to (letter_names).
    """
    allowed = (
        _content_stems(_collect_text(master))
        | _content_stems(cert_vocabulary(cert_skills))
        | approved
    )
    from_posting = _content_stems(job_description)

    warnings: list[str] = []
    checked = [
        ("summary", tailored.get("summary", "")),
        ("tagline", tailored.get("tagline", "")),
        ("cover letter", cover_letter),
    ]
    for line in tailored.get("competencies", []):
        checked.append(("competency", line))
    for project in tailored.get("projects", []):
        checked.append((f"project ({project.get('title', '?')})", project.get("description", "")))

    for where, text in checked:
        novel = _content_stems(text) - allowed
        if where in ("summary", "cover letter"):
            novel -= _LETTER_STEMS
            if where == "cover letter":
                novel -= letter_names
            novel &= from_posting | _proper_name_stems(text)
        if novel:
            warnings.append(f"{where}: unsourced wording {sorted(novel)}")
    return warnings


def validate(master: dict, tailored: dict) -> list[str]:
    """Return a list of violations; empty list means the tailored resume is safe."""
    problems: list[str] = []

    # Reordering by relevance is allowed (prompt rule 2); changing the wording
    # of a credential is not. Compare as multisets so order is free but content
    # is locked.
    for field in ("certifications", "education"):
        if sorted(tailored.get(field, [])) != sorted(master[field]):
            problems.append(f"{field} was modified")

    m_jobs, t_jobs = master["experience"], tailored.get("experience", [])
    if len(m_jobs) != len(t_jobs):
        problems.append("job count changed")
    else:
        for m, t in zip(m_jobs, t_jobs):
            if t.get("title") != m["title"]:
                problems.append(f"title changed: {m['title']!r} -> {t.get('title')!r}")
            if t.get("org_line") != m["org_line"]:
                problems.append(f"org/dates changed for {m['title']!r}")
            if len(t.get("bullets", [])) != len(m["bullets"]):
                problems.append(f"bullet count changed for {m['title']!r}")

    # Master competency lines may be reordered but not reworded or dropped.
    master_competencies = sorted(_normalize(c) for c in master["competencies"])
    tailored_competencies = sorted(
        _normalize(c) for c in tailored.get("competencies", [])
    )
    missing = [c for c in master_competencies if c not in tailored_competencies]
    if missing:
        problems.append(f"competency lines were removed or reworded: {missing}")

    if len(tailored.get("projects", [])) != len(master["projects"]):
        problems.append("project count changed")
    master_project_titles = sorted(p["title"] for p in master["projects"])
    tailored_project_titles = sorted(
        p.get("title", "") for p in tailored.get("projects", [])
    )
    if master_project_titles != tailored_project_titles:
        problems.append("project titles were modified")
    if tailored.get("name") != master["name"]:
        problems.append("name changed")
    if tailored.get("contact") != master["contact"]:
        problems.append("contact line changed")

    return problems


def flatten_bullets(master: dict, tailored: dict) -> dict:
    """Convert validated {"text", "source_index"} bullets back to plain strings."""
    resume = dict(tailored)
    resume["experience"] = [
        {
            **job,
            "bullets": [e["text"] for e in bullet_entries(job, master_job)[0]],
        }
        for master_job, job in zip(master["experience"], tailored.get("experience", []))
    ]
    return resume



# ------------------------------------------------------------ master schema ----

_MASTER_TEXT_FIELDS = ("name", "tagline", "contact", "summary")
_MASTER_LIST_FIELDS = ("competencies", "certifications", "education")


def check_master_schema(master: dict) -> list[str]:
    """Shape problems that would crash the checks or the renderer.

    The CLI's master is hand-written and the web app's is converted from an
    upload; either can be malformed, and a precise message beats a TypeError
    from deep inside a check.
    """
    if not isinstance(master, dict):
        return ["master resume must be a JSON object"]
    problems: list[str] = []

    def is_text_list(value) -> bool:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)

    for field in _MASTER_TEXT_FIELDS:
        if not isinstance(master.get(field), str):
            problems.append(f'"{field}" must be a string')
    for field in _MASTER_LIST_FIELDS:
        if not is_text_list(master.get(field)):
            problems.append(f'"{field}" must be a list of strings')

    experience = master.get("experience")
    if not isinstance(experience, list) or not experience:
        problems.append('"experience" must be a non-empty list of jobs')
    else:
        for i, job in enumerate(experience):
            if not isinstance(job, dict) or not all(
                isinstance(job.get(k), str) for k in ("title", "org_line")
            ):
                problems.append(f'experience[{i}] needs string "title" and "org_line"')
            elif not is_text_list(job.get("bullets")) or not job["bullets"]:
                problems.append(f'experience[{i}] "bullets" must be a non-empty list of strings')

    projects = master.get("projects")
    if not isinstance(projects, list) or not all(
        isinstance(p, dict) and isinstance(p.get("title"), str)
        and isinstance(p.get("description"), str)
        for p in projects
    ):
        problems.append('"projects" must be a list of {"title", "description"} objects')
    return problems
