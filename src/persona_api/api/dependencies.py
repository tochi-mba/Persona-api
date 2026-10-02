"""FastAPI dependency wiring.

The container is built once at startup and parked on the app; these turn it into typed
parameters so handlers never reach into application state themselves.

:data:`CurrentCallerDep` is the important one, and it is the *only* way a request becomes
an account in this service. That is what makes "every ``/v1`` route is scoped to the
caller" a fact about the code rather than a convention that holds until somebody forgets
a decorator -- there is no other source of an account id, and no route accepts one as a
parameter.

It is also where ``asserted_by`` comes from. The audience of the verified token, read
from the verified claims, never from a request body -- which is exactly what makes it the
trustworthy half of a field's provenance. See
``docs/adr/0004-provenance-is-partly-a-claim.md``.

:data:`ProfileDep` is how ``@default`` becomes a persona. Every route with a ``{profile}``
segment takes the profile through it, so the reserved segment means the same thing on
every one of them, and a route cannot forget to honour it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from persona_api.auth.verifier import VerifiedCaller
from persona_api.core.container import Container
from persona_api.core.context import set_account_id
from persona_api.core.preferences import Preferences
from persona_api.domain.errors import AuthenticationError, InvalidProfileError
from persona_api.domain.personas import DEFAULT_PERSONA_SEGMENT, normalize_profile

bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "A short-lived RS256 token from keyring, minted with "
        '`POST /v1/auth/service-token {"audience": "persona"}`.'
    ),
)
"""``auto_error=False`` so a missing header raises our error, in our problem+json shape.

Left to itself, HTTPBearer raises a bare 403 with a plain JSON body -- a different status
and a different shape from every other failure this service produces, on the single most
common mistake a caller can make.
"""

MISSING_CREDENTIALS = "a keyring token is required"


def get_container(request: Request) -> Container:
    """Return the container assembled during startup."""
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_current_caller(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VerifiedCaller:
    """Resolve the bearer token to the account it was minted for.

    Binds the account into the request context as a side effect, so every log record
    produced by the rest of the request says who it was for without any handler passing
    it along.

    Raises:
        AuthenticationError: no token, or one that is not accepted. Undifferentiated.
        KeyringUnreachableError: the keys could not be fetched -- not the caller's fault
            and deliberately not their error.
    """
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)

    caller = await container.verifier.verify(credentials.credentials)
    set_account_id(caller.account_id)
    return caller


CurrentCallerDep = Annotated[VerifiedCaller, Depends(get_current_caller)]


async def resolve_profile(container: Container, caller: VerifiedCaller, profile: str) -> str:
    """The profile a path segment names: itself, or for ``@default`` the person's default.

    With no ``persona.default_persona`` chosen -- or settings-api off, or unable to say --
    the segment is handed on unchanged, and it fails normalization exactly as it always
    has: a 422 naming the profile rule. The catalogue calls that the conservative answer,
    because loading *a* persona the person did not choose is worse than failing loudly.

    Matched after the same trim and case-fold a profile gets, so ``@Default`` is not a
    different, unstorable profile from ``@default``.
    """
    if profile.strip().lower() != DEFAULT_PERSONA_SEGMENT:
        return profile
    preferences = await container.preferences.for_token(caller.token)
    return preferences.default_persona or profile


async def get_profile(
    container: ContainerDep,
    caller: CurrentCallerDep,
    profile: Annotated[
        str,
        Path(
            description=(
                "The persona's profile, such as 'work'. Or '@default', for the persona "
                "this person chose as their default in settings-api "
                "(persona.default_persona); with none chosen, '@default' is refused."
            )
        ),
    ],
) -> str:
    """The ``{profile}`` path segment, with ``@default`` resolved."""
    return await resolve_profile(container, caller, profile)


ProfileDep = Annotated[str, Depends(get_profile)]


async def get_preferences(
    container: ContainerDep, caller: CurrentCallerDep, request: Request
) -> Preferences:
    """This caller's caps, for the profile the path names: their own, or the deployment's.

    ``persona.recall_default_limit`` is profile-scoped in settings-api, so a read that
    names no profile gets the catalogue default rather than what this person chose for
    the persona they are paging through. The profile is taken from the path rather than
    declared as a parameter, so no route gains a ``profile`` query parameter in its
    contract, and a route with no profile in its path -- ``recall_everywhere`` -- asks
    for none. ``@default`` is resolved first, so the person's caps are the ones for the
    persona it stands for. A name that cannot be stored is not sent: the route refuses
    it on its own.
    """
    profile: str | None = request.path_params.get("profile")
    if profile is not None:
        try:
            profile = normalize_profile(await resolve_profile(container, caller, profile))
        except InvalidProfileError:
            profile = None
    return await container.preferences.for_token(caller.token, profile)


PreferencesDep = Annotated[Preferences, Depends(get_preferences)]


async def page_limit(
    container: ContainerDep,
    preferences: PreferencesDep,
    limit: Annotated[
        int | None,
        Query(
            ge=1,
            description=(
                "Rows per page. Defaults to this person's PERSONA_RECALL_DEFAULT_LIMIT "
                "(or the deployment's, when settings-api is off) and may not exceed "
                "PERSONA_RECALL_MAX_LIMIT (20 and 100 unless the operator changed them)."
            ),
        ),
    ] = None,
) -> int:
    """The page size for this request: the caller's own, or the deployment's default.

    Read from preferences per request rather than written into each route's signature.
    The ``= 20`` and ``le=100`` this replaced were second copies of
    ``recall_default_limit`` and ``recall_max_limit`` that nothing kept in step, so
    changing either setting changed nothing at all. When settings-api is on, the default
    is this person's, narrowed by the deployment's.

    Raises:
        RequestValidationError: above the configured maximum, in exactly the shape any
            other out-of-range query parameter is refused in.
        PreferencesUnavailableError: settings-api refused this service.
    """
    if limit is None:
        return preferences.recall_default_limit
    if limit > container.settings.recall_max_limit:
        raise RequestValidationError(
            [
                {
                    "type": "less_than_equal",
                    "loc": ("query", "limit"),
                    "msg": (
                        "Input should be less than or equal to "
                        f"{container.settings.recall_max_limit}"
                    ),
                    "input": limit,
                }
            ]
        )
    return limit


LimitQuery = Annotated[int, Depends(page_limit)]
