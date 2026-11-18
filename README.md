# mywhoosh-mcp

Small, unofficial [MCP](https://modelcontextprotocol.io) server for [MyWhoosh](https://mywhoosh.com):
create, list, delete and schedule **custom cycling workouts**. Written from scratch against the
MyWhoosh API as used by the official app and the web editor at workout.mywhoosh.com.

Not affiliated with MyWhoosh. The API is undocumented and may change at any time.

## Tools

| Tool | What it does |
| --- | --- |
| `mywhoosh_create_workout` | Build a workout from simple blocks (% FTP) and upload it. ERG on/off per workout. |
| `mywhoosh_update_workout` | Replace a custom workout in place (same id, calendar entries stay). |
| `mywhoosh_list_workouts` | List custom workouts (optionally with a per-step breakdown) and free slots. |
| `mywhoosh_delete_workout` | Delete a custom workout (refuses if a training plan uses it, unless `force`). |
| `mywhoosh_schedule_workout` | Put a custom workout on the MyWhoosh calendar (date + local start time). |
| `mywhoosh_list_scheduled_workouts` | List calendar entries in a date range. |
| `mywhoosh_unschedule_workout` | Remove a calendar entry (the workout stays). |
| `mywhoosh_get_profile` | FTP, weight, height and best power values from the MyWhoosh profile. |
| `mywhoosh_set_ftp` | Set the cycling FTP (only that field changes; verified after writing). |

Block types for `mywhoosh_create_workout`:

```json
[
  {"type": "warmup",   "duration_s": 600, "start_power": 50, "end_power": 75},
  {"type": "interval", "duration_s": 240, "power": 105, "repeat": 4, "rest_duration_s": 180, "rest_power": 55},
  {"type": "steady",   "duration_s": 1200, "power": 70, "cadence": 90,
   "messages": [{"at_s": 10, "text": "Relax your shoulders"}]},
  {"type": "freeride", "duration_s": 300},
  {"type": "cooldown", "duration_s": 300, "start_power": 65, "end_power": 45}
]
```

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/derbauer97/mywhoosh-mcp
```

Credentials are read from environment variables, or on macOS from the Keychain:

```sh
# Option A: environment
export MYWHOOSH_USERNAME=you@example.com MYWHOOSH_PASSWORD=...

# Option B: macOS Keychain (service name "mywhoosh-mcp"); -w without a value prompts for the password
security add-generic-password -U -s mywhoosh-mcp -a you@example.com -w
```

MCP client config:

```json
{
  "mcpServers": {
    "mywhoosh": { "command": "mywhoosh-mcp" }
  }
}
```

Optional: `MYWHOOSH_TZ` (default `Europe/Berlin`) sets the time zone for scheduling.

## Things the API does that you should know

- **One session per account.** While the MyWhoosh app is logged in, API logins are rejected
  ("already logged in from another device"). Close the app; the lock clears after a few minutes.
  The access token is cached in `~/.cache/mywhoosh-mcp/token.json` (mode 0600) to avoid re-logins.
- **ASCII only.** The backend silently drops non-ASCII characters, so names, descriptions and
  messages are transliterated (`ö` -> `oe`, `ß` -> `ss`, `–` -> `-`).
- **Writes are asynchronous** and overlapping writes can be lost. The server serializes uploads and
  deletes and only returns once the change is visible (`confirmed: true`).
- **Limited slots** on free accounts (custom workout "credits"); `mywhoosh_list_workouts` reports
  `free_slots`. Updating a workout in place does not use a slot.
- **Calendar entries keep their own copy** of name and TSS; `mywhoosh_update_workout` does not
  change them.
- Cycling only. Whether `ERGMode: E_ON` makes the app start in ERG mode is up to the app.

## Development

```sh
uv sync
uv run pytest
```

## License

MIT
