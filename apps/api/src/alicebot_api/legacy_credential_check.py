"""v0.16.0's credential check, in linear time, at the two doors v0.16.0 checked.

Why it exists (S4.4 round 5, 2026-09-23). A third adversary on 6c24a97 found
eight shapes that v0.16.0 refused at the commit door and the branch stored:
dotted values under secret names, Telegram tokens, xxd hex dumps of key
files, unarmored DER under PRIVATE_KEY_DATA and similar names, numeric prose
passwords, primary and routing keys, hex AES keys, and Django SECRET_KEY
values with symbols. The tower confirmed the class is structural: v0.16.0
also refused PASSWORD_DB=, TOKEN_GITHUB=, API_KEY_RAW=, GITHUB_TOKEN_WRITE=
and VAULT_TOKEN=hvs., because its name rule reads a secret word anywhere in
the name, and the branch's grammar reads only the last segment. A panel
ruled (H1) that the two doors v0.16.0 checked get parity by construction:

- The commit gate (vnext_memory_commit) refuses when the credential floor
  refuses, OR when this module's re-implementation of v0.16.0's commit check
  refuses and no carve-out applies to what it found.
- The promotion floor (vnext_promotion_policy.hard_floor_hits) does the same
  with v0.16.0's floor check, looks_like_credential, which never had the
  commit gate's prefix patterns.
- Every other door keeps credential_floor alone.

What "the same as v0.16.0" means here. The rules below are v0.16.0's, with
the same patterns, the same surfaces (each field raw and normalised; the
floor also joins its fields with a space and with nothing), the same base64
and hex decoding and the same despacing. Two of v0.16.0's expressions were
quadratic, the NAME=value rule (4.3 s on 16,000 characters of "a_a_...",
over 30 s on "key_key_...", measured 2026-09-23) and the JWT shape, and
they are replaced by linear scans that yield the same matches. tests/unit/test_legacy_credential_check.py compares this module
with v0.16.0's own code, kept verbatim as an oracle, on generated inputs.

The carve-outs. v0.16.0's check refused some ordinary notes, and the design
round named them. A finding is excused only by one of four named functions,
each tested both ways:

(a) carve_out_ssh_public_key: an SSH public key or a key-type name.
(b) carve_out_sk_word_chain: "sk-" followed by lower-case words, as in
    sk-learn.
(c) carve_out_structural_key_name: a structural key name (fact_key and the
    continuity subject keys, cache_key, partition_key, sort_key,
    next_page_token, memory_key, dedupe and idempotency keys) with an
    identifier value.
(d) carve_out_code_reference: a dotted reference rooted at a known code
    object (settings., config., self., secrets., ENV., process.env,
    os.environ).

No carve-out ever applies to a password-kind name (one containing "passw"),
and findings are excused one at a time: a note with an excused sk-learn and
a real token is still refused for the token. Every carve-out ends at a
boundary (round 6), so a token glued straight onto an excused key type,
public key, cursor or code reference is not excused with it.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Iterator, Mapping
import re
from typing import NamedTuple

from alicebot_api.credential_floor import normalize_for_matching

# ---------------------------------------------------------------------------
# v0.16.0's patterns, copied from vnext_promotion_policy.py and
# vnext_memory_commit.py at 880915a. The oracle test pins each one against
# the verbatim copy.
# ---------------------------------------------------------------------------

_CREDENTIAL_PHRASES = (
    "begin openssh private key",
    "begin private key",
    "begin rsa private key",
)
_CREDENTIAL_TOKEN_MARKERS = (
    "ghp_",
    "gho_",
    "ghs_",
    "ghu_",
    "github_pat_",
    "glpat-",
    "npm_",
    "sk-",
    "ssh-rsa ",
    "ssh-ed25519 ",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "xoxs-",
)
_CREDENTIAL_TOKEN_PATTERNS = tuple(
    re.compile(r"(?<![0-9A-Za-z_])" + re.escape(marker), re.IGNORECASE) for marker in _CREDENTIAL_TOKEN_MARKERS
)
_ARMOR = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_AWS_ID = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Za-z]{16}\b", re.IGNORECASE)
_GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")
_SLACK = re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")
_SSH_PUBLIC_KEY = re.compile(r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]{20,}")
_BEARER = re.compile(r"\bBearer\s+[0-9A-Za-z._\-]{16,}")
_PROSE = re.compile(
    r"\b(?:password|passphrase|api\s+key|access\s+key|secret\s+key|private\s+key"
    r"|auth\s+token|access\s+token|credential)\b[^.\n]{0,24}?\b(?:is|was|are|were)\s+"
    r"(?=\S{6,})\S*[0-9!@#$%^&*_+=/\\-]\S*",
    re.IGNORECASE,
)
# v0.16.0's _CREDENTIAL_PATTERNS, in its order, with the JWT shape (index 4)
# read by _jwt_hit instead.
_SEARCHED_PATTERNS = (
    ("armor", _ARMOR),
    ("aws_id", _AWS_ID),
    ("google_key", _GOOGLE_KEY),
    ("slack", _SLACK),
    ("bearer", _BEARER),
    ("prose", _PROSE),
)
_JWT_PATTERN = r"\beyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{4,}"

_BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{16,512}={0,2}(?![A-Za-z0-9+/_=-])")
_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{24,512}\b")
_RUN_SEPARATORS = " \t\r\n.\u00b7,;|/"
_SPACED_OUT_RUN = re.compile(r"(?:\S[" + re.escape(_RUN_SEPARATORS) + r"]{1,3}){7,}\S")
_RUN_SEPARATOR_STRIP = str.maketrans({char: None for char in _RUN_SEPARATORS})

# The commit gate's own prefix patterns, read on the casefolded field.
_PREFIX_SK = re.compile(r"(?<![0-9a-z])sk-[0-9a-z_-]{8,}")
_PREFIX_PATTERNS = (
    re.compile(r"(?<![0-9a-z])ghp_[0-9a-z]{8,}"),
    re.compile(r"(?<![0-9a-z])xoxb-[0-9a-z-]{8,}"),
    re.compile(r"(?<![0-9a-z])akia[0-9a-z]{12,}"),
    re.compile(r"-----begin(?: [a-z]+)* private key-----"),
)

# ---------------------------------------------------------------------------
# The NAME=value rule, in one linear pass. It moved here from
# vnext_promotion_policy (which re-exports it) because it is v0.16.0's rule
# and this module is where v0.16.0's rule now runs.
#
# "password" or "secret" embedded in a longer word is still a credential
# name, as in PGPASSWORD. "key" is not: monkey, turkey and keyboard all
# contain it, so it only counts as a whole underscore or hyphen separated
# segment.
# ---------------------------------------------------------------------------

_SECRET_NAME_EMBEDDABLE = r"(?:password|passwd|secret|token|credentials?|apikey)"  # nosec B105 # a regex fragment naming secret words, not a password
_SECRET_NAME_SEGMENTED = r"key"  # nosec B105 # a regex fragment naming a secret word, not a password

# The rule, stated as the single regular expression it used to be:
#
#     (?<![0-9A-Za-z])
#     (?:[A-Za-z0-9]+[_-])*
#     (?:[A-Za-z0-9]*NAME|key)
#     (?:[_-][A-Za-z0-9]+)*
#     ["']?\s*[:=]\s*["']?
#     (?P<value>[A-Za-z0-9_\-+/=.]{6,})
#
# read case-insensitively and applied with finditer. That expression was
# quadratic: every underscore or hyphen in a long "a_a_a_..." run is a legal
# start, and from each start the segment loops backtrack across the rest of
# the run looking for a name. 16 KB cost 4.4 s per scan and 50 KB cost 43 s,
# and the commit gate scanned each request field five or six times.
#
# assignment_matches reads the same rule in one left-to-right pass and
# yields exactly the values finditer yielded. It rests on three facts about
# the expression:
#
# 1. The name, its prefix segments and its suffix segments are all drawn from
#    [A-Za-z0-9_-], so a match's name part lies inside one maximal run of
#    those characters. The suffix loop cannot stop early inside the run,
#    because the operator that must follow it cannot start with a run
#    character. So the name ends where the run ends, and the run end is
#    followed by the operator or there is no match.
# 2. Given the run, a match exists iff some segment (splitting the run on "_"
#    and "-") ends in an embeddable name or is exactly "key", and every
#    segment after it is non-empty. The lookbehind admits exactly the segment
#    starts, and the prefix loop can always be taken zero times from the
#    naming segment's own start.
# 3. finditer resumes at the end of the previous match's value. The value
#    class contains every run character and a value ends at a character
#    outside that class, so each run lies wholly before or wholly after that
#    point, and skipping a run that starts before it is exactly finditer's
#    non-overlap rule.
#
# Every step is a regular expression with no nested repetition applied once
# per run, so the pass is linear in the text. The expression above is kept as
# an oracle in tests/unit/test_credential_floor_every_door.py, which checks
# the two agree value for value.
_ASSIGNMENT_NAME_RUN = re.compile(r"[A-Za-z0-9_-]+", re.IGNORECASE)
_ASSIGNMENT_OPERATOR = re.compile(r"[\"']?\s*[:=]", re.IGNORECASE)
_ASSIGNMENT_VALUE = re.compile(r"\s*[\"']?(?P<value>[A-Za-z0-9_\-+/=.]{6,})", re.IGNORECASE)
_ASSIGNMENT_SEGMENT_SEPARATOR = re.compile(r"[_-]")
_SECRET_NAME_SEGMENT = re.compile(
    r"[A-Za-z0-9]*" + _SECRET_NAME_EMBEDDABLE + r"|" + _SECRET_NAME_SEGMENTED,
    re.IGNORECASE,
)


def _run_names_a_secret(run: str) -> bool:
    segments = _ASSIGNMENT_SEGMENT_SEPARATOR.split(run)
    last_empty = max((index for index, segment in enumerate(segments) if not segment), default=-1)
    return any(_SECRET_NAME_SEGMENT.fullmatch(segment) for segment in segments[last_empty + 1 :])


def assignment_matches(text: str) -> Iterator[tuple[str, str, int, int]]:
    """(name run, value, value start, run start) for every match of the old rule, in order.

    The name run is the whole [A-Za-z0-9_-] run before the operator, which
    holds the old match's name part (see the comment above).
    """

    resume_at = 0
    for run in _ASSIGNMENT_NAME_RUN.finditer(text):
        if run.start() < resume_at:
            continue
        operator = _ASSIGNMENT_OPERATOR.match(text, run.end())
        if operator is None or not _run_names_a_secret(run.group(0)):
            continue
        value = _ASSIGNMENT_VALUE.match(text, operator.end())
        if value is None:
            continue
        resume_at = value.end()
        yield run.group(0), value.group("value"), value.start("value"), run.start()


def secret_assignment_values(text: str) -> Iterator[str]:
    """Every value assigned to a secret-shaped name, in one linear pass.

    Yields what finditer over the old SECRET_ASSIGNMENT_PATTERN yielded for
    group("value"), in the same order. See the comment above for why the two
    are the same.
    """

    for _name, value, _start, _run_start in assignment_matches(text):
        yield value


def looks_like_secret_value(value: str) -> bool:
    """Tell a credential from an ordinary word sitting after a colon.

    Real credentials carry entropy: a digit, a capital, punctuation, or
    simple length. Without this, "the password= convention in the wiki"
    reads as an assignment of the secret "convention".
    """

    if len(value) >= 24:
        return True
    if any(character.isdigit() or character.isupper() for character in value):
        return True
    return any(character in "_-+/=." for character in value)


# The JWT shape, linear: trying only the first legal start in each token run
# gives the same answer as a plain search. A later start in the same run
# matches only if the first one does, because the first one's middle part can
# run through to the same dot.
_JWT_START = re.compile(r"\beyJ")
_JWT_SHAPE = re.compile(r"eyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{4,}")
_JWT_TOKEN_RUN = re.compile(r"[0-9A-Za-z_\-]+")


def _jwt_start(text: str) -> int:
    skip_until = -1
    for start in _JWT_START.finditer(text):
        position = start.start()
        if position < skip_until:
            continue
        if _JWT_SHAPE.match(text, position):
            return position
        run = _JWT_TOKEN_RUN.match(text, position)
        skip_until = run.end() if run is not None else position + 1
    return -1


# ---------------------------------------------------------------------------
# Findings.
# ---------------------------------------------------------------------------


class Finding(NamedTuple):
    """One thing v0.16.0's check would have refused.

    ``text`` is the string the rule read and ``start`` the match position in
    it. ``exact`` is False when the rule read a casefolded copy whose length
    differs from the original, so a carve-out that needs the original
    spelling cannot apply. ``name`` and ``value`` are set for the NAME=value
    rule; ``value`` holds the marker for the marker rule. ``field_name`` and
    ``field_value`` are the parts of the name and value written in the
    assignment's own field: the floor also reads its fields joined with
    nothing, where a title's last word runs into a body's first name
    ("Release" + "fact_key=..." reads as the name "Releasefact_key") and a
    value runs into the next field. The next field is read on its own as
    well, so what the cut leaves out is never unread.
    """

    rule: str
    text: str
    start: int
    name: str = ""
    value: str = ""
    exact: bool = True
    field_name: str = ""
    field_value: str = ""


def _credential_findings(
    text: str, *, require_token_boundary: bool = True, seams: tuple[int, ...] = ()
) -> Iterator[Finding]:
    """v0.16.0's _matches_credential, one finding per match.

    ``seams`` are the offsets where one field ends and the next begins, for
    the floor's surface joined with nothing.
    """

    folded = text.casefold()
    same_length = len(folded) == len(text)
    for phrase in _CREDENTIAL_PHRASES:
        index = folded.find(phrase)
        if index >= 0:
            yield Finding("phrase", folded, index)
    if require_token_boundary:
        for marker, pattern in zip(_CREDENTIAL_TOKEN_MARKERS, _CREDENTIAL_TOKEN_PATTERNS):
            for match in pattern.finditer(text):
                yield Finding("marker", text, match.start(), value=marker)
    else:
        # A collapsed run lost its boundaries, so v0.16.0 read the markers as
        # substrings of the casefolded run.
        for marker in _CREDENTIAL_TOKEN_MARKERS:
            index = folded.find(marker)
            while index >= 0:
                yield Finding("marker", text if same_length else folded, index, value=marker, exact=same_length)
                index = folded.find(marker, index + 1)
    for rule, pattern in _SEARCHED_PATTERNS[:4]:
        found = pattern.search(text)
        if found is not None:
            yield Finding(rule, text, found.start())
    jwt = _jwt_start(text)
    if jwt >= 0:
        yield Finding("jwt", text, jwt)
    for match in _SSH_PUBLIC_KEY.finditer(text):
        yield Finding("ssh_public_key", text, match.start())
    for rule, pattern in _SEARCHED_PATTERNS[4:]:
        found = pattern.search(text)
        if found is not None:
            yield Finding(rule, text, found.start())
    for name, value, start, run_start in assignment_matches(text):
        if looks_like_secret_value(value):
            field_name, field_value = name, value
            for seam in seams:
                if run_start < seam < run_start + len(name):
                    field_name = name[seam - run_start :]
                if start < seam < start + len(field_value):
                    field_value = field_value[: seam - start]
            yield Finding(
                "assignment", text, start, name=name, value=value, field_name=field_name, field_value=field_value
            )


def _decoded_variants(text: str) -> list[str]:
    """v0.16.0's base64 and hex decodings, unchanged (every token is bounded)."""

    variants: list[str] = []
    for match in _BASE64_TOKEN.finditer(text):
        token = match.group(0)
        if len(token) % 4:
            continue
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                variants.append(decoder(token).decode("utf-8"))
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
    for match in _HEX_TOKEN.finditer(text):
        token = match.group(0)
        if len(token) % 2:
            continue
        try:
            decoded = bytes.fromhex(token).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        variants.append(decoded)
    return variants


