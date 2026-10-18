"""Extension isolation and AI response tests; endpoints are injected fakes."""

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from myne.errors import ProviderError
from myne.mix import AIMixStrategy, MixService, MoodSearchStrategy
from myne.models import Track
from myne.plugins import PluginRegistry
from myne.providers.local import LocalMusicProvider


def entry(name: str, register: Any) -> Mock:
    result = Mock()
    result.name = name
    result.load.return_value = register
    return result


def test_registry_default_providers_are_lazy_and_can_be_configured() -> None:
    registry = PluginRegistry()
    assert registry.provider_names == ("local", "youtube")
    factory = Mock(return_value=LocalMusicProvider())
    registry.register_provider("local", factory)
    assert registry.provider("local").name == "local"
    factory.assert_called_once()
    with pytest.raises(ProviderError, match="Unknown"):
        registry.provider("unknown")


def test_only_enabled_plugins_are_imported_and_loads_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    strategy = Mock()
    strategy.suggest.return_value = ["Artist - Song"]
    enabled = entry("enabled", lambda registry: registry.register_mix("custom", strategy))
    disabled = entry("disabled", lambda registry: None)
    monkeypatch.setattr("myne.plugins.entry_points", lambda **kwargs: [enabled, disabled])
    registry = PluginRegistry()
    assert registry.load_plugins(("enabled",)) == []
    assert registry.load_plugins(("enabled",)) == []
    assert registry.mix("custom") is strategy
    enabled.load.assert_called_once()
    disabled.load.assert_not_called()


def test_failed_plugin_registrations_roll_back_but_next_plugin_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(registry: PluginRegistry) -> None:
        registry.register_provider("local", lambda: None)  # type: ignore[arg-type, return-value]
        registry.register_mix("broken", Mock())
        raise RuntimeError("Do not print credentials")

    good = Mock()
    monkeypatch.setattr(
        "myne.plugins.entry_points",
        lambda **kwargs: [
            entry("broken", broken),
            entry("good", lambda registry: registry.register_mix("good", good)),
        ],
    )
    registry = PluginRegistry()
    diagnostics = registry.load_plugins(("broken", "good", "missing"))
    assert len(diagnostics) == 2
    assert registry.mix_names == ("good",)
    assert registry.provider("local").name == "local"
    assert "credentials" not in " ".join(diagnostics)


def test_ambiguous_plugin_names_are_never_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = entry("duplicate", Mock()), entry("duplicate", Mock())
    monkeypatch.setattr("myne.plugins.entry_points", lambda **kwargs: [first, second])
    assert PluginRegistry().load_plugins(("duplicate",))
    first.load.assert_not_called()
    second.load.assert_not_called()


def test_plugin_contract_validation() -> None:
    registry = PluginRegistry()
    with pytest.raises(ProviderError):
        registry.register_mix("bad", object())  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        registry.register_provider("BAD NAME", lambda: LocalMusicProvider())
    registry.register_provider("bad", lambda: object())  # type: ignore[arg-type, return-value]
    with pytest.raises(ProviderError, match="initialized"):
        registry.provider("bad")


def test_mix_resolves_strategy_suggestions_and_deduplicates() -> None:
    provider = Mock()
    provider.search.side_effect = [
        [Track("one", "First")],
        [Track("one", "First")],
        [Track("two", "Second")],
    ]
    strategy = Mock()
    strategy.suggest.return_value = ["query1", "query2", "query3"]
    result = MixService(provider, strategy).generate("focus", 3)
    assert [track.id for track in result] == ["one", "two"]
    assert provider.search.call_args_list[0].kwargs == {"limit": 1}


def test_default_mix_uses_honest_mood_search_heuristic() -> None:
    provider = Mock()
    provider.search.return_value = [Track("one", "First")]
    service = MixService(provider)
    assert isinstance(service.strategy, MoodSearchStrategy)
    assert service.generate("gym", 1)[0].id == "one"
    assert "workout" in provider.search.call_args.args[0]


