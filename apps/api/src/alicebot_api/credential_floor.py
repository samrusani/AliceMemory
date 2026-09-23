"""The credential floor: one check that the memory write paths call.

Why it exists. Until 2026-09-22 credential detection ran in two places that
disagreed, and several write paths called neither (S4.4, doors 2 to 7 of the
2026-09-03 dump). The first shared check (41faa9a) joined every field in
reading order and paired every mapping key with its value; the review of it
found that this refused honest exports and structural bodies such as
{"fact_key": "release_mode"}, and still missed a key split across a title and
a mapping body. This module is the port of the design round's ruling,
"precision-first plus keyed pairs" (S4.4 round 2, 2026-09-23).

The design, in one paragraph. A credential is recognised by its value, not
by a join of unrelated text. Three things are read:

1. Each field on its own (every mapping value and every mapping key, each
   alone), raw and unicode-normalised, plus base64/hex decodings and
   despaced runs: self-identifying token shapes that need real key material
   after the prefix, the AWS id, the Google key, a PEM header with a body, an
   SSH key with a body, Bearer with an opaque token, a JWT, the prose rule
   ("the password for X is <value>"), a password inside a URL, and a
   NAME=value scanner whose name must be a secret name and whose value must
   be secret-shaped.
2. Each (key, value) pair of a mapping, alone and never joined: the key must
   be a secret name under the same grammar and the value secret-shaped.
3. Across fields, only case-exact, high-specificity formats, on two
   sequences (values only, and keys interleaved with values) joined with
   nothing. A derived copy (16 or more characters, equal to an earlier text or
   a prefix of one of the four texts before it) breaks the seam chain, so a
   text's end is never read against its own start.

Nothing else crosses a seam: no space join, and no assignment, prose, Bearer
or URL rule. The residuals this leaves are listed in
docs/memory/promotion-personas.md.

Every scan is a regular expression without nested unbounded repetition, run
over a single string per surface, so one call is linear in the input. The
one exception is unicode normalisation, which can expand a character up to
eighteen times; a field whose normalised form is more than four times its
source length (and over 1,024 characters) is refused with its own message
rather than scanned or truncated.

Callers pass the row AS IT WILL BE STORED, in reading order: title, body,
other persisted free text, structured values (mapping bodies as mappings),
then persisted identifiers. One call per row; never two rows in one call.
Provenance is always passed through string_values, by value only.

``refuse_credential_material`` raises the caller's own validation error, so
each surface keeps its existing error contract.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
import re
import unicodedata

from alicebot_api.agent_key_format import AGENT_KEY_PREFIX


CREDENTIAL_MATERIAL_REFUSED_MESSAGE = (
    "credential material cannot be written to memory; remove the secret and retry"
)
EXPANSION_REFUSED_MESSAGE = (
    "text that expands more than four times under unicode normalisation cannot be checked "
    "for credential material; remove the expanding characters and retry"
)

VERDICT_CREDENTIAL = "credential"
VERDICT_EXPANSION = "expansion"

# The expansion cap (S4.4 round 2, ruling R3). NFKD can turn one character
# into eighteen (U+FDFA). Real scripts measured at most 1.44x. A field whose
# normalised form is over four times its source is refused, never truncated.
# The floor keeps a single short field such as one ligature from being
# refused: under it the expansion is a constant, not a multiplier.
MAX_NORMALIZATION_EXPANSION = 4
EXPANSION_FLOOR_CHARS = 1_024

# ---------------------------------------------------------------------------
# Normalisation. The same result as vnext_promotion_policy.normalize_for_matching,
# with an ASCII fast path and the per-character category loop replaced by one
# str.translate. Each distinct text is normalised once per call.
# ---------------------------------------------------------------------------

_CONFUSABLE_PAIRS = (
    ("а", "a"), ("е", "e"), ("о", "o"), ("р", "p"), ("с", "c"), ("у", "y"),
    ("х", "x"), ("і", "i"), ("ј", "j"), ("ѕ", "s"), ("һ", "h"), ("ԁ", "d"),
    ("к", "k"), ("м", "m"), ("т", "t"), ("в", "b"), ("н", "h"), ("г", "r"),
    ("А", "A"), ("В", "B"), ("Е", "E"), ("К", "K"), ("М", "M"), ("Н", "H"),
    ("О", "O"), ("Р", "P"), ("С", "C"), ("Т", "T"), ("У", "Y"), ("Х", "X"),
    ("І", "I"), ("Ј", "J"), ("Ѕ", "S"),
    ("α", "a"), ("ε", "e"), ("ο", "o"), ("ρ", "p"), ("ι", "i"), ("κ", "k"),
    ("ν", "v"), ("τ", "t"), ("υ", "u"), ("χ", "x"), ("ϲ", "c"), ("ϳ", "j"),
    ("Α", "A"), ("Β", "B"), ("Ε", "E"), ("Ζ", "Z"), ("Η", "H"), ("Ι", "I"),
    ("Κ", "K"), ("Μ", "M"), ("Ν", "N"), ("Ο", "O"), ("Ρ", "P"), ("Τ", "T"),
    ("Υ", "Y"), ("Χ", "X"),
    ("ɡ", "g"), ("ɑ", "a"), ("ɩ", "i"), ("ɪ", "i"), ("ʏ", "y"), ("ʙ", "b"),
    ("ᴄ", "c"), ("ᴅ", "d"), ("ᴇ", "e"), ("ᴋ", "k"), ("ᴍ", "m"), ("ᴏ", "o"),
    ("ᴘ", "p"), ("ᴛ", "t"), ("ᴜ", "u"), ("ᴠ", "v"), ("ᴢ", "z"),
    ("ı", "i"), ("ȷ", "j"), ("ɫ", "l"), ("ɵ", "o"), ("ʂ", "s"), ("ʐ", "z"),
    ("ո", "n"), ("օ", "o"), ("ա", "w"), ("տ", "un"), ("ց", "g"), ("հ", "h"),
    ("ս", "u"), ("զ", "q"), ("Օ", "O"), ("Ս", "U"),
    ("ᏼ", "B"), ("Ꭺ", "A"), ("Ꮯ", "C"), ("Ꭼ", "E"), ("Ꮋ", "H"), ("Ꭻ", "J"),
    ("Ꮶ", "K"), ("Ꮮ", "L"), ("Ꮇ", "M"), ("Ꮲ", "P"), ("Ꮪ", "S"), ("Ꮩ", "V"),
    ("Ꮃ", "W"), ("Ꭹ", "Y"), ("Ꮓ", "Z"),
)


def _build_confusable_table() -> dict[int, str]:
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
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
_INVISIBLE_TABLE: dict[int, None] | None = None


def _invisible_table() -> dict[int, None]:
    global _INVISIBLE_TABLE
    if _INVISIBLE_TABLE is None:
        _INVISIBLE_TABLE = {
            cp: None for cp in range(0x80, 0x110000) if unicodedata.category(chr(cp)) in _INVISIBLE_CATEGORIES
        }
    return _INVISIBLE_TABLE


def normalize_for_matching(text: str) -> str:
    if text.isascii():
        return text
    decomposed = unicodedata.normalize("NFKD", text).translate(_invisible_table())
    return unicodedata.normalize("NFKC", decomposed).translate(_CONFUSABLE_TABLE)


# ---------------------------------------------------------------------------
# Value tests.
# ---------------------------------------------------------------------------

_DIGIT = re.compile(r"[0-9]")
_INNER_UPPER = re.compile(r"[a-z0-9][A-Z]")
_SEGMENT_SPLIT = re.compile(r"[-_./]+")
_PLACEHOLDER = re.compile(r"[xX]{6,}|\*{3,}|(.)\1{7,}")


def _word_segment(segment: str) -> bool:
    if segment.isalpha():
        return len(segment) <= 12
    if segment.isdigit():
        return len(segment) <= 4
    return len(segment) <= 3


def _word_chain(material: str) -> bool:
    """Short words joined by - _ . or /, as in "learn-intelex" or "user-profile-v2"."""

    segments = [segment for segment in _SEGMENT_SPLIT.split(material) if segment]
    return len(segments) >= 2 and all(_word_segment(segment) for segment in segments)


def _opaque(material: str, min_len: int = 8, long_len: int = 12) -> bool:
    """Key material after a known prefix: long enough, not a word, not a placeholder."""

    if len(material) < min_len or _PLACEHOLDER.search(material) or _word_chain(material):
        return False
    return bool(_DIGIT.search(material) or _INNER_UPPER.search(material) or len(material) >= long_len)


def _not_placeholder(material: str) -> bool:
    return not _PLACEHOLDER.search(material)


def _always(_material: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# Per-field token shapes: (triggers, bounded regex, boundary-free regex,
# validator on group 1). Triggers are casefolded literals, one of which every
# match contains; a text without any trigger is skipped. The boundary-free
# form is used only on despaced runs, where collapsing destroyed the boundary.
# ---------------------------------------------------------------------------

_I = re.IGNORECASE
_Shape = tuple[tuple[str, ...], "re.Pattern[str]", "re.Pattern[str]", Callable[[str], bool]]


def _shape(
    prefix: str,
    body: str,
    lead: str,
    flags: int,
    validator: Callable[[str], bool],
    triggers: tuple[str, ...],
) -> _Shape:
    bounded = re.compile(lead + prefix + "(" + body + ")", flags)
    free = re.compile(prefix + "(" + body + ")", flags)
    return triggers, bounded, free, validator


# SSH public keys are not refused (addendum F2, 2026-09-23). A public key is
# published on purpose; until this change every door refused one, which made
# "add this deploy key to the server" unrememberable. It shipped only once
# the private-key body fix (F4) and the promotion-floor unification (F1)
# landed in the same port, so no door and no floor disagrees about it. A
# private key pasted next to its public half is still refused, by the PEM
# rule. The FIDO key types start with "sk-", so the sk- token shape skips
# exactly those two names rather than reading them as an OpenAI-style key.
_FIDO_KEY_TYPE = r"(?!ssh-ed25519@openssh\.com|ecdsa-sha2-nistp(?:256|384|521)@openssh\.com)"

_LEAD = r"(?<![0-9A-Za-z])"
_LEAD_US = r"(?<![0-9A-Za-z_])"

_PREFIX_SHAPES: tuple[_Shape, ...] = (
    # OpenAI / Anthropic style. Case-exact: an upper-case "SK-" is a part
    # number, and "sk-learn" has too little material to be a key.
    _shape(r"sk-" + _FIDO_KEY_TYPE, r"[0-9A-Za-z_-]+", _LEAD, 0, lambda m: _opaque(m, 8, 12), ("sk-",)),
    # Stripe secret and restricted keys (owner ruling C3), and webhook secrets.
    _shape(r"[sr]k_(?:live|test)_", r"[0-9A-Za-z]+", _LEAD, _I, lambda m: _opaque(m, 8, 12), ("k_live_", "k_test_")),
    _shape(r"whsec_", r"[0-9A-Za-z]+", _LEAD, _I, lambda m: _opaque(m, 16, 24), ("whsec_",)),
    # GitHub.
    _shape(
        r"gh[pousr]_",
        r"[0-9A-Za-z]+",
        _LEAD_US,
        _I,
        lambda m: _opaque(m, 8, 12),
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    ),
    _shape(r"github_pat_", r"[0-9A-Za-z_]+", _LEAD_US, _I, lambda m: _opaque(m, 16, 20), ("github_pat_",)),
    # GitLab, npm.
    _shape(r"glpat-", r"[0-9A-Za-z_-]+", _LEAD_US, _I, lambda m: _opaque(m, 8, 12), ("glpat-",)),
    _shape(r"npm_", r"[0-9A-Za-z]+", _LEAD_US, _I, lambda m: _opaque(m, 16, 24), ("npm_",)),
    # Slack.
    # Case-exact, and the token carries digits: "xoxo-SarahAndMike" is a
    # sign-off (adversary review, round 3).
    _shape(r"xox[baprs]-", r"[0-9A-Za-z-]+", _LEAD, 0, lambda m: _opaque(m, 8, 12) and bool(_DIGIT.search(m)), ("xox",)),
    # AWS access key id: the exact 20 character shape, any case.
    _shape(r"(?:AKIA|ASIA)", r"[0-9A-Za-z]{16}(?![0-9A-Za-z])", _LEAD, _I, _not_placeholder, ("akia", "asia")),
    # Google API key.
    _shape(r"AIza", r"[0-9A-Za-z_-]{30,}", _LEAD, 0, _not_placeholder, ("aiza",)),
    # Published formats (addition A4).
    _shape(r"hf_", r"[A-Za-z0-9]{30,}", _LEAD, 0, lambda m: _opaque(m, 30, 34), ("hf_",)),
    _shape(
        re.escape(AGENT_KEY_PREFIX),
        r"[A-Za-z0-9_-]{20,}",
        _LEAD,
        0,
        lambda m: _opaque(m, 20, 30),
        (AGENT_KEY_PREFIX.casefold(),),
    ),
)

_FORMAT_SHAPES: tuple[_Shape, ...] = (
    _shape(r"SG\.", r"[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", _LEAD, 0, _always, ("sg.",)),
    _shape(
        r"hooks\.slack\.com/services/",
        r"T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{20,}",
        "",
        0,
        _always,
        ("hooks.slack.com/services/",),
    ),
    _shape(r"pypi-AgE", r"[A-Za-z0-9_-]{50,}", _LEAD, 0, _always, ("pypi-age",)),
)

_PER_FIELD_SHAPES = _PREFIX_SHAPES + _FORMAT_SHAPES

# The compiled per-field prefix patterns, kept under the name the commit path
# has always exported. A match is only a candidate: the shape's validator
# decides whether the material after the prefix is real key material.
SECRET_PREFIX_PATTERNS = tuple(bounded for _triggers, bounded, _free, _validator in _PREFIX_SHAPES)

# Private key material: recall wins over precision (S4.4 round 3, tower
# ruling, 2026-09-23). Four rules, any of which refuses: the armor line, a
# case-exact header with a real body, a PuTTY file, and a base64-encoded key
# file (below, with the decoders).
#
# 1. The dashed, case-exact armor line of a private key, on its own, anywhere
#    in a field, whatever precedes or follows it. This is v0.16.0's rule,
#    restored. Round 2 dropped it in favour of reading the body, and the
#    adversary review then pasted real keys behind "> " and "# ", as
#    double-escaped JSON, as a JSON array of lines and joined with <br>: every
#    one passed. A PUBLIC KEY, a CERTIFICATE and a PGP PUBLIC KEY BLOCK stay
#    allowed. The case-insensitive dashless form ("begin with the private
#    key ...") is not read at all: it refused prose.
# The five dashes are joined in, so the source holds no literal armor line
# for a repository secret scanner to read as a key.
_FIVE_DASHES = "-" * 5
_ARMOR_LINE = re.compile(
    _FIVE_DASHES + r"BEGIN (?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY" + _FIVE_DASHES
    + "|" + _FIVE_DASHES + r"BEGIN PGP PRIVATE KEY BLOCK" + _FIVE_DASHES
)
# The same line read across a field boundary, where a split can swallow a
# space ("-----BEGIN RSA PRIVATE" over "KEY-----", or "-----BEGIN " over
# "RSA PRIVATE KEY-----", round 4).
_ARMOR_LINE_ACROSS = re.compile(
    r"-----BEGIN ?(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) ?)?PRIVATE ?KEY-----|-----BEGIN ?PGP ?PRIVATE ?KEY ?BLOCK-----"
)
# 2. A case-exact header of any key type with a real key body after it. This
#    also catches an armor line mangled on the way (round 4): its dashes
#    replaced by an em dash or another dash-like character, or dropped, and
#    the header and body lines quoted or commented ("> ", "# ", "//"), joined
#    with <br>, or flattened with a literal \n. The real body is what keeps
#    prose from being refused. An OpenPGP secret key's header ends in
#    "PRIVATE KEY BLOCK", which v0.16.0 missed in every placement.
_PEM_HEADER_EXACT = re.compile(r"(?:(?<=---)\s?|\b)BEGIN[A-Z ]{0,40}PRIVATE ?KEY(?: ?BLOCK)?")
# ASCII hyphen-minus, U+2010 to U+2015 (hyphens, figure dash, en and em
# dashes, horizontal bar) and U+2212 minus sign.
_DASH_LIKE = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
# What a quote, a comment, an HTML join or a flattened line break leaves at
# the start of a line.
_LINE_JUNK = r"(?:[ \t\r\n>#]|//|<br\s{0,2}/?>|\\\\[nr]|\\[nr])"
# A line break as a key's lines carry it: real, an HTML <br>, or a literal
# \n or \r, single or double escaped.
_LINE_BREAK = r"(?:\r?\n|<br\s{0,2}/?>|\\\\[nr]|\\[nr])"
# The body after a header starts a following line (S4.4 round 5). Round 4
# read the first run of eight base64 characters anywhere after the header, so
# a note that mentions the header and then says "1Password", "HashiCorp" or
# "2026/12/31" was refused. The body must now be a run of at least 16 base64
# characters (every key's first body line is 64 or 70) at the start of a
# line, reached one of two ways:
#
# 1. The header line ends in a line break, after any armor dashes. Up to four
#    "Name: value" header lines (encrypted PEM, PGP armor headers) may
#    follow, each ending in a line break of its own.
# 2. The key was folded onto one line, as the commit door folds whitespace:
#    the header's armor dashes (three or more, of any dash-like character),
#    or a quote or comment prefix on the next line ("> ", "# ", "//"). One
#    folded "Name: value" line may follow (a PGP "Comment:"), ending at the
#    first space before a 16-character run.
# 3. Folded with no dashes and no prefix left ("BEGIN EC PRIVATE KEY MHcC..."
#    at the commit door): the run must be at least 40 characters, a whole
#    body line, which no word or date is.
#
# Each header line's value ends at a forced point (a line break, or the first
# space before a run), so the pattern cannot backtrack across combinations.
# Round 4's value ended at any junk character, and four of them in a row cost
# 2.4 s on a 1.2 KB note of "a: a: a: ..." after a header (found in round 5).
_FOLDED_HEADER_LINE = r"(?:[A-Za-z-]{1,40}:[ \t][^\n<\\]{0,120}?[ \t](?=[A-Za-z0-9+/=]{%d}))?"
_PEM_BODY = re.compile(
    r"(?:"
    + r"[" + _DASH_LIKE + r"]{0,24}[ \t]{0,24}" + _LINE_BREAK + _LINE_JUNK + r"{0,24}"
    + r"(?:[A-Za-z-]{1,40}:[^\n<\\]{0,200}" + _LINE_BREAK + _LINE_JUNK + r"{0,24}){0,4}"
    + r"|"
    + r"(?:[" + _DASH_LIKE + r"]{3,24}[ \t]{0,24}|[ \t]{1,24}(?=>|#|//))" + _LINE_JUNK + r"{0,24}"
    + _FOLDED_HEADER_LINE % 16
    + r"|"
    + r"[ \t]{1,24}" + _FOLDED_HEADER_LINE % 40 + r"(?=[A-Za-z0-9+/=]{40})"
    + r")"
    + r"([A-Za-z0-9+/=]{16,512})"
)
# 3. A PuTTY private key file: its header, and in the same field either the
#    Private-MAC (40 or 64 hex digits) or a real private body after
#    "Private-Lines: N". The header and the section names alone are how
#    people describe the format (round 4: round 3 refused such notes, and a
#    v0.16.0 export holding one failed the whole import).
_PPK_HEADER = re.compile(r"PuTTY-User-Key-File-[23]:")
_PPK_MAC = re.compile(r"Private-MAC:[ \t]{0,8}(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])")
_PPK_BODY = re.compile(r"Private-Lines:[ \t]{0,8}\d{1,4}" + _LINE_JUNK + r"{1,24}([A-Za-z0-9+/=]{16,512})")


# Filler written where a key was removed. The repeated-character test used
# elsewhere is NOT applied to a PEM body: an OpenSSH private key's first body
# line carries runs of "A" (zero bytes in the openssh-key-v1 header), and
# reading those as a placeholder missed every unencrypted OpenSSH key
# (addendum F4, 2026-09-23). Instead a body needs four distinct characters,
# which a run of "A" with padding does not have.
_PEM_FILLER = re.compile(r"[xX]{6,}|\*{3,}")
_PEM_MIN_DISTINCT = 4


def _pem_body_ok(text: str, end: int, seams: tuple[int, ...] = ()) -> bool:
    body = _PEM_BODY.match(text, end)
    if body is None:
        return False
    # Across fields, the body's first 16 characters must sit in one field: a
    # note ending "... 1Password" glued to a field starting "Deploys" is not a
    # 16-character body (round 5). A body split inside its first 16
    # characters is a deliberate split, which the residual list names.
    start = body.start(1)
    if any(start < seam < start + 16 for seam in seams):
        return False
    return _real_body(body.group(1))


def _pem_hit(text: str, header: re.Pattern[str], seams: tuple[int, ...] = ()) -> bool:
    return any(_pem_body_ok(text, match.end(), seams) for match in header.finditer(text))


def _real_body(material: str) -> bool:
    if _PEM_FILLER.search(material) or len(set(material)) < _PEM_MIN_DISTINCT:
        return False
    return bool(_DIGIT.search(material) or _INNER_UPPER.search(material) or any(c in "+/=" for c in material))


def _ppk_hit(text: str) -> bool:
    if "PuTTY-User-Key-File-" not in text or not _PPK_HEADER.search(text):
        return False
    if "Private-MAC:" in text and _PPK_MAC.search(text):
        return True
    return "Private-Lines:" in text and any(_real_body(match.group(1)) for match in _PPK_BODY.finditer(text))


def _private_key_hit(text: str) -> bool:
    if "-----BEGIN" in text and _ARMOR_LINE.search(text):
        return True
    if "BEGIN" in text and _pem_hit(text, _PEM_HEADER_EXACT):
        return True
    return _ppk_hit(text)


# "Bearer <token>". The token must be opaque, so "Ring Bearer
# responsibilities" is a wedding note and not a header.
_BEARER = re.compile(r"\bBearer[ \t]+([0-9A-Za-z._~+/-]+=*)")


def _bearer_ok(material: str) -> bool:
    if len(material) < 16 or _PLACEHOLDER.search(material) or _word_chain(material):
        return False
    return bool(
        _DIGIT.search(material)
        or _INNER_UPPER.search(material)
        or len(material) >= 21
        or any(c in "~+/" for c in material)
    )


# JWT, linear: trying only the first legal start in each token run gives the
# same answer as a plain search (argued in vnext_promotion_policy._matches_jwt).
_JWT_START = re.compile(r"\beyJ")
_JWT_SHAPE = re.compile(r"eyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{4,}")
_JWT_TOKEN_RUN = re.compile(r"[0-9A-Za-z_\-]+")


def _matches_jwt(text: str) -> bool:
    skip_until = -1
    for start in _JWT_START.finditer(text):
        position = start.start()
        if position < skip_until:
            continue
        if _JWT_SHAPE.match(text, position):
            return True
        run = _JWT_TOKEN_RUN.match(text, position)
        skip_until = run.end() if run is not None else position + 1
    return False


# ---------------------------------------------------------------------------
# In-field textual rules.
# ---------------------------------------------------------------------------

# Prose disclosure: "the password for the vault is hunter2hunter2".
_PROSE = re.compile(
    r"\b(?:password|passphrase|api\s+key|access\s+key|secret\s+key|private\s+key"
    r"|auth\s+token|access\s+token|credential)\b[^.\n\x00]{0,24}?\b(?:is|was|are|were)[ \t]+(\S{6,})",
    _I,
)
_TRAILING_PUNCT = ".,;:!?)]}\"'"
_URLISH = re.compile(r"://|^www\.|^[~/.$%{<]|^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_NUMERICISH = re.compile(r"^[0-9._:/,+-]+$")
_QUANTITY = re.compile(r"^[0-9]+[-_]?[A-Za-z]+$")
_FILENAME = re.compile(r"\.[A-Za-z]{2,4}$")
_ALGORITHM = re.compile(
    r"^(?:id_)?(?:rsa|dsa|ecdsa|ed25519|ed448|x25519|curve25519)(?:[-_]?(?:sk|pub|\d{3,4}))?$"
    r"|^(?:rsa|aes|sha|hs|rs|es|ps|p)[-_]?\d{3,4}(?:[-_][a-z]{2,4})?$"
    r"|^secp\d{3}[rk]1$|^ecdsa[-_]p?\d{3}$|^sha\d-\d{3}$",
    _I,
)


def _prose_value_ok(value: str) -> bool:
    value = value.rstrip(_TRAILING_PUNCT)
    if len(value) < 8:
        return False
    if _URLISH.search(value) or _NUMERICISH.match(value) or _QUANTITY.match(value):
        return False
    if _FILENAME.search(value) or _ALGORITHM.match(value) or _PLACEHOLDER.search(value):
        return False
    has_letter = any(c.isalpha() for c in value)
    has_digit = bool(_DIGIT.search(value))
    has_symbol = any(c in "!@#$%^&*+=~?<>|\\" for c in value)
    return has_letter and (has_digit or has_symbol)


# A password embedded in a URL: scheme://user:password@host. Anchored on "://"
# so each attempt is bounded, whatever surrounds it.
# The userinfo of a URL ends before the first "/", "?" or "#": a port and an
# "@" in the query string ("http://localhost:5173?invite=alex@example.com") is
# not user:password@host (adversary review, round 3).
_URL_CREDENTIAL = re.compile(r"://([^\s:/?#@\x00]{0,64}):([^\s@/?#\x00]{6,128})@[A-Za-z0-9\[]")


def _url_password_ok(password: str) -> bool:
    folded = password.casefold()
    if folded in _PLACEHOLDER_VALUES or _PLACEHOLDER.search(password) or password.startswith(("$", "{", "<", "%")):
        return False
    return bool(_DIGIT.search(password) or _INNER_UPPER.search(password) or any(c in "!#^&*+=~?" for c in password))


def _url_credential_hit(text: str) -> bool:
    return any(_url_password_ok(match.group(2)) for match in _URL_CREDENTIAL.finditer(text))


def _prose_hit(text: str) -> bool:
    return any(_prose_value_ok(match.group(1)) for match in _PROSE.finditer(text))


# NAME=value inside one field. The name is a secret name only when its LAST
# segment is one, after neutral suffixes are stripped (DB_PASSWORD_RO,
# MAPBOX_TOKEN_V2, SECRET_KEY_BASE). A bare "key" segment counts only behind a
# secret qualifier, or in an ALL-CAPS environment-style name whose qualifier
# is not a structural word, so fact_key, cache_key, sort_key and
# partition_key are schema vocabulary.
_NAME_RUN = re.compile(r"[A-Za-z0-9_-]+")
_OPERATOR = re.compile(r"[\"']?[ \t]*[:=]")
_VALUE = re.compile(r"[ \t]*(?:\r?\n[ \t]*)?[\"']?([A-Za-z0-9_\-+/=.]{6,})")
# A password can hold punctuation, so a password-kind name reads a wider
# value (addition A3). It still stops at whitespace, quotes, backticks and
# closing punctuation.
_PASSWORD_VALUE = re.compile(r"[ \t]*(?:\r?\n[ \t]*)?[\"']?([^\s\"'`,;)\]}]{6,})")
_PASSWORD_REFERENCE_PREFIXES = ("$", "%s", "%(", "{{", "<", "~/", "/")
_SEGMENT_SEP = re.compile(r"[_-]")
# nextPageToken -> next, Page, Token; PGPASSWORD stays whole; apiKey -> api, Key.
# _camel_humps gives what re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[0-9]+",
# piece) gave, built from runs of capitals and runs of lower case and digits.
# The two runs cannot overlap, so nothing backtracks however long a run is
# (CodeQL py/polynomial-redos flagged the old pattern on a run of "A", S4 CI).
_HUMP_RUN = re.compile(r"[A-Z]+|[a-z0-9]+")


def _camel_humps(piece: str) -> list[str]:
    humps: list[str] = []
    runs = list(_HUMP_RUN.finditer(piece))
    index = 0
    while index < len(runs):
        run = runs[index]
        text = run.group(0)
        following = runs[index + 1] if index + 1 < len(runs) else None
        if (
            "A" <= text[0] <= "Z"
            and following is not None
            and following.start() == run.end()
            and "a" <= piece[run.end()] <= "z"
        ):
            # The last capital starts the next hump: "HTTPServer" is HTTP, Server.
            if len(text) > 1:
                humps.append(text[:-1])
            humps.append(text[-1] + following.group(0))
            index += 2
            continue
        humps.append(text)
        index += 1
    return humps

# Suffixes that say which copy of a secret this is, not what it is (A2).
_NEUTRAL_SUFFIX = re.compile(
    r"ro|rw|readonly|v\d*|\d+|old|new|prev|previous|current|prod|production|dev|stg|staging|test|live"
    r"|backup|base|b64|hex|value|str|env|var|json"
)
# The same, for a whole "_"/"-" piece, read before camel splitting: the hump
# rule splits "B64" into "B" and "64", which left SSH_PRIVATE_KEY_B64 and
# DB_PASSWORD_B64 ending in "b" (adversary review, round 3). A letter and
# digits ("B2", "S3", "R2") is a copy or storage marker, not the secret's name.
_NEUTRAL_PIECE = re.compile(_NEUTRAL_SUFFIX.pattern + r"|[a-z]\d+")

# A segment names a secret when it is lower case letters and digits ending in
# one of these words. Read as a suffix test rather than the old
# "[a-z0-9]*(?:word|...)" patterns, which CodeQL flagged on a run of "0"
# (py/polynomial-redos, S4 CI): fullmatch kept them linear, but a search
# with the same pattern is quadratic, and the suffix test cannot be.
_LOWER_ALNUM = re.compile(r"[a-z0-9]+")
_PASSWORD_WORDS = ("password", "passwd", "passphrase")
_SECRET_WORDS = (
    "secret", "credential", "credentials", "apikey", "secretkey", "accesskey", "privatekey", "signingkey",
    "authtoken", "accesstoken",
)
_TOKEN_WORDS = ("token",)


def _segment_ends_in(segment: str, words: tuple[str, ...]) -> bool:
    return segment.endswith(words) and _LOWER_ALNUM.fullmatch(segment) is not None

_KEY_QUALIFIERS = frozenset(
    {
        "api", "secret", "access", "private", "signing", "sign", "encryption", "enc", "crypt", "crypto",
        "master", "account", "auth", "client", "app", "service", "deploy", "ssh", "gpg", "pgp", "hmac",
        "jwt", "sas", "shared", "storage", "webhook", "license", "licence", "subscription", "consumer",
        "root", "admin", "server", "live", "prod", "production", "decryption", "recovery",
    }
)
_KEY_STRUCTURAL = frozenset(
    {
        "fact", "preference", "state", "decision", "cache", "sort", "partition", "primary", "foreign",
        "dedupe", "dedup", "idempotency", "memory", "object", "lookup", "map", "hash", "index", "group",
        "routing", "row", "shard", "composite", "unique", "natural", "surrogate", "lock", "i18n",
        "translation", "trace", "cursor", "page", "next", "prev", "continuation", "order",
        "join", "dict", "record", "entity", "subject", "topic", "bucket", "s3", "redis", "hotkey",
        "short", "shortcut", "keyboard", "music", "public", "publishable", "pub", "project", "tenant",
        "user", "session_id", "external", "idem", "event", "message", "query", "column", "field",
    }
)
_TOKEN_STRUCTURAL = frozenset(
    {
        "page", "next", "prev", "previous", "continuation", "sync", "cursor", "pagination", "max",
        "min", "num", "total", "input", "output", "prompt", "completion", "context", "stop", "eos",
        "bos", "pad", "unk", "cls", "sep", "mask", "special", "limit", "count", "design", "lexer",
        "csrf", "xsrf",
    }
)
_PRECEDING_QUALIFIER = re.compile(
    r"(?:^|[^0-9A-Za-z])(api|secret|access|private|signing|encryption|master|license|licence|account"
    r"|auth|client|deploy|service|ssh|hmac|webhook|subscription|consumer|admin)[ \t]+$",
    _I,
)
_PLACEHOLDER_VALUES = frozenset(
    {
        "redacted", "changeme", "change_me", "change-me", "placeholder", "password", "secret", "example",
        "none", "null", "undefined", "notset", "not_set", "not-set", "hidden", "removed", "masked",
        "yourpassword", "your_password", "your-password", "yoursecret", "your_secret", "your-secret",
        "yourtoken", "your_token", "your-token", "yourkey", "your_key", "your-key", "dummy", "sample",
        "required", "optional", "string", "secret123", "token123",
    }
)
_DOTTED_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+$")


def _name_kind(run: str, text: str, run_start: int) -> str | None:
    """"password", "secret", "weak", "bare" (a lone word such as "Secret:"
    that also labels ordinary prose), or None when the run is no secret name."""

    pieces = [piece for piece in _SEGMENT_SEP.split(run) if piece]
    while len(pieces) > 1 and _NEUTRAL_PIECE.fullmatch(pieces[-1].lower()):
        pieces.pop()
    segments = [hump for piece in pieces for hump in _camel_humps(piece)]
    # Addition A2: drop neutral suffixes before deciding on the last segment.
    while len(segments) > 1 and _NEUTRAL_SUFFIX.fullmatch(segments[-1].lower()):
        segments.pop()
    if not segments:
        return None
    last = segments[-1].lower()
    previous = segments[-2].lower() if len(segments) >= 2 else ""
    qualified = bool(previous) or bool(_PRECEDING_QUALIFIER.search(text[max(0, run_start - 24) : run_start]))
    if _segment_ends_in(last, _PASSWORD_WORDS):
        return "password"
    if _segment_ends_in(last, _SECRET_WORDS):
        return "secret" if qualified or last not in {"secret", "credential", "credentials"} else "bare"
    if _segment_ends_in(last, _TOKEN_WORDS):
        if previous in _TOKEN_STRUCTURAL:
            return None
        return "secret" if qualified or last != "token" else "bare"
    if last == "key":
        if previous:
            if previous in _KEY_QUALIFIERS:
                return "secret"
            if previous in _KEY_STRUCTURAL:
                return None
            if run.isupper() and not run.startswith(("_", "-")):
                return "secret"  # OPENAI_KEY, MAPS_KEY: environment-variable style
            return "weak"  # stripe_key, backup_key: only a very key-like value counts
        return "secret" if qualified else None
    return None


_SNAKE_IDENTIFIER = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
_HEXISH = re.compile(r"^[0-9a-fA-F-]+$")  # digests and UUIDs are identifiers


def _assignment_value_ok(kind: str, value: str, following: str) -> bool:
    if following and following in "([":
        return False  # a call or subscript: password = hash_password(pw)
    folded = value.casefold()
    if folded in _PLACEHOLDER_VALUES or folded.startswith(("your_", "your-", "<")) or folded.endswith(("_here", "-here")):
        return False
    if _NUMERICISH.match(value) and not value.isdigit():
        return False  # dates, times, versions, ranges
    if kind != "password" and _DOTTED_REFERENCE.match(value):
        # settings.SECRET_KEY, process.env.OPENAI_API_KEY. Never for a
        # password-kind name (S4.4 round 5): a DB_PASSWORD set to a dotted
        # phrase with a year in it is a dotted password, and it was stored.
        return False
    if _ALGORITHM.match(value) or _FILENAME.search(value):
        return False  # "SSH key: id_ed25519", "encryption key: AES-256", "key.pem"
    if kind == "password":
        # A password can be a human word with one digit, or a long
        # passphrase. A snake_case identifier is a variable name.
        if _PLACEHOLDER.search(value) or _SNAKE_IDENTIFIER.match(value):
            return False
        if len(value) >= 24:
            return True
        if any(c.isdigit() or c.isupper() for c in value):
            return True
        return any(c in "_-+/=." for c in value)
    if value.isdigit():
        return False  # machine-issued secrets are never plain decimal numbers
    if kind == "bare":
        return _opaque(value, 12, 20)
    if kind == "weak":
        return (
            len(value) >= 20
            and bool(_DIGIT.search(value))
            and any(c.isalpha() for c in value)
            and not _HEXISH.match(value)
            and _opaque(value, 20, 20)
        )
    return _opaque(value, 6, 20)


# A GPG key id or fingerprint (8, 16 or 40 hex digits, optional 0x) under
# git's signingkey: an identifier, not the key (adversary review, round 3).
_GPG_KEY_ID = re.compile(r"(?:0[xX])?(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40})")
# An SSH public key algorithm and its body: "Deploy key: ssh-ed25519 AAAA...".
_SSH_ALGORITHM = re.compile(
    r"ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-nistp(?:256|384|521)|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com"
)
_SSH_PUBLIC_BODY = re.compile(r"[ \t]+AAAA[0-9A-Za-z+/]{20,}")


def _identifier_not_secret(run: str, value: str, text: str, value_end: int) -> bool:
    if run.lower().replace("_", "").replace("-", "") == "signingkey" and _GPG_KEY_ID.fullmatch(value):
        return True
    return bool(_SSH_ALGORITHM.fullmatch(value)) and bool(_SSH_PUBLIC_BODY.match(text, value_end))


def _password_value(raw: str) -> str | None:
    """Addition A3: the wider password value, or None for a reference or template."""

    value = raw.rstrip(".:!?")
    if "://" in value or value.startswith(_PASSWORD_REFERENCE_PREFIXES):
        return None
    return value


def _assignment_hit(text: str) -> bool:
    resume_at = 0
    for run in _NAME_RUN.finditer(text):
        if run.start() < resume_at:
            continue
        operator = _OPERATOR.match(text, run.end())
        if operator is None:
            continue
        # The name is judged BEFORE any value is matched, so a value is only
        # read after a secret name. Matching the value first was quadratic.
        kind = _name_kind(run.group(0), text, run.start())
        if kind is None:
            continue
        pattern = _PASSWORD_VALUE if kind == "password" else _VALUE
        value = pattern.match(text, operator.end())
        if value is None:
            continue
        resume_at = value.end()
        candidate: str | None = value.group(1)
        if kind == "password":
            candidate = _password_value(value.group(1))
        if candidate is None or _identifier_not_secret(run.group(0), candidate, text, value.end()):
            continue
        if _assignment_value_ok(kind, candidate, text[value.end() : value.end() + 1]):
            return True
    return False


_PAIR_MIN_CHARS = 6
_PAIR_MAX_CHARS = 512
_WHITESPACE = re.compile(r"\s")


def _pair_hit(key: str, value: str) -> bool:
    """Addition A1: one mapping pair, checked alone and never joined."""

    val = value.strip().strip("\"'")
    if len(val) < _PAIR_MIN_CHARS or len(val) > _PAIR_MAX_CHARS or _WHITESPACE.search(val):
        return False
    kind = _name_kind(key, "", 0)
    if kind is None or _identifier_not_secret(key, val, "", 0):
        return False
    if kind == "password":
        password = _password_value(val)
        return password is not None and _assignment_value_ok(kind, password, "")
    return _assignment_value_ok(kind, val, "")


# ---------------------------------------------------------------------------
# Decoding and despacing (per field only).
# ---------------------------------------------------------------------------

_BASE64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{16,512}={0,2}(?![A-Za-z0-9+/_=-])")
# A base64-encoded key file (kubeconfig client-key-data, a Kubernetes
# tls.key, SSH_PRIVATE_KEY_B64=...). Its first characters decode to the armor
# line, so only a bounded prefix of each run of 40 or more base64 characters
# is decoded, however long the run and whether or not it wraps across lines;
# the cost stays linear. The run may start right after "=" (NAME_B64=...).
# Round 2 decoded only tokens of at most 512 characters, which no one-line
# key file is: as client-key-data it caught 2 of 14 real keys, through other
# rules (adversary review, round 3; v0.16.0 caught all 14).
# A run starts with 40 base64 characters on one line. Its wrapped
# continuation lines are joined across a line break (with an optional quote
# prefix) or across a single space, which is what the commit door leaves of
# a line break after it folds whitespace: round 3 joined only a real "\n",
# so a key file wrapped at 40 or 44 characters passed the commit door
# (round 4). Enough is joined to decode the whole armor line.
_BASE64_RUN_START = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40}")
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]+(={0,2})")
_BASE64_WRAP = re.compile(r" |[ \t]*\r?\n[ \t>#]{0,4}")
_BASE64_PREFIX_CHARS = 128
# A PuTTY file is decoded in full (at most this much) to read its MAC.
_BASE64_PPK_CHARS = 16_384
_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{24,512}\b")
_RUN_SEPARATORS = " \t\r\n.·,;|/"
_SPACED_OUT_RUN = re.compile(r"(?:[^\s\x00][" + re.escape(_RUN_SEPARATORS) + r"]{1,3}){7,}[^\s\x00]")
_RUN_SEPARATOR_STRIP = str.maketrans({char: None for char in _RUN_SEPARATORS})


def _decoded_variants(text: str) -> list[str]:
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
            variants.append(bytes.fromhex(token).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
    return variants


def _joined_base64(text: str, start: int, limit: int) -> tuple[str, int]:
    """The base64 characters from ``start``, joined across wraps, at most
    ``limit`` of them (a multiple of four), and where the walk stopped."""

    parts: list[str] = []
    total = 0
    position = start
    while total < limit:
        run = _BASE64_RUN.match(text, position)
        if run is None or not run.group(0):
            break
        parts.append(run.group(0))
        total += len(run.group(0))
        position = run.end()
        if run.group(1):
            break  # padding ends the encoding
        wrap = _BASE64_WRAP.match(text, position)
        if wrap is None:
            break
        position = wrap.end()
    joined = "".join(parts)[:limit]
    return joined[: len(joined) - len(joined) % 4], position


def _decode_base64(material: str) -> list[str]:
    decoded: list[str] = []
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded.append(decoder(material).decode("utf-8", errors="replace"))
        except (binascii.Error, ValueError):
            continue
    return decoded


def _encoded_key_file(text: str) -> bool:
    """True when a long base64 run holds a private key file.

    Decodes at most the first 128 base64 characters of each run of 40 or more
    (joined across wraps) and refuses on a private key armor line in them. A
    PuTTY header there is not enough on its own: that run is decoded in full,
    up to 16 KB, and must pass the same PuTTY rule as plain text. Each start
    costs a bounded walk and a PuTTY run is skipped past once read, so the
    cost stays linear.
    """

    position = 0
    while True:
        start = _BASE64_RUN_START.search(text, position)
        if start is None:
            return False
        position = start.end()
        prefix_material, _ = _joined_base64(text, start.start(), _BASE64_PREFIX_CHARS)
        for prefix in _decode_base64(prefix_material):
            if _ARMOR_LINE_ACROSS.search(prefix):
                return True
            if _PPK_HEADER.search(prefix):
                material, stopped = _joined_base64(text, start.start(), _BASE64_PPK_CHARS)
                if any(_ppk_hit(decoded) for decoded in _decode_base64(material)):
                    return True
                position = max(position, stopped)
                break


def _despaced_runs(text: str) -> list[str]:
    return [match.group(0).translate(_RUN_SEPARATOR_STRIP) for match in _SPACED_OUT_RUN.finditer(text)]


_ESCAPED_WHITESPACE = re.compile(r"\\r\\n|\\n|\\r|\\t")
_UNESCAPED = {"\\r\\n": "\n", "\\n": "\n", "\\r": "\n", "\\t": "\t"}


def _unescaped(text: str) -> str | None:
    """Literal backslash-n, backslash-r-backslash-n, backslash-r and
    backslash-t turned into real whitespace, or None when there are none.

    A key pasted from a JSON file (a service-account "private_key") or a
    one-line .env value arrives with its newlines escaped, and the PEM body
    rule needs the real line break after the header (addendum F4).
    """

    if "\\" not in text or not _ESCAPED_WHITESPACE.search(text):
        return None
    return _ESCAPED_WHITESPACE.sub(lambda match: _UNESCAPED[match.group(0)], text)


# ---------------------------------------------------------------------------
# Surface scanners.
# ---------------------------------------------------------------------------

# Joins the per-field surfaces into one string. The newline stops the prose
# rule and the NUL stops every token class, operator and value, so no pattern
# spans two fields here; it only saves per-call overhead on many fields.
_SEP = "\n\x00\n"


def _has(folded: str, triggers: tuple[str, ...]) -> bool:
    return any(trigger in folded for trigger in triggers)


def _scan_per_field(text: str, *, boundary_free: bool = False) -> bool:
    if not text:
        return False
    folded = text.casefold()
    for triggers, bounded, free, validator in _PER_FIELD_SHAPES:
        if not _has(folded, triggers):
            continue
        pattern = free if boundary_free else bounded
        if any(validator(match.group(1)) for match in pattern.finditer(text)):
            return True
    if _private_key_hit(text):
        return True
    if "Bearer" in text and any(_bearer_ok(match.group(1)) for match in _BEARER.finditer(text)):
        return True
    if "eyJ" in text and _matches_jwt(text):
        return True
    if _has(folded, ("passw", "passp", "key", "token", "credential")) and _prose_hit(text):
        return True
    if "://" in text and _url_credential_hit(text):
        return True
    if ("=" in text or ":" in text) and _assignment_hit(text):
        return True
    return False


# Across seams: case-exact, high-specificity shapes only. The AWS id is the
# one fixed-length shape, so its right edge matters: it is matched on the
# fields joined with a private-use seam mark that may sit between any two of
# its characters and that the right-edge lookahead reads as a boundary.
_SEAM = "\ue000"
_SEAM_OPT = _SEAM + "?"
_CROSS_AWS = re.compile(
    _LEAD
    + "(?:" + _SEAM_OPT.join("AKIA") + "|" + _SEAM_OPT.join("ASIA") + ")" + _SEAM_OPT
    + "((?:[0-9A-Z]" + _SEAM_OPT + "){15}[0-9A-Z])(?![0-9A-Za-z])"
)
_CROSS_SHAPES: tuple[tuple[tuple[str, ...], re.Pattern[str], Callable[[str], bool]], ...] = (
    (
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
        re.compile(_LEAD_US + r"gh[pousr]_([0-9A-Za-z]+)"),
        lambda m: _opaque(m, 8, 12),
    ),
    (("github_pat_",), re.compile(_LEAD_US + r"github_pat_([0-9A-Za-z_]+)"), lambda m: _opaque(m, 16, 20)),
    (("xox",), re.compile(_LEAD + r"xox[baprs]-([0-9A-Za-z-]+)"), lambda m: _opaque(m, 8, 12) and bool(_DIGIT.search(m))),
    (("k_live_", "k_test_"), re.compile(_LEAD + r"[sr]k_(?:live|test)_([0-9A-Za-z]+)"), lambda m: _opaque(m, 8, 12)),
    (("sk-",), re.compile(_LEAD + r"sk-" + _FIDO_KEY_TYPE + r"([0-9A-Za-z_-]+)"), lambda m: _opaque(m, 8, 12)),
    (("glpat-",), re.compile(_LEAD_US + r"glpat-([0-9A-Za-z_-]+)"), lambda m: _opaque(m, 8, 12)),
    (("npm_",), re.compile(_LEAD_US + r"npm_([0-9A-Za-z]+)"), lambda m: _opaque(m, 16, 24)),
    (("AIza",), re.compile(_LEAD + r"AIza([0-9A-Za-z_-]{30,})"), _not_placeholder),
    # Addendum F5: the product's own key and the long-prefix published formats.
    (("hf_",), re.compile(_LEAD + r"hf_([A-Za-z0-9]{30,})"), lambda m: _opaque(m, 30, 34)),
    (
        (AGENT_KEY_PREFIX,),
        re.compile(_LEAD + re.escape(AGENT_KEY_PREFIX) + r"([A-Za-z0-9_-]{20,})"),
        lambda m: _opaque(m, 20, 30),
    ),
    (("SG.",), re.compile(_LEAD + r"SG\.([A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43})"), _always),
    (
        ("hooks.slack.com/services/",),
        re.compile(r"hooks\.slack\.com/services/(T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{20,})"),
        _always,
    ),
    (("pypi-AgE",), re.compile(_LEAD + r"pypi-AgE([A-Za-z0-9_-]{50,})"), _always),
)


def _scan_cross(parts: list[str]) -> bool:
    text = "".join(parts)
    if "AKIA" in text or "ASIA" in text:
        marked = _SEAM.join(parts)
        if any(_not_placeholder(match.group(1).replace(_SEAM, "")) for match in _CROSS_AWS.finditer(marked)):
            return True
    for triggers, pattern, validator in _CROSS_SHAPES:
        if not any(trigger in text for trigger in triggers):
            continue
        if any(validator(match.group(1)) for match in pattern.finditer(text)):
            return True
    if "-----BEGIN" in text and _ARMOR_LINE_ACROSS.search(text):
        return True
    if "BEGIN" in text:
        seams: list[int] = []
        for part in parts[:-1]:
            seams.append((seams[-1] if seams else 0) + len(part))
        if _pem_hit(text, _PEM_HEADER_EXACT, tuple(seams)):
            return True
    if _ppk_hit(text):
        return True
    return "eyJ" in text and _matches_jwt(text)


# ---------------------------------------------------------------------------
# Flattening and seams.
# ---------------------------------------------------------------------------


def _flatten(fields: tuple[object, ...]) -> tuple[list[tuple[str, bool]], list[tuple[str, str]]]:
    """(text, is_mapping_key) in reading order, and every (str key, str value)
    pair of every mapping reached. Iterative, so depth cannot blow the stack."""

    out: list[tuple[str, bool]] = []
    pairs: list[tuple[str, str]] = []
    stack: list[tuple[object, bool]] = [(field, False) for field in reversed(fields)]
    while stack:
        value, is_key = stack.pop()
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            if value:
                out.append((value, is_key))
            continue
        if isinstance(value, Mapping):
            pending: list[tuple[object, bool]] = []
            for key, item in value.items():
                if isinstance(key, str) and key:
                    pending.append((key, True))
                    if isinstance(item, str):
                        pairs.append((key, item))
                pending.append((item, False))
            stack.extend(reversed(pending))
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            stack.extend((item, False) for item in reversed(list(value)))
            continue
        if isinstance(value, (bytes, bytearray)):
            text = bytes(value).decode("utf-8", errors="replace")
        else:
            text = str(value)
        if text:
            out.append((text, is_key))
    return out, pairs


_DERIVED_LOOKBACK = 4
_DERIVED_MIN_CHARS = 16


def _seam_groups(texts: list[str]) -> list[list[str]]:
    """Runs of adjacent texts to read across, broken at derived copies.

    A text is a derived copy when it equals any earlier text, or is a prefix
    of one of the few texts just before it (summary = canonical_text[:280]).
    Its content was already scanned on its own; its seams are artefacts of
    how the caller listed the row, not of how anyone reads it. Only a long
    copy counts, so a caller cannot cut a real seam with a short repeat.
    """

    groups: list[list[str]] = []
    current: list[str] = []
    seen: set[str] = set()
    recent: list[str] = []
    for text in texts:
        stripped = text.strip()
        derived = len(stripped) >= _DERIVED_MIN_CHARS and (
            stripped in seen or any(prior.startswith(stripped) for prior in recent)
        )
        if derived:
            if len(current) > 1:
                groups.append(current)
            current = []
            continue
        seen.add(stripped)
        recent.append(stripped)
        if len(recent) > _DERIVED_LOOKBACK:
            recent.pop(0)
        current.append(text)
    if len(current) > 1:
        groups.append(current)
    return groups


def _expands(source_chars: int, normalized_chars: int) -> bool:
    return normalized_chars > EXPANSION_FLOOR_CHARS and normalized_chars > MAX_NORMALIZATION_EXPANSION * source_chars


# The check's cost, counted only while scan_cost() runs (owner ruling R3).
# Wall-clock time depends on the machine; these two numbers do not, so the
# import budget is pinned on them: surfaces scanned per record, and
# characters scanned per input character.
_COST: list[int] | None = None


def _charge(chars: int) -> None:
    if _COST is not None:
        _COST[0] += 1
        _COST[1] += chars


def scan_cost(*fields: object) -> tuple[int, int, int]:
    """(surfaces scanned, characters scanned, input characters) for one call.

    Runs the same verdict, counting as it goes. The input is every string
    the fields hold, keys and values, repeats included, as the caller passed
    them. A call that stops at its first hit is cheaper than a clean one, so
    budgets are measured on clean input.
    """

    global _COST
    flat, _pairs = _flatten(fields)
    input_chars = sum(len(text) for text, _ in flat)
    _COST = [0, 0]
    try:
        credential_verdict(*fields)
        surfaces, chars = _COST
    finally:
        _COST = None
    return surfaces, chars, input_chars


def credential_verdict(*fields: object) -> str | None:
    """VERDICT_CREDENTIAL, VERDICT_EXPANSION, or None when the fields are clean."""

    flat, pairs = _flatten(fields)
    if not flat:
        return None

    unique = list(dict.fromkeys(text for text, _ in flat))
    normal_of: dict[str, str] = {}
    source_total = normalized_total = 0
    for text in unique:
        normalized = normalize_for_matching(text)
        if _expands(len(text), len(normalized)):
            return VERDICT_EXPANSION
        normal_of[text] = normalized
        source_total += len(text)
        normalized_total += len(normalized)
    if _expands(source_total, normalized_total):
        return VERDICT_EXPANSION

    raw = _SEP.join(unique)
    normalized_joined = _SEP.join(normal_of[text] for text in unique)
    surfaces = (raw,) if normalized_joined == raw else (raw, normalized_joined)

    # 1. Each field on its own.
    for surface in surfaces:
        _charge(len(surface))
        if _scan_per_field(surface):
            return VERDICT_CREDENTIAL

    # 2. Each mapping pair on its own, raw and normalised (addition A1).
    for key, value in pairs:
        _charge(len(key) + len(value))
        if _pair_hit(key, value):
            return VERDICT_CREDENTIAL
        normalized_key, normalized_value = normal_of.get(key, key), normal_of.get(value, value)
        if (normalized_key, normalized_value) != (key, value) and _pair_hit(normalized_key, normalized_value):
            return VERDICT_CREDENTIAL

    # 1b. Decoded and despaced forms of each field.
    decoded: list[str] = []
    despaced: list[str] = []
    for surface in surfaces:
        unescaped = _unescaped(surface)
        if unescaped is not None:
            decoded.append(unescaped)
        _charge(len(surface))
        if _encoded_key_file(surface) or (unescaped is not None and _encoded_key_file(unescaped)):
            return VERDICT_CREDENTIAL
        decoded.extend(_decoded_variants(surface))
        despaced.extend(_despaced_runs(surface))
    if decoded:
        _charge(sum(map(len, decoded)))
        if _scan_per_field(_SEP.join(decoded)):
            return VERDICT_CREDENTIAL
    if despaced:
        _charge(sum(map(len, despaced)))
        if _scan_per_field(_SEP.join(despaced), boundary_free=True):
            return VERDICT_CREDENTIAL

    # 3. Across seams, case-exact shapes only: values alone, then keys and
    # values interleaved (a split between a mapping key and its value).
    values_only = [normal_of[text] for text, is_key in flat if not is_key]
    with_keys = [normal_of[text] for text, _ in flat]
    sequences = [values_only]
    if len(with_keys) != len(values_only):
        sequences.append(with_keys)
    for sequence in sequences:
        if len(sequence) < 2:
            continue
        for group in _seam_groups(sequence):
            _charge(sum(map(len, group)))
            if _scan_cross(group):
                return VERDICT_CREDENTIAL
    return None


def carries_credential_material(*fields: object) -> bool:
    """True when the fields, each alone or read across a seam, carry a credential.

    Also True when a field expands too far under normalisation to be checked.
    Pass every field the write persists, as it will be stored, in the order a
    reader meets them (title before body), with transformed copies left out.
    """

    return credential_verdict(*fields) is not None


def refuse_credential_material(*fields: object, error: Callable[[str], BaseException]) -> None:
    """Raise ``error`` when the fields carry credential material. The choke point."""

    verdict = credential_verdict(*fields)
    if verdict == VERDICT_EXPANSION:
        raise error(EXPANSION_REFUSED_MESSAGE)
    if verdict is not None:
        raise error(CREDENTIAL_MATERIAL_REFUSED_MESSAGE)


def string_values(value: object) -> list[str]:
    """The strings inside a structure, without its keys, in reading order.

    Provenance is passed this way at every door (owner ruling C3). Flattened
    with its keys, ``openclaw_dedupe_key`` holding a SHA-256 digest reads like
    a secret assigned to a key. The cost, stated plainly: under a key name, a
    Stripe key, an AWS secret access key or a plain password in provenance is
    not caught unless its value is self-identifying. Iterative, like _flatten.
    """

    out: list[str] = []
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, (list, tuple)):
            stack.extend(reversed(list(item)))
    return out


def is_derived_copy(candidate: object, text: object) -> bool:
    """True when ``candidate`` is ``text``, a prefix of it, or a prefix preview
    ending in "..." or the ellipsis character. Doors leave such copies out of
    the call, because the floor only breaks seams at exact copies."""

    if not isinstance(candidate, str) or not isinstance(text, str):
        return False
    stem = candidate.strip()
    if not stem:
        return True
    if text.startswith(stem) or text.strip().startswith(stem):
        return True
    for ellipsis in ("...", "\u2026"):
        if stem.endswith(ellipsis):
            head = stem[: -len(ellipsis)].rstrip()
            if head and text.startswith(head):
                return True
    return False


# Statuses a reader can find a memory in, the same pair as
# vnext_retrieval.MEMORY_SEARCHABLE_STATUSES (a test pins them equal). A row
# created in one of these, or moving into one, passes the activation check
# (owner ruling C2).
SEARCHABLE_STATUSES = frozenset({"active", "accepted"})

ACTIVATION_REFUSED_MESSAGE = (
    "credential material cannot become active memory; remove the secret, or redact the memory, and retry"
)


class CredentialActivationRefused(ValueError):
    """A memory row would become searchable while its text carries a credential.

    A ValueError, so a surface that does not catch it still answers with its
    ordinary validation contract (the MCP registry maps every ValueError to a
    tool error); the approve doors pass their own error class instead.
    """


def refuse_credential_activation(
    title: object,
    text: object,
    summary: object = None,
    *reasons: object,
    error: Callable[[str], BaseException] = CredentialActivationRefused,
) -> None:
    """The activation check, one helper every approve and accept path calls.

    It reads the row's title, canonical text and summary as the row will be
    stored (derived copies left out), never provenance keys, then any reason
    the action persists with it, in one call. The SQLite and Postgres memory
    writers call it whenever a row is created in, or moves into, a
    searchable status; every approve door also calls it before it writes, so
    the refusal arrives in the door's own error contract. Making it a no-op
    must fail every approve door's test.
    """

    verdict = credential_verdict(*stored_text_fields(title, text, summary), *reasons)
    if verdict == VERDICT_EXPANSION:
        raise error(EXPANSION_REFUSED_MESSAGE)
    if verdict is not None:
        raise error(ACTIVATION_REFUSED_MESSAGE)


RATIONALE_WITHHELD_PLACEHOLDER = "rationale withheld: it carried credential material"
TEXT_WITHHELD_PLACEHOLDER = "text withheld: it carried credential material"


def withhold_credential_text(
    text: str | None, placeholder: str = RATIONALE_WITHHELD_PLACEHOLDER
) -> tuple[str | None, bool]:
    """(text, False) when clean, or (placeholder, True) when it carries a credential.

    For what a retirement stores as given: a reject's rationale, a forget's
    reason, a caller's canonical_text on reject. Owner rulings C6 and R2: a
    retirement always completes, so the text is replaced, never refused, and
    the response says so with rationale_withheld (or text_withheld). Text
    that expands past the cap is withheld the same way.
    """

    if text is None or not carries_credential_material(text):
        return text, False
    return placeholder, True


def stored_text_fields(title: object, text: object, summary: object = None) -> tuple[object, ...]:
    """(title, text, summary) as a door should pass them, derived copies left out.

    A title or summary that is the text, a prefix of it, or a "..." preview
    of it adds nothing a per-field scan has not already read, and its seam
    against the text is an artefact of how the row repeats itself.
    """

    fields: list[object] = []
    if title is not None and not is_derived_copy(title, text):
        fields.append(title)
    fields.append(text)
    if summary is not None and not is_derived_copy(summary, text):
        fields.append(summary)
    return tuple(fields)


__all__ = [
    "ACTIVATION_REFUSED_MESSAGE",
    "CREDENTIAL_MATERIAL_REFUSED_MESSAGE",
    "EXPANSION_FLOOR_CHARS",
    "EXPANSION_REFUSED_MESSAGE",
    "MAX_NORMALIZATION_EXPANSION",
    "RATIONALE_WITHHELD_PLACEHOLDER",
    "SEARCHABLE_STATUSES",
    "SECRET_PREFIX_PATTERNS",
    "TEXT_WITHHELD_PLACEHOLDER",
    "VERDICT_CREDENTIAL",
    "VERDICT_EXPANSION",
    "CredentialActivationRefused",
    "carries_credential_material",
    "credential_verdict",
    "is_derived_copy",
    "normalize_for_matching",
    "refuse_credential_activation",
    "refuse_credential_material",
    "scan_cost",
    "stored_text_fields",
    "string_values",
    "withhold_credential_text",
]
