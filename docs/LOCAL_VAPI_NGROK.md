# Local Vapi + ngrok runbook

## 1. Start the application

From `F:\rpt`:

```powershell
docker compose up --build -d
Invoke-RestMethod http://localhost:8000/ready
Invoke-RestMethod http://localhost:9000/health
```

`/ready` must return `status=ready`. If it does not, inspect `docker compose logs api` and verify the
development `SUPABASE_DB_URL` (including TLS and pooler settings).

## 2. Start the HTTPS tunnel

Install/authenticate ngrok once, then expose only the API port:

```powershell
ngrok http --domain=cornmeal-sixtyfold-enclose.ngrok-free.dev 8000
```

Keep this process running. Confirm the tunnel:

```powershell
Invoke-RestMethod https://cornmeal-sixtyfold-enclose.ngrok-free.dev/health
Invoke-RestMethod https://cornmeal-sixtyfold-enclose.ngrok-free.dev/ready
```

Do not expose port 9000. Stride/Twilio/Keap mocks are reached only over the internal Compose network.

## 3. Synchronize Vapi

Ensure `.env` contains the Vapi API key, assistant ID, phone-number ID, webhook secret, and:

```dotenv
PUBLIC_BASE_URL=https://cornmeal-sixtyfold-enclose.ngrok-free.dev
VAPI_MODE=real
STRIDE_MODE=mock
TWILIO_MODE=mock
KEAP_MODE=mock
```

Then run:

```powershell
$env:PYTHONPATH = "src"
python scripts/sync_vapi.py
```

The sync configures these server URLs:

- tools: `https://cornmeal-sixtyfold-enclose.ngrok-free.dev/api/v1/vapi/tools`
- end-of-call webhook: `https://cornmeal-sixtyfold-enclose.ngrok-free.dev/api/v1/vapi/webhook`

All three tools use the same custom credential and `X-Vapi-Secret`. The local API rejects authentication
before database/business processing. If the ngrok domain changes, update `PUBLIC_BASE_URL` and rerun sync.

## 4. Test safely

Create a synthetic lead only after the phone owner explicitly consents. The command in the README records
a consent reference and marks the row `is_test=true`. Never use `rpt demo` with real Vapi; the CLI blocks it.

Useful diagnostics:

```powershell
docker compose ps
docker compose logs --tail 200 api worker mock-provider
docker compose exec worker rpt tick
```

Vapi request trace IDs appear in `X-Trace-ID` and in `logs/rpt-agent-api.jsonl`. A successful call dispatch
must return a non-empty Vapi call ID. Vapi tool execution returns HTTP 200 even for a business error; inspect
the matching result/error string and exact `toolCallId`.

## Current contract references

- [Custom tools](https://docs.vapi.ai/tools/custom-tools)
- [Custom-tool troubleshooting](https://docs.vapi.ai/tools/custom-tools-troubleshooting)
- [Outbound calls](https://docs.vapi.ai/calls/outbound-calling)
- [Dynamic variables](https://docs.vapi.ai/assistants/dynamic-variables)
- [Server authentication](https://docs.vapi.ai/server-url/server-authentication)
- [Prompting guide](https://docs.vapi.ai/prompting-guide)
- [Composer](https://docs.vapi.ai/composer)

Composer can help draft the assistant, but these external tool contracts, authentication rules, and workflow
state transitions remain source-controlled here and are synchronized through the Vapi API.
