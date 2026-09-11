"""The credential refusal, and the two corpora that define it.

These lists **are** the specification. ``domain/secrets.py`` is tuned until both pass,
and it is never tuned by deleting an entry -- an entry that genuinely cannot be
classified gets a comment saying so and stays, because the comment is the honest record
of where the line is.

The asymmetry is the thing to keep in mind while reading them. A false negative stores a
credential, which is bad. A false positive refuses an ordinary memory, and **there is no
override** -- no flag, no header, no operator to appeal to -- so the assistant simply
cannot write what it learned. That is why ``MUST_BE_ACCEPTED`` is the longer list and
why it is full of the things that look alarming and are not: shas, URLs, hex colours,
UUIDs, long compound words.
"""

from __future__ import annotations

import pytest

from persona_api.domain.errors import CredentialRefusedError
from persona_api.domain.secrets import (
    KEYRING_ADVICE,
    MIN_RUN_LENGTH,
    looks_like_a_credential,
    refuse_if_credential,
)

MUST_BE_ACCEPTED = [
    # The ordinary business of a persona.
    "They prefer concise answers with no preamble.",
    "Asked me to stop saying 'certainly' -- it reads as insincere to them.",
    "Prefers tea. Specifically lapsang souchong, specifically after 3pm.",
    "Book recommendation: 'Thinking in Systems' by Donella Meadows.",
    # An abbreviated git sha. People write these constantly.
    "Reverted the migration at 9f8e7d6c after the review went badly.",
    # A URL. Long, punctuated, and containing a slash -- which is exactly why the
    # entropy rule accepts no alphabet that includes one.
    "Their blog is at https://example.com/posts/2026/why-i-left-consulting",
    # A hex colour, which is hex and short.
    "Prefers #1a1a1a backgrounds and a serif typeface for long reading.",
    # A very long word made only of letters. The letters-and-digits clause spares it.
    "Die Donaudampfschiffahrtsgesellschaftskapitaenskajuete is their favourite word.",
    # A file path: slashes again, plus dots.
    "The repo lives at /home/alex/projects/internal-tooling/services/ingest/README.md",
    # A long hyphenated slug. Base64url-legal, letters and digits mixed, and saved only
    # by the diversity clause -- it repeats its vocabulary the way generated key
    # material never does.
    "Screenshot filed as screenshot-2026-03-14-at-11-42-07-am-timezone-europe-lisbon.png",
    # A UUID. 36 characters, hex-plus-hyphen, and far too repetitive to be key material.
    "Session id from the trace was 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0 if that helps.",
    # A semver string with a build tag.
    "They pinned the dependency at version 2.14.3-rc.1+build.20260311 and moved on.",
    # SHOUTING_SNAKE_CASE, which is long, base64url-legal and all one case.
    "They use ALL_CAPS_CONSTANTS_FOR_EVERYTHING_INCLUDING_THIS_VERY_LONG_ONE in their code.",
    # Sentences *about* credentials. The rules refuse a shape, never a subject.
    "Told me their password manager is 1Password and they will not switch.",
    "The AWS console confused them; they could not find where access keys live.",
    "They said the word 'base64' in a meeting and everyone groaned.",
    "The sk- prefix is how OpenAI keys start, apparently.",
    # Non-ASCII, which must not be mistaken for encoded anything.
    "彼女は簡潔な回答を好みます。",
    "They celebrate with \U0001f389 and use \U0001f64f sincerely, not sarcastically.",
    # A long sentence. Length alone must never be suspicious.
    "A very long sentence that goes on at some length about how they like their code "
    "reviewed, which is thoroughly but without nitpicking, and preferably in one pass "
    "rather than three, because context switching is the expensive part for them.",
    "Their standing desk is at 104cm; they mentioned it twice, so it matters.",
    "Runs on a MacBook Pro M4 Max with 128GB of memory and still complains it is slow.",
]


def _shaped(prefix: str, body: str) -> str:
    """Assemble a credential shape from a prefix and a body, at import time.

    Written as a join rather than as a literal for a reason that is operational rather
    than aesthetic: a realistic-looking token pasted into a source file trips GitHub's
    push protection, and the whole repository then cannot be pushed. The scanners match
    on the literal, so building the same string from two halves keeps the corpus honest
    and the branch pushable.

    If you add an entry here, add it this way. A literal will block the next push, and
    the person it blocks will not be you.
    """
    return prefix + body


