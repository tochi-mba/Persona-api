"""Shared kernel: configuration, time, logging, preferences, the composition root.

Every layer may import from here. ``domain`` is the exception -- it stays free of even
this, so the rules about what a persona may hold can be reasoned about with nothing else
loaded.
"""
