.PHONY: install doctor run serve web test export stats

PY=backend/.venv/bin/python

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -q -e . -e ".[dev]" && .venv/bin/python -m spacy download en_core_web_sm && .venv/bin/playwright install chromium
	cd frontend && npm install

doctor:
	cd backend && .venv/bin/python -m app.cli doctor

run:
	cd backend && .venv/bin/python -m app.cli run --target $(or $(TARGET),1000)

serve:
	cd backend && .venv/bin/python -m app.cli serve

web:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m pytest -q && .venv/bin/ruff check app tests

export:
	cd backend && .venv/bin/python -m app.cli export ../leads.csv

stats:
	cd backend && .venv/bin/python -m app.cli stats
