# Slack bridge

The bridge connects one Slack channel (or a DM with the bot) to the swarm, so you can work with
it away from the laptop:

- a message from you becomes a prompt to the overall manager; `@backend rerun the tests` goes to
  the manager `backend`;
- a few words are commands: `status`, `list`, `tasks <owner>`, `screen <agent>`, `help`;
- each task that becomes waiting-on-you, done or dropped gets one notice thread; reply in a
  waiting task's thread to answer its question;
- agents answer you with `swarm slack post`.

Code: `lib/tstack/slack.py`. It uses only the Python standard library and the plain Slack Web API
(`urllib`, a bot token, polling). Nothing is exposed to the internet: the bridge only makes
outgoing HTTPS calls, so it works on a laptop or a server behind NAT.

## Setup

The same steps work on macOS and Linux.

1. **Create the Slack app.** Open <https://api.slack.com/apps> → *Create New App* → *From an app
   manifest*, pick your workspace, and paste:

   ```yaml
   display_information:
     name: tstack
     description: Bridge between this channel and a tstack agent swarm
   features:
     bot_user:
       display_name: tstack
       always_online: false
     app_home:
       messages_tab_enabled: true            # lets you DM the bot
       messages_tab_read_only_enabled: false
   oauth_config:
     scopes:
       bot:
         - channels:history    # read messages in public channels the bot is in
         - groups:history      # same for private channels
         - im:history          # same for DMs with the bot
         - chat:write          # post replies and notices
         - reactions:write     # 👀 when a message is received, ✅ when it is delivered
   settings:
     org_deploy_enabled: false
     socket_mode_enabled: false
     token_rotation_enabled: false
   ```

   You only need the `*:history` scope for the kind of conversation you use; the others can go.
   Keep the app internal to your workspace (do not distribute it): Slack gives internal apps the
   normal rate limits for `conversations.history`.

2. **Install it** (*Install App* → *Install to Workspace*) and copy the *Bot User OAuth Token*
   (`xoxb-...`).

3. **Store the token** outside the settings file, readable only by you:

   ```bash
   mkdir -p ~/.config/tstack
   ( umask 077; printf '%s\n' 'xoxb-...' > ~/.config/tstack/slack-token )
   ```

   Or export `SLACK_BOT_TOKEN` in the shell that starts the bridge; it wins over the file.

4. **Pick the conversation.** Use a private channel only you (and the bot) are in, or a DM with
   the bot. Invite the bot to a channel with `/invite @tstack`. The channel id is at the bottom of
   the channel's details (`C...` or `G...`); for a DM, open the bot's *Messages* tab and copy the
   id from the conversation's details or URL (`D...`).

5. **Find your user id**: your profile → ⋮ → *Copy member ID* (`U...`).

6. **Configure** `~/.config/tstack/config.json` (only the keys you change):

   ```json
   "slack": {
     "channel": "C0123456789",
     "allowed_users": ["U0123456789"]
   }
   ```

7. **Check and start:**

   ```bash
   swarm config check     # validates the slack section
   swarm slack test       # auth.test, posts a hello, reads the channel back
   swarm slack start      # background; log in <data_dir>/logs/slack.log
   ```

   Send `help` in the channel.

## Settings (`slack` section)

| Key | Default | Meaning |
|---|---|---|
| `channel` | `""` | Channel or DM id the bridge reads and posts in. Empty: bridge not set up. |
| `allowed_users` | `[]` | Slack user ids whose messages are obeyed. Everyone else is ignored. |
| `token_file` | `~/.config/tstack/slack-token` | Bot token file, read when `$SLACK_BOT_TOKEN` is unset. |
| `api_base` | `https://slack.com/api` | Web API base URL. Point it at a local stub for testing. |
| `backend` | `webapi` | Chat transport, see "Swapping the backend". |
| `poll_seconds` | `10` | Seconds between poll cycles. |
| `notices` | `bridge` | Who posts task notices: `bridge`, `routine` (the routine daemon does), or `off`. |
| `say_wait_minutes` | `30` | How long a forwarded message waits for its agent to be idle. |
| `runner` | `nohup` | How `swarm slack start` runs the loop: `nohup` or `tmux`. |

