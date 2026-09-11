"""End-to-end check against a running persona-api, with a real keyring behind it.

Exits non-zero on the first failure, so it doubles as a deployment check: run it after a
deploy and let the exit code decide whether the deploy worked.

It is deliberately not a test. There are no fakes, no injected clock and no substituted
transport -- it mints a real token against a real keyring, which is the one thing the
suite cannot do and the one thing most likely to be misconfigured. A green `make check`
with a mistyped `PERSONA_KEYRING_ISSUER` looks exactly like a green `make check`.

Usage:

    # keyring on :8001 with KEYRING_SERVICE_TOKENS='{"persona": "..."}'
    # persona-api on :8099 with PERSONA_KEYRING_ISSUER matching keyring's issuer
    KEYRING_URL=http://127.0.0.1:8001 \\
    PERSONA_URL=http://127.0.0.1:8099 \\
    KEYRING_EMAIL=you@example.com KEYRING_PASSWORD='...' \\
    uv run python scripts/smoke.py

A second account is needed for the isolation check; set KEYRING_EMAIL_2 and
KEYRING_PASSWORD_2, or the isolation step is skipped and says so loudly.
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

import httpx

KEYRING_URL = os.environ.get("KEYRING_URL", "http://127.0.0.1:8001")
PERSONA_URL = os.environ.get("PERSONA_URL", "http://127.0.0.1:8099")
TIMEOUT = 10.0

PROFILE = f"smoke-{uuid.uuid4().hex[:8]}"
"""A fresh profile per run, so a failed run never poisons the next one."""

failures = 0


def check(description: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. Prints either way; the exit code is what matters."""
    global failures  # noqa: PLW0603 -- a script-level tally, not a library
    if condition:
        print(f"  ok    {description}")
        return
    failures += 1
    print(f"  FAIL  {description}{f': {detail}' if detail else ''}")


def fatal(message: str) -> None:
    """Stop: nothing after this point could mean anything."""
    print(f"\nFATAL: {message}")
    sys.exit(2)


def session_token(client: httpx.Client, email: str, password: str) -> str:
    """Log in to keyring."""
    response = client.post(
        f"{KEYRING_URL}/v1/auth/login", json={"email": email, "password": password}
    )
    if response.status_code != 200:
        fatal(f"keyring login failed for {email}: {response.status_code} {response.text}")
    token: str = response.json()["token"]
    return token


