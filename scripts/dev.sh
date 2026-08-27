#!/usr/bin/env sh
set -eu
command="${1:-start}"
case "$command" in
  setup) python -m venv .venv; .venv/bin/python -m pip install -e '.[dev]' ;;
  migrate|verify|seed|demo) .venv/bin/rpt "$command" ;;
  start)
    docker compose run --rm api rpt migrate
    docker compose up --build -d
    python scripts/wait_for_services.py
    echo 'RPT API:        http://localhost:8000/docs'
    echo 'Provider mocks: http://localhost:9000/docs'
    docker compose logs -f
    ;;
  test) .venv/bin/python -m pytest ;;
  *) echo "usage: $0 setup|migrate|verify|seed|start|test|demo"; exit 2 ;;
esac