Files under the data folder: `logs/slack.log`, `state/slack.pid`, `state/slack.json` (read
cursors), `state/notices.json` (shared notice state, below).

## Commands

```bash
swarm slack start [--foreground] [--runner nohup|tmux]   # run the bridge in the background
swarm slack stop
swarm slack status          # running or not, channel, users, token found, last log lines
swarm slack logs [-f] [-n 40]
swarm slack test            # auth.test + a hello post + a channel read; clear error without a token
swarm slack run [--once]    # the poll loop in the foreground; --once: one cycle, wait, exit
swarm slack post TEXT [--task ID | --thread TS]   # post as the bot
```

`start` with the `nohup` runner keeps a pid file; with `tmux` it runs in the tmux session
`tstack-slack` (attach with `tmux attach -t tstack-slack`). The tmux runner needs the token in
`token_file`, since the tmux server does not see your shell's environment. Without tmux installed,
`--runner tmux` says so and stops.

## In Slack

Top-level messages in the channel, from an allowed user:

| Message | What happens |
|---|---|
| `help` | the command list |
| `status` | every named herdr agent and its state, active task counts per owner |
| `list` / `list all` | the task list (as `swarm list` / `swarm list --all`) |
| `tasks <owner>` | all tasks of one owner |
| `screen <agent>` | the last 40 lines of the agent's screen, secrets masked |
| `@<manager> <text>` | `<text>` goes to that manager |
| anything else | goes to the overall manager (`swarm.overall`) |

Forwarded messages go through `messaging.say`, the same safe prompt as `swarm say`: it waits until
the agent is idle and you are not typing in its pane. The bridge reacts 👀 on receipt and ✅ on
delivery, or replies in the thread when the agent stayed busy for `say_wait_minutes`. The prompt
tells the agent how to answer: `swarm slack post --thread <ts> "<text>"`.

Replies in a **task's notice thread** go to the task's owner, as the answer to the waiting
question (or as a note when the task is not waiting). Only threads of tasks that are still open,
running or waiting are read. Other threads are not read.

The first run starts reading from that moment; older history is never replayed.

## Task notices

With `notices: bridge`, each cycle posts:

- **waiting-on-you**: the question and the recommended answer; reply in the thread to answer;
- **done**: the last verdict, the result summary (600 characters), the result path;
- **dropped**: the reason.

The first notice of a task starts its thread; later notices of the same task are thread replies
also sent to the channel. A task that waits again gets a new notice; a second verdict does not.
Waiting tasks always qualify; done and dropped tasks only within 7 days. On the very first run,
tasks already done or dropped are marked seen without a post, and waiting tasks are posted. A
failed post is retried next cycle.

### Shared state: `<data_dir>/state/notices.json`

The bridge and the routine daemon share this file so each event is announced once, whoever owns
notices. Take the lock `<data_dir>/state/notices.lock` (an exclusive `flock`, `util.file_lock`)
for each read-modify-write, and write atomically (`util.write_atomic`).

```json
{
  "version": 1,
  "seen": {
    "slack": {"2026-10-03-fix-login": "waiting-on-you@2026-10-03T11:12:58-07:00"},
    "herdr": {"2026-10-03-fix-login": "done"}
  },
  "threads": {
    "2026-10-03-fix-login": {"channel": "C0123456789", "ts": "1791051178.677955"}
  }
}
```

- `seen.<consumer>.<task id>`: the last notice key that consumer delivered. One consumer per
  channel of delivery (`slack` for Slack posts; another daemon picks its own name, such as
  `herdr` for pop-ups). A missing consumer means "first run" for it. Key: `waiting-on-you@<waiting_since>`
  while waiting, else the status (`done`, `dropped`).
- `threads.<task id>`: the Slack thread of the task's notices. The bridge reads replies there;
  `swarm slack post --task` adds one when missing. Entries of tasks that no longer exist are pruned.
- Readers ignore keys they do not know; writers keep them.

When the routine daemon owns notices (`slack.notices: routine`), it calls
`slack.post_notices(slack.transport(), channel)` on its own schedule. That keeps the same keys
and threads, so the bridge still routes thread replies to task owners.