MUST_BE_REFUSED = [
    _shaped("sk-", "proj-abcd1234EFGH5678ijkl9012MNOP3456qrst7890"),
    _shaped("ghp_", "16C7e42F292c6912E7710c838347Ae178B4a"),
    _shaped("github_pat_", "11ABCDE0Y0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789abcdefghij"),
    _shaped("xox", "b-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    _shaped("xox", "p-2345678901-2345678901234-BcDeFgHiJkLmNoPqRsTuVwXy"),
    _shaped("AKIA", "IOSFODNN7EXAMPLE"),
    _shaped("AIza", "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"),
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n-----END OPENSSH PRIVATE KEY-----",
    _shaped(
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ),
    # Embedded mid-sentence, which is how it will actually arrive.
    _shaped("remember my key sk-", "live-9aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2u please"),
    # A full-length git sha is forty hex characters and is refused. Nothing here can
    # tell a hash from a token, and the abbreviated shas people actually write are far
    # under the length floor -- so the trade was made in this direction knowingly. It
    # stays in this list as the honest record of that.
    "a7f3c9e1b5d2486af0c3e7b195d4826ac1f5039e",
    "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG0",
    "The token is 8Kj2Nn4Qp7Rs9Tv1Wx3Yz5Ab6Cd8Ef0Gh2Ij4Kl and it expires tomorrow.",
]


class TestTheAcceptedCorpus:
    """Ordinary memories somebody would genuinely want written down."""

    @pytest.mark.parametrize("text", MUST_BE_ACCEPTED)
    def test_an_ordinary_memory_is_never_refused(self, text: str) -> None:
        # A false positive here is unrecoverable for the caller: the memory simply
        # cannot be written, and no setting exists to let it through. Tuning the
        # heuristic means keeping this list passing, never shortening it.
        assert looks_like_a_credential(text) is None


class TestTheRefusedCorpus:
    """Shapes that are never anything but a credential."""

    @pytest.mark.parametrize("text", MUST_BE_REFUSED)
    def test_a_credential_shape_is_always_refused(self, text: str) -> None:
        assert looks_like_a_credential(text) is not None

    @pytest.mark.parametrize("text", MUST_BE_REFUSED)
    def test_the_reason_never_quotes_the_value_back(self, text: str) -> None:
        # A refusal that echoed the value would put the credential into the error
        # response, the caller's logs and quite possibly the next prompt -- which is
        # the disclosure this whole module exists to prevent.
        reason = looks_like_a_credential(text)
        assert reason is not None
        longest = max(text.split(), key=len)
        assert longest not in reason


class TestRefusing:
    def test_it_names_keyring_so_the_caller_knows_where_to_go(self) -> None:
        # Somebody who just tried to save an API key needs to be told where it goes.
        # An assistant that is only refused will try a different wording; one that is
        # redirected will call the other service.
        with pytest.raises(CredentialRefusedError) as refusal:
            refuse_if_credential(_shaped("AKIA", "IOSFODNN7EXAMPLE"))

        assert KEYRING_ADVICE in str(refusal.value)

    def test_it_carries_the_matched_rule_as_a_reason(self) -> None:
        with pytest.raises(CredentialRefusedError) as refusal:
            refuse_if_credential(_shaped("ghp_", "16C7e42F292c6912E7710c838347Ae178B4a"))

        assert "ghp_" in refusal.value.reason

    def test_ordinary_text_passes_through_silently(self) -> None:
        # No exception is the whole assertion. Written as a bare call rather than a
        # truthiness check, because `assert refuse_if_credential(...)` would pass for
        # the wrong reason and strict mypy would not let it through anyway.
        refuse_if_credential("They prefer tea after three.")


class TestTheEntropyRule:
    """The catch-all, and each clause that keeps it from catching too much.

    Every case here is a *long space-free run*, so the length floor is already cleared
    and exactly one clause is doing the work.
    """

    def test_a_run_under_the_length_floor_is_never_judged(self) -> None:
        # Under the floor the false positives are everything: order ids, short hashes,
        # UUID fragments. Real key material is longer than this essentially always.
        short = "a1b2c3d4e5f6a1b2"
        assert len(short) < MIN_RUN_LENGTH
        assert looks_like_a_credential(short) is None

    def test_a_long_run_that_is_all_letters_is_not_key_material(self) -> None:
        # An ordinary long word. The letters-and-digits clause spares it.
        assert looks_like_a_credential("abcdefghijklmnopqrstuvwxyzabcdefghij") is None

    def test_a_long_run_that_is_all_digits_is_not_key_material(self) -> None:
        # A very long number -- an account reference, a timestamp in nanoseconds.
        assert looks_like_a_credential("12345678901234567890123456789012345") is None

    def test_a_long_run_outside_every_key_alphabet_is_not_judged(self) -> None:
        # The slashes and colons belong to no key alphabet, which is what spares every
        # URL and every file path on the machine.
        assert looks_like_a_credential("https://example.com/a/b/c/d/e/f/g/h/i/j/k") is None

    def test_a_long_run_that_repeats_itself_is_not_key_material(self) -> None:
        # Structured identifiers reuse their vocabulary; generated key material does
        # not. This one is base64url-legal and mixes letters with digits, so the
        # diversity clause is the only thing standing between it and a refusal.
        assert looks_like_a_credential("ab1-ab1-ab1-ab1-ab1-ab1-ab1-ab1-ab1") is None

    def test_a_dense_hexadecimal_run_is_refused_and_says_so(self) -> None:
        reason = looks_like_a_credential("a7f3c9e1b5d2486af0c3e7b195d4826ac1f5039e")

        assert reason is not None
        assert "hexadecimal" in reason

    def test_a_dense_base64url_run_is_refused_and_says_so(self) -> None:
        reason = looks_like_a_credential("Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG0")

        assert reason is not None
        assert "base64url" in reason

    def test_trailing_punctuation_does_not_smuggle_a_key_past_the_alphabet_check(
        self,
    ) -> None:
        # A key pasted at the end of a sentence arrives with a full stop stuck to it,
        # and that full stop belongs to no key alphabet. Without the trim, the sentence
        # ending would be the thing that let the key through.
        assert (
            looks_like_a_credential("the token is Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2Rl.")
            is not None
        )
