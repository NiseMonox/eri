.PHONY: dev test media deploy restart logs

dev:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8300 --reload

test:
	uv run pytest -q

media:
	bash scripts/gen_media.sh

deploy:
	cp deploy/health-hub.service /etc/systemd/system/health-hub.service
	systemctl daemon-reload
	systemctl enable --now health-hub

restart:
	systemctl restart health-hub

logs:
	journalctl -u health-hub -f
