.PHONY: install demo run worker test bench lint clean

install:
	pip install -r requirements-dev.txt

demo:
	python scripts/seed_demo.py --reset

run:
	PYTHONPATH=src python -m uvicorn cuadre.web.app:app --reload --port 8000

worker:
	PYTHONPATH=src python -m cuadre.jobs.worker

test:
	python -m pytest -q

bench:
	python bench/run_bench.py --max-false-positive 0.02

clean:
	rm -rf var/cuadre.db* var/blobs var/exports .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
