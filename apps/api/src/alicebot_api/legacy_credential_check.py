"""v0.16.0's credential check, in linear time, at the two doors v0.16.0 checked.

Why it exists (S4.4 round 5, 2026-09-23). A third adversary on 6c24a97 found
eight shapes that v0.16.0 refused at the commit door and the branch stored:
dotted values under secret names, Telegram tokens, xxd hex dumps of key
files, unarmored DER under PRIVATE_KEY_DATA and similar names, numeric prose
passwords, primary and routing keys, hex AES keys, and Django SECRET_KEY
values with symbols. A maintainer confirmed the class is structural: v0.16.0
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
- The sleep writer (``run_local_vault_sleep``) uses this commit check as
  well as the floor. A sleep row exists only to be handed to the commit
  door, so the writer refuses a proposal that door would refuse.
- `create_continuity_object_record` calls `commit_door_secret_verdict`
  on the title and on the body's string values, not on a JSON dump of
  the body. The helper runs the credential floor and then
  `commit_gate_refuses`. The record also runs the floor on the stored
  body before that call.
- Capture and the source doctor use ``commit_door_fields_verdict``: the
  floor, then this commit gate on every string. A low-entropy AKIA-shaped
  key the floor treats as a placeholder is refused there, as the commit
  door already refuses it. Every other remaining door keeps
  credential_floor alone.

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

Two differences from v0.16.0 (2026-10-22). The first: a token marker needs a
token body.
v0.16.0 read the bare marker, so a short product code that starts with one,
SK-2291 or SK-229 or "sk-2291 is current", was a credential, and so was
npm_config_cache. A marker now counts only when at least a floor of token
characters follows it, and each floor is no longer than the shortest body a
real token of that kind has (see _TOKEN_BODIES, which states the shape behind
every number and where it comes from). The two ssh markers keep the bare
reading: they name a public key line, which the first carve-out excuses.
Nothing else changed: the commit gate's sk- prefix pattern takes the floor of
the sk- marker, and the other patterns, its other prefix patterns (ghp_,
xoxb-, akia) and the carve-outs are v0.16.0's, so a marker followed by a body
of a listed shape is refused as before, and only a marker with too little
after it to be one is let through.

The second: an underscore before a token marker no longer hides it. v0.16.0's
lead boundary was "no letter, digit or underscore before", so _ghs_ and a
stateless token, x_ghs_ and a classic one, TOKEN_npm_ and 36 characters were
never read by the marker rule. With a floor on every token marker the
underscore is no longer needed to keep ordinary names out, so a marker with a
floor is hidden only by a letter or a digit before it (a marker glued to a
word is part of that word). The ssh markers have no floor and keep v0.16.0's
lead. So a token of these shapes is withheld or refused when its marker starts
the text or follows a space, punctuation or an underscore; a marker glued
directly onto a letter or digit (as in X followed by a ghs_ token) is not read,
as in v0.21.0. tests/unit/test_legacy_credential_check.py runs its comparison with
v0.16.0's own code with the floors and the lead turned off, to show that these
are the only differences, and tests/unit/test_credential_marker_lengths.py
pins the floors and the lead.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Iterator, Mapping
import re
from typing import NamedTuple

from alicebot_api.credential_floor import (
    VERDICT_CREDENTIAL,
    credential_verdict,
    _prep_normal,
    _prep_variants,
    string_values,
)

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


class _TokenBody(NamedTuple):
    """What must follow a marker for it to count: ``floor`` characters of ``chars``.

    ``chars`` is the inside of a regular expression character class. The
    floor is read as a look-ahead of exactly that many characters of one
    class, so the pattern is as linear as the bare marker was: the engine
    finds the marker literal, then looks at most ``floor`` characters ahead
    and stops at the first character outside the class.

    ``owned`` is a regular expression for the text a carve-out names, which
    is read after the marker whatever its length, so that the carve-out still
    decides it, boundary and all (see _FIDO_KEY_TYPE_NAMES).
    """

    chars: str
    floor: int
    owned: str = ""


# The two FIDO key type names that follow "sk-" (carve-out a). Their body is
# short and ends at "@", so the floor alone would stop reading them, and the
# boundary rule that refuses a token glued straight onto one would go with it.
_FIDO_KEY_TYPE_NAMES = r"(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com"

_BASE62 = "0-9A-Za-z"
_BASE62_UNDERSCORE = "0-9A-Za-z_"
_URLSAFE = "0-9A-Za-z_-"
_SLACK_CHARS = "0-9A-Za-z-"
# GitHub's own pattern for ghs_ tokens: letters, digits, dots, hyphens and underscores.
_GITHUB_APP_CHARS = "0-9A-Za-z._-"

# The shape of the token each marker targets, the shortest body such a token
# has after the marker, and where that number comes from. A floor is no longer
# than that body, so a marker followed by a body of the listed shape is read
# (when it starts the text or follows a space, punctuation or an underscore;
# glued directly onto a letter or digit it is not read, as in v0.21.0), and
# long enough that a part code, a ticket id or a variable name is not. A body is
# counted in the characters of the class beside it, which are the characters
# the real body is made of. Where no provider publishes a minimum, the entry
# says so, and the floor is a choice below the shortest shape known.
#
# - ghp_ (personal access token, classic) and gho_ (OAuth access token).
#   Source: GitHub's 2021 post on its token formats: the prefix, then 36
#   base62 characters (30 random and a 6 character checksum), 40 in all.
#   Shortest body: 36. Floor: 36.
# - ghs_ (installation token). Source: GitHub's changelog of 2026-05-15 on
#   installation tokens, which says to treat them as opaque and to match both
#   formats with ghs_[A-Za-z0-9\.\-_]{36,}. The classic form is the prefix
#   and 36 base62 characters. The stateless form is the prefix, a numeric app
#   id, an underscore and a JWT of three base64url parts with two dots, about
#   520 characters in all, so the underscore and the dots belong to the body:
#   with base62 alone a short app id ended the run before 36 characters and
#   the token was not read. Shortest body: 36, which is the number in GitHub's
#   pattern. Floor: 36.
# - ghu_ (user-to-server token). The 36 base62 characters are from the same
#   2021 post as ghp_. GitHub's changelog of 2026-05-15 gives its pattern for
#   ghs_ only, and says that ghu_ will change format too, without details. So
#   ghu_ takes the ghs_ class and floor by our choice, not by a published
#   pattern for ghu_. Floor: 36.
#   The cost for both: a ghs_ or ghu_ followed by 36 or more identifier
#   characters (letters, digits, dots, hyphens, underscores) is read, as
#   GitHub's pattern reads ghs_. A snake_case name shorter than that, GHS_2291
#   and ghu_config are not.
# - github_pat_ (fine-grained personal access token). The prefix, then 22
#   base62 characters, an underscore and 59 base62 characters, 93 in all.
#   No GitHub page states the length that this module has found; 82 is the
#   length public secret scanning rules use. Shortest body: 82. Floor: 82.
# - glpat- (GitLab personal, project and group access tokens). GitLab
#   documents the prefix. The 20 characters of [0-9A-Za-z_-] after it are the
#   length public secret scanning rules use for the fixed length format, and
#   newer routable tokens are longer. Shortest body: 20. Floor: 20.
# - npm_ (npm access tokens). The prefix, then 36 base62 characters, 40 in
#   all, the same shape as GitHub's tokens. No npm page states the length that
#   this module has found; 36 is the length public secret scanning rules use.
#   Shortest body: 36. Floor: 36. An environment variable such as
#   npm_config_cache or npm_package_version has underscores, which a base62
#   body does not, so none is read as a token.
# - sk- (OpenAI style secret keys). No provider publishes a minimum length.
#   The older OpenAI keys are the prefix and 48 base62 characters, and
#   project, service account and admin keys (sk-proj-, sk-svcacct-, sk-admin-)
#   are the prefix, a kind word and 100 or more characters of [0-9A-Za-z_-];
#   both are shapes seen, not shapes documented. Anthropic keys
#   (sk-ant-api03-, sk-ant-admin01-) show the prefix, a kind word and about 90
#   more of the same characters in its examples. Some OpenAI compatible
#   providers issue the prefix and 32 hex characters, also seen and not
#   documented. Shortest body seen: 32. Floor: 20, a choice below it, so a
#   compatible provider's shorter key is still read, and far above the 4 to 6
#   digits of a part code such as SK-2291 or SK-22910. The commit gate's own
#   prefix pattern (_PREFIX_SK) is a separate rule with the same floor, taken
#   from here. The two FIDO key type names (sk-ssh-ed25519@openssh.com and
#   sk-ecdsa-sha2-nistp256@openssh.com) are still read at any length, so that
#   carve-out a keeps refusing a token glued straight onto one.
# - xoxb-, xoxp-, xoxa-, xoxs- (Slack bot, user, app and legacy tokens). Slack
#   publishes no minimum length and tells integrations to treat tokens as
#   opaque. The shapes seen: the prefix, then groups of digits and hyphens,
#   then an alphanumeric or hex tail. A bot token is 10 to 13 digits, a
#   hyphen, 10 to 13 digits, a hyphen and 24 alphanumerics: 46 characters at
#   the shortest. User and legacy tokens carry a third digit group and a 32
#   character tail, 65 or more. Older Slack tokens may be shorter than any of
#   these and are not covered by a source. Floor: 10, which is not derived
#   from a real token: it is the floor of the slack rule below (v0.16.0's
#   own, xox[baprs]- and 10 token characters), so the two agree.
# - ssh-rsa and ssh-ed25519, each followed by a space. These name a public
#   key line, not a secret, and carve_out_ssh_public_key excuses the public
#   key shapes. They keep the bare reading: a space after a key type name is
#   not how a product code is written, and a floor would not change what a
#   real private key is refused for.
_TOKEN_BODIES: Mapping[str, _TokenBody] = {
    "ghp_": _TokenBody(_BASE62, 36),
    "gho_": _TokenBody(_BASE62, 36),
    "ghs_": _TokenBody(_GITHUB_APP_CHARS, 36),
    "ghu_": _TokenBody(_GITHUB_APP_CHARS, 36),
    "github_pat_": _TokenBody(_BASE62_UNDERSCORE, 82),
    "glpat-": _TokenBody(_URLSAFE, 20),
    "npm_": _TokenBody(_BASE62, 36),
    "sk-": _TokenBody(_URLSAFE, 20, _FIDO_KEY_TYPE_NAMES),
    "xoxb-": _TokenBody(_SLACK_CHARS, 10),
    "xoxp-": _TokenBody(_SLACK_CHARS, 10),
    "xoxa-": _TokenBody(_SLACK_CHARS, 10),
    "xoxs-": _TokenBody(_SLACK_CHARS, 10),
}


def _marker_pattern(marker: str, *, bounded: bool) -> re.Pattern[str]:
    """The marker, with the floor of token characters after it when it has one.

    ``bounded`` puts a lead boundary in front. v0.16.0's was: no letter, digit
    or underscore before the marker, so a token right after an underscore
    (``_ghs_...``, ``TOKEN_npm_...``) was never read. A marker with a floor now
    gives up only the underscore: a letter or digit before it still makes it
    part of a word, but "_" does not (2026-10-22). That is safe because the
    floor, not the lead, keeps ordinary names out: ``PART_SK-2291`` and
    ``deploy_ghu_config`` have too little after the marker. The two ssh
    markers have no floor and keep v0.16.0's lead. A collapsed run (see
    _despaced_runs) lost its boundaries and is read without any.
    """

    body = _TOKEN_BODIES.get(marker)
    floor = ""
    if body is not None:
        reads = "[" + body.chars + "]{" + str(body.floor) + "}"
        floor = "(?=" + reads + ("|" + body.owned if body.owned else "") + ")"
    lead = ""
    if bounded:
        lead = r"(?<![0-9A-Za-z])" if body is not None else r"(?<![0-9A-Za-z_])"
    return re.compile(lead + re.escape(marker) + floor, re.IGNORECASE)


_CREDENTIAL_TOKEN_PATTERNS = tuple(_marker_pattern(marker, bounded=True) for marker in _CREDENTIAL_TOKEN_MARKERS)
_CREDENTIAL_TOKEN_UNBOUNDED_PATTERNS = tuple(
    _marker_pattern(marker, bounded=False) for marker in _CREDENTIAL_TOKEN_MARKERS
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

# The commit gate's own prefix patterns, read on the casefolded field. The sk-
# one takes the floor of the sk- marker (2026-10-22): v0.16.0 read "sk-" and 8
# token characters, which made SK-20260301, SK-2025-0618 and SK-2291-REV keys.
# A real sk- key has 32 or more characters after the prefix. The FIDO key type
# names are short, and the marker pattern above still reads them at any length,
# so they stay with their carve-out. The ghp_ and xoxb- patterns below still
# read 8 characters, and are unchanged.
_PREFIX_SK = re.compile(r"(?<![0-9a-z])sk-[0-9a-z_-]{" + str(_TOKEN_BODIES["sk-"].floor) + r",}")
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
        # substrings of the casefolded run. The floor still applies: a
        # spaced-out "S K - 2 2 9 1" is a part code too.
        for marker, pattern in zip(_CREDENTIAL_TOKEN_MARKERS, _CREDENTIAL_TOKEN_UNBOUNDED_PATTERNS):
            for match in pattern.finditer(folded):
                yield Finding(
                    "marker", text if same_length else folded, match.start(), value=marker, exact=same_length
                )
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


def _floor_findings(*texts: str | None, _prep: dict | None = None) -> Iterator[Finding]:
    """v0.16.0's looks_like_credential: every field raw and normalised, the
    normalised fields joined with a space and with nothing, each surface
    decoded and despaced.

    ``_prep`` is the caller's per-call cache; a cached surface's normalised
    form and variants are exactly what this function would recompute."""

    present = [text for text in texts if text]
    if not present:
        return
    normalized = [_prep_normal(_prep, text) for text in present]
    glued = "".join(normalized)
    surfaces = [*present, *normalized, " ".join(normalized), glued]
    seams: list[int] = []
    for text in normalized[:-1]:
        seams.append((seams[-1] if seams else 0) + len(text))
    # A repeated surface gives the same findings, so each is read once.
    for surface in dict.fromkeys(surfaces):
        yield from _credential_findings(surface, seams=tuple(seams) if surface is glued else ())
        surface_decoded, surface_despaced = _prep_variants(_prep, surface)
        for variant in surface_decoded:
            yield from _credential_findings(variant)
        for variant in surface_despaced:
            yield from _credential_findings(variant, require_token_boundary=False)


def _commit_field_findings(text: str, _prep: dict | None = None) -> Iterator[Finding]:
    """v0.16.0's _contains_secret_marker, on one field.

    v0.16.0 then ran the NAME=value rule on the raw field a second time; the
    raw field is already a surface of the floor check, so that pass is not
    repeated here.
    """

    yield from _floor_findings(text, _prep=_prep)
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
    _prep: dict | None = None,
) -> Iterator[Finding]:
    fields = [title, canonical_text]
    if conversation_excerpt:
        fields.append(conversation_excerpt)
    if rationale:
        fields.append(rationale)
    fields.extend(_flatten_text(source_refs))
    for field in fields:
        yield from _commit_field_findings(field, _prep)


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
_FIDO_KEY_TYPE = re.compile(r"sk-" + _FIDO_KEY_TYPE_NAMES + _BOUNDARY)
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
    """What v0.16.0's commit gate decided, carve-outs not applied, with the token floors of the module docstring."""

    return next(_commit_findings(title, canonical_text, conversation_excerpt, rationale, source_refs), None) is not None