## Swapping the backend

All chat traffic goes through `slack.Transport`, five methods:

```python
class Transport:
    def auth(self) -> dict: ...                                    # {"user_id": <bot's own id>, ...}
    def history(self, channel, oldest) -> list: ...                # top-level messages after oldest, oldest first
    def replies(self, channel, thread_ts, oldest) -> list: ...     # thread replies after oldest, oldest first
    def post(self, channel, text, thread_ts=None, broadcast=False) -> str: ...   # returns the new ts
    def react(self, channel, ts, name) -> None: ...                # may ignore failures
```

Messages are dicts with `ts`, `user`, `text`, and `thread_ts` in threads; `ts` values must sort as
numbers. `SlackWebAPI` is the default. To add another (Slack Socket Mode, Mattermost, Matrix,
Discord), write a class with these methods, add its name to `transport()` in `slack.py`, and set
`slack.backend` to it. A push-based backend can buffer events in memory and hand them out from
`history`/`replies`.

## Rate limits

Each cycle calls `conversations.history` once, plus `conversations.replies` once per active task
thread. With the default 10 seconds that stays well inside Slack's limits for internal apps. On
HTTP 429 the client waits the `Retry-After` seconds (at most 10 minutes) and retries up to 4 times;
errors are logged and never stop the loop. Raise `poll_seconds` if you see rate-limit lines in the
log.

## Security

- Only messages from `allowed_users` in `channel` are acted on; other users, bots, edits and the
  bot's own posts are ignored. Use a private channel or a DM.
- Anyone who can post as an allowed user can prompt your agents, and agents can run commands on
  your machine. Protect that Slack account with two-factor sign-in.
- The token never goes in the settings file or on a command line. Keep the token file at mode 600;
  the bridge logs a warning when others can read it. Revoke the token in the app settings if it
  leaks.
- Everything the bridge posts passes through a mask for common secret shapes (Slack, cloud,
  GitHub, GitLab and API keys, private keys, `password=`/`token:` assignments). It is a safety net,
  not a guarantee: do not ask for screens or results that hold secrets, and tell agents never to
  `swarm slack post` credentials.
- Slack keeps every message. Treat the channel as a log that others in your workspace's admin
  roles could read.

## Running it at login (optional samples)

`swarm slack start` is enough for most people. To have the OS keep the bridge running, adapt one of
these samples; tstack does not install them. Both run `swarm slack run` in the foreground and read
the token from `token_file`. Replace `HOME_DIR` with your home folder (`echo $HOME`).

macOS, `~/Library/LaunchAgents/dev.tstack.slack.plist`, then
`launchctl load ~/Library/LaunchAgents/dev.tstack.slack.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.tstack.slack</string>
  <key>ProgramArguments</key>
  <array><string>HOME_DIR/.local/bin/swarm</string><string>slack</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>HOME_DIR/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>HOME_DIR/tstack-data/logs/slack.log</string>
  <key>StandardErrorPath</key><string>HOME_DIR/tstack-data/logs/slack.log</string>
</dict>
</plist>
```

Linux, `~/.config/systemd/user/tstack-slack.service`, then
`systemctl --user daemon-reload && systemctl --user enable --now tstack-slack`
(add `loginctl enable-linger $USER` to keep it running after you log out):

```ini
[Unit]
Description=tstack Slack bridge
After=network-online.target

[Service]
ExecStart=%h/.local/bin/swarm slack run
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=30
StandardOutput=append:%h/tstack-data/logs/slack.log
StandardError=append:%h/tstack-data/logs/slack.log

[Install]
WantedBy=default.target
```

herdr must be reachable from that environment (put it on `PATH` or set `HERDR_BIN`). Do not mix
these units with `swarm slack start`; run one or the other.

## Testing without Slack

Set `slack.api_base` to a local stub (a small `http.server` that answers `auth.test`,
`conversations.history`, `conversations.replies`, `chat.postMessage` and `reactions.add` with
`{"ok": true, ...}`), set `HERDR_BIN` to a fake herdr script, and use a temporary `TSTACK_CONFIG`
and data folder. `swarm slack run --once` then runs one full cycle and waits for deliveries.
