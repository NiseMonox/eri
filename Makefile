.PHONY: dev test media deploy restart logs

dev:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8300 --reload

test:
	uv run pytest -q

media:
	bash scripts/gen_media.sh

deploy:
	cp deploy/health-hub.service /etc/systemd/system/health-hub.service
	# 本机专用的 drop-in(deploy/local/ 不进仓库,没有就跳过)
	if [ -d deploy/local/health-hub.service.d ]; then \
	  mkdir -p /etc/systemd/system/health-hub.service.d && \
	  cp deploy/local/health-hub.service.d/*.conf /etc/systemd/system/health-hub.service.d/; fi
	systemctl daemon-reload
	systemctl enable --now health-hub

restart:
	systemctl restart health-hub

logs:
	journalctl -u health-hub -f