def _despaced_runs(text: str) -> list[str]:
    """v0.16.0's despacing, unchanged: each attempt is bounded by seven steps."""

    return [match.group(0).translate(_RUN_SEPARATOR_STRIP) for match in _SPACED_OUT_RUN.finditer(text)]


def _floor_findings(*texts: str | None) -> Iterator[Finding]:
    """v0.16.0's looks_like_credential: every field raw and normalised, the
    normalised fields joined with a space and with nothing, each surface
    decoded and despaced."""

    present = [text for text in texts if text]
    if not present:
        return
    normalized = [normalize_for_matching(text) for text in present]
    glued = "".join(normalized)
    surfaces = [*present, *normalized, " ".join(normalized), glued]
    seams: list[int] = []
    for text in normalized[:-1]:
        seams.append((seams[-1] if seams else 0) + len(text))
    # A repeated surface gives the same findings, so each is read once.
    for surface in dict.fromkeys(surfaces):
        yield from _credential_findings(surface, seams=tuple(seams) if surface is glued else ())
        for variant in _decoded_variants(surface):
            yield from _credential_findings(variant)
        for variant in _despaced_runs(surface):
            yield from _credential_findings(variant, require_token_boundary=False)


def _commit_field_findings(text: str) -> Iterator[Finding]:
    """v0.16.0's _contains_secret_marker, on one field.

    v0.16.0 then ran the NAME=value rule on the raw field a second time; the
    raw field is already a surface of the floor check, so that pass is not
    repeated here.
    """

    yield from _floor_findings(text)
    folded = text.casefold()
    same_length = len(folded) == len(text)
    for match in _PREFIX_SK.finditer(folded):
        yield Finding("prefix_sk", text if same_length else folded, match.start(), exact=same_length)
    for pattern in _PREFIX_PATTERNS:
        found = pattern.search(folded)
        if found is not None:
            yield Finding("prefix", folded, found.start())


