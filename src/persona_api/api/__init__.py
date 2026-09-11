"""The HTTP surface, and the contract it is.

Route ``operation_id``\\ s are public API: they become MCP tool names, so renaming one is
a breaking change for every client with a tool bound to it. A contract test pins the
exact set and requires a real description on every operation, because those descriptions
are what a model reads to decide whether and how to call something.

Handlers are thin on purpose. They raise domain errors and let :mod:`.errors` decide what
that means over HTTP, which is what keeps status-code decisions in one readable place --
including the one that matters most, that a cross-account read is 404 rather than 403.
"""
