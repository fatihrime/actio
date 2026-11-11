"""Every module that defines tables, in one list.

Alembic compares the tables these modules declare with the database, to write a
migration and to check that none is missing. A module left out of this list would be
invisible to it, so a test fails when a `models` module exists that isn't named here.
"""

import importlib

from actio.core.database import Base

MODULES = ("actio.chat.models", "actio.identity.models")

for _name in MODULES:
    importlib.import_module(_name)

metadata = Base.metadata
"""The description of every table, once the modules above are loaded."""
