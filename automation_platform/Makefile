.PHONY: install demo run test lint clean

PYTHON ?= python3
ROOT   := $(shell cd .. && pwd)
DB     ?= $(ROOT)/var/platform.sqlite3

install:
	$(PYTHON) -m pip install -r requirements.txt

demo:
	cd $(ROOT) && $(PYTHON) -m automation_platform.cli demo --database $(DB) --force

run:
	cd $(ROOT) && PLATFORM_DB=$(DB) $(PYTHON) -m automation_platform.cli serve --port 4000

test:
	cd $(ROOT) && $(PYTHON) -m unittest discover -s automation_platform/tests -t .

health:
	cd $(ROOT) && PLATFORM_DB=$(DB) $(PYTHON) -m automation_platform.cli health

clean:
	rm -f $(DB) $(DB)-wal $(DB)-shm
	rm -rf $(ROOT)/var/exports
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
