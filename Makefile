.PHONY: help install pipeline scrape classify signals brief dashboard test lint doctor checkpoint clean-experiments

help:
	@echo "make install           install all dependencies"
	@echo "make pipeline          full run: scrape -> classify -> signals -> brief"
	@echo "make scrape            scrape careers pages only"
	@echo "make classify          classify pending postings only"
	@echo "make signals           print this week's signals"
	@echo "make brief             generate the weekly brief"
	@echo "make dashboard         launch the Streamlit dashboard"
	@echo "make doctor            check LLM provider health and quota"
	@echo "make test              run the test suite"
	@echo "make lint              run ruff"
	@echo "make checkpoint        fold the SQLite WAL into the .db file"
	@echo "make clean-experiments drop non-v1 classification rows"

install:
	uv sync --all-extras

# The manual equivalent of the weekly GitHub Actions workflow.
pipeline:
	uv run telltale run --prompt-version v3

scrape:
	uv run telltale scrape

classify:
	uv run telltale classify --prompt-version v3

signals:
	uv run telltale signals

brief:
	uv run telltale brief

dashboard:
	uv run streamlit run dashboard/app.py

doctor:
	uv run telltale doctor --deep

test:
	uv run python -m pytest tests/ -q

lint:
	uv run ruff check telltale/ tests/ scripts/

checkpoint:
	uv run python scripts/checkpoint_db.py

clean-experiments:
	uv run python scripts/cleanup_experiments.py
