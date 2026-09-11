"""The append-only record of persona changes.

Sits below ``memory`` and ``personas`` in the layering, so a store can record an event
inside the transaction that made the change -- see
:meth:`persona_api.events.log.EventLog.append`. That ordering is the whole reason this is
its own layer rather than a function on a store: a write and its event have to commit
together, and the only way to arrange that is for the thing doing the writing to be able
to reach the thing doing the recording without reaching sideways.
"""
