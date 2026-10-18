# Plugins and future providers

Plugins are ordinary installed Python packages. Discovery does not import them.
Only entry-point names in `MYNE_PLUGINS` are loaded. Enabled plugins execute with
your account's permissions, so install code you trust. Exceptions during
registration discard that plugin's registry changes and leave diagnostics.

## Try the included mix plugin

```sh
python -m pip install ./examples/mood_plugin
export MYNE_PLUGINS="mood-example"
myne plugins
myne mix "after dark" --strategy night-drive --play
```

If a player service was already running, restart it so it sees updated plugins:
`myne shutdown`, then start playback again. See the example's README for its
exact entry-point name and complete source.

## Entry-point contract

```toml
[project.entry-points."myne.plugins"]
my-extension = "my_package:register"
```

The registration function receives a `PluginRegistry`:

```python
from myne.plugins import PluginRegistry


class EveningMix:
    def suggest(self, mood: str, limit: int) -> list[str]:
        return [f"{mood} ambient", "night drive instrumental"][:limit]


def register(registry: PluginRegistry) -> None:
    registry.register_mix("evening", EveningMix())
```

Enable with `MYNE_PLUGINS=my-extension`, then use
`myne mix "quiet evening" --strategy evening`. Mix strategies return search
queries; Myne resolves them through the selected provider and deduplicates track
identities. Unavailable queries may yield fewer tracks than requested.

## Provider contract

Subclass `myne.providers.base.MusicProvider`, set a unique `name`, and implement:

| Method | Return |
| --- | --- |
| `search(query: str, limit: int = 20)` | `list[Track]` |
| `get_song(song_id: str)` | `Track` |
| `get_album(album_id: str)` | `Album` |
| `get_artist(artist_id: str)` | `Artist` |
| `resolve_stream(track: Track)` | `Stream` |
| `recommendations(track: Track, limit: int = 25)` | `list[Track]`; optional |
| `lyrics(track: Track)` | `str \| None`; optional |

The included `LocalMusicProvider` is a complete minimal implementation to study;
`YouTubeMusicProvider` demonstrates metadata mapping, injected upstream clients
and extraction. A provider that cannot browse albums or artists should raise
`ProviderError` with an actionable message.

Set `provider` on **every Track** to the registration name. Keep IDs stable and
store metadata only. `resolve_stream` returns a current stream URL and optional
HTTP headers, or an absolute local `file:` URI. Do not download media as a side
effect. Never include signed URLs or credentials in exception text. Follow the
provider's account and access requirements.

Register a factory in your plugin:

```python
from my_package.provider import MyProvider
from myne.plugins import PluginRegistry


def register(registry: PluginRegistry) -> None:
    registry.register_provider("my-provider", lambda: MyProvider())
```

Then enable the entry point and use `myne --provider my-provider search "query"`.
Factories are created on demand in the CLI and separately in the player service.
Use environment variables or your own credential storage for configuration;
Python object instances do not cross process boundaries. The selected provider
is responsible for mapping its own metadata and obtaining playback access.

Registrations can intentionally override a built-in provider. Duplicate plugin
entry-point names are rejected as ambiguous. `myne plugins` reports installed
names, enabled names, and load diagnostics. `registry.provider_names` and
`registry.mix_names` expose available registrations programmatically.

## Testing an extension

Inject a fake network client into your provider and test malformed/missing
metadata, unavailable tracks, authorization failures, and expired-stream
re-resolution. Verify extraction never downloads media. Test plugin registration
against `PluginRegistry()` and exercise it in a fresh player process. The
repository tests provide examples without contacting YouTube.
