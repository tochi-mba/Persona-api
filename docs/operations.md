# Operations

How to run persona-api, back it up, and check it is behaving. Short, because there is not
much of it: one process, one file, one outbound dependency.

## What it needs

| | |
| --- | --- |
| Python | 3.11 or 3.12 (both are in CI) |
| Disk | one SQLite file plus its `-wal`/`-shm` sidecars |
| Network, inbound | one port, behind a TLS-terminating proxy |
| Network, outbound | keyring's JWKS URL, and settings-api when `PERSONA_SETTINGS_API_BASE_URL` is set |
| Secrets | **none**, unless settings-api is on, in which case `PERSONA_SETTINGS_API_TOKEN` |

That last row is worth pausing on. Without settings-api, persona-api has no master key, no
signing key, no admin token and no service credential. It verifies somebody else's
signatures with a public key it fetches over HTTP. Turning settings-api on adds one
service token, held only to prove which service is calling, never to impersonate a
person.

## Configuration

Every knob is an environment variable prefixed `PERSONA_`. See
[`.env.example`](../.env.example) for the full list with commentary.

**A `PERSONA_`-prefixed variable that matches no setting is a startup error**, naming every
offender at once, not one restart per typo. `PERSONA_AUDEINCE=persona` would otherwise
leave the audience on its default with nothing in the logs to say so.

The four that a deployment must actually think about:

| Setting | Why it matters |
| --- | --- |
| `PERSONA_KEYRING_ISSUER` | Pinned against the token's `iss`. Must match keyring's `KEYRING_ISSUER` **exactly**. A mismatch refuses every token, identically and unhelpfully — which is by design, and is the first thing to check when nothing authenticates. |
| `PERSONA_KEYRING_JWKS_URL` | Where the verifying key comes from. Reachable from this process, over a network you trust to the same degree you trust keyring. |
| `PERSONA_AUDIENCE` | Pinned against the token's `aud`, exactly: `persona.work` is refused like `media-tool`. keyring mints a token for whatever audience it is asked for, so nothing needs setting there — in particular persona-api needs **no** entry in keyring's `KEYRING_SERVICE_TOKENS`, which only admits services to keyring's `/v1/internal` endpoints. |
| `PERSONA_DATABASE_PATH` | See the next section. |

### settings-api

Off by default. Both `PERSONA_SETTINGS_API_BASE_URL` and `PERSONA_SETTINGS_API_TOKEN`
must be set together, or neither; half a pair is a startup error. The token is checked
with the same 32-character rule settings-api enforces, and is never echoed on failure.

When on, each authenticated request that needs a page size or a pin ceiling asks
settings-api for that caller's `persona` namespace, presenting the same user token
keyring minted. The grant there needs `audience_prefix` equal to `PERSONA_AUDIENCE`
(`persona` unless you changed it). A person may **lower** `recall_default_limit`,
`max_pinned_fields` and `max_pinned_notes`; they cannot raise them above this
deployment, and `PERSONA_RECALL_MAX_LIMIT` remains the hard cap on a named page size.

If settings-api has never answered, those three fall back to the configuration. If it
refuses this service (401/403), the request is **503** with a fixed body that names
neither the grant nor the URL.

`default_persona`, `log_values`, `erasure_mode` and `grace_days` are in the catalogue
and do nothing here. Honouring them means this service grows a default-persona
resolution and a sweeper; until it does, setting them stores a value and changes
nothing.

Do not fetch settings at startup. An empty URL keeps today's behaviour exactly.

### Deliberate absences

There is no setting that disables token verification, none that lets one account read
another's persona, none that turns off the credential refusal, and none that permits an
unsigned or `HS256` token. If you are looking for one of those, the answer is in
[docs/adr/](adr/) rather than in a flag.

## The database file, and why its mode matters more here

One SQLite file. WAL mode. `synchronous = NORMAL` — see
[ADR-0008](adr/0008-sqlite.md) for why that differs from keyring's `FULL`.

**Nothing in this file is encrypted.** keyring encrypts its credential material, so a
world-readable file there leaks metadata. Here a world-readable file leaks *everything*: a
person's own words about themselves, and an assistant's notes about them, in plaintext.

The service creates the file `0600` and the directory `0700`, and it re-applies the mode to
the `-wal` and `-shm` sidecars, because SQLite creates them `0644`. Check it:

```bash
stat -c '%a %n' var/persona.db var/persona.db-wal var/persona.db-shm
# 600 var/persona.db
# 600 var/persona.db-wal
# 600 var/persona.db-shm
```

Put it somewhere nothing serves from. Never inside a static directory, a backup directory
another process syncs, or a container volume shared with anything else.

### Backups

```bash
sqlite3 var/persona.db "VACUUM INTO '/backups/persona-$(date -u +%Y%m%dT%H%M%SZ).db'"
chmod 600 /backups/persona-*.db
```

**`VACUUM INTO`, not `cp`.** A plain copy of a live WAL database can miss committed
transactions that are still in the write-ahead log, which produces a backup that restores
cleanly and is quietly missing the last few minutes.

**`chmod` the result.** `VACUUM INTO` creates the destination with the process umask, not
with the source's mode. A 0600 database backed up into a 0644 file is a 0644 database.

Restoring is `cp` in the other direction, with the service stopped.

## Health

`GET /healthy` and `GET /ready` are the only unauthenticated endpoints, and everything on
them is written on the assumption that a stranger is reading it: counts and yes/no answers,
never a profile name, a field key or a note. Liveness does no I/O and never fails;
readiness is where the database and keyring are reported.

