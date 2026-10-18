import traceback

import pytest
import requests
import responses

from greenhouse.fans import IftttClient, IftttError

KEY = "secret-webhook-key"
URL = f"https://maker.ifttt.com/trigger/start_greenhouse_fans/with/key/{KEY}"


@responses.activate
def test_trigger_posts_event():
    responses.post(URL, body="Congratulations!")
    assert IftttClient(KEY).trigger("start_greenhouse_fans") == 200
    assert len(responses.calls) == 1


@pytest.mark.parametrize(
    "mock_kwargs, status",
    [
        ({"status": 401}, 401),
        ({"status": 500}, 500),
        ({"body": requests.Timeout(f"timed out on {URL}")}, None),
        ({"body": requests.ConnectionError(f"failed to reach {URL}")}, None),
    ],
)
@responses.activate
def test_failures_never_leak_the_key(mock_kwargs, status):
    responses.post(URL, **mock_kwargs)
    with pytest.raises(IftttError) as excinfo:
        IftttClient(KEY).trigger("start_greenhouse_fans")
    assert excinfo.value.status == status
    # The whole traceback, including any chained exceptions, is what a logger would print.
    assert KEY not in "".join(traceback.format_exception(excinfo.value))


@responses.activate
def test_trigger_is_a_single_attempt():
    responses.post(URL, status=503)
    with pytest.raises(IftttError):
        IftttClient(KEY).trigger("start_greenhouse_fans")
    assert len(responses.calls) == 1
