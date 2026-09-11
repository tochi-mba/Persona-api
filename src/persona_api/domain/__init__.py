"""Pure types and rules: what a persona is, and what may be written into one.

This package imports nothing else from ``persona_api`` -- not the storage layer, not the
API layer, and deliberately not ``core`` either. An import-linter contract enforces it,
because a domain that quietly grows a dependency on configuration stops being readable
in isolation long before anybody notices, and the rules kept here are exactly the ones
that have to be readable: which text is refused, how a key is folded onto an existing
one, what a value may hold.

Three consequences of the rule are worth stating, because each one looks like an
omission until you know it is a decision:

* **Nothing here reads a clock.** Timestamps arrive as arguments. ``Persona``, ``Field``
  and ``Note`` carry the times they were given and compute none of them.
* **Nothing here reads configuration.** The limits that a deployment can tune are passed
  in -- :class:`~persona_api.domain.fields.ValueLimits` for values, ``limit`` for a note
  body -- while the limits that are structural, like the length of a field key, are
  constants in the module that enforces them.
* **Nothing here talks about HTTP.** Failures are the errors in
  :mod:`persona_api.domain.errors`; turning one into a status code is the API layer's
  job.
"""
