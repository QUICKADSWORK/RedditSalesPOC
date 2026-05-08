.PHONY: help run dev install clean reset

help:
	@echo "Reddit Sales POC — local site"
	@echo ""
	@echo "  make run        Set up everything and start the site on :8000"
	@echo "  make dev        Same, but with auto-reload"
	@echo "  make install    Just install dependencies into .venv"
	@echo "  make reset      Wipe .venv and start fresh"
	@echo "  make clean      Remove caches and venv"

run:
	./run.sh

dev:
	RELOAD=1 ./run.sh

install:
	@python3 -m venv .venv 2>/dev/null || true
	@. .venv/bin/activate && pip install --upgrade pip && pip install -r backend/requirements.txt
	@echo "Dependencies installed in .venv"

reset:
	./run.sh --reset

clean:
	rm -rf .venv backend/__pycache__ backend/agent/__pycache__
