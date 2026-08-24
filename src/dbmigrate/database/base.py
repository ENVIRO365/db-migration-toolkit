"""Re-export adapter contract from the package root for convenience."""

from dbmigrate.database import (  # noqa: F401
    Database,
    get_adapter,
    list_adapters,
    register_adapter,
)
