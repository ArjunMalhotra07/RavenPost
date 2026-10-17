# Print Server Makefile
# Update PI_HOST to match your Raspberry Pi's IP or hostname

PI_HOST = arjun-malhotra@192.168.1.20
PI_DIR  = ~/programs/wifi_printer

# --- Local ---

venv:
	python3 -m venv venv

install: venv
	. venv/bin/activate && pip install -r requirements.txt

run: install
	. venv/bin/activate && python -m app.main

# --- Deploy to Pi ---

deploy:
	scp -r app/ requirements.txt .env Makefile $(PI_HOST):$(PI_DIR)/
	ssh $(PI_HOST) 'mkdir -p $(PI_DIR)/uploads'

# Full setup on Pi (run once after first deploy)
pi-setup:
	ssh $(PI_HOST) 'cd $(PI_DIR) && python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt'

# Start the server on the Pi
pi-run:
	ssh $(PI_HOST) 'cd $(PI_DIR) && . venv/bin/activate && python -m app.main'

# Install and enable systemd service on the Pi
pi-service:
	scp print-server.service $(PI_HOST):/tmp/print-server.service
	ssh $(PI_HOST) 'sudo cp /tmp/print-server.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable print-server && sudo systemctl restart print-server'

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

.PHONY: install run deploy pi-setup pi-run pi-service pi-status pi-logs tunnel hash
