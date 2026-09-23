.PHONY: dev test media deploy restart logs stt-model deploy-stt dev-stt logs-stt

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

# 语音识别进程 eri-stt(语音入口用;不装也不影响其他功能)
stt-model:
	bash scripts/fetch_stt_model.sh

deploy-stt: stt-model
	cp deploy/eri-stt.service /etc/systemd/system/eri-stt.service
	if [ -d deploy/local/eri-stt.service.d ]; then \
	  mkdir -p /etc/systemd/system/eri-stt.service.d && \
	  cp deploy/local/eri-stt.service.d/*.conf /etc/systemd/system/eri-stt.service.d/; fi
	systemctl daemon-reload
	systemctl enable --now eri-stt

dev-stt:
	uv run uvicorn stt.server:app --host 127.0.0.1 --port 8310

logs-stt:
	journalctl -u eri-stt -f