# ---------------------------------------------------------------------------
# The fields each door read in v0.16.0.
# ---------------------------------------------------------------------------


def _flatten_text(value: object) -> list[str]:
    """v0.16.0's commit-gate flattening: every string, keys included, plus key=value."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        flattened: list[str] = []
        for key, item in value.items():
            if isinstance(key, str):
                flattened.append(key)
                if isinstance(item, (str, int, float)):
                    flattened.append(f"{key}={item}")
            flattened.extend(_flatten_text(item))
        return flattened
    if isinstance(value, (list, tuple)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_text(item))
        return flattened
    if value is None:
        return []
    return [str(value)]


def _source_ref_strings(refs: Iterable[object]) -> list[str]:
    """v0.16.0's promotion-floor flattening of source refs."""

    out: list[str] = []
    for ref in refs:
        if isinstance(ref, str):
            out.append(ref)
        elif isinstance(ref, Mapping):
            for key, value in ref.items():
                if isinstance(value, str):
                    out.append(f"{key}={value}")
                    out.append(value)
                elif isinstance(value, (list, tuple)):
                    out.extend(_source_ref_strings(value))
                elif isinstance(value, Mapping):
                    out.extend(_source_ref_strings((value,)))
        elif isinstance(ref, (list, tuple)):
            out.extend(_source_ref_strings(ref))
    return out


