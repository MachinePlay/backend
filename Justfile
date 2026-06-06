run:
    uv run uvicorn app.main:app --reload --timeout-graceful-shutdown 0

test:
    uv run pytest

coverage:
    uv run pytest --cov --cov-report=term-missing --cov-report=html
    xdg-open htmlcov/index.html