@pytest.mark.parametrize("mood,limit", [("", 5), ("mood", 0), ("mood", 101), ("mood", True)])
def test_mix_validates_input_before_remote_calls(mood: str, limit: int) -> None:
    provider, strategy = Mock(), Mock()
    with pytest.raises(ProviderError):
        MixService(provider, strategy).generate(mood, limit)
    strategy.suggest.assert_not_called()
    provider.search.assert_not_called()


def test_mix_no_matches_is_an_actionable_error() -> None:
    provider = Mock()
    provider.search.return_value = []
    with pytest.raises(ProviderError, match="No playable"):
        MixService(provider).generate("focus")


def fake_opener(content: Any, observed: dict[str, Any]) -> Any:
    @contextmanager
    def open_response(request: Any, timeout: float) -> Any:
        observed.update(request=request, timeout=timeout)
        body = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()
        yield Mock(read=Mock(return_value=body))

    return open_response


def test_ai_endpoint_request_and_strict_song_resolution() -> None:
    observed: dict[str, Any] = {}
    strategy = AIMixStrategy(
        "https://ai.example/v1/chat/completions",
        api_key="test-key",
        model="music-model",
        opener=fake_opener({"queries": ["Artist - Song", "Artist - Song", "Other - Track"]}, observed),
    )
    assert strategy.suggest("night drive", 2) == ["Artist - Song", "Other - Track"]
    request = observed["request"]
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.data)["model"] == "music-model"
    assert observed["timeout"] == 20


@pytest.mark.parametrize(
    "content",
    [
        {"queries": []},
        {"queries": ["https://bad.example/audio"]},
        {"queries": [5]},
        {"queries": ["bad\nquery"]},
        {"tracks": ["Song"]},
        ["Song"],
    ],
)
def test_ai_rejects_malformed_suggestions(content: Any) -> None:
    strategy = AIMixStrategy("https://ai.example/chat", opener=fake_opener(content, {}))
    with pytest.raises(ProviderError, match="invalid"):
        strategy.suggest("focus", 2)


def test_ai_http_failure_does_not_expose_request_secrets() -> None:
    opener = Mock(side_effect=HTTPError("https://ai.example/private", 401, "private-key", {}, None))
    strategy = AIMixStrategy("https://ai.example/chat", api_key="private-key", opener=opener)
    with pytest.raises(ProviderError, match="authentication") as error:
        strategy.suggest("focus", 2)
    assert "private-key" not in str(error.value)


def test_ai_configuration_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYNE_MIX_ENDPOINT", raising=False)
    assert AIMixStrategy.from_environment() is None
    monkeypatch.setenv("MYNE_MIX_ENDPOINT", "http://localhost:8888/v1/chat/completions")
    monkeypatch.setenv("MYNE_MIX_MODEL", "local-model")
    assert AIMixStrategy.from_environment().model == "local-model"  # type: ignore[union-attr]
    with pytest.raises(ProviderError, match="HTTPS"):
        AIMixStrategy("http://remote.example/chat")


def test_provider_instances_are_reused_and_override_invalidates_cache() -> None:
    registry = PluginRegistry()
    original, replacement = LocalMusicProvider(), LocalMusicProvider()
    factory = Mock(return_value=original)
    registry.register_provider("local", factory)
    assert registry.provider("local") is original
    assert registry.provider("local") is original
    factory.assert_called_once()
    registry.register_provider("local", lambda: replacement)
    assert registry.provider("local") is replacement


def test_concurrent_provider_requests_initialize_exactly_once() -> None:
    from concurrent.futures import ThreadPoolExecutor

    registry = PluginRegistry()
    factory = Mock(side_effect=LocalMusicProvider)
    registry.register_provider("local", factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: registry.provider("local"), range(40)))
    assert all(provider is results[0] for provider in results)
    factory.assert_called_once()


def test_failed_plugin_restores_previous_provider_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = PluginRegistry()
    original = registry.provider("local")

    def broken(registry: PluginRegistry) -> None:
        registry.register_provider("local", LocalMusicProvider)
        assert registry.provider("local") is not original
        raise RuntimeError("registration failed")

    monkeypatch.setattr("myne.plugins.entry_points", lambda **kwargs: [entry("broken", broken)])
    assert registry.load_plugins(("broken",))
    assert registry.provider("local") is original