def _commit_findings(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None,
    rationale: str | None,
    source_refs: object,
) -> Iterator[Finding]:
    fields = [title, canonical_text]
    if conversation_excerpt:
        fields.append(conversation_excerpt)
    if rationale:
        fields.append(rationale)
    fields.extend(_flatten_text(source_refs))
    for field in fields:
        yield from _commit_field_findings(field)


def _floor_findings_for(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None,
    source_refs: Iterable[object],
) -> Iterator[Finding]:
    return _floor_findings(title, canonical_text, conversation_excerpt, *_source_ref_strings(source_refs))


# ---------------------------------------------------------------------------
# The carve-outs. Closed: these four and no others.
# ---------------------------------------------------------------------------

# (a) SSH public keys and key-type names. A public key is not a secret, and
# v0.16.0 refused "Prefer ssh-ed25519 keys over ssh-rsa keys" on its marker.
_SSH_KEY_TYPE_VALUES = frozenset(
    {
        "ssh-rsa", "ssh-dss", "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521", "sk-ssh-ed25519", "sk-ecdsa-sha2-nistp256",
    }
)
# Round 6 (2026-09-23): every carve-out ends at a boundary. A key type or a
# public key excused at its start let a token glued straight onto its end
# through: "sk-ssh-ed25519@openssh.com" + "ghp_...", a public key body +
# "ghp_...", "ssh-ed25519 keys" + "sk-proj-..." were all stored at commit,
# where v0.16.0 refused them. A boundary is any character that cannot extend
# a token: not a letter, digit, "_" or "-".
_BOUNDARY = r"(?![A-Za-z0-9_-])"
_AT_BOUNDARY = re.compile(_BOUNDARY)
_FIDO_KEY_TYPE = re.compile(r"sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com" + _BOUNDARY)
# What may follow the "ssh-rsa " or "ssh-ed25519 " marker: a public key body,
# a plain word ("keys"), or nothing, each ending at a boundary.
_AFTER_KEY_TYPE = re.compile(r"(?:AAAA[0-9A-Za-z+/=]*|[A-Za-z]{0,24})" + _BOUNDARY)
# A PuTTY private key file opens with "PuTTY-User-Key-File-2: ssh-rsa", which
# v0.16.0 read as NAME=value. That key type names the private key that
# follows, so it is not excused.
_PUTTY_HEADER_NAME = re.compile(r"PuTTY-User-Key-File-\d", re.IGNORECASE)


