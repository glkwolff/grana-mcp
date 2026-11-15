"""Schema migrations.

Version 1 is ``store/schema.sql``. Each later change is a file in this package
named ``NNNN_<description>.sql`` (``NNNN`` >= 0002), applied in order by
``store/db.py`` inside a transaction, which then records the version in the
``schema_version`` table. Never edit a migration after it has been released.
"""
