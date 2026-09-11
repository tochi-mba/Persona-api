"""Liveness and dependency health.

The one route that does not require authentication, because a load balancer cannot hold
a token. Everything it reports is therefore written on the assumption that a stranger is
reading it: counts and yes/no answers, never a profile name, a field key or a note.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field

from persona_api.api.dependencies import ContainerDep
from persona_api.core.version import service_version

router = APIRouter(tags=["health"])

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


class CheckResult(BaseModel):
    """One dependency's contribution to overall health."""

    status: str = Field(description="'ok' or 'degraded'.")
    detail: dict[str, object] = Field(
        default_factory=dict, description="Check-specific facts, such as whether keyring answered."
    )


class HealthResponse(BaseModel):
    """What ``GET /healthy`` returns.

    Reported at both levels deliberately: the top-level status is what a load balancer
    reads, the per-check detail is what a human reads at three in the morning.

    Nothing here is sensitive. This is the one endpoint that does not require a token,
    so every field on it is written assuming a stranger can read it -- counts and yes/no
    answers, never a profile, a key or a body.
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
    summary="Report service health",
    description=(
        "Returns the service version, uptime, and the state of every dependency: the "
        "persona database and the keyring whose tokens this service verifies. Responds "
        "200 when everything is usable and 503 when any check fails, with the same body "
        "shape either way. This is the only endpoint that does not require a token, and "
        "it reports no personal data -- counts and yes/no answers only."
    ),
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def get_health(container: ContainerDep, response: Response) -> HealthResponse:
    """Check every dependency and summarize."""
    reachable = container.jwks.is_reachable

    checks = {
        "database": CheckResult(
            status=STATUS_OK,
            # A count, and deliberately nothing else. A per-account or per-profile
            # breakdown on an unauthenticated endpoint would be an enumeration oracle
            # for who uses this service and how much.
            detail={"personas": await container.personas.count_all()},
        ),
        "keyring": CheckResult(
            # Unreachable is degraded rather than dead, the same shape keyring uses for
            # a sealed vault: the process is fine, the database is fine, and a request
            # whose kid is already cached still works. What fails is a token with an
            # uncached kid, and it fails as a 503 rather than a 401.
            #
            # `None` means nothing has needed a key yet, which is not the same as being
            # down -- reporting it as down would have every fresh deployment start
            # degraded before anybody had called it.
            status=STATUS_DEGRADED if reachable is False else STATUS_OK,
            detail={
                "reachable": reachable,
                "fix": (
                    "check PERSONA_KEYRING_JWKS_URL is reachable" if reachable is False else None
                ),
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
