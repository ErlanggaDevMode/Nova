# Nova Assistant — 30-Day Improvement Roadmap (150 Phases)

This roadmap details the daily action items for Nova's next 30 days of development, matching the updated specifications in `prd.md`.

---

## 📅 Week 1: Database, Hybrid Router & Context Synchronization

### Day 1 — Phase 8: Advanced DB Architecture
- [x] **F8.1**: Implement connection pooling for SQLite and PostgreSQL in `store.py`.
- [x] **F8.2**: Develop automated reconnect retry loops on database locks.
- [x] **F8.3**: Add dynamic runtime database schema profiles switcher.
- [x] **F8.4**: Build SQL transaction execution trace logger.
- [x] **F8.5**: Expose connection performance and status check REST API.

### Day 2 — Phase 9: Schema Migrations & Fixtures
- [x] **F9.1**: Create migration scripts state and version tracker.
- [x] **F9.2**: Build schema rollback capability framework.
- [x] **F9.3**: Develop connection database validation and integrity scanner.
- [x] **F9.4**: Implement database state backup export and JSON fixtures import.
- [x] **F9.5**: Configure automated database vacuuming scheduler task.

### Day 3 — Phase 10: Router Calibrator & Heuristics
- [x] **F10.1**: Add sentiment and confidence calibration mappings.
- [x] **F10.2**: Implement fast rule matcher fallback heuristics.
- [x] **F10.3**: Enable latency trace monitors on cloud requests.
- [x] **F10.4**: Wrap cloud LLM fallback exception interceptors.
- [x] **F10.5**: Create model accuracy statistics logging table.

### Day 4 — Phase 11: Context Window Optimization
- [x] **F11.1**: Code context size pruning algorithm using sliding windows.
- [x] **F11.2**: Implement short-term memory limits on device logs.
- [x] **F11.3**: Set context metadata keys hierarchy prioritizer.
- [x] **F11.4**: Schedule time-based state expiry.
- [x] **F11.5**: Expose workspace active context dump API.

### Day 5 — Phase 12: Context Synchronization Merge
- [x] **F12.1**: Implement multi-device context merging logic.
- [x] **F12.2**: Write device hierarchy conflict resolver rules.
- [x] **F12.3**: Enable context state delta update WebSocket broadcasts.
- [x] **F12.4**: Create conflict logging table.
- [x] **F12.5**: Scaffold context sync health dashboard widget feed.

### Day 6 — Phase 13: File System Searching Enhancements
- [ ] **F13.1**: Add regular expression filter support in searches.
- [ ] **F13.2**: Configure exclude directories path filters.
- [ ] **F13.3**: Enable index caching for fast repeat queries.
- [ ] **F13.4**: Integrate file MIME type detector action.
- [ ] **F13.5**: Add file metadata reader integration.

### Day 7 — Phase 14: LLM Structured Response Validators
- [ ] **F14.1**: Define JSON schema validators on tool call arguments.
- [ ] **F14.2**: Write structured output parsing error repair logic.
- [ ] **F14.3**: Integrate interactive command query CLI validator.
- [ ] **F14.4**: Create execution history logging table.
- [ ] **F14.5**: Expose command schema check REST endpoint.

---

## 📅 Week 2: Cron, Notification Triggers & Smart Home Integration

### Day 8 — Phase 15: Time & Cron Scheduler Trigger
- [ ] **F15.1**: Build cron expressions parsing and evaluator.
- [ ] **F15.2**: Develop DST and Timezone offset calculator.
- [ ] **F15.3**: Add scheduler overlap and lock checking.
- [ ] **F15.4**: Create executions log stats database.
- [ ] **F15.5**: Add rule scheduling CRUD REST endpoints.

### Day 9 — Phase 16: Advanced Notification Rules
- [ ] **F16.1**: Implement RegEx matching on notification text payloads.
- [ ] **F16.2**: Add app package source whitelisting checks.
- [ ] **F16.3**: Define notification alert classification rules.
- [ ] **F16.4**: Configure silent notification schedules.
- [ ] **F16.5**: Create incoming notification history logging.