def _sk_finding(finding: Finding) -> bool:
    """An "sk-" match read on the original spelling."""

    return finding.exact and (finding.rule == "prefix_sk" or (finding.rule == "marker" and finding.value == "sk-"))


def carve_out_ssh_public_key(finding: Finding) -> bool:
    if finding.rule == "ssh_public_key":
        key = _SSH_PUBLIC_KEY.match(finding.text, finding.start)
        return key is not None and _AT_BOUNDARY.match(finding.text, key.end()) is not None
    if finding.rule == "marker" and finding.value in ("ssh-rsa ", "ssh-ed25519 "):
        return _AFTER_KEY_TYPE.match(finding.text, finding.start + len(finding.value)) is not None
    if _sk_finding(finding):
        return _FIDO_KEY_TYPE.match(finding.text, finding.start) is not None
    # A key type as a NAME=value value ends where v0.16.0's value class ends,
    # which is a boundary. A FIDO value stops at its "@", but the "sk-" marker
    # always matches at the same place (an operator, a space or a quote
    # precedes it), and that finding needs the whole bounded FIDO name.
    return (
        finding.rule == "assignment"
        and (finding.field_value or finding.value) in _SSH_KEY_TYPE_VALUES
        and _PUTTY_HEADER_NAME.search(finding.name) is None
    )


