# Greenhouse Alerter

Greenhouse Alerter keeps a greenhouse cool. It reads a SensorPush sensor and switches the fans on and off through IFTTT Webhooks, based on temperature thresholds, active hours and an hourly air-circulation cycle.

## Architecture

```
Cloud Scheduler "greenhouse-tick" (every minute, OIDC as greenhouse-scheduler)
      │  POST /tasks/tick
      ▼
greenhouse-worker   Cloud Run, private; only the scheduler may invoke it; one instance, one request at a time
      │ reads/writes                         │ the only caller of IFTTT
      ▼                                      ▼
Firestore "greenhouse"                    SensorPush API, IFTTT Webhooks
  config/settings   controller/state   controller/status   config/override   events/*
      ▲
greenhouse-admin    Cloud Run behind IAP; status, kill switch, reconcile, sensor linking
```

- **Decision rules:** `src/greenhouse/controller.py`, a pure function covering thresholds with hysteresis, active hours, the hourly cycle, stale readings, the startup baseline and manual overrides.
- **Command safety:** `actuation.py` handles the lease, generations, the pre-dispatch guard and reconcile. A tick that has lost its lease or outlived its budget can't send, and flipping the kill switch stops even a tick already in flight.
- **Persistence:** `store.py` runs every transition in one transaction, on Firestore in production and in memory in tests.
- **Failure handling:** a sensor failure leaves the fans as they are and raises an email alert (`terraform/monitoring.tf`).
- **Observability:** every tick writes one structured log line. Log-based metrics built on those lines drive the alerts and the **Greenhouse** Cloud Monitoring dashboard (`terraform/dashboard.tf`; `terraform output dashboard_url`), which charts temperature, successful and stale ticks, IFTTT failures, worker errors, latency and open alerts.
- **Retention:** audit events expire after 90 days through a Firestore TTL policy (set up in the bootstrap).

New deployments start in **shadow mode**: decisions are computed and logged, but no commands are sent until fan control is enabled on the admin page.

## Development

Requires Python 3.12+.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m pytest
```

On Apple Silicon, if the terminal runs under Rosetta, create and run the venv with `arch -arm64` so native wheels install.

The worker tests also run against the Firestore emulator when `FIRESTORE_EMULATOR_HOST` is set (CI does this):

```sh
gcloud emulators firestore start --host-port=127.0.0.1:8085 &
FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 python -m pytest src/tests/test_worker.py
```

### Updating dependencies

Edit `requirements.in` or `requirements-dev.in`, then regenerate the pinned, hashed files with pip-tools:

```sh
pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --allow-unsafe -o requirements-dev.txt requirements-dev.in
```

## Infrastructure and deploys

- `terraform/bootstrap/` holds the one-time privileged setup, applied by a project owner (see its README).
- `terraform/` holds the services, the scheduler job and the alerts. CI applies it on every push to `main`: it builds the image, pushes it, and deploys it by digest. It authenticates keyless, through Workload Identity Federation. The deploy job stays skipped until the bootstrap's repository variables are set.

## Runbooks

### Go live
1. Deploy. Fan control starts disabled, in shadow mode.
2. Add the sensor to the SensorPush account in the SensorPush app, within range of a gateway.
3. Admin → **Sensors** → **Link…**, check the live reading, then **Confirm**.
4. Watch the shadow decisions on the Status page for at least a day. Check that both IFTTT applets set the switch on or off (rather than toggling it).
5. **Enable fan control**, then force-run the job (`gcloud scheduler jobs run greenhouse-tick --location us-east1`) and confirm the event and the fans.

### Rollback: stop automation and run the fans by hand
1. Pause the job: `gcloud scheduler jobs pause greenhouse-tick --location us-east1` (and set `tick_paused = true` in Terraform so the next deploy doesn't resume it).
2. Admin → **Disable fan control**. Any tick already in flight sees this at its pre-dispatch guard.
3. Wait 90 seconds (request timeout plus lease lifetime).
4. Admin → **Reconcile**. This invalidates in-flight work and marks the fan state uncertain.
5. Run the fans by hand in IFTTT or the switch app.

### Resume
1. Deploy the fix, or redeploy the previous image digest. Fan control is still disabled.
2. Force-run the job and check the shadow decision on the Status page.
3. Confirm the "Fan state uncertain" banner is showing. While it's set, the first eligible tick resends the desired state.
4. **Enable fan control**, resume the job, and confirm the first command and the fans' physical state.

### Swap a sensor
Add the new sensor in the SensorPush app, then Admin → **Sensors** → **Link…** → **Confirm**. No deploy is needed.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details
