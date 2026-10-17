# Print Server

A lightweight, secure web-based remote printing service for Raspberry Pi. Upload PDFs through a web interface, and they get printed via CUPS on the connected printer.

**Stack:** FastAPI + Jinja2 + CUPS (lp/lpstat) + Cloudflare Tunnel

## Architecture

```
Internet → Cloudflare Tunnel → 127.0.0.1:8000 (FastAPI) → CUPS → Canon LBP2900B
```

The FastAPI server binds to localhost only. Cloudflare Tunnel provides external HTTPS access without opening any router ports. Cloudflare Access adds an additional authentication layer.

## Project Structure

```
print-server/
├── app/
│   ├── main.py          # FastAPI routes, upload handling, cleanup task
│   ├── auth.py          # Password verification, session tokens, CSRF
│   ├── printer.py       # Safe wrappers around lp and lpstat
│   ├── config.py        # Env-based configuration
│   ├── templates/       # Jinja2 HTML templates
│   │   ├── login.html   # Login page
│   │   ├── index.html   # Upload & print form
│   │   └── jobs.html    # Job history table
│   └── static/          # CSS/JS (currently empty, inline styles used)
├── uploads/             # Temporary PDF storage (auto-cleaned)
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
├── print-server.service # systemd unit file
└── README.md
```

## Setup on Raspberry Pi (Debian 13 Trixie)

### 1. Prerequisites

CUPS must be installed and your printer working:

```bash
# Verify printer is available
lpstat -p LBP2900
lp -d LBP2900 /usr/share/cups/data/testprint  # test print
```

### 2. Install the Print Server

```bash
# Clone or copy the project to the Pi
cd /home/pi
# (copy print-server/ directory here)

cd print-server

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env

# Generate a secret key
python3 -c "import secrets; print(secrets.token_hex(32))"
# Paste the output as SECRET_KEY in .env

# Generate a password hash
python3 -c "from passlib.hash import bcrypt; print(bcrypt.hash('your-secure-password'))"
# Paste the output as ADMIN_PASSWORD_HASH in .env

# Edit .env with your values
nano .env
```

### 4. Test Manually

```bash
source venv/bin/activate
python -m app.main
# Visit http://127.0.0.1:8000 from the Pi
```

### 5. Install systemd Service

```bash
# Edit print-server.service if your username or paths differ
sudo cp print-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable print-server
sudo systemctl start print-server

# Check status
sudo systemctl status print-server
sudo journalctl -u print-server -f
```

### 6. Ensure User Can Print

The service runs as user `pi` in group `lpadmin`. Make sure this user can submit print jobs:

```bash
sudo usermod -aG lpadmin pi
```

## Cloudflare Tunnel Setup

This exposes the local server to the internet without opening router ports.

### 1. Install cloudflared

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb
```

### 2. Authenticate and Create Tunnel

```bash
cloudflared tunnel login
# This opens a browser; select your Cloudflare domain

cloudflared tunnel create print-server
# Note the tunnel ID printed

cloudflared tunnel route dns print-server print.yourdomain.com
# Replace with your actual subdomain
```

### 3. Configure the Tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL-ID>
credentials-file: /home/pi/.cloudflared/<TUNNEL-ID>.json

ingress:
  - hostname: print.yourdomain.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

### 4. Run as a Service

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

### 5. Add Cloudflare Access (Recommended)

For an additional authentication layer in front of the app:

1. Go to [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/)
2. Navigate to **Access → Applications → Add an application**
3. Choose **Self-hosted**
4. Set the application domain to `print.yourdomain.com`
5. Create an **Access Policy**:
   - Policy name: "Print Server Access"
   - Action: **Allow**
   - Add a rule: **Emails** — enter the allowed email addresses
6. Save

Now users must authenticate with Cloudflare Access (email OTP, Google, GitHub, etc.) before they even reach the FastAPI login page. This provides two layers of authentication.

## Security Notes

- **No shell injection:** All subprocess calls use argument arrays, never string concatenation.
- **PDF validation:** Files are checked for the `%PDF-` header and parsed with PyPDF2 before printing.
- **Random filenames:** Uploaded files get UUID-based names; original filenames are discarded.
- **Path traversal protection:** File paths are validated to be within the uploads directory.
- **Auto-cleanup:** Uploaded files are deleted immediately after printing, and a background task cleans up any stragglers older than 5 minutes.
- **CSRF tokens:** All POST forms include a session-bound CSRF token.
- **Rate limiting:** Login is limited to 5 attempts per minute per IP.
- **Session security:** Cookies are httponly, secure, and samesite=strict.
- **Localhost binding:** The server only listens on 127.0.0.1.
- **systemd hardening:** NoNewPrivileges, ProtectSystem, PrivateTmp are enabled.
- **Secrets in .env:** No passwords or keys in source code.