# (b) "sk-" and lower-case words: sk-learn, sk-learn-bench-2024. A key after
# "sk-" is long and mixed, so no segment of it is a short lower-case word.
_SK_WORD_CHAIN = re.compile(r"sk-[a-z]{2,12}(?:-(?:[a-z]{2,12}|[0-9]{1,4})){0,3}(?![A-Za-z0-9_-])")


def carve_out_sk_word_chain(finding: Finding) -> bool:
    return _sk_finding(finding) and _SK_WORD_CHAIN.match(finding.text, finding.start) is not None


# (c) Structural key names with identifier values. The names are the design
# round's: fact_key and the continuity subject keys, cache_key,
# partition_key, sort_key, next_page_token, memory_key, and dedupe keys
# (dedupe_key, dedup_key, any *_dedupe_key, idempotency_key). A name is read
# in snake case, so nextPageToken and Idempotency-Key are the same names.
_STRUCTURAL_KEY_NAMES = frozenset(
    {
        "fact_key", "preference_key", "state_key", "status_key", "decision_key", "commitment_key",
        "waiting_for_key", "blocker_key", "action_key", "subject_key", "cache_key", "partition_key",
        "sort_key", "next_page_token", "memory_key", "dedupe_key", "dedup_key", "idempotency_key",
    }
)
_DEDUPE_KEY_NAME = re.compile(r"(?:[a-z]+_)+dedupe?_key")
_PAGE_TOKEN_NAMES = frozenset({"next_page_token"})
_NAME_HUMP = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[0-9]+")
# An identifier: segments split on . _ : - that are each a lower-case word,
# a short number, a letter and a digit or two ("v2"), or a hex group (UUIDs,
# digests). Upper and mixed case, "/", "+" and "=" are not identifier text.
_IDENTIFIER_SPLIT = re.compile(r"[._:-]")
_IDENTIFIER_SEGMENT = re.compile(r"[a-z]{1,24}|[0-9]{1,8}|[a-z][0-9]{1,3}|[0-9a-f]{4,64}|[0-9A-F]{4,64}")
# A page token is an opaque cursor by design, so its value may be opaque,
# up to 128 characters of base64 text, but not one holding a token prefix
# v0.16.0 knew (round 6: a cursor with "ghp_..." glued on was excused).
_PAGE_CURSOR = re.compile(r"[A-Za-z0-9+/_=-]{1,128}")
_CURSOR_FORBIDDEN = tuple(
    marker for marker in _CREDENTIAL_TOKEN_MARKERS if not marker.startswith("ssh-")
) + ("akia", "asia", "aiza")


