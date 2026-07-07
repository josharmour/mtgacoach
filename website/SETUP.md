# mtgacoach.com Proxy Server Setup

## 1. Cloudflare DNS Setup

In your Cloudflare dashboard for `mtgacoach.com`:

### DNS Records

| Type | Name | Content | Proxy | TTL |
|------|------|---------|-------|-----|
| A | `@` | `YOUR_HOME_IP` | Proxied | Auto |
| A | `api` | `YOUR_HOME_IP` | Proxied | Auto |

**Important:** Keep "Proxied" (orange cloud) enabled — this gives you:
- DDoS protection
- Free SSL/TLS (so clients connect via HTTPS)
- Your home IP stays hidden

### SSL/TLS Settings

1. Go to **SSL/TLS** > **Overview**
2. Set mode to **Full (strict)** if you have a local cert, or **Full** if using self-signed
3. Or set to **Flexible** to let Cloudflare handle SSL and connect to your server via HTTP

For simplest setup, use **Flexible** — Cloudflare terminates HTTPS and connects to your server on port 80/8443 via HTTP.

### Port Forwarding

On your home router, forward:
- **External port 443** → **Internal port 8443** (to your server's LAN IP)
- Or if using Cloudflare Tunnel (recommended), no port forwarding needed

### Cloudflare Tunnel (Alternative — No Port Forwarding)

This is the recommended approach. No ports need to be opened on your router.

```bash
# Install cloudflared
# Linux:
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/

# Login to Cloudflare
cloudflared tunnel login

# Create a tunnel
cloudflared tunnel create mtgacoach

# Configure the tunnel
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: <TUNNEL_ID>
credentials-file: ~/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: mtgacoach.com
    service: http://localhost:8443
  - hostname: api.mtgacoach.com
    service: http://localhost:8443
  - service: http_status:404
EOF

# Route DNS through the tunnel
cloudflared tunnel route dns mtgacoach mtgacoach.com
cloudflared tunnel route dns mtgacoach api.mtgacoach.com

# Run the tunnel
cloudflared tunnel run mtgacoach
```

To run as a service:
```bash
sudo cloudflared service install
```

## 2. Server Setup

### Option A: Docker (Recommended)

```bash
cd website/

# Copy and edit env file
cp .env.example .env
# Edit .env with your API keys and admin password

# Edit config.yaml with your Azure/provider endpoints

# Build and start
docker compose up -d

# Check logs
docker compose logs -f
```

### Option B: Direct Python

```bash
cd website/

# Create venv
python3 -m venv venv
source venv/bin/activate

# Install deps
pip install -r requirements.txt
pip install pyyaml

# Copy and edit env file
cp .env.example .env
source .env

# Run
python app.py
```

### Option C: systemd Service

```bash
# Create service file
sudo tee /etc/systemd/system/mtgacoach.service << 'EOF'
[Unit]
Description=mtgacoach.com Proxy Server
After=network.target

[Service]
Type=simple
User=joshu
WorkingDirectory=/home/joshu/repos/mtgacoach/website
EnvironmentFile=/home/joshu/repos/mtgacoach/website/.env
ExecStart=/home/joshu/repos/mtgacoach/website/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8443
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mtgacoach
sudo systemctl start mtgacoach

# Check status
sudo systemctl status mtgacoach
journalctl -u mtgacoach -f
```

## 3. First-Time Setup

Once the server is running:

1. **Access the admin panel:** `https://mtgacoach.com/admin`
2. **Login** with your admin password
3. **Create your first subscriber** (yourself!) — note the license key
4. **Test the API:**
   ```bash
   curl https://api.mtgacoach.com/health
   curl -H "Authorization: Bearer YOUR_LICENSE_KEY" https://api.mtgacoach.com/v1/models
   ```

## 4. Provider Configuration

Edit `config.yaml` to add/configure AI providers. The proxy tries providers in priority order (lowest first).

Example with Azure as primary, Anthropic as fallback:

```yaml
providers:
  - name: azure
    priority: 1
    base_url: "https://your-resource.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
    api_key: "${AZURE_OPENAI_API_KEY}"
    api_version: "2025-04-01-preview"
    models: ["gpt-4o", "gpt-4o-mini"]
    enabled: true

  - name: anthropic
    priority: 2
    base_url: "https://api.anthropic.com/v1"
    api_key: "${ANTHROPIC_API_KEY}"
    models: ["claude-sonnet-4-5-20250929"]
    enabled: true
```

## 5. Stripe Integration (Later)

The `/subscribe` page currently creates keys directly. To add Stripe:

1. Create a Stripe account and product
2. Add `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` to `.env`
3. Replace the `/subscribe/request` endpoint with Stripe Checkout
4. Add a webhook handler for `checkout.session.completed`
