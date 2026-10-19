.PHONY: setup run test lint eval-retail eval-second data
setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
run:
	.venv/bin/uvicorn app.main:default_app --factory --port 8000
test:
	.venv/bin/pytest -q
lint:
	.venv/bin/ruff check .
data:
	.venv/bin/python -I scripts/fetch_uci.py
eval-retail: data
	.venv/bin/python -m evals.run evals/uci_retail.yaml
eval-second:
	.venv/bin/python -m evals.run evals/orders_second.yaml
