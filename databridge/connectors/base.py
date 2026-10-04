"""Connector plugin contract.

A connector declares a Pydantic config model; the Studio renders the connection form from it,
so adding a connector needs no UI code. Fields listed in `secret_fields` are stored encrypted.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

import polars as pl
from pydantic import BaseModel


@dataclass
class Node:
    """One entry in the Object Explorer tree."""

    name: str
    kind: str  # folder | file | sheet | schema | table | view
    ref: dict[str, Any]
    has_children: bool = False
    detail: str = ""


@dataclass
class TestResult:
    ok: bool
    message: str
    info: dict[str, Any] = field(default_factory=dict)


class Connector(ABC):
    type_name: ClassVar[str]
    label: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    secret_fields: ClassVar[set[str]] = set()
    capabilities: ClassVar[set[str]] = set()
    form_exclude: ClassVar[set[str]] = set()  # config fields set by other means (e.g. certificate uploads)

    def __init__(self, config: dict[str, Any], secrets: dict[str, Any] | None = None):
        self.cfg = self.config_model(**{**config, **(secrets or {})})

    @abstractmethod
    def test(self) -> TestResult: ...

    @abstractmethod
    def browse(self, ref: dict[str, Any] | None = None) -> list[Node]:
        """Children of `ref` (or top level when None)."""

    @abstractmethod
    def describe(self, ref: dict[str, Any]) -> list[dict[str, Any]]:
        """Field list (name, type, nullable, ...) for a readable object."""

    @abstractmethod
    def preview(self, ref: dict[str, Any], limit: int = 100) -> pl.DataFrame: ...

    @abstractmethod
    def read(self, ref: dict[str, Any], batch_size: int = 50_000) -> Iterator[pl.DataFrame]:
        """Yields batches so large objects never load into memory at once."""

    def close(self) -> None:  # pragma: no cover - optional
        pass


def split_config(connector_cls: type[Connector], values: dict[str, Any]) -> tuple[dict, dict]:
    """Splits form values into (plain config, secrets)."""
    plain = {k: v for k, v in values.items() if k not in connector_cls.secret_fields}
    secret = {k: v for k, v in values.items() if k in connector_cls.secret_fields}
    return plain, secret
