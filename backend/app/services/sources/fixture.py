"""Local fixture providers. They perform no network access and use sanitized data only."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.services.sources.base import FetchPage, FetchRequest, ProviderDescriptor
from app.services.sources.errors import InvalidProviderResponseError

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"
# Dataset names are an allowlist, never a path supplied by configuration.
BUNDLED_DATASETS = {"jobs": "jobs.json"}

FIXTURE_DESCRIPTOR = ProviderDescriptor(
    kind="fixture",
    version="fixture-provider-v1",
    display_name="Sanitized local fixture catalog",
    allowed_bases=frozenset({"local_fixture"}),
    requires_network=False,
)


class BundledFixtureProvider:
    """Serves a bundled sanitized dataset as one complete listing."""

    descriptor = FIXTURE_DESCRIPTOR

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        dataset = str(request.config.get("dataset", "jobs"))
        filename = BUNDLED_DATASETS.get(dataset)
        if filename is None:
            raise InvalidProviderResponseError("Fixture dataset is not in the bundled allowlist")
        try:
            records = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InvalidProviderResponseError("Bundled fixture dataset is unreadable") from exc
        return FetchPage(
            records=records, listing_complete=True, source_url=f"fixture://{dataset}"
        )


class StaticFixtureProvider:
    """In-memory records split into pages; used for seeding helpers and contract tests."""

    def __init__(
        self,
        records: Sequence[Any] | None = None,
        *,
        pages: Sequence[Sequence[Any]] | None = None,
        listing_complete: bool = False,
        descriptor: ProviderDescriptor = FIXTURE_DESCRIPTOR,
    ) -> None:
        if (records is None) == (pages is None):
            raise ValueError("Provide either records or pages")
        self._pages: list[Sequence[Any]] = [records] if records is not None else list(pages or [])
        self._listing_complete = listing_complete
        self.descriptor = descriptor

    def fetch_page(self, request: FetchRequest) -> FetchPage:
        index = int(request.cursor) if request.cursor else 0
        is_last = index >= len(self._pages) - 1
        return FetchPage(
            records=self._pages[index] if self._pages else [],
            next_cursor=None if is_last else str(index + 1),
            listing_complete=self._listing_complete,
            source_url="fixture://static",
        )
