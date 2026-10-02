"""Provider-agnostic source interface.

A provider only *fetches* raw records. It never writes to the database, never decides
authorization, and never receives candidate data. Everything after fetch (validation,
normalization, dedup, snapshots, lifecycle, audit) is owned by the pipeline so that
all providers get identical safety behavior.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol


def freeze(value: Any) -> Any:
    """Deep read-only copy: mappings become MappingProxyType, lists become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ProviderDescriptor:
    kind: str
    version: str
    display_name: str
    # Authorization bases this provider may operate under (see policy.ALLOWED_BASES).
    allowed_bases: frozenset[str]
    requires_network: bool
    min_interval_seconds: int = 0
    max_pages: int = 50
    max_records_per_run: int = 1000


@dataclass(frozen=True)
class FetchRequest:
    connector_id: str
    config: Mapping[str, Any]
    cursor: str | None = None
    page_index: int = 0
    # Set only for providers that declare `requires_network`: a ConnectorHttpClient bound to
    # the connector's egress policy. Providers must not open connections any other way.
    http: Any = None


@dataclass(frozen=True)
class FetchPage:
    """One page of raw provider records.

    `listing_complete` means the provider asserts the pages of this run together
    enumerate every currently listed job; only then may absent postings become stale.
    """

    records: Sequence[Any]
    next_cursor: str | None = None
    listing_complete: bool = False
    source_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SourceProvider(Protocol):
    descriptor: ProviderDescriptor

    def fetch_page(self, request: FetchRequest) -> FetchPage: ...
