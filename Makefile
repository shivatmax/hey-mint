PY ?= .venv/bin/python

.PHONY: help setup app install run demo test test-all lint guide clean

help:            ## Show the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

setup:           ## Create .venv and install dependencies (plus pytest and ruff)
	python3 -m venv .venv && $(PY) -m pip install -q -r requirements.txt pytest ruff

app:             ## Build dist/Hey Mint.app and a shareable DMG (Python and models bundled)
	packaging/build_app.sh

install:         ## Build and install ~/Applications/Mint.app
	./install.sh

run:             ## Run from the checkout in the terminal (hands-free)
	$(PY) -m mint --hands-free

demo:            ## Play the animation tour (no API key needed)
	$(PY) -m mint --demo

test:            ## Offline checks
	$(PY) -m pytest

test-all:        ## Also the checks that use the network, AppleScript and ~/Documents/Mint
	MINT_INTEGRATION=1 $(PY) -m pytest

lint:            ## Syntax errors and undefined names
	$(PY) -m ruff check mint tests

guide:           ## Rebuild the guide site from guide/src
	$(PY) guide/build.py

clean:           ## Remove caches and build output
	find . -name __pycache__ -type d -prune -exec rm -rf {} + ; rm -rf build dist .pytest_cache .ruff_cache