```json
{
  "status": "degraded",
  "version": "0.1.0",
  "environment": "production",
  "uptime_seconds": 1204.5,
  "checks": {
    "database": {"status": "ok", "detail": {"personas": 7}},
    "keyring": {"status": "degraded",
                "detail": {"reachable": false,
                           "reason": "keyring's signing keys could not be fetched",
                           "fix": "check PERSONA_KEYRING_JWKS_URL is reachable"}}
  }
}
```

`/ready` asks keyring for its keys itself whenever it holds none fresh, so a fresh process
reports keyring's real state rather than waiting for a token to find out. A fetch that
failed is not retried until `PERSONA_JWKS_MIN_REFETCH_SECONDS` has passed, so polling the
endpoint does not hammer a keyring that is already down.

**keyring unreachable is `degraded`, not dead** — the same shape keyring uses for a sealed
vault. The process is fine and the database is fine; what fails is every authenticated
request, with **503 and a problem body** rather than a 401, because nothing is wrong with the
caller's token and telling them it was rejected sends them to re-authenticate against a
service that is down.

**Unless keys are already held.** Keys from a successful fetch keep verifying tokens through
an outage for up to a day past `PERSONA_JWKS_CACHE_SECONDS`. That is reported as `ok` — the
instance still works, and taking it out of rotation would turn keyring's outage into this
service's — but with `reachable: false` and a `reason` saying tokens are being verified
against cached keys, so an operator finds out before the grace runs out.

`degraded` returns HTTP 503 so a load balancer takes the instance out; the body shape is
identical either way. The image's `HEALTHCHECK` wants a 200, so a container started while
keyring is down reports unhealthy until keyring answers.

## Logs

Structured, JSON in deployment (`PERSONA_LOG_FORMAT=json`). Every record carries the
request id, and the account id once a request has been authenticated.

A redaction processor runs **before anything is rendered**, and it redacts by field name.
It covers the usual credential names — which matters here because the *refusal* path is the
one most likely to want to log what it refused, and a refused value is still a live API key
— and it also covers `value_json` and `body`. A persona is not a secret, but it is
personal, and a log aggregator is not a place to accumulate what an assistant has noticed
about somebody.

There is a test asserting the processor is installed in the configured pipeline rather than
merely written.

## Running it

```bash
make run                                   # :8004 with reload, docs at /docs
uv run persona-api                         # the entry point a deployment uses
GITHUB_TOKEN="$(gh auth token)" docker build --secret id=github_token,env=GITHUB_TOKEN -t persona-api:local . && docker run -p 8004:8004 \
  -e PERSONA_KEYRING_ISSUER=https://keyring.example \
  -e PERSONA_KEYRING_JWKS_URL=https://keyring.example/.well-known/jwks.json \
  -v persona-data:/var/lib/persona persona-api:local
```

`PERSONA_KEYRING_ISSUER` and `PERSONA_KEYRING_JWKS_URL` are deliberately **not** baked into
the image. They name the keyring this deployment trusts, and a default in the image is how
a container ends up trusting the wrong one.

## Verifying a deployment

```bash
make smoke      # against a persona-api already running on :8099
```

`scripts/smoke.py` mints a real token against a real keyring, creates a persona, sets a
field, writes a note, recalls both, forgets one, confirms it is gone from recall but
present with `include_forgotten`, and confirms a second account sees none of it. It exits
non-zero on the first failure, so it doubles as a deployment check.

Two refusals worth confirming by hand after any change to `domain/secrets.py`:

```bash
# both must be 422, and the message must name keyring
curl -sX PUT .../v1/personas/work/fields/api_key -d '{"value":"sk-live-abc…","description":"x"}'
curl -sX POST .../v1/personas/work/notes -d '{"body":"-----BEGIN RSA PRIVATE KEY-----…"}'
```

## Retention, and the things that grow

| | Bound |
| --- | --- |
| Personas per account | `PERSONA_MAX_PERSONAS_PER_ACCOUNT` (20) |
| Fields per persona | `PERSONA_MAX_FIELDS_PER_PERSONA` (500) |
| Notes per persona | `PERSONA_MAX_NOTES_PER_PERSONA` (5000) |
| Pinned fields / notes | `PERSONA_MAX_PINNED_FIELDS` / `_NOTES` (20 each); a person may lower these via settings-api |
| Events | `PERSONA_MAX_EVENTS` (10000), trimmed in the same transaction as the insert |

Every one of these is enforced **inside the transaction that does the write**. A caller
that counts and then writes has a window in between, and two concurrent writes both pass
it; there is an `asyncio.gather` test per cap that would find it if that ever regressed.

**Forgotten rows are not reclaimed.** A soft forget sets a tombstone and stops the row
appearing in reads; the text stays in the table and in the full-text index so
`?include_forgotten=true` can bring it back. They still count against nothing — the caps
above count live rows — but they do occupy disk. `DELETE /v1/personas/{profile}` is the
only hard delete, and it cascades.

## What to do when somebody asks for their data to be gone

They delete it: `DELETE /v1/personas/{profile}` is a hard delete, it cascades to every
field, note and index row, and it is theirs to call.

**There is no operator path**, by design — see
[ADR-0003](adr/0003-no-administrative-surface.md). An endpoint that let you delete
somebody's persona for them would need to be able to find it, which is the one thing this
service must never let anybody but its owner do. If the account is gone from keyring and
nobody can mint a token for it, the rows are unreachable through the API and the remaining
option is SQL on the box by whoever runs it.
