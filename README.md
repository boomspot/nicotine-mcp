# Nicotine MCP

A [Nicotine+](https://nicotine-plus.org) plugin that runs a local [MCP](https://modelcontextprotocol.io)
server, so an AI assistant like Claude can search Soulseek, look up artists and albums on
MusicBrainz, and queue downloads in your running Nicotine+.

Built for the packaged Nicotine+ **3.3.x** app. That app bundles a Python with only the
standard library, so this plugin has no dependencies.

## Install

```sh
./scripts/install.sh
```

This symlinks `nicotine_mcp/` into `~/.local/share/nicotine/plugins/`. Then in Nicotine+ open
**Preferences → Plugins** and enable **Nicotine MCP**. The log shows
`MCP server listening on http://127.0.0.1:8765/mcp`.

Connect Claude Code:

```sh
claude mcp add --transport http nicotine http://127.0.0.1:8765/mcp
```

For Claude Desktop, add the same URL as a custom connector. The server only accepts
connections from this computer, and only while Nicotine+ is running with the plugin enabled.

## Tools

| Tool | Purpose |
|---|---|
| `nicotine_status` | Connection state, username, download folder |
| `search_music` | Search Soulseek, wait for results, return ranked folders (albums) |
| `get_search_results` | More results for a search, with filters (format, bitrate, free slot, words, user) and paging |
| `download` | Queue whole folders (`mode: folder`) or just the matched files |
| `list_downloads` | Downloads grouped by folder, with status and progress |
| `clear_searches` | Close search tabs opened through MCP |
| `artist_discography` | An artist's albums and EPs from MusicBrainz |
| `album_tracklist` | Track list of an album, to check a result is complete |

Results are ranked by free upload slot, then format preference (lossless by default), then number
of tracks, speed and queue length.

## Settings

In Preferences → Plugins → Nicotine MCP:

- **Port**: default 8765
- **Search wait**: seconds `search_music` collects results before answering (default 10)
- **MusicBrainz contact**: an email or URL for the User-Agent, which MusicBrainz asks for
  (defaults to this project's URL)

## Security and etiquette

The MCP server only listens on `127.0.0.1` and rejects requests from other hosts or web pages,
but it has no password: any program running on your computer can call it while the plugin is
enabled. Disable the plugin if that matters to you.

Soulseek is a community of people sharing their own collections, and mass automated downloading
is frowned upon. The plugin spaces searches at least 3 seconds apart, and tells the assistant to
download only what you asked for and to confirm before queueing many albums. Keep it that way.

## Development

Edit the code here, then turn the plugin off and on in Preferences to reload it. Errors appear
in the Nicotine+ log, and HTTP request logs in the debug log.

```sh
python3 -m unittest discover -s tests -t .
```

`tests/test_nicotine_compat.py` loads the plugin through a real Nicotine+ core (no GUI or network).
Point it at the source of the Nicotine+ version you run:

```sh
git -C /path/to/nicotine-plus archive 3.3.10 pynicotine | tar -x -C /tmp/n3310
NICOTINE_SOURCE=/tmp/n3310 python3 -m unittest discover -s tests -t .
```

Helper modules are named `nmcp_*` because Nicotine+ adds the plugin folder to `sys.path` and
imports them as top-level modules. Everything that touches Nicotine+ state goes through
`NicotineBridge` in `__init__.py`, which runs it on the Nicotine+ main thread.

### Nicotine+ 3.4 / master

The plugin loads and works on current master, except `download` with `mode: folder`, because
3.4 replaced `enqueue_folder()` with a GUI-driven flow. It returns an error asking for
`matched_files` instead.

## License

GPL-3.0-or-later, the same as Nicotine+. See [LICENSE](LICENSE).
