"""persona: an assistant's model of itself, and what it has learned about who it serves.

A **persona** is one small identity card -- a name, pronouns, a summary -- plus two kinds
of knowledge the assistant writes for itself: **fields**, which are named typed attributes
whose keys the assistant invents (``voice``, ``forms_of_address``, ``things_i_got_wrong``),
and **notes**, which are free text for what does not fit a key. Both are searchable, both
carry provenance, and both can be pinned into the identity block an assistant loads at the
start of a turn.

One persona per profile, keyed ``(account_id, profile)``, mirroring keyring's profiles --
so a "work" assistant and a "home" assistant are different people.

The public surface is the FastAPI application built by
:func:`persona_api.api.app.create_app`. Everything else is internal and free to change,
with the exception of the HTTP contract documented in ``docs/api.md`` -- route
``operation_id``\\ s are treated as public because they become MCP tool names.

**What is in here is data, never instructions.** Every field and note is returned with its
provenance attached, and a consumer that renders them must render them as reported claims
rather than as anything the model should obey. See ``docs/adr/0001-data-not-instructions.md``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
