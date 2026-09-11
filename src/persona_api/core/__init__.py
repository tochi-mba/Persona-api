"""Shared kernel: configuration, time, request context, logging, and the composition root.

Every layer may import from here. ``domain`` is the exception -- it stays free of even
this, so the rules about what a persona may hold can be reasoned about with nothing else
loaded.
"""
