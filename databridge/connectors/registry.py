"""Connector registry: maps type_name -> class and builds instances from stored Connections."""

from typing import Any

from databridge.connectors.base import Connector
from databridge.connectors.database import DatabaseConnector
from databridge.connectors.filesystem import FileSystemConnector
from databridge.connectors.rest import RestConnector
from databridge.core.security import decrypt_json

CONNECTORS: dict[str, type[Connector]] = {
    FileSystemConnector.type_name: FileSystemConnector,
    DatabaseConnector.type_name: DatabaseConnector,
    RestConnector.type_name: RestConnector,
}


def connector_class(type_name: str) -> type[Connector]:
    try:
        return CONNECTORS[type_name]
    except KeyError as e:
        raise ValueError(f"Unknown connector type {type_name!r}") from e


def build_connector(type_name: str, config: dict[str, Any], secret_token: str | None) -> Connector:
    return connector_class(type_name)(config, decrypt_json(secret_token))


def register(cls: type[Connector]) -> type[Connector]:
    """Decorator for third-party connectors: @register class MyConnector(Connector): ..."""
    CONNECTORS[cls.type_name] = cls
    return cls
