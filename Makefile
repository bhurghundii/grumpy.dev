.PHONY: dev test down build up logs seed eval

# Starts Postgres via compose, then runs the app locally with uv against it
# (published on host port 5433 — see README/.env.example).
dev:
	docker compose up -d db
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	uv run pytest

down:
	docker compose down

build:
	docker compose build

up:
	docker compose up --build

logs:
	docker compose logs -f

# Inserts a session with a realistic diff directly into Postgres and
# prints the local /s/{token} URL — no Action, no GitHub, no webhook.
seed:
	uv run python -m scripts.seed

# Runs the eval cases in evals/cases/ against the real grader and prints
# pass/fail per case. Model calls are cached in evals/cassettes/ — delete
# a cassette to force a re-record. Needs MODEL_API_KEY set to record.
eval:
	uv run python -m evals.run
