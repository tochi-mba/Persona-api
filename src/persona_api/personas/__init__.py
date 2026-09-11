"""The identity card, and the reads that assemble a persona from its parts.

:mod:`.store` holds the card as rows. :mod:`.service` holds the rules about it and the
three reads an assistant actually calls: the identity block, the export, and recall.

Deliberately small, because the card is. Everything an assistant might want to remember
is a field or a note -- those grow without a migration, and this does not.
"""
