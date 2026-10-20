# Natural Language Insights Engine

Ask plain-English questions about any transactional CSV. Upload a file, get a queryable dataset, ask a question,
and get an answer with the SQL, the assumptions, and the result table. Questions the data cannot support are refused.

## Quick start (local)

```bash
make setup                      # venv + dependencies
cp .env.example .env            # then set ANTHROPIC_API_KEY
make run                        # http://localhost:8000
```

Or with Docker: `cp .env.example .env && docker compose up --build`.

Open http://localhost:8000, upload a CSV, wait for "ready", ask a question (or click one of the suggested questions).
Requires Python 3.11+.
Results that suit it are charted automatically (line for time series, bar for rankings, scatter, histogram); the table is always one click away.

Sample data: `make data` downloads the UCI Online Retail dataset to `data/online_retail.csv`.
Small test CSVs (a retail-shaped one and a differently-shaped semicolon-delimited one) are in `tests/fixtures/`.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/datasets` | multipart `file`. 202 `{dataset_id, job_id}` |
| GET | `/datasets`, `/datasets/{id}` | status, rows, inferred schema profile |
| POST | `/datasets/{id}/questions` | `{"question": "...", "follow_up_of": "<job_id>"}` (`follow_up_of` optional; carries up to 5 earlier turns of the thread). 202 `{job_id}`, or 200 `{job_id, cached: true}` when answered from the question cache (`"no_cache": true` regenerates). Optional `Idempotency-Key` header |
| GET | `/jobs/{id}` | `queued / running / succeeded / refused / failed` plus result or error |
| GET | `/health` | liveness, queue depth |

```bash
curl -F file=@data/online_retail.csv localhost:8000/datasets
curl -X POST localhost:8000/datasets/$DS/questions -H 'content-type: application/json' \
     -d '{"question":"Top 10 products by revenue"}'
curl localhost:8000/jobs/$JOB
```

Errors always look like `{"error": {"code", "message", "request_id"}}`; no stack traces are returned.
Status codes: 202 accepted, 400 empty file, 404 unknown id, 409 dataset not ready, 413 too large, 415 wrong type,
422 invalid body, 429 queue full, 500 generic.

## Configuration (env vars, see `.env.example`)

`ANTHROPIC_API_KEY` (required for questions), `ANTHROPIC_WORKSPACE_ID` (only for keys not scoped to a workspace),
`ANTHROPIC_MODEL`, `DATA_DIR`, `WORKERS`, `MAX_UPLOAD_MB`,
`QUERY_TIMEOUT_S`, `MAX_ROWS`, `MAX_QUEUE`, `MAX_ATTEMPTS`.

Optional LLM tracing in [LangSmith](https://smith.langchain.com) (prompts, tokens, cost, latency per question): set
`LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT`. Nothing is sent unless tracing is enabled.

## Tests, lint, evaluation

```bash
make test        # unit + API tests, no network or API key needed (LLM is faked)
make lint
python -m evals.run evals/orders_second.yaml    # live LLM; second CSV
python -m evals.run evals/uci_retail.yaml       # live LLM; needs `make data`
```

Eval suites hold golden questions with reference SQL written independently of the app, plus unanswerable
questions where refusing is the correct result. See `docs/DESIGN.md` for what is and is not covered.
