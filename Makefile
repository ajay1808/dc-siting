.PHONY: all grid ingest score tiles web serve check clean audit
PY := ./.venv/bin/python

all: grid ingest score tiles web

check:  ; $(PY) pipeline/check.py
grid:   ; $(PY) pipeline/grid.py
ingest: ; $(PY) pipeline/ingest.py
score:  ; $(PY) pipeline/score.py
tiles:  ; $(PY) pipeline/tiles.py && mkdir -p web/tiles && cp tiles/*.pmtiles web/tiles/
web:    ; $(PY) pipeline/webconfig.py
serve:  ; npx --yes http-server web -p 8099 -c-1 --cors

audit: ## fail if any .env key ever reached git history or the published site
	@./.venv/bin/python pipeline/audit_secrets.py

clean:  ; rm -rf data/interim/* data/out/* tiles/*.pmtiles web/tiles/*.pmtiles