def _snake_name(run: str) -> str | None:
    pieces = _ASSIGNMENT_SEGMENT_SEPARATOR.split(run)
    if not all(pieces):
        return None
    return "_".join(hump.lower() for piece in pieces for hump in _NAME_HUMP.findall(piece))


def _identifier_value(value: str) -> bool:
    return all(
        segment and _IDENTIFIER_SEGMENT.fullmatch(segment) for segment in _IDENTIFIER_SPLIT.split(value)
    )


def carve_out_structural_key_name(finding: Finding) -> bool:
    if finding.rule != "assignment":
        return False
    name = _snake_name(finding.field_name or finding.name)
    if name is None or not (name in _STRUCTURAL_KEY_NAMES or _DEDUPE_KEY_NAME.fullmatch(name)):
        return False
    value = finding.field_value or finding.value
    if _identifier_value(value):
        return True
    if name not in _PAGE_TOKEN_NAMES or _PAGE_CURSOR.fullmatch(value) is None:
        return False
    folded = value.casefold()
    return not any(marker in folded for marker in _CURSOR_FORBIDDEN)


# (d) A dotted reference to where the secret lives, rooted at a known code
# object: api_key = settings.OPENAI_API_KEY, token: secrets.GITHUB_TOKEN,
# api_key=os.environ['X'] (read up to the bracket, as v0.16.0 read it).
# Each attribute after the root is a name as people write one: snake case
# or upper snake case of words each ending in at most three digits, or
# camel or Pascal case of letter words. A random run of mixed letters and
# digits is not an attribute name (round 6: settings.<random> was excused).
_CODE_ROOT = re.compile(r"(?:settings|config|self|secrets|ENV)(?=\.)|process\.env|os\.environ")
_CODE_WORD_LOWER = r"[a-z]+[0-9]{0,3}"
_CODE_WORD_UPPER = r"[A-Z]+[0-9]{0,3}"
_CODE_ATTRIBUTE = re.compile(
    r"_{0,2}(?:"
    + _CODE_WORD_LOWER + r"(?:_" + _CODE_WORD_LOWER + r")*"
    + r"|" + _CODE_WORD_UPPER + r"(?:_" + _CODE_WORD_UPPER + r")*"
    + r"|[a-z]+(?:[A-Z][a-z]+)+[0-9]{0,3}"
    + r"|(?:[A-Z][a-z]+)+[0-9]{0,3}"
    + r")_{0,2}"
)


