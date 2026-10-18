# Greenhouse Alerter

Greenhouse Alerter keeps a greenhouse cool. It reads a SensorPush sensor and switches the fans on and off through IFTTT Webhooks, based on temperature thresholds, active hours and an hourly air-circulation cycle.

The project is moving from App Engine to Cloud Run. This branch holds the core library: the decision rules (`src/greenhouse/controller.py`), the SensorPush client (`sensors.py`), the IFTTT client (`fans.py`) and secret loading (`config.py`). The Cloud Run worker and admin services come next.

## Development

Requires Python 3.12+.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
```

On Apple Silicon, if the terminal runs under Rosetta, create and run the venv with `arch -arm64` so native wheels install.

### Running the tests

```sh
python -m pytest
```

### Updating dependencies

Edit `requirements.in` or `requirements-dev.in`, then regenerate the pinned, hashed files with pip-tools:

```sh
pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements-dev.txt requirements-dev.in
```

## Infrastructure

- `terraform/bootstrap/`: one-time privileged setup, applied by a project owner (see its README).
- `terraform/`: the main configuration, with remote state in the bootstrap bucket.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details
