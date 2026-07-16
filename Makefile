.PHONY: install test

install:
	pip3 install -e ".[test]"

test:
	pytest
