# Example Myne mix plugin

Install into the same environment as Myne:

```sh
python -m pip install ./examples/mood_plugin
```

Enable `mood-example` in the Myne `plugins` configuration list, then use:

```sh
myne mix "night drive" --strategy night-drive
```

Myne loads the `register(registry)` entry point only when explicitly enabled.
The example supplies hand-picked song searches, rather than claiming AI output.
Plugins can also register provider factories with `register_provider(name, factory)`.
Provider classes must extend `MusicProvider` and expose the same `name`.
Install only trusted plugins: Python plugins execute with your user's privileges.