def carve_out_code_reference(finding: Finding) -> bool:
    if finding.rule != "assignment":
        return False
    value = finding.field_value or finding.value
    root = _CODE_ROOT.match(value)
    if root is None:
        return False
    rest = value[root.end() :]
    if not rest:
        return True  # process.env or os.environ, read up to a bracket
    return rest.startswith(".") and all(
        _CODE_ATTRIBUTE.fullmatch(attribute) is not None for attribute in rest[1:].split(".")
    )


CARVE_OUTS = (
    carve_out_ssh_public_key,
    carve_out_sk_word_chain,
    carve_out_structural_key_name,
    carve_out_code_reference,
)


def password_kind(finding: Finding) -> bool:
    """A NAME=value finding whose name holds a password word. No carve-out applies."""

    return finding.rule == "assignment" and "passw" in finding.name.casefold()


def excused(finding: Finding) -> bool:
    if password_kind(finding):
        return False
    return any(carve_out(finding) for carve_out in CARVE_OUTS)


# ---------------------------------------------------------------------------
# The two doors.
# ---------------------------------------------------------------------------


def v0160_commit_gate_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    rationale: str | None = None,
    source_refs: object = (),
) -> bool:
    """What v0.16.0's commit gate decided, carve-outs not applied."""

    return next(_commit_findings(title, canonical_text, conversation_excerpt, rationale, source_refs), None) is not None


def v0160_promotion_floor_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    source_refs: Iterable[object] = (),
) -> bool:
    """What v0.16.0's promotion floor decided, carve-outs not applied."""

    return next(_floor_findings_for(title, canonical_text, conversation_excerpt, source_refs), None) is not None


def commit_gate_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    rationale: str | None = None,
    source_refs: object = (),
) -> bool:
    """v0.16.0's commit check less the carve-outs: True on the first finding none excuses."""

    return any(
        not excused(finding)
        for finding in _commit_findings(title, canonical_text, conversation_excerpt, rationale, source_refs)
    )


def promotion_floor_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    source_refs: Iterable[object] = (),
) -> bool:
    """v0.16.0's floor check less the carve-outs."""

    return any(
        not excused(finding)
        for finding in _floor_findings_for(title, canonical_text, conversation_excerpt, source_refs)
    )


__all__ = [
    "CARVE_OUTS",
    "Finding",
    "assignment_matches",
    "carve_out_code_reference",
    "carve_out_sk_word_chain",
    "carve_out_ssh_public_key",
    "carve_out_structural_key_name",
    "commit_gate_refuses",
    "excused",
    "looks_like_secret_value",
    "password_kind",
    "promotion_floor_refuses",
    "secret_assignment_values",
    "v0160_commit_gate_refuses",
    "v0160_promotion_floor_refuses",
]
