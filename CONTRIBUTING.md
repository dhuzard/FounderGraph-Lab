# Contributing to FounderGraph-Lab

Thank you for your interest in contributing! This document covers how to report issues, propose changes, and submit pull requests.

## Getting started

1. Fork the repository and clone your fork:
   ```bash
   git clone https://github.com/<your-username>/FounderGraph-Lab.git
   cd FounderGraph-Lab
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Copy the example env file:
   ```bash
   cp .env.example .env
   ```

4. Run the test suite to confirm everything is working:
   ```bash
   python -m pytest
   ```

## Reporting bugs

Open a [GitHub issue](https://github.com/dhuzard/FounderGraph-Lab/issues) with:

- A clear, descriptive title
- Steps to reproduce
- Expected behaviour vs. actual behaviour
- Python version and OS

## Suggesting features

Open an issue with the `enhancement` label. Describe the use case, not just the feature.

## Submitting pull requests

1. Create a branch from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```

2. Make your changes. Keep commits focused — one logical change per commit.

3. Ensure lint and tests pass:
   ```bash
   python -m ruff check .
   python -m pytest
   ```

4. Open a pull request against `main`. The description should explain *why* the change is needed, not just *what* it does.

## Code style

- **Formatter/linter:** [Ruff](https://docs.astral.sh/ruff/) — run `python -m ruff format .` before committing.
- **Type hints:** use them on all public function signatures.
- **Tests:** add or update tests in `tests/` for any changed or new behaviour.
- **No secrets:** never commit credentials, API keys, or service account files.

## Ontology changes

Changes to `app/ontology/startup_ontology.yaml` affect the extraction prompt, the Neo4j allowlists, and the ontology validator simultaneously. Any PR that modifies the ontology should include a brief rationale for each added/removed type or predicate.

## Licence

By contributing, you agree that your contributions will be licenced under the [MIT Licence](LICENSE).
