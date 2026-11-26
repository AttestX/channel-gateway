# channel-gateway

Chat channels (Telegram, Feishu/Lark, Slack, Discord, DingTalk, WeCom, Mattermost) for any agent
that serves an OpenAI-compatible chat endpoint.

It is [Hermes](https://github.com/NousResearch/hermes-agent)'s messaging gateway in **proxy
mode**: Hermes handles the chat apps (connecting, allowlists, pairing, formatting, retries) and
forwards each message to the agent's `/v1/chat/completions`; it runs no model of its own. We
reuse it rather than writing adapters: Hermes's are mature and maintained, and proxy mode is a
supported part of it.

```
Telegram / Feishu / ...  <-->  channel-gateway  --POST /v1/chat/completions-->  your agent
     (outbound only)           (in the agent's container)       (Bearer key)
```

## Embedding it

Everything is in `/opt/channels`, with its own Python, so any glibc-based image can take it
whatever Python it has, or none:

```dockerfile
COPY --from=ghcr.io/attestx/channel-gateway:0.23.3-1 /opt/channels /opt/channels
```

Then start it beside the agent, in the background:

```sh
CHANNEL_GATEWAY_HOME=/path/in/the/agent/state/channels \
  /opt/channels/bin/channel-gateway &
```

It waits until a chat app is configured in `$CHANNEL_GATEWAY_HOME/.env`, so it can be started
unconditionally. [CONTRACT.md](CONTRACT.md) says what the agent must serve, which files the
platform writes, and how users are let in. It adds about 700 MB to the image (uncompressed),
most of it the Python and the chat apps' SDKs.

## Versions

Images are `ghcr.io/attestx/channel-gateway:<hermes version>-<revision>`, e.g. `0.23.3-1`, and
every commit on main is also `sha-<commit>`. A published version is never moved: changing this
repo releases nothing until `GATEWAY_REVISION` (in `.github/workflows/ci.yml`) or the Hermes pin
(`HERMES_TAG` / `HERMES_COMMIT` in the `Dockerfile`) is bumped. CI checks that the pinned commit
is the one Hermes's tag points to.

Hermes is installed from the pinned commit with its own lockfile, on the CPython release its own
package manager pins (python-build-standalone, stripped build), each download checked by hash.

## Tests

```sh
python3 -m unittest -v tests/test_supervisor.py          # the supervisor, no image needed
docker build -t channel-gateway:test .
(cd tests && python3 -m unittest -v test_image)         # the image, end to end (Linux, Docker)
```

The image tests run the gateway against a stand-in Telegram Bot API and a stand-in agent: an
allowed user's message reaches the agent and its answer comes back, a stranger's does not, a new
user is paired with a code, Hermes's own commands are refused, and the gateway follows changes
to `.env`.

## License

MIT. Hermes is MIT too; its license is shipped in `/opt/channels/licenses/`.
