# FAQ

## Does my data leave my machine?

Not unless you send a message that routes to Claude. Document
embedding, semantic search, memory, and local-model chat all run
entirely on your machine. The Anthropic API call only carries the
text of messages routed to Claude.

## Can I run it without Ollama or LM Studio?

Yes. The app works fine with just Claude, but every message will go
to the API and your costs will be higher. The status bar shows
"Local model offline" so you know.

## Why is the API key stored in the keyring instead of a config file?

So a stolen settings file or accidental git commit doesn't leak it.
The key lives in Windows Credential Manager / macOS Keychain / Linux
SecretService.

If the OS keyring is unavailable — a headless Linux box without
SecretService, a misconfigured Keychain, a broken DPAPI — the app
falls back to plaintext in `settings.json` so it can still function.
When that happens the status bar shows a yellow "⚠ API key stored in
plaintext" chip so you know what's in effect.

## How do I change the Claude model?

Settings → Model → Claude model. Pricing is captured per family
(Haiku / Sonnet / Opus); set custom prices in `model_prices` if
Anthropic changes them and the app's defaults drift.

## How do I add documents?

Documents panel → Add files / Add folder. Files are chunked,
embedded with fastembed, and stored in sqlite-vec alongside the rest
of the database.

## What's the per-conversation budget for?

A safety net so a runaway agent loop doesn't burn through your API
credit. Set it in Settings; the conversation pauses when cumulative
cost crosses the limit. Set to 0 for no limit.

## Why does the assistant sometimes say "Approaching budget limit"?

You crossed the warning threshold (default 80%). Adjust the threshold
or the budget in Settings.

## Where do I report a bug?

[GitHub Issues](https://github.com/zasonic/AltoSymbiosisAgents/issues).
Attach the diagnostics export from Settings → Troubleshooting.

## Where's the v5 code?

The pre-v6 codebase lives on the
[legacy/v5 branch](https://github.com/zasonic/AltoSymbiosisAgents/tree/legacy/v5).
