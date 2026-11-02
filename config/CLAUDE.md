# config — provider configuration kept in git

| Path | Purpose |
| --- | --- |
| `vapi_tools.json` | Vapi tool definitions (`check_availability`, `create_appointment`, `update_lead_status`). Pushed to the Vapi assistant by `scripts/sync_vapi.py`. |
| `vapi_assistant_prompt.md` | The voice assistant's system prompt. Also synced by `scripts/sync_vapi.py`. |
| `sms_agent_prompt.md` | SMS agent ("Elliot by text") prompt, loaded at runtime by `services/sms_agent.py` (no sync step). Reads the Context labels `services/sms_agent_context.context_block` writes and the tool reply codes. |
| `sms_agent_tools.json` | The SMS agent's 8 function tools (strict JSON schema); names and parameters fixed by `docs/SMS_AGENT.md`, dispatched by `services/sms_agent_tools.call_tool`. |
| `n8n/workflows/*.json` | **Older** exports of the n8n workflows (intake, recovery, Sheet webhook, profile sync). |

## Notes

- Tool names and argument shapes in `vapi_tools.json` must match `routes/` and `vapi_contract.py`; see
  `tests/test_vapi_tool_config.py`.
- The n8n exports here are out of date: the live Sheet renamed "Action Status" to "Outcome Status" and the
  live workflows differ. The current production copies are in `../../n8n/` (outside this repo), and the live
  versions can be read through the `n8n-mcp` server. Do not import these files over the live workflows.
