"""The public `myne` command line."""

import json
import os
import shutil
import sqlite3
import sys
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from functools import wraps
from typing import Any, Callable, ParamSpec, TypeVar

import questionary
import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from myne import __version__
from myne.config import Config
from myne.errors import MyneError
from myne.models import Track
from myne.motion import AudioMotion, reduced_motion_requested
from myne.preferences import CHOICES, Preferences
from myne.service import Application

app = typer.Typer(name="myne", help="My Music. My Queue. My Experience. Myne CLI.", no_args_is_help=True)
queue_app = typer.Typer(help="Manage your persistent upcoming queue.", no_args_is_help=True)
app.add_typer(queue_app, name="queue")
playlist_app = typer.Typer(help="Import and browse playlists from shared links.", no_args_is_help=True)
app.add_typer(playlist_app, name="playlist")
settings_app = typer.Typer(help="View or change saved settings.", no_args_is_help=True)
app.add_typer(settings_app, name="settings")
console = Console(highlight=False)
P = ParamSpec("P")
R = TypeVar("R")


def handled(fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except MyneError as exc:
            report_error(args, kwargs, str(exc))
            raise typer.Exit(1) from None
        except (OSError, ValueError, sqlite3.Error) as exc:
            # Never echo arbitrary exception strings: upstream errors may contain credentials.
            report_error(args, kwargs, f"{type(exc).__name__}. Check configuration and run myne doctor.")
            raise typer.Exit(1) from None

    return wrapped


def report_error(args: tuple[Any, ...], kwargs: dict[str, Any], message: str) -> None:
    ctx = kwargs.get("ctx") or next((item for item in args if isinstance(item, typer.Context)), None)
    if ctx and ctx.obj and ctx.obj.get("json"):
        typer.echo(json.dumps({"error": message}, ensure_ascii=False))
    else:
        Console(stderr=True, highlight=False).print(Text(f"Myne: {message}", style="red"))


@app.callback(invoke_without_command=True)
def options(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    provider: str | None = typer.Option(None, "--provider", help="Music provider (youtube or local)."),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override the SQLite data directory."),
    version: bool = typer.Option(False, "--version", is_eager=True),
) -> None:
    if version:
        console.print(f"Myne CLI {__version__}")
        raise typer.Exit()
    ctx.obj = {"json": json_output, "provider": provider, "data_dir": data_dir}


def application(ctx: typer.Context) -> Application:
    state = ctx.ensure_object(dict)
    if "application" not in state:
        # Overrides belong to the composed configuration; avoid mutating the parent environment.
        from dataclasses import replace
        from pathlib import Path

        config = Config.from_env()
        if state.get("provider"):
            config = replace(config, provider=state["provider"])
        if state.get("data_dir"):
            import hashlib

            data = Path(state["data_dir"]).expanduser().absolute()
            suffix = hashlib.sha256(str(data.resolve()).encode()).hexdigest()[:10]
            uid = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
            config = replace(
                config, data_dir=data, runtime_dir=config.runtime_dir.parent / f"myne-{uid}-{suffix}"
            )
        config.prepare()
        state["application"] = Application(config)
    return state["application"]


def emit(ctx: typer.Context, value: Any, message: str | None = None) -> None:
    if ctx.obj.get("json"):
        typer.echo(json.dumps(value, ensure_ascii=False, default=str))
    elif message is not None:
        console.print(Text(message))


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def tracks_table(ctx: typer.Context, tracks: list[Track], title: str = "Myne CLI") -> None:
    if ctx.obj.get("json"):
        emit(ctx, [t.to_dict() for t in tracks])
        return
    table = Table(title=Text(title), border_style="grey35", header_style="bold cyan", expand=True)
    for col in ("#", "Song", "Artist", "Time", "ID"):
        table.add_column(col, overflow="fold")
    for i, track in enumerate(tracks, 1):
        table.add_row(
            str(i),
            Text(track.title),
            Text(", ".join(track.artists) or "Unknown"),
            duration(track.duration),
            Text(track.id),
        )
    console.print(table)
    if not tracks:
        console.print("No tracks yet.", style="dim")


@app.command()
@handled
def search(ctx: typer.Context, query: str, limit: int = typer.Option(20, min=1, max=100)) -> None:
    """Search for music."""
    tracks_table(ctx, application(ctx).search(query, limit), f"Search · {query}")


@app.command()
@handled
def play(
    ctx: typer.Context,
    query: str | None = typer.Argument(None),
    choose: bool = typer.Option(False, "--choose", help="Choose interactively from search results."),
) -> None:
    """Stream a query, video ID, YouTube URL, or local file. No query resumes the queue."""
    service = application(ctx)
    if query is None:
        result = service.control("resume")
        emit(ctx, result, "Queue resumed.")
        return
    if choose:
        if not sys.stdin.isatty():
            raise MyneError("--choose requires an interactive terminal; omit it to play the first result.")
        tracks = service.search(query, 15)
        if not tracks:
            raise MyneError("No matching tracks found.")
        track = questionary.select(
            "Your music",
            choices=[questionary.Choice(f"{t.title} · {', '.join(t.artists)}", value=t) for t in tracks],
        ).ask()
        if track is None:
            raise typer.Exit(0)
    else:
        track = service.resolve(query)
    result = service.play([track])
    emit(ctx, result, f"Playing · {track.title} — {', '.join(track.artists)}")


@app.command()
@handled
def album(ctx: typer.Context, album_id: str, play_album: bool = typer.Option(False, "--play")) -> None:
    """Browse an album, optionally play its tracks."""
    service = application(ctx)
    result = service.album(album_id)
    if play_album:
        emit(ctx, service.play(list(result.tracks)), f"Playing album · {result.title}")
    elif ctx.obj.get("json"):
        emit(ctx, asdict(result))
    else:
        tracks_table(ctx, list(result.tracks), result.title)


@app.command()
@handled
def artist(ctx: typer.Context, artist_id: str) -> None:
    """Browse an artist's top tracks and albums."""
    result = application(ctx).artist(artist_id)
    if ctx.obj.get("json"):
        emit(ctx, asdict(result))
        return
    tracks_table(ctx, list(result.top_tracks), result.name)
    for item in result.albums:
        console.print(Text(f"Album · {item.title}  [{item.id}]"))


@queue_app.command("show")
@handled
def queue_show(ctx: typer.Context) -> None:
    tracks_table(ctx, application(ctx).queue(), "My Queue · upcoming")


@queue_app.command("add")
@handled
def queue_add(ctx: typer.Context, query: str) -> None:
    service = application(ctx)
    track = service.resolve(query)
    emit(ctx, service.enqueue([track]), f"Queued · {track.title}")


@queue_app.command("save")
@handled
def queue_save(ctx: typer.Context, name: str = "default") -> None:
    """Save a named snapshot of upcoming tracks."""
    count = application(ctx).store.save_queue(name)
    emit(ctx, {"name": name, "tracks": count}, f"Saved {count} upcoming tracks as {name}.")


@queue_app.command("restore")
@handled
def queue_restore(ctx: typer.Context, name: str = "default") -> None:
    """Replace upcoming tracks with a snapshot; use myne play to start."""
    count = application(ctx).store.restore_queue(name)
    emit(ctx, {"name": name, "tracks": count}, f"Restored {count} upcoming tracks from {name}.")


@queue_app.command("saved")
@handled
def queue_saved(ctx: typer.Context) -> None:
    names = application(ctx).store.snapshots()
    emit(ctx, names, "\n".join(names) or "No saved queues.")


@queue_app.command("clear")
@handled
def queue_clear(ctx: typer.Context) -> None:
    application(ctx).store.clear_queue()
    emit(ctx, {"cleared": True}, "Upcoming queue cleared.")


@queue_app.command("remove")
@handled
def queue_remove(ctx: typer.Context, position: int = typer.Argument(..., min=1)) -> None:
    track = application(ctx).store.remove(position - 1)
    emit(ctx, track.to_dict(), f"Removed · {track.title}")


@playlist_app.command("import")
@handled
def playlist_import(
    ctx: typer.Context, link: str,
    name: str | None = typer.Option(None, help="Name the saved playlist."),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Import a Spotify, YouTube Music, YouTube or SoundCloud playlist link."""
    result = application(ctx).import_playlist(link, name, limit=limit)
    emit(ctx, asdict(result), f"Imported {len(result.playlist.tracks)} tracks as {result.playlist.name}.")
    if not ctx.obj.get("json"):
        for warning in result.playlist.warnings:
            console.print(Text(warning, style="yellow"))
        for skipped in result.skipped:
            console.print(Text(f"Unmatched · {skipped}", style="dim"))


@playlist_app.command("list")
@handled
def playlist_list(ctx: typer.Context) -> None:
    rows = application(ctx).playlists()
    emit(ctx, [{"name": name, "tracks": count} for name, count in rows],
         "\n".join(f"{name} · {count} tracks" for name, count in rows) or "No playlists. Import a playlist link to start.")


@playlist_app.command("show")
@handled
def playlist_show(ctx: typer.Context, name: str) -> None:
    playlist = application(ctx).playlist(name)
    tracks_table(ctx, list(playlist.tracks), playlist.name)


@playlist_app.command("play")
@handled
def playlist_play(ctx: typer.Context, name: str) -> None:
    service = application(ctx)
    emit(ctx, service.play(list(service.playlist(name).tracks)), f"Playing playlist · {name}")


@playlist_app.command("queue")
@handled
def playlist_queue(ctx: typer.Context, name: str) -> None:
    service = application(ctx)
    emit(ctx, service.enqueue(list(service.playlist(name).tracks)), f"Playlist added to queue · {name}")


@app.command()
@handled
def now(ctx: typer.Context) -> None:
    """Show the current track and player state."""
    status = application(ctx).now()
    track = status.get("track")
    label = (
        (
            f"{'Paused' if status.get('paused') else 'Playing' if status.get('playing') else 'Loading'} · {track['title']}\n"
            f"{duration(status.get('position'))} / {duration(status.get('duration'))} · Volume {status.get('volume', 70):g}%"
        )
        if track
        else "Nothing is playing."
    )
    if status.get("muted"):
        label += "\nSound is muted. Run myne unmute or raise the volume."
    if status.get("audio_output"):
        output = str(status["audio_output"])
        label += f"\nAudio output: {output}" + (" (silent)" if output == "null" else "")
    if status.get("error"):
        label += f"\n{status['error']}"
    emit(ctx, status, label)


@app.command()
@handled
def lyrics(
    ctx: typer.Context,
    query: str | None = typer.Argument(None, help="Optional song query; defaults to the playing song."),
    lrc: bool = typer.Option(False, "--lrc", help="Print timestamped lyrics when available."),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the local lyrics cache."),
) -> None:
    """Get lyrics from LRCLIB or the music provider, with optional LRC timestamps."""
    service = application(ctx)
    document = service.lyrics_document(service.resolve(query) if query else None, refresh=refresh)
    if document is None:
        emit(ctx, {"lyrics": None}, "No lyrics found for this track. Try myne lyrics --refresh later.")
        return
    if ctx.obj.get("json"):
        emit(ctx, document.to_dict())
        return
    if document.instrumental:
        console.print("Instrumental · no sung lyrics.")
    elif lrc and document.synced:
        for line in document.synced:
            minutes, seconds = divmod(line.time, 60)
            console.print(Text(f"[{int(minutes):02}:{seconds:05.2f}]{line.text}"))
    else:
        console.print(Text(document.plain))
        if lrc:
            console.print("Timed lyrics are unavailable; showing plain lyrics.", style="dim")
    console.print(Text(f"Source · {document.source}", style="dim"))


def _audio_bar(value: float, width: int) -> tuple[str, str]:
    """Draw measured amplitude at one eighth of a terminal cell resolution."""
    cells = round(max(0.0, min(1.0, value)) * width * 8)
    full, partial = divmod(cells, 8)
    filled = "█" * full + ("▏▎▍▌▋▊▉"[partial - 1] if partial else "")
    return filled, "░" * (width - full - bool(partial))


def _audio_frame(levels: dict[str, Any], history: deque[float], width: int = 42) -> Text:
    """Render a frame without adding invented samples to amplitude history."""
    frame = Text("MYNE CLI · LIVE AUDIO\n", style="bold cyan")
    if levels.get("error"):
        frame.append(str(levels["error"]) + "\n", style="yellow")
    if not levels.get("available"):
        state = "Paused" if levels.get("paused") else "Waiting for audio measurements"
        frame.append(state + "\n", style="dim")
    else:
        rms = levels.get("rms") or []
        peak = levels.get("peak") or []
        for index, value in enumerate(rms[:2]):
            filled, empty = _audio_bar(float(value), width)
            label = "M" if len(rms) == 1 else ("L" if index == 0 else "R")
            frame.append(f"{label}  ", style="bold")
            frame.append(filled, style="cyan")
            frame.append(empty, style="grey35")
            peak_value = float(peak[index]) if index < len(peak) else float(value)
            frame.append(f"  {max(-60.0, min(0.0, peak_value * 60 - 60)):5.1f} dBFS peak\n", style="dim")
        blocks = "▁▂▃▄▅▆▇█"
        frame.append("AMPLITUDE HISTORY\n", style="dim")
        frame.append("".join(blocks[min(7, max(0, int(value * 7)))] for value in history), style="cyan")
        frame.append("\n")
    frame.append("Actual sample levels · -60 to 0 dBFS · Ctrl+C closes this view", style="dim")
    return frame


class _AudioTelemetry:
    """Poll at most ten times per second with one bounded IPC request in flight."""

    def __init__(self, read: Callable[[], dict[str, Any]]) -> None:
        self._read = read
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="myne-levels")
        self._future: Future[dict[str, Any]] | None = None
        self._next_poll = 0.0

    def poll(self, now: float) -> dict[str, Any] | None:
        sample = None
        if self._future is not None and self._future.done():
            sample = self._future.result()
            self._future = None
        if self._future is None and now >= self._next_poll:
            self._future = self._executor.submit(self._read)
            self._next_poll = now + 0.1
        return sample

    def close(self) -> None:
        # The service's live IPC client has a two second timeout. Do not block
        # terminal restoration waiting for a request that is already running.
        self._executor.shutdown(wait=False, cancel_futures=True)


@app.command("visualize")
@handled
def visualize(
    ctx: typer.Context,
    seconds: float | None = typer.Option(
        None, "--seconds", min=0.1, max=3600, help="Stop after this many seconds."
    ),
    fps: int = typer.Option(30, "--fps", min=1, max=60, help="Maximum terminal frames per second."),
    off: bool = typer.Option(False, "--off", help="Disable the audio analysis filter."),
    no_animation: bool = typer.Option(False, "--no-animation", help="Use static measured meter updates."),
) -> None:
    """Show a live stereo visualizer for the playing music."""
    service = application(ctx)
    state = service.visualizer(not off)
    if off:
        emit(ctx, state, "Visualizer disabled.")
        return
    telemetry = None
    try:
        if state.get("error"):
            raise MyneError(str(state["error"]))
        if not state.get("track_id"):
            raise MyneError("Start a song with myne play, then run myne visualize.")
        if ctx.obj.get("json"):
            emit(ctx, service.audio_levels())
            return
        reduced = no_animation or reduced_motion_requested()
        render_fps = min(fps, 10) if reduced else fps
        motion = AudioMotion(reduced_motion=reduced, stale_after=0.6)
        history: deque[float] = deque(maxlen=42)
        telemetry = _AudioTelemetry(service.audio_levels)
        started = time.monotonic()
        deadline = started + seconds if seconds is not None else float("inf")
        with Live(console=console, refresh_per_second=render_fps, auto_refresh=False) as live:
            while (now := time.monotonic()) < deadline:
                sample = telemetry.poll(now)
                if sample is not None:
                    motion.update(sample, now=now)
                    raw = sample.get("rms") or []
                    if sample.get("available") and raw:
                        history.append(sum(float(value) for value in raw) / len(raw))
                frame = motion.frame(now=now)
                if not frame.get("available"):
                    history.clear()
                live.update(_audio_frame(frame, history), refresh=True)
                pause = min(now + 1 / render_fps, deadline) - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
    except KeyboardInterrupt:
        pass
    finally:
        if telemetry is not None:
            telemetry.close()
        service.visualizer(False)


@app.command()
@handled
def mix(
    ctx: typer.Context,
    mood: str,
    limit: int = typer.Option(20, min=1, max=100),
    ai: bool = typer.Option(False, "--ai", help="Use a configured AI endpoint."),
    strategy: str | None = typer.Option(None, "--strategy", help="Use a registered mix plugin."),
    play_mix: bool = typer.Option(False, "--play", help="Start the generated mix now."),
) -> None:
    """Generate and queue a mood mix; --ai enables an AI planning service."""
    service = application(ctx)
    tracks = service.mix(mood, limit, ai, strategy)
    if play_mix:
        service.play(tracks)
    else:
        service.enqueue(tracks)
    tracks_table(ctx, tracks, f"{strategy or ('AI' if ai else 'Mood search')} mix · {mood}")


@app.command()
@handled
def radio(ctx: typer.Context, query: str) -> None:
    """Start a seed song and continuously refill with similar music."""
    emit(ctx, application(ctx).radio(query), f"Radio started · {query}")


@app.command("radio-off")
@handled
def radio_off(ctx: typer.Context) -> None:
    emit(ctx, application(ctx).control("radio_off"), "Radio refill disabled.")


@app.command()
@handled
def stats(ctx: typer.Context) -> None:
    """Show listening time, top songs, artists and known genres."""
    data = application(ctx).stats()
    if ctx.obj.get("json"):
        emit(ctx, data)
        return
    console.print(
        f"[bold cyan]My Experience[/] · {data['plays']} plays · {data['listening_seconds'] / 3600:.2f} hours"
    )
    for key, label in (
        ("artists", "Most Played Artists"),
        ("songs", "Most Played Songs"),
        ("genres", "Top Genres"),
    ):
        table = Table(title=label, header_style="cyan")
        table.add_column("Name")
        table.add_column("Plays", justify="right")
        for name, count in data[key]:
            table.add_row(Text(name), str(count))
        console.print(table)
        if key == "genres" and not data[key]:
            console.print("No genre metadata supplied by the provider yet.", style="dim")


@app.command()
@handled
def pause(ctx: typer.Context) -> None:
    """Toggle pause."""
    emit(ctx, application(ctx).control("pause"), "Pause toggled.")


@app.command()
@handled
def resume(ctx: typer.Context) -> None:
    emit(ctx, application(ctx).control("resume"), "Playback resumed.")


@app.command("next")
@handled
def next_track(ctx: typer.Context) -> None:
    emit(ctx, application(ctx).control("next"), "Skipped to next track.")


@app.command()
@handled
def stop(ctx: typer.Context) -> None:
    """Stop the current song and retain upcoming tracks."""
    emit(ctx, application(ctx).control("stop"), "Playback stopped.")


@app.command()
@handled
def seek(ctx: typer.Context, seconds: float) -> None:
    """Seek by relative seconds (use `myne seek -- -10` to rewind)."""
    emit(ctx, application(ctx).control("seek", seconds=seconds), f"Seeked {seconds:g} seconds.")


@app.command()
@handled
def volume(ctx: typer.Context, value: int = typer.Argument(..., min=0, max=100)) -> None:
    emit(ctx, application(ctx).control("volume", value=value), f"Volume · {value}%")


@app.command()
@handled
def mute(ctx: typer.Context) -> None:
    """Toggle sound mute without pausing the song."""
    result = application(ctx).control("mute")
    emit(ctx, result, "Sound muted." if result.get("muted") else "Sound unmuted.")


@app.command()
@handled
def unmute(ctx: typer.Context) -> None:
    """Unmute the shared player. A zero volume still needs to be raised."""
    emit(ctx, application(ctx).control("mute", value=False), "Sound unmuted.")


@app.command()
@handled
def shutdown(ctx: typer.Context) -> None:
    """Stop the background service, preserving its interrupted song for recovery."""
    application(ctx).player.shutdown()
    emit(ctx, {"shutdown": True}, "Player service stopped.")


@app.command()
@handled
def ui(ctx: typer.Context) -> None:
    """Launch the full-screen Myne terminal interface."""
    from myne.tui import launch_ui

    launch_ui(application(ctx))


@settings_app.command("show")
@handled
def settings_show(ctx: typer.Context) -> None:
    values = application(ctx).preferences().to_dict()
    emit(ctx, values, "\n".join(f"{key}: {value}" for key, value in values.items()))


@settings_app.command("set")
@handled
def settings_set(ctx: typer.Context, key: str, value: str) -> None:
    service = application(ctx)
    values = service.preferences().to_dict()
    if key not in values:
        raise MyneError("Unknown setting. Run myne settings show for supported names.")
    if key in CHOICES:
        parsed: Any = value
    elif key == "volume":
        if not value.isascii() or not value.isdigit():
            raise MyneError("Volume must be an integer from 0 to 100.")
        parsed = int(value)
    elif value.lower() in {"true", "on", "yes", "1", "false", "off", "no", "0"}:
        parsed = value.lower() in {"true", "on", "yes", "1"}
    else:
        raise MyneError("Use true or false for this setting.")
    values[key] = parsed
    preferences = Preferences.from_dict(values, strict=True)
    saved = service.save_preferences(preferences.to_dict())
    emit(ctx, saved.to_dict(), f"Saved {key} · {value}")


@app.command("update")
@handled
def update_command(ctx: typer.Context, install: bool = typer.Option(False, "--install", help="Install a newer official version in a supported environment.")) -> None:
    """Check for an update; add --install to prepare, verify, and install it."""
    service = application(ctx)
    info = service.install_update() if install else service.check_update(True)
    emit(ctx, asdict(info), f"{info.message}\n{info.install_note}")


@app.command()
@handled
def plugins(ctx: typer.Context) -> None:
    """List installed plugin entry points and activation diagnostics."""
    from importlib.metadata import entry_points

    service = application(ctx)
    data = {
        "installed": sorted(ep.name for ep in entry_points(group="myne.plugins")),
        "enabled": list(service.config.enabled_plugins),
        "diagnostics": service.registry.diagnostics,
    }
    emit(ctx, data, json.dumps(data, ensure_ascii=False, indent=2))


@app.command()
@handled
def doctor(ctx: typer.Context) -> None:
    """Check dependencies and configuration without making network requests."""
    service = application(ctx)
    data = {
        "version": __version__,
        "python": sys.version.split()[0],
        "mpv": shutil.which(service.config.mpv_binary) is not None,
        "javascript_runtime": next((name for name in ("deno", "node") if shutil.which(name)), None),
        "platform_supported": os.name == "posix",
        "provider": service.config.provider,
        "data_dir": str(service.config.data_dir),
        "runtime_dir": str(service.config.runtime_dir),
        "queue_tracks": len(service.queue()),
        "enabled_plugins": list(service.config.enabled_plugins),
        "plugin_diagnostics": service.registry.diagnostics,
    }
    emit(ctx, data, json.dumps(data, indent=2))
    if not data["mpv"]:
        if not ctx.obj.get("json"):
            console.print("Install mpv, then run myne play. See docs/INSTALL.md.", style="yellow")
        raise typer.Exit(1)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except MyneError as exc:
        console.print(Text(f"Myne: {exc}", style="red"))
        raise SystemExit(1) from None
