from collections.abc import Callable

from app.services.sources.base import SourceProvider
from app.services.sources.errors import SourceErrorCode, SourcePolicyError

ProviderFactory = Callable[[], SourceProvider]


class ProviderRegistry:
    """Maps a connector `kind` to reviewed provider code. Unknown kinds fail closed."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, kind: str, factory: ProviderFactory, *, replace: bool = False) -> None:
        if not kind or (kind in self._factories and not replace):
            raise ValueError(f"Provider kind already registered or invalid: {kind!r}")
        self._factories[kind] = factory

    def kinds(self) -> list[str]:
        return sorted(self._factories)

    def create(self, kind: str) -> SourceProvider:
        factory = self._factories.get(kind)
        if factory is None:
            raise SourcePolicyError(
                SourceErrorCode.PROVIDER_UNKNOWN, "No reviewed provider is registered for kind"
            )
        provider = factory()
        if provider.descriptor.kind != kind:
            raise SourcePolicyError(
                SourceErrorCode.PROVIDER_MISMATCH, "Provider descriptor kind does not match"
            )
        return provider


def default_registry() -> ProviderRegistry:
    # Reviewed providers only. Network providers still fail closed unless live access is
    # enabled, the host is allowlisted, and the connector's authorization is complete.
    from app.services.sources.ashby import AshbyJobBoardProvider
    from app.services.sources.fixture import BundledFixtureProvider, SandboxFixtureProvider

    registry = ProviderRegistry()
    registry.register("fixture", BundledFixtureProvider)
    registry.register("ashby_job_board", AshbyJobBoardProvider)
    registry.register("sandbox_ats_fixture", SandboxFixtureProvider)
    return registry
