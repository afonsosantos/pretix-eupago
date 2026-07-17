.PHONY: install test bump-version

install:
	pip3 install -e ".[test]"

test:
	pytest

bump-version:
	@version="$(filter-out $@,$(MAKECMDGOALS))"; \
	if [ -z "$$version" ]; then \
		echo "Usage: make bump-version <version>" >&2; \
		exit 1; \
	fi; \
	sed -i.bak -E "s/^version = \".*\"/version = \"$$version\"/" pyproject.toml && rm pyproject.toml.bak; \
	sed -i.bak -E "s/^__version__ = \".*\"/__version__ = \"$$version\"/" pretix_eupago/__init__.py && rm pretix_eupago/__init__.py.bak; \
	echo "Bumped version to $$version in pyproject.toml and pretix_eupago/__init__.py"

# Swallow the version argument so make doesn't try to build it as a target.
%:
	@:
