"""Domain error vocabulary.

These describe what went wrong in persona terms. Translating them into HTTP status codes
is the API layer's job -- nothing here knows what a status code is.

Two rules shape the module, and both are inherited from keyring rather than invented:

**An error must not reveal whether something exists.** A caller reaching for another
account's persona gets the same :class:`PersonaNotFoundError` they would get for a profile
nobody has ever used. There is no ``PersonaBelongsToSomeoneElseError``, and adding one
would be a security change rather than a diagnostic improvement -- see
``docs/adr/0003-no-administrative-surface.md``.

**Every authentication failure is one error.** Bad signature, wrong audience, wrong
issuer, expired, missing claim: one :class:`AuthenticationError`, one message, one body.
A caller holding a forged token learns nothing from the difference.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error this package raises deliberately."""


class AuthenticationError(DomainError):
    """A token was not accepted.

    Deliberately undifferentiated -- see the module docstring. The specific reason goes
    to the logs, where only the operator reads it.
    """


class KeyringUnreachableError(DomainError):
    """keyring could not be reached to fetch the keys that verify its tokens.

    Distinct from :class:`AuthenticationError` on purpose: nothing is wrong with the
    caller's token, and telling them it was rejected would send them to re-authenticate
    against a service that is down. This is the operator's problem and is reported as
    one -- 503, and ``/healthy`` says so.
    """


class InvalidProfileError(DomainError, ValueError):
    """A profile name cannot be stored or addressed as given.

    Shape only. persona-api **cannot check that a profile exists in keyring** -- there is
    no endpoint for it, and a signed token carries no profile list. A typo therefore
    makes a new empty persona rather than an error, which is why ``list_personas`` exists
    and why ``docs/adr/0005-one-persona-per-profile.md`` says so out loud.
    """


class PersonaNotFoundError(DomainError):
    """No persona for that profile is owned by this account.

    Raised identically whether the persona does not exist or belongs to somebody else.
    """


class PersonaExistsError(DomainError):
    """This account already has a persona for that profile."""


class InvalidFieldKeyError(DomainError, ValueError):
    """A field key cannot be stored as given, even after normalization.

    Keys are normalized rather than rejected wherever that is possible -- "Favourite
    Topics" becomes ``favourite_topics`` -- because the alternative is three fields
    holding one fact. This is for what normalization cannot rescue: an empty key, one
    that is only punctuation, or one that is too long.
    """


class InvalidFieldValueError(DomainError, ValueError):
    """A field value is not something a persona may hold.

    Names which limit it failed -- size, depth, list length, object width -- because a
    caller told only "invalid" has to bisect its own payload to find out.
    """


class InvalidNoteError(DomainError, ValueError):
    """A note cannot be stored as given: empty, too long, or an unknown kind."""


class FieldNotFoundError(DomainError):
    """No live field by that key in this persona."""


class NoteNotFoundError(DomainError):
    """No live note by that id in this persona."""


class CredentialRefusedError(DomainError, ValueError):
    """The text offered looks like a credential, so it was not stored.

    A persona is not a vault, and there is **no setting that disables this check**. The
    message names keyring, because somebody who just tried to save an API key here needs
    to be told where it goes rather than merely told no.

    Carries ``reason`` -- which rule matched -- so the refusal can say what it saw
    without ever quoting the value back.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class InvalidSearchError(DomainError, ValueError):
    """A search query contains nothing that can be searched for.

    Its own error rather than an empty result, because "no matches" and "you did not ask
    for anything" are different answers and a model reading the first will try harder
    against a query that can never match.
    """


class InvalidCursorError(DomainError, ValueError):
    """A pagination cursor was not one this service issued."""


class LimitExceededError(DomainError):
    """A per-account or per-persona limit would be exceeded.

    Names the limit, so a caller can act on it. Every one of these is raised from inside
    the transaction that does the write -- a cap checked before the write is a cap two
    concurrent writers both pass.
    """


class PreferencesUnavailableError(DomainError):
    """A person's settings were needed and could not be read honestly.

    Either settings-api refused this service -- a grant it was not given, a token it does
    not recognise -- or it cannot be reached and the setting in question is one that must
    not be guessed at. Neither is the caller's doing, so it is not a 4xx.
    """