### Day 10 — Phase 17: Weather API & Conditions
- [ ] **F17.1**: Build weather client integrations (OpenWeatherMap API).
- [ ] **F17.2**: Implement temperature/humidity alert matching checks.
- [ ] **F17.3**: Configure dynamic location updates adapter.
- [ ] **F17.4**: Enable weather condition changes trigger evaluation.
- [ ] **F17.5**: Add weather configuration credentials API.

### Day 11 — Phase 18: Platform System Event Triggers
- [ ] **F18.1**: Create disk usage threshold listeners.
- [ ] **F18.2**: Add CPU/RAM status utilization triggers.
- [ ] **F18.3**: Hook network connectivity online/offline state changes.
- [ ] **F18.4**: Build session screen lock/unlock monitor.
- [ ] **F18.5**: Support low battery alert action trigger scripts.

### Day 12 — Phase 19: Rule Chain & Loop Protections
- [ ] **F19.1**: Build sequential rule execution chaining logic.
- [ ] **F19.2**: Develop circular reference loop checks.
- [ ] **F19.3**: Implement rule trigger limits and cool-down schedules.
- [ ] **F19.4**: Add pre-execution dependency validations.
- [ ] **F19.5**: Setup automation loop breaker alerts.

### Day 13 — Phase 20: Smart Plug HTTP Integrations
- [ ] **F20.1**: Add Shelly plug HTTP commands sender.
- [ ] **F20.2**: Integrate TP-Link Kasa local smart plug driver.
- [ ] **F20.3**: Build local network smart plug discovery scanner.
- [ ] **F20.4**: Add smart plug status sync endpoints.
- [ ] **F20.5**: Implement custom smart plug category rule parser.

### Day 14 — Phase 21: Home Assistant Event Adapters
- [ ] **F21.1**: Connect to Home Assistant WebSocket connection manager.
- [ ] **F21.2**: Implement entity state modification trigger tracking.
- [ ] **F21.3**: Add event dispatch API.
- [ ] **F21.4**: Define device capabilities sync maps.
- [ ] **F21.5**: Create REST integration endpoints.

---

## 📅 Week 3: Foreground Android Services, Offline STT & Geofencing

### Day 15 — Phase 22: Android Foreground Service
- [ ] **F22.1**: Implement Android persistent foreground service launcher.
- [ ] **F22.2**: Add sticky status bar notification handlers.
- [ ] **F22.3**: Enable boot receiver start hook.
- [ ] **F22.4**: Implement WorkManager connection sync routines.
- [ ] **F22.5**: Set power saver wake lock hooks.

### Day 16 — Phase 23: Kotlin Notification Listener
- [ ] **F23.1**: Add active notification listener config toggles.
- [ ] **F23.2**: Configure package notification filters.
- [ ] **F23.3**: Create message content privacy scrubber.
- [ ] **F23.4**: Setup event bundling timers.
- [ ] **F23.5**: Create notification events log sqlite.

### Day 17 — Phase 24: Compose UI & System Statistics
- [ ] **F24.1**: Apply custom Outfit typography integration.
- [ ] **F24.2**: Build real-time connectivity state dashboard badge.
- [ ] **F24.3**: Create device hardware utilization log viewer.
- [ ] **F24.4**: List rule toggle control settings.
- [ ] **F24.5**: Add color palette theme settings.

### Day 18 — Phase 25: Android Platform Actions
- [ ] **F25.1**: Implement camera photo capturing action.
- [ ] **F25.2**: Add system volume/brightness slider managers.
- [ ] **F25.3**: Build device flashlight toggle action.
- [ ] **F25.4**: Add media play/pause/skip key dispatchers.
- [ ] **F25.5**: Add screen dimming lock controls.

### Day 19 — Phase 26: Offline Speech Recognition
- [ ] **F26.1**: Configure native offline speech recognizer engine.
- [ ] **F26.2**: Add spoken text listener states.
- [ ] **F26.3**: Map voice query correction.
- [ ] **F26.4**: Run hotword wake-up voice recognition thread.
- [ ] **F26.5**: Create voice command feedback interface.

