# Exposing Nova Core Server Securely (Tunneling & Access Guide)

Nova Core defaults to listening on `127.0.0.1:8000` to prevent exposure. When you want to connect your devices (like your Android phone) while away from home, configure one of the following secure options:

---

## Option 1: Tailscale (Recommended & Simplest)
Tailscale sets up a zero-config, encrypted virtual private network (overlay mesh network) between your devices.

1. **Install Tailscale**: Download and sign up on both the Core server host machine and your Android phone.
2. **Retrieve MagicDNS / IP**: Tailscale assigns a stable 100.x.x.x IP address (or MagicDNS name, like `my-server.tail-domain.ts.net`) to your Core host.
3. **Configure Clients**:
   * Change `serverUrl` in your Android client (`MainActivity.kt`) or configure environment overrides:
     ```kotlin
     serverUrl = "http://100.x.y.z:8000"
     ```
   * Since Tailscale handles wireguard encryption internally, you do not need to set up SSL/HTTPS. Only devices inside your Tailscale account can communicate.

---

## Option 2: Cloudflare Tunnel (Public domain with SSL)
Exposes local port 8000 to a public URL (e.g. `https://nova.yourdomain.com`) passing through Cloudflare's edge security.

1. **Install cloudflared**: Download the Cloudflare Tunnel daemon on the server.
2. **Authenticate & Create Tunnel**:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create nova-tunnel
   ```
3. **Configure Ingress Rules**: Create a `config.yml`:
   ```yaml
   url: http://localhost:8000
   tunnel: <tunnel-uuid>
   credentials-file: <credentials-path>.json
   ```
4. **Expose WebSocket Support**: Ensure WebSockets are enabled in the Cloudflare dashboard under Network settings.
5. **Run Tunnel**:
   ```bash
   cloudflared tunnel run nova-tunnel
   ```
   * Your clients connect using:
     ```kotlin
     serverUrl = "https://nova.yourdomain.com"
     ```
   * **Security Reminder**: Because this exposes a public HTTPS endpoint, our **JWT Bearer Token** layer on `POST /command`, `POST /event`, and `/ws/` is critical to prevent anonymous execution triggers from crawling bots or unauthorized visitors.
