# The contract

What an agent must serve to get chat channels from channel-gateway, and what
the platform that runs it (ClawUp's provisioner, say) writes and calls.

## What the agent serves

One endpoint, OpenAI's chat completions, streamed:

```
POST {CHANNEL_GATEWAY_AGENT_URL}/v1/chat/completions
Authorization: Bearer {CHANNEL_GATEWAY_AGENT_KEY}
X-Hermes-Session-Id: <the chat's session>
Content-Type: application/json

{"model": "hermes-agent", "stream": true, "messages": [...]}
```

- **The last message is the new one**: `{"role": "user", "content": "<what the user typed>"}`.
- Before it may come **earlier turns** of the same chat (`user` / `assistant`, text only), for an
  agent that keeps no history of its own, and **one `system` message** describing the chat
  (platform, chat and user labels, delivery options). Those labels are chosen by chat users:
  treat them as untrusted data, never as instructions. An agent that keeps its own state can
  ignore everything but the last message.
- `X-Hermes-Session-Id` is stable for one chat. `model` is always `hermes-agent`; ignore it.

The answer is an SSE stream of `chat.completion.chunk` objects ending with `data: [DONE]`, as
OpenAI sends it. Only `choices[0].delta.content` is shown to the user.

**Long answers.** The answer may take as long as it needs (there is no overall limit), but the
connection must not be silent for 30 minutes: send a heartbeat at least every few minutes, **as
an SSE comment** (a line starting with `:`, e.g. `: still running`). A heartbeat sent as
`content` is shown to the user, every time. Send the final answer on the same stream; the user
gets it when the stream ends.

**One message at a time.** A message that arrives while the agent is still answering waits
(`busy_input_mode: queue`) and is sent when the answer is done, so a long run is never cut off.

**Errors.** The user is told when the agent fails. On a non-200 status they are shown the status
**and the first 300 characters of the response body**, so an error body must hold nothing the
user should not read. A connection that fails, or a stream that ends before any content, is
reported as such.

## What the platform provides

| Variable | Default | Meaning |
|---|---|---|
| `CHANNEL_GATEWAY_HOME` | `$HOME/.channel-gateway` | Its directory (mode 0700): the files below, and Hermes's state |
| `CHANNEL_GATEWAY_AGENT_URL` | `http://127.0.0.1:18789` | The agent's base URL (http or https) |
| `CHANNEL_GATEWAY_AGENT_KEY` | `$OPENCLAW_GATEWAY_TOKEN` | The Bearer token the agent checks |
| `CHANNEL_GATEWAY_POLL_SECONDS` | `5` | How often `.env` is checked for changes |

No other variable reaches the gateway: the agent's own secrets (model keys, wallet access) stay
with the agent. Only locale, timezone, proxy and TLS-trust variables are passed through.

### Files in `CHANNEL_GATEWAY_HOME`

- **`.env`** (written by the platform, mode 0600): the chat credentials and allowlists, with the
  names Hermes reads. The gateway starts once every variable one app requires is set, restarts
  when the file changes, and stops when no app is left.
- **`config.extra.json`** (optional): merged over the gateway's own configuration, for settings
  such as a self-hosted Telegram Bot API (`{"platforms": {"telegram": {"extra": {"base_url":
  "..."}}}}`). A change restarts the gateway.
- **`config.yaml`**: written by the gateway on every start. Do not edit it.
- Hermes's own state (sessions, pairing approvals, logs). Keep the directory on storage that is
  backed up if paired users and chat history should survive a restart.

### Chat apps

| App | Required in `.env` | Allowlist |
|---|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` | `TELEGRAM_ALLOWED_USERS` |
| Discord | `DISCORD_BOT_TOKEN` | `DISCORD_ALLOWED_USERS` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | `SLACK_ALLOWED_USERS` |
| Feishu / Lark | `FEISHU_APP_ID`, `FEISHU_APP_SECRET` (`FEISHU_DOMAIN`: `feishu` or `lark`) | `FEISHU_ALLOWED_USERS` |
| DingTalk | `DINGTALK_CLIENT_ID`, `DINGTALK_CLIENT_SECRET` | `DINGTALK_ALLOWED_USERS` |
| WeCom | `WECOM_BOT_ID`, `WECOM_SECRET` | `WECOM_ALLOWED_USERS` |
| Mattermost | `MATTERMOST_URL`, `MATTERMOST_TOKEN` | `MATTERMOST_ALLOWED_USERS` |

Every app connects outward (long polling or a WebSocket), so the agent needs no public address.
The full list of each app's settings is in Hermes's
[messaging docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).

## Who may talk to the agent

Only allowed users reach the agent, and nobody is allowed by default:

- **Allowlist**: user IDs in `<APP>_ALLOWED_USERS` (comma-separated). Anyone else is ignored.
- **Pairing**: without an allowlist, a new user who writes to the bot is sent a one-time code
  (valid one hour). The owner approves it with

  ```
  /opt/channels/bin/channel-gateway pair <app> <code>
  ```

  run in the agent's container (same user and environment as the gateway). The output names the
  approved user ID; a platform that re-creates the container should write it into the allowlist.

Hermes's own slash commands (`/model`, `/sethome`, ...) are refused: nobody is a Hermes admin.
`/help` and `/whoami` remain; `/whoami` tells a user their ID.