### Day 20 — Phase 27: Whisper API Voice Routing
- [ ] **F27.1**: Enable Whisper API audio file posts.
- [ ] **F27.2**: Write audio recorder file helper class.
- [ ] **F27.3**: Configure voice query silence threshold trigger.
- [ ] **F27.4**: Build conversational TTS speech responses.
- [ ] **F27.5**: Embed voice engine settings overlay.

### Day 21 — Phase 28: GPS Location Geofencing
- [ ] **F28.1**: Create GPS coordinate scheduler loggers.
- [ ] **F28.2**: Build location geofencing rule monitors.
- [ ] **F28.3**: Capture geofence enter/leave events.
- [ ] **F28.4**: Embed Android location map display widget.
- [ ] **F28.5**: Code location permission request overlay layout.

---

## 📅 Week 4: UI Visualizations, JWT Access Controls & System Audits

### Day 22 — Phase 29: Web Console Analytics
- [ ] **F29.1**: Add live chart histories for CPU, Memory, and Disk stats.
- [ ] **F29.2**: Integrate interactive network throughput loggers.
- [ ] **F29.3**: Show historical device statistics graphs.
- [ ] **F29.4**: Build REST analytics reporting queries.
- [ ] **F29.5**: Set up canvas chart graphics integration.

### Day 23 — Phase 30: Drag-and-Drop Rule Designer
- [ ] **F30.1**: Create visual rule builder canvas.
- [ ] **F30.2**: Add rule status toggles.
- [ ] **F30.3**: Code JSON template editor form.
- [ ] **F30.4**: Implement rule template import/export module.
- [ ] **F30.5**: Configure manual rule test triggers.

### Day 24 — Phase 31: JWT Token Management
- [ ] **F31.1**: Build OAuth2 scope validation filters.
- [ ] **F31.2**: Write token revocation list tables.
- [ ] **F31.3**: Set up access hierarchy control policies.
- [ ] **F31.4**: Configure token refresh token rotators.
- [ ] **F31.5**: Track idle session timeout logs.

### Day 25 — Phase 32: SSL & Network Tunneling Checks
- [ ] **F32.1**: Integrate Cloudflare Tunnel check API.
- [ ] **F32.2**: Implement Tailscale overlay network verification.
- [ ] **F32.3**: Build Let's Encrypt SSL certificate checker.
- [ ] **F32.4**: Set CORS lock configuration parameters.
- [ ] **F32.5**: Show secure tunnel setup help panel.

### Day 26 — Phase 33: Multi-Device Broadcast Channels
- [ ] **F33.1**: Allow cross-device targeted action dispatches.
- [ ] **F33.2**: Add device grouping controls.
- [ ] **F33.3**: Configure command broadcast WebSocket channels.
- [ ] **F33.4**: Trace remote execution pipeline results.
- [ ] **F33.5**: Map device status sync.

### Day 27 — Phase 34: Desktop System Tray Interface
- [ ] **F34.1**: Build tray indicator icon class.
- [ ] **F34.2**: Configure desktop system notification widgets.
- [ ] **F34.3**: Add action permission prompt popups.
- [ ] **F34.4**: Track desktop mic shortcut listener.
- [ ] **F34.5**: Show tray status dashboard window.

### Day 28 — Phase 35: Ollama Local Engine Benchmarks
- [ ] **F35.1**: Build Ollama model manager REST endpoints.
- [ ] **F35.2**: Add Ollama service validator.
- [ ] **F35.3**: Track performance latency speed.
- [ ] **F35.4**: Set context token window settings.
- [ ] **F35.5**: Log offline failover histories.

### Day 29 — Phase 36: System Performance Audit Reports
- [ ] **F36.1**: Build command history CSV/PDF exporter.
- [ ] **F36.2**: Add analytics dashboard widgets.
- [ ] **F36.3**: Track vulnerability scanning results.
- [ ] **F36.4**: Configure system performance analyzers.
- [ ] **F36.5**: Display performance report dashboard.

### Day 30 — Phase 37: System Deployment & Diagnostics
- [ ] **F37.1**: Configure production Docker Compose configs.
- [ ] **F37.2**: Write database backup/restore scripts.
- [ ] **F37.3**: Create global diagnostic validator script.
- [ ] **F37.4**: Configure environment cleanup tasks.
- [ ] **F37.5**: Display diagnostics results dashboard.
