.PHONY: install test run lint smoke

PYTHON ?= python3

install:
	$(PYTHON) -m pip install -r requirements.txt

test:
	$(PYTHON) -m pytest -q

# Import smoke test — catches syntax/import errors across the app package.
smoke:
	$(PYTHON) -c "import app.main"

lint: smoke
	$(PYTHON) -m pytest --collect-only -q >/dev/null

run:
	$(PYTHON) app/main.py
