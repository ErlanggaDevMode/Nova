# Smart Home Integration (Phase 4 Scaffold)

This folder is a structured scaffold for smart home / IoT integration per `prd.md` F4.2.

## Scoping Rule
Per the PRD constraints:
> **Smart home / IoT integration** — scoped per whatever devices Erlangga actually owns at the time this phase starts (not speculative — confirm real hardware before building integrations).

Do not write speculative integration drivers for Philips Hue, Home Assistant, etc. until physical hardware or concrete client APIs are confirmed directly by Erlangga.

## Integration Blueprint
When adding new integrations:
1. Define a sub-package for the target device (e.g. `nova_core/integrations/smart_home/tuya/`).
2. Add device action templates under a dedicated category (e.g. `smart_home`).
3. Add the category keys and validation check rules inside `policy.yaml` (e.g., allow/deny ranges for lights, locks, or plugs).
4. Integrate the execution dispatch logic in `nova_core/action_executor.py`'s server-side dispatching.
