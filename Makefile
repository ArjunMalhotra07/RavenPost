# Print Server Makefile

PI_HOST = arjun-malhotra@192.168.1.20
PI_DIR  = ~/programs/RavenPost

# --- Local ---

venv:
	python3 -m venv venv

install: venv
	. venv/bin/activate && pip install -r requirements.txt

run: install
	. venv/bin/activate && python -m app.main

# --- Deploy to Pi (git-based) ---

# Commit all changes, push, and pull on Pi
deploy:
	git add -A
	git commit -m "deploy" || true
	ggpush
	ssh $(PI_HOST) 'cd $(PI_DIR) && git pull'

# Copy .env to Pi (one-time, not in git)
deploy-env:
	scp .env $(PI_HOST):$(PI_DIR)/.env

# Full setup on Pi (run once after first clone)
pi-setup:
	ssh $(PI_HOST) 'cd $(PI_DIR) && python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt && mkdir -p uploads'

# Start the server on the Pi
pi-run:
	ssh $(PI_HOST) 'cd $(PI_DIR) && . venv/bin/activate && python -m app.main'

# Install and enable systemd service on the Pi
pi-service:
	scp print-server.service $(PI_HOST):/tmp/print-server.service
	ssh $(PI_HOST) 'sudo cp /tmp/print-server.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable print-server && sudo systemctl restart print-server'

# Restart the service on Pi after a deploy
pi-restart:
	ssh $(PI_HOST) 'sudo systemctl restart print-server'

# Check service status on the Pi
pi-status:
	ssh $(PI_HOST) 'sudo systemctl status print-server'

# View logs on the Pi
pi-logs:
	ssh $(PI_HOST) 'sudo journalctl -u print-server -f'

# SSH tunnel for testing (access at http://127.0.0.1:8000 on your Mac)
tunnel:
	ssh -L 8000:127.0.0.1:8000 $(PI_HOST)

# --- Helpers ---

# Generate a new password hash (usage: make hash PASS=mysecretpassword)
hash:
	@python3 -c "import bcrypt; print(bcrypt.hashpw('$(PASS)'.encode(), bcrypt.gensalt()).decode())"

.PHONY: install run deploy deploy-env pi-setup pi-run pi-service pi-restart pi-status pi-logs tunnel hash
