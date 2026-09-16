"""Liveness and readiness.

The two routes that do not require a token, because a load balancer cannot hold one.
Everything they report is therefore written on the assumption that a stranger is reading
it: counts and yes/no answers, never a profile name, a field key or a note.

The split matters. ``/healthy`` says only that this process is running, and it must never
fail: an orchestrator restarts a container whose liveness check fails, so reporting
keyring there would have it restart a working process during keyring's outage. ``/ready``
is where the database and keyring are reported, one line each.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field

from persona_api.api.dependencies import ContainerDep
from persona_api.core.version import service_version

router = APIRouter(tags=["health"])

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


class LivenessResponse(BaseModel):
    """What ``GET /healthy`` returns.

    Deliberately says nothing about dependencies: a liveness endpoint that failed while
    keyring was down would have an orchestrator restart healthy processes, repeatedly,
    for somebody else's outage.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "alive",
                    "version": "0.1.0",
                    "environment": "production",
                    "uptime_seconds": 1204.5,
                }
            ]
        }
    )

    status: str = Field(description="Always 'alive'. This endpoint does no I/O and never fails.")
    version: str = Field(description="Running version of the service.")
    environment: str = Field(description="Which deployment this is.")
    uptime_seconds: float = Field(description="Seconds since the process started serving.")


class CheckResult(BaseModel):
    """One dependency's contribution to readiness."""

    status: str = Field(description="'ok' or 'degraded'.")
    detail: dict[str, object] = Field(
        default_factory=dict, description="Check-specific facts, such as whether keyring answered."
    )


class HealthResponse(BaseModel):
    """What ``GET /ready`` returns.

    Reported at both levels deliberately: the top-level status is what a load balancer
    reads, the per-check detail is what a human reads at three in the morning.

    Nothing here is sensitive. This endpoint requires no token, so every field on it is
    written assuming a stranger can read it -- counts and yes/no answers, never a profile,
    a key or a body.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "degraded",
                    "version": "0.1.0",
                    "environment": "production",
                    "uptime_seconds": 1204.5,
                    "checks": {
                        "database": {"status": "ok", "detail": {"personas": 7}},
                        "keyring": {
                            "status": "degraded",
                            "detail": {
                                "reachable": False,
                                "reason": "keyring's signing keys could not be fetched",
                                "fix": "check PERSONA_KEYRING_JWKS_URL is reachable",
                            },
                        },
                    },
                }
            ]
        }
    )

    status: str = Field(description="'ok' when every check passed, otherwise 'degraded'.")
    version: str = Field(description="Running version of the service.")
    environment: str = Field(description="Which deployment this is.")
    uptime_seconds: float = Field(description="Seconds since the process started serving.")
    checks: dict[str, CheckResult] = Field(description="Per-dependency results.")


@router.get(
    "/healthy",
    operation_id="get_health",
    summary="Check that the service process is running",
    description=(
        "Liveness only. It reports nothing about the database or keyring, because a "
        "liveness check that failed when a dependency did would have an orchestrator "
        "restart a healthy process during somebody else's outage. Needs no token. Use "
        "`check_readiness` to find out whether requests will actually work."
    ),
    response_model=LivenessResponse,
)
async def get_health(container: ContainerDep) -> LivenessResponse:
    """Report that this process is alive, whatever else is not."""
    return LivenessResponse(
        status="alive",
        version=service_version(),
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
    )


@router.get(
    "/ready",
    operation_id="check_readiness",
    summary="Check that every dependency this service needs is usable",
    description=(
        "Reports the persona database and the keyring whose tokens this service verifies. "
        "Answers 200 when everything is usable and 503 when any check fails, with the "
        "same body shape either way. Needs no token, and it reports no personal data -- "
        "counts and yes/no answers only."
    ),
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def check_readiness(container: ContainerDep, response: Response) -> HealthResponse:
    """Check every dependency and summarize."""
    # Fetches keyring's keys when none fresh are held, so a fresh process reports keyring's
    # real state rather than whatever earlier requests happened to find. A fetch that failed
    # is not retried within the refetch floor, so polling this does not hammer a keyring
    # that is already down.
    usable, reason = await container.jwks.healthy()
    # A reason means keyring did not answer: either no token can be verified at all, or
    # tokens are being verified against the keys a good fetch left behind.
    reachable = reason is None

    checks = {
        "database": CheckResult(
            status=STATUS_OK,
            # A count, and deliberately nothing else. A per-account or per-profile
            # breakdown on an unauthenticated endpoint would be an enumeration oracle
            # for who uses this service and how much.
            detail={"personas": await container.personas.count_all()},
        ),
        "keyring": CheckResult(
            # Degraded rather than dead, the same shape keyring uses for a sealed vault:
            # the process is fine, the database is fine, and what fails is every
            # authenticated request -- as a 503 rather than a 401.
            #
            # But only when no token could be verified. An outage survived on cached keys
            # is ok, because taking a working instance out of rotation would turn keyring's
            # outage into this service's; it still says keyring is unreachable, because the
            # cached keys will not be served for ever.
            status=STATUS_OK if usable else STATUS_DEGRADED,
            detail={
                "reachable": reachable,
                # Fixed text from the shared client, never a traceback and never the URL,
                # which may carry userinfo and would be handed to whoever can reach this.
                "reason": reason,
                "fix": None if reachable else "check PERSONA_KEYRING_JWKS_URL is reachable",
            },
        ),
    }

    healthy = all(check.status == STATUS_OK for check in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=STATUS_OK if healthy else STATUS_DEGRADED,
        version=service_version(),
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
        checks=checks,
    )