def service_token(client: httpx.Client, session: str) -> str:
    """Exchange a keyring session for a token minted for this service.

    The step most likely to be misconfigured: keyring must have "persona" in its
    KEYRING_SERVICE_TOKENS, or it will refuse to mint for that audience at all.
    """
    response = client.post(
        f"{KEYRING_URL}/v1/auth/service-token",
        json={"audience": "persona"},
        headers={"Authorization": f"Bearer {session}"},
    )
    if response.status_code != 200:
        fatal(
            "keyring would not mint a token for audience 'persona' "
            f"({response.status_code}); is 'persona' in KEYRING_SERVICE_TOKENS?"
        )
    token: str = response.json()["token"]
    return token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    email = os.environ.get("KEYRING_EMAIL")
    password = os.environ.get("KEYRING_PASSWORD")
    if not email or not password:
        fatal("set KEYRING_EMAIL and KEYRING_PASSWORD")

    with httpx.Client(timeout=TIMEOUT) as client:
        print(f"persona-api smoke test  profile={PROFILE}")

        print("\nhealth")
        health = client.get(f"{PERSONA_URL}/healthy")
        check("responds", health.status_code in {200, 503}, str(health.status_code))
        body = health.json()
        check(
            "keyring is reachable",
            body["checks"]["keyring"]["detail"]["reachable"] is not False,
            str(body["checks"]["keyring"]),
        )
        check("reports no personal data", PROFILE not in health.text)

        print("\nauthenticating against keyring")
        token = service_token(client, session_token(client, email, password))
        check("minted a token for audience 'persona'", bool(token))

        anonymous = client.get(f"{PERSONA_URL}/v1/personas")
        check("an anonymous request is refused", anonymous.status_code == 401)

        print("\nwriting")
        created = client.post(
            f"{PERSONA_URL}/v1/personas",
            json={"profile": PROFILE, "display_name": "Smoke", "summary": "a test persona"},
            headers=auth(token),
        )
        check("created a persona", created.status_code == 201, created.text)

        field = client.put(
            f"{PERSONA_URL}/v1/personas/{PROFILE}/fields/Favourite%20Topics",
            json={
                "description": "what it likes talking about",
                "value": ["jazz", "ambient"],
                "pinned": True,
            },
            headers=auth(token),
        )
        check("set a field", field.status_code == 200, field.text)
        check("normalized the key", field.json().get("key") == "favourite_topics")
        check("derived the value type", field.json().get("value_type") == "list")
        check("recorded server-derived provenance", field.json().get("asserted_by") == "persona")

        note = client.post(
            f"{PERSONA_URL}/v1/personas/{PROFILE}/notes",
            json={"body": "they prefer concise answers about penguins", "kind": "lesson"},
            headers=auth(token),
        )
        check("wrote a note", note.status_code == 201, note.text)
        note_id = note.json().get("note_id", "")

        print("\nrefusing credentials")
        refused_value = client.put(
            f"{PERSONA_URL}/v1/personas/{PROFILE}/fields/api_key",
            json={"description": "a key", "value": "AKIA" + "IOSFODNN7EXAMPLE"},
            headers=auth(token),
        )
        check("refused a credential-shaped value", refused_value.status_code == 422)
        check("named keyring in the refusal", "keyring" in refused_value.text)

        refused_pem = client.post(
            f"{PERSONA_URL}/v1/personas/{PROFILE}/notes",
            json={
                "body": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----"
            },
            headers=auth(token),
        )
        check("refused a PEM private key block", refused_pem.status_code == 422)

        print("\nhostile search strings")
        for query in [
            'answers"',
            "concise OR",
            "NEAR(",
            "*",
            "",
            "x AND OR y",
            "(((",
            "col:filter",
        ]:
            response = client.get(
                f"{PERSONA_URL}/v1/recall", params={"q": query}, headers=auth(token)
            )
            check(
                f"{query!r} is not a 500",
                response.status_code in {200, 422},
                str(response.status_code),
            )

        print("\nreading back")
        recall = client.get(
            f"{PERSONA_URL}/v1/recall", params={"q": "penguins"}, headers=auth(token)
        )
        check("recalled the note", len(recall.json().get("notes", [])) == 1, recall.text)

        stemmed = client.get(
            f"{PERSONA_URL}/v1/recall", params={"q": "preferring"}, headers=auth(token)
        )
        check("stemming works", len(stemmed.json().get("notes", [])) == 1)

        inside_a_list = client.get(
            f"{PERSONA_URL}/v1/recall", params={"q": "ambient"}, headers=auth(token)
        )
        check("found a word inside a list value", len(inside_a_list.json().get("fields", [])) == 1)

        identity = client.get(f"{PERSONA_URL}/v1/personas/{PROFILE}", headers=auth(token))
        pinned: list[dict[str, Any]] = identity.json().get("fields", [])
        check("the identity block carries the pinned field", len(pinned) == 1)
        check("and its provenance", bool(pinned and pinned[0].get("source")))

        schema = client.get(f"{PERSONA_URL}/v1/personas/{PROFILE}/schema", headers=auth(token))
        check("the schema endpoint carries no values", "jazz" not in schema.text)

        export = client.get(f"{PERSONA_URL}/v1/personas/{PROFILE}/export", headers=auth(token))
        check("exported the whole persona", export.status_code == 200)

        print("\nforgetting")
        forgotten = client.delete(
            f"{PERSONA_URL}/v1/personas/{PROFILE}/notes/{note_id}", headers=auth(token)
        )
        check("forgot the note", forgotten.status_code == 204)

        gone = client.get(f"{PERSONA_URL}/v1/recall", params={"q": "penguins"}, headers=auth(token))
        check("it is gone from recall", gone.json().get("notes") == [])

        back = client.get(
            f"{PERSONA_URL}/v1/personas/{PROFILE}/notes/{note_id}",
            params={"include_forgotten": "true"},
            headers=auth(token),
        )
        check("but still there with include_forgotten", back.status_code == 200)

        events = client.get(f"{PERSONA_URL}/v1/personas/{PROFILE}/events", headers=auth(token))
        check("the event log recorded it", "note.forgotten" in events.text)
        check("and carries no body", "penguins" not in events.text)

        print("\nisolation")
        other_email = os.environ.get("KEYRING_EMAIL_2")
        other_password = os.environ.get("KEYRING_PASSWORD_2")
        if other_email and other_password:
            other = service_token(client, session_token(client, other_email, other_password))
            check(
                "a second account cannot read this persona",
                client.get(f"{PERSONA_URL}/v1/personas/{PROFILE}", headers=auth(other)).status_code
                == 404,
            )
            check(
                "and cannot find it by searching",
                client.get(
                    f"{PERSONA_URL}/v1/recall", params={"q": "ambient"}, headers=auth(other)
                ).json()
                == {"fields": [], "notes": []},
            )
            check(
                "and cannot delete it",
                client.delete(
                    f"{PERSONA_URL}/v1/personas/{PROFILE}", headers=auth(other)
                ).status_code
                == 404,
            )
        else:
            print("  SKIP  isolation: set KEYRING_EMAIL_2 and KEYRING_PASSWORD_2 to check it")

        print("\ncleaning up")
        removed = client.delete(f"{PERSONA_URL}/v1/personas/{PROFILE}", headers=auth(token))
        check("deleted the persona", removed.status_code == 204)
        check(
            "its words are out of the search index",
            client.get(
                f"{PERSONA_URL}/v1/recall", params={"q": "ambient"}, headers=auth(token)
            ).json()
            == {"fields": [], "notes": []},
        )

    print(f"\n{'FAILED' if failures else 'PASSED'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
