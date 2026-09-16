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
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from persona_api.auth.verifier import VerifiedCaller
from persona_api.core.container import Container
from persona_api.core.context import set_account_id
from persona_api.core.preferences import Preferences
from persona_api.domain.errors import AuthenticationError

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


async def get_preferences(container: ContainerDep, caller: CurrentCallerDep) -> Preferences:
    """This caller's caps: their own, or the deployment's when settings-api is off."""
    return await container.preferences.for_token(caller.token)


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