def v0160_promotion_floor_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    source_refs: Iterable[object] = (),
) -> bool:
    """What v0.16.0's promotion floor decided, carve-outs not applied, with the token floors of the module docstring."""

    return next(_floor_findings_for(title, canonical_text, conversation_excerpt, source_refs), None) is not None


def commit_gate_refuses(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    rationale: str | None = None,
    source_refs: object = (),
    *,
    _prep: dict | None = None,
) -> bool:
    """v0.16.0's commit check less the carve-outs: True on the first finding none excuses.

    ``_prep`` is the caller's per-call cache: each string's normalised form
    and its decoded and despaced variants are computed once and reused, so
    the answer is identical with and without it.
    """

    return any(
        not excused(finding)
        for finding in _commit_findings(title, canonical_text, conversation_excerpt, rationale, source_refs, _prep)
    )


def commit_door_secret_verdict(
    title: str,
    canonical_text: str,
    conversation_excerpt: str | None = None,
    rationale: str | None = None,
    source_refs: object = (),
    *identifiers: object,
) -> str | None:
    """The commit door's check for one write.

    Returns the floor's verdict when ``credential_verdict`` refuses, or
    ``VERDICT_CREDENTIAL`` when ``commit_gate_refuses`` refuses. ``identifiers``
    are read by the floor only, in the order given. The commit door passes
    idempotency_key, trace_id, and project_scope there. A single sleep text
    is passed as ``canonical_text``.
    """

    verdict = credential_verdict(
        title,
        canonical_text,
        conversation_excerpt,
        source_refs,
        rationale,
        *identifiers,
    )
    if verdict is not None:
        return verdict
    if commit_gate_refuses(
        title,
        canonical_text,
        conversation_excerpt,
        rationale,
        source_refs,
    ):
        return VERDICT_CREDENTIAL
    return None


def commit_door_fields_verdict(*fields: object) -> str | None:
    """The commit door's verdict over every string in ``fields``.

    ``credential_verdict`` first. When that is clean, the legacy commit
    gate runs on each string. Capture and the doctor use this so they
    refuse a low-entropy AKIA-shaped key the floor leaves alone.

    One per-call cache carries each string's normalised form and its
    decoded and despaced variants from the floor half to the gate half of
    this call, so no string is normalised or decoded twice here. The
    cached values are exactly what each half would recompute, so the
    verdict is identical with and without the cache.
    """

    prep: dict = {}
    verdict = credential_verdict(*fields, _prep=prep)
    if verdict is not None:
        return verdict
    for text in string_values(fields):
        if text and commit_gate_refuses("", text, _prep=prep):
            return VERDICT_CREDENTIAL
    return None


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
    "commit_door_fields_verdict",
    "commit_door_secret_verdict",
    "commit_gate_refuses",
    "excused",
    "looks_like_secret_value",
    "password_kind",
    "promotion_floor_refuses",
    "secret_assignment_values",
    "v0160_commit_gate_refuses",
    "v0160_promotion_floor_refuses",
]
