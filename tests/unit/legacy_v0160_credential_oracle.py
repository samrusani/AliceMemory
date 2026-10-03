"""v0.16.0's credential checks, copied verbatim as a test oracle (S4.4 round 5).

Defect this guards against: S4.4 round 5 (2026-09-23) put v0.16.0's own
check back at the two doors v0.16.0 checked, re-implemented in linear time
(alicebot_api.legacy_credential_check), because a third adversary found the
branch storing eight credential shapes v0.16.0 refused at the commit door.
A re-implementation can drift from what it re-implements without any test
noticing, which is how the round 2 floor lost the OpenSSH key. This module is
the reference it is compared against.

Every definition below is copied line for line from commit 880915a (v0.16.0):
vnext_promotion_policy.py for the promotion floor's looks_like_credential and
its helpers, vnext_memory_commit.py for the commit gate's prefix patterns,
_contains_secret_marker and _flatten_text. Only the two functions at the end
are new: they call the copied code the way v0.16.0's two doors called it.

Do not edit the copied code, and never run it on long input: its assignment
and JWT expressions are quadratic (the assignment rule alone took 4.3 s on
16,000 characters of "a_a_..." and over 30 s on "key_key_...").
test_legacy_credential_check.py pins this file's patterns against the
running module so a later edit to either shows up.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
import re
import unicodedata

_CREDENTIAL_PHRASES = (
    "begin openssh private key",
    "begin private key",
    "begin rsa private key",
)

# Token prefixes and key names. These MUST start at a token boundary. Matching
# them as bare substrings gated "task-list" and "risk-based testing" on the
# "sk-" fragment, which is a false positive on the unconfigurable tier and so
# unrelievable by any setting. "akia" is absent entirely: the AWS key id has a
# precise shape and _CREDENTIAL_PATTERNS carries it, so the bare acronym no
# longer gates a sentence that merely mentions it.
# Only prefixes that are themselves the start of a key. A key NAME with no
# key after it ("the api_key rotation policy is quarterly", "the access_token
# lifetime is fifteen minutes") is a sentence about credentials, not a
# credential, and it was landing on the unconfigurable tier. Names are still
# caught with a value attached, by the assignment and prose patterns below.
_CREDENTIAL_TOKEN_MARKERS = (
    "ghp_",
    "gho_",
    "ghs_",
    "ghu_",
    "github_pat_",
    "glpat-",
    "npm_",
    # "password=" is deliberately NOT here. It is a credential NAME with no
    # value, and as a bare marker it floored "her password= convention in the
    # wiki is outdated" on the unconfigurable tier. Every form that carries an
    # actual value is caught by SECRET_ASSIGNMENT_PATTERN, which reads the
    # value rather than counting characters after the sign.
    "sk-",
    "ssh-rsa ",
    "ssh-ed25519 ",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "xoxs-",
)
# A token boundary is the start of the text or any character that is not a
# letter, digit or underscore. Hyphen counts as a boundary so "sk-live-..."
# matches while "task-list" does not.
_CREDENTIAL_TOKEN_PATTERNS = tuple(
    re.compile(r"(?<![0-9A-Za-z_])" + re.escape(marker), re.IGNORECASE)
    for marker in _CREDENTIAL_TOKEN_MARKERS
)

_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Case-insensitive but shape-exact: the full 20 character key id is
    # caught however it was transcribed, while a sentence that merely
    # mentions the AKIA acronym is not.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Za-z]{16}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"\beyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{4,}"),
    re.compile(r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]{20,}"),
    re.compile(r"\bBearer\s+[0-9A-Za-z._\-]{16,}"),
    # The key = value shape is NOT here. It needs its value inspected rather
    # than merely counted, so it lives in SECRET_ASSIGNMENT_PATTERN below and
    # is applied through looks_like_secret_value.
    #
    # Prose form: "the password for the vault is hunter2hunter2". The value
    # must carry a digit or a symbol, so "the api key rotation policy is
    # quarterly" is a sentence about a policy rather than a disclosed secret.
    re.compile(
        r"\b(?:password|passphrase|api\s+key|access\s+key|secret\s+key|private\s+key"
        r"|auth\s+token|access\s+token|credential)\b[^.\n]{0,24}?\b(?:is|was|are|were)\s+"
        r"(?=\S{6,})\S*[0-9!@#$%^&*_+=/\\-]\S*",
        re.IGNORECASE,
    ),
)

# The assignment rule, shared with the memory-commit reject path.
#
# This module owns it because the reject path already imports from here and
# the reverse would be a cycle. Two implementations of "is this an
# assignment of a secret" would drift, and the round 7 branch proved that in
# miniature: the floor and the reject path disagreed on twelve shapes.
#
# "password" or "secret" embedded in a longer word is still a credential
# name, as in PGPASSWORD. "key" is not: monkey, turkey and keyboard all
# contain it, so it only counts as a whole underscore or hyphen separated
# segment.
_SECRET_NAME_EMBEDDABLE = r"(?:password|passwd|secret|token|credentials?|apikey)"
_SECRET_NAME_SEGMENTED = r"key"

SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?:[A-Za-z0-9]+[_-])*"
    r"(?:[A-Za-z0-9]*" + _SECRET_NAME_EMBEDDABLE + r"|" + _SECRET_NAME_SEGMENTED + r")"
    r"(?:[_-][A-Za-z0-9]+)*"
    r"[\"']?\s*[:=]\s*[\"']?"
    r"(?P<value>[A-Za-z0-9_\-+/=.]{6,})",
    re.IGNORECASE,
)


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


def _matches_secret_assignment(text: str) -> bool:
    return any(looks_like_secret_value(match.group("value")) for match in SECRET_ASSIGNMENT_PATTERN.finditer(text))


_BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{16,512}={0,2}(?![A-Za-z0-9+/_=-])")
_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{24,512}\b")
# A run of at least eight single characters held apart by whitespace or light
# punctuation is the classic "s k - a b c d e" evasion and is vanishingly rare
# in prose. The separator class deliberately excludes "-" and "_", which are
# part of real credential prefixes and must survive the collapse.
_RUN_SEPARATORS = " \t\r\n.·,;|/"
_SPACED_OUT_RUN = re.compile(r"(?:\S[" + re.escape(_RUN_SEPARATORS) + r"]{1,3}){7,}\S")
_RUN_SEPARATOR_STRIP = str.maketrans({char: None for char in _RUN_SEPARATORS})


_INVISIBLE_CATEGORIES = frozenset({"Cf", "Mn", "Me"})

# Cyrillic and Greek letters that render identically to a Latin letter. NFKD
# does not touch them, because they are separate letters rather than
# compatibility variants, so "аgent_output" with a Cyrillic a survives every
# unicode normalisation and still reads as the ASCII token to a human.
_CONFUSABLE_PAIRS = (
    # Cyrillic
    ("а", "a"), ("е", "e"), ("о", "o"), ("р", "p"), ("с", "c"), ("у", "y"),
    ("х", "x"), ("і", "i"), ("ј", "j"), ("ѕ", "s"), ("һ", "h"), ("ԁ", "d"),
    ("к", "k"), ("м", "m"), ("т", "t"), ("в", "b"), ("н", "h"), ("г", "r"),
    ("А", "A"), ("В", "B"), ("Е", "E"), ("К", "K"), ("М", "M"), ("Н", "H"),
    ("О", "O"), ("Р", "P"), ("С", "C"), ("Т", "T"), ("У", "Y"), ("Х", "X"),
    ("І", "I"), ("Ј", "J"), ("Ѕ", "S"),
    # Greek
    ("α", "a"), ("ε", "e"), ("ο", "o"), ("ρ", "p"), ("ι", "i"), ("κ", "k"),
    ("ν", "v"), ("τ", "t"), ("υ", "u"), ("χ", "x"), ("ϲ", "c"), ("ϳ", "j"),
    ("Α", "A"), ("Β", "B"), ("Ε", "E"), ("Ζ", "Z"), ("Η", "H"), ("Ι", "I"),
    ("Κ", "K"), ("Μ", "M"), ("Ν", "N"), ("Ο", "O"), ("Ρ", "P"), ("Τ", "T"),
    ("Υ", "Y"), ("Χ", "X"),
    # Latin extended and IPA lookalikes
    ("ɡ", "g"), ("ɑ", "a"), ("ɩ", "i"), ("ɪ", "i"), ("ʏ", "y"), ("ʙ", "b"),
    ("ᴄ", "c"), ("ᴅ", "d"), ("ᴇ", "e"), ("ᴋ", "k"), ("ᴍ", "m"), ("ᴏ", "o"),
    ("ᴘ", "p"), ("ᴛ", "t"), ("ᴜ", "u"), ("ᴠ", "v"), ("ᴢ", "z"),
    ("ı", "i"), ("ȷ", "j"), ("ɫ", "l"), ("ɵ", "o"), ("ʂ", "s"), ("ʐ", "z"),
    # Armenian
    ("ո", "n"), ("օ", "o"), ("ա", "w"), ("տ", "un"), ("ց", "g"), ("հ", "h"),
    ("ս", "u"), ("զ", "q"), ("Օ", "O"), ("Ս", "U"),
    # Other scripts with single-letter lookalikes
    ("ᏼ", "B"), ("Ꭺ", "A"), ("Ꮯ", "C"), ("Ꭼ", "E"), ("Ꮋ", "H"), ("Ꭻ", "J"),
    ("Ꮶ", "K"), ("Ꮮ", "L"), ("Ꮇ", "M"), ("Ꮲ", "P"), ("Ꮪ", "S"), ("Ꮩ", "V"),
    ("Ꮃ", "W"), ("Ꭹ", "Y"), ("Ꮓ", "Z"),
)


def _build_confusable_table() -> dict[int, str]:
    """Hand pairs, plus every character Unicode names as a plain ASCII letter.

    The name sweep is what stops this being purely a list of the characters
    that broke the last review. Anything whose Unicode name ends in
    ``LETTER <X>`` for a single ASCII letter maps to that letter, which picks
    up fullwidth, circled, mathematical and small-capital variants across
    every script without enumerating them. The hand pairs cover the rest,
    where the Unicode name gives no single-letter equivalence (Cyrillic ``а``
    is ``CYRILLIC SMALL LETTER A``, which the sweep does catch, but Greek
    ``ρ`` is ``GREEK SMALL LETTER RHO``, which it does not).

    Still incomplete by construction: the correct long-term source is the
    Unicode confusables data file, and a test records which of the reviewer's
    probes this catches and which it does not.
    """

    table: dict[int, str] = {}
    for codepoint in range(0x80, 0x2E80):
        char = chr(codepoint)
        if unicodedata.category(char) not in {"Ll", "Lu", "Lo", "Lm"}:
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        head, _, tail = name.rpartition("LETTER ")
        if not head or len(tail) != 1 or not tail.isascii() or not tail.isalpha():
            continue
        table[codepoint] = tail if name.split()[1] != "SMALL" else tail.lower()
    for source, target in _CONFUSABLE_PAIRS:
        table[ord(source)] = target
    return table


_CONFUSABLE_TABLE = _build_confusable_table()


def normalize_for_matching(text: str) -> str:
    """Canonicalise text so invisible and lookalike characters cannot hide."""

    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if unicodedata.category(char) not in _INVISIBLE_CATEGORIES)
    return unicodedata.normalize("NFKC", stripped).translate(_CONFUSABLE_TABLE)


def _decoded_variants(text: str) -> list[str]:
    """Base64 and hex decodings of long opaque tokens found in the text.

    Bounded and deterministic: only tokens between 16 and 512 characters are
    decoded, and only into UTF-8 text that decodes cleanly.
    """

    variants: list[str] = []
    for match in _BASE64_TOKEN.finditer(text):
        token = match.group(0)
        if len(token) % 4:
            continue
        # Standard and URL-safe alphabets are two spellings of one encoding,
        # so both decoders run rather than only the one the corpus happened
        # to exercise.
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
    """Collapse "s k - a b c d e" style runs back into single tokens.

    Any whitespace or light punctuation counts as the separator, so two
    spaces, a newline or a dot between characters collapse the same way one
    space does.
    """

    return [match.group(0).translate(_RUN_SEPARATOR_STRIP) for match in _SPACED_OUT_RUN.finditer(text)]


def _matches_credential(text: str, *, require_token_boundary: bool = True) -> bool:
    folded = text.casefold()
    if any(phrase in folded for phrase in _CREDENTIAL_PHRASES):
        return True
    if require_token_boundary:
        if any(pattern.search(text) for pattern in _CREDENTIAL_TOKEN_PATTERNS):
            return True
    elif any(marker in folded for marker in _CREDENTIAL_TOKEN_MARKERS):
        # Collapsing a spaced-out run destroys the token boundaries that made
        # the marker readable in the first place, so the collapsed form is
        # matched as a substring. Only genuinely spread-out runs reach here,
        # which prose does not produce.
        return True
    if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS):
        return True
    return _matches_secret_assignment(text)


def looks_like_credential(*texts: str | None) -> bool:
    """True when the supplied texts carry credential-shaped material.

    Every field is checked on its own, joined in reading order with a space,
    and joined with no separator at all. The empty join is what catches a
    marker split mid-token across a title and a body ("gh" + "p_0123..."),
    which the space join reassembles as two words and therefore misses.

    Each surface is checked as written and after unicode normalisation. Long
    base64 (standard and URL-safe) and hex tokens are decoded and rechecked,
    and character-spaced runs are collapsed, so a wrapped or spread-out key
    does not read as opaque noise.
    """

    present = [text for text in texts if text]
    if not present:
        return False
    normalized = [normalize_for_matching(text) for text in present]
    surfaces = [
        *present,
        *normalized,
        " ".join(normalized),
        "".join(normalized),
    ]
    for text in surfaces:
        if _matches_credential(text):
            return True
        for variant in _decoded_variants(text):
            if _matches_credential(variant):
                return True
        for variant in _despaced_runs(text):
            if _matches_credential(variant, require_token_boundary=False):
                return True
    return False


def _source_ref_strings(refs: Iterable[object]) -> list[str]:
    """Flatten heterogeneous source refs into inspectable strings."""

    out: list[str] = []
    for ref in refs:
        if isinstance(ref, str):
            out.append(ref)
        elif isinstance(ref, Mapping):
            for key, value in ref.items():
                if isinstance(value, str):
                    # Both shapes matter: "generated_by=agent" is a flag, and
                    # the bare value carries prefixes like "agent_run:...".
                    out.append(f"{key}={value}")
                    out.append(value)
                elif isinstance(value, (list, tuple)):
                    out.extend(_source_ref_strings(value))
                elif isinstance(value, Mapping):
                    out.extend(_source_ref_strings((value,)))
        elif isinstance(ref, (list, tuple)):
            out.extend(_source_ref_strings(ref))
    return out


SECRET_PREFIX_PATTERNS = (
    re.compile(r"(?<![0-9a-z])sk-[0-9a-z_-]{8,}"),
    re.compile(r"(?<![0-9a-z])ghp_[0-9a-z]{8,}"),
    re.compile(r"(?<![0-9a-z])xoxb-[0-9a-z-]{8,}"),
    re.compile(r"(?<![0-9a-z])akia[0-9a-z]{12,}"),
    re.compile(r"-----begin(?: [a-z]+)* private key-----"),
)


def _contains_secret_marker(text: str) -> bool:
    """Whether one text carries credential-shaped material.

    Delegates to the promotion floor's detector so the two guards cannot
    drift apart. They had: measured on eleven real credential shapes, this
    path caught four and the floor caught ten, so six genuine secrets were
    accepted outright by the guard whose job is to refuse them, and on an
    unconfigured deployment the floor never runs to catch them. The floor
    also normalises, which this path did not, so a zero-width space or a
    fullwidth "s" defeated it.

    The prefix patterns and the assignment rule are still consulted after it,
    so this stays a strict superset rather than a replacement whose coverage
    has to be argued. The two are complementary, not redundant: the floor
    normalises and decodes, which catches unicode, zero-width and fullwidth
    defeats; the assignment rule reads a secret-shaped name followed by a
    real value, which catches X_API_TOKEN= and PGPASSWORD=. Neither catches
    the other's set.
    """

    if looks_like_credential(text):
        return True
    folded = text.casefold()
    if any(pattern.search(folded) for pattern in SECRET_PREFIX_PATTERNS):
        return True
    return any(
        looks_like_secret_value(match.group("value"))
        for match in SECRET_ASSIGNMENT_PATTERN.finditer(text)
    )


def _flatten_text(value: object) -> list[str]:
    """Every string reachable inside a caller-supplied structure, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        flattened: list[str] = []
        for key, item in value.items():
            if isinstance(key, str):
                flattened.append(key)
                # Keep the pair adjacent as well. A credential is recognised by
                # a secret-shaped name next to its value, and flattening a
                # mapping into separate strings would break exactly that.
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


# ---------------------------------------------------------------------------
# The two doors, called the way v0.16.0 called them.
# ---------------------------------------------------------------------------


def commit_gate_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    rationale: str | None = None,
    source_refs: object = (),
) -> bool:
    """vnext_memory_commit._request_contains_secret_marker at 880915a, on fields."""

    fields = [title, canonical_text]
    if conversation_excerpt:
        fields.append(conversation_excerpt)
    if rationale:
        fields.append(rationale)
    fields.extend(_flatten_text(source_refs))
    return any(_contains_secret_marker(value) for value in fields)


def promotion_floor_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    source_refs: Iterable[object] = (),
) -> bool:
    """The credential clause of vnext_promotion_policy.hard_floor_hits at 880915a."""

    return looks_like_credential(title, canonical_text, conversation_excerpt, *_source_ref_strings(source_refs))
