"""The only package that knows keyring exists.

An import-linter contract says so: nothing outside ``persona_api.auth`` may import
``keyring_client``, ``jwt`` or ``httpx``. That is the fourth contract in ``pyproject.toml``
and the one most worth explaining, because it is the one that will be inconvenient.

persona-api's entire relationship with keyring is *verify this signed token against those
published keys*. It is a small relationship and it should stay small. The rules for it are
the family's, in ``keyring_client``; what this package adds is the audience this service
answers to and the translation of the library's verdict into this service's own errors. The
contract keeps every question this service asks of keyring, and every rule by which it
believes an answer, behind two modules a reviewer can read in a sitting.

``keyring_client`` is named in the contract as well as ``httpx``, and not for symmetry: the
same library carries a client for keyring's internal credential endpoints, which this
service has no reason to call, and importing it is one line.

Without the contract, the failure is predictable rather than hypothetical: somebody needs a
fact keyring holds -- a display name, a profile list, whether an account is disabled -- and
an ``httpx`` call or a ``keyring_client`` import appears in a router. It works, it is
reviewed, it is merged, and the boundary that made
``docs/adr/0007-local-jwks-verification.md`` a bounded decision is gone before anybody
notices, replaced by a service that is unavailable whenever keyring is.
"""
