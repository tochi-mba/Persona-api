"""The half the service exists for: writing memories down, and getting them back.

``FieldStore`` and ``NoteStore`` are the two write paths; ``search``, the filters and
cursor pagination are the read side. Three things here are not obvious and are covered in
the module docstrings rather than repeated:

* The full-text index is written by the stores, explicitly, inside the same transaction
  as the row -- not by external-content tables and not by triggers. See :mod:`.fields`.
* Every search query is sanitised before it reaches ``MATCH``. See :mod:`.search`.
* Pagination is keyset, not offset, so a walk across a concurrent write cannot show a row
  twice or skip one. See :mod:`.pagination`.
"""
