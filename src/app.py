#!/usr/bin/python3

from flask import Flask
from google.cloud import secretmanager
from pysensorpush import PySensorPush
from ifttt_webhook import IftttWebhook

import logging
import os
import pprint

app = Flask(__name__)

last_reading = None

def get_secret(secret_id):
    # Initialize the Secret Manager client
    client = secretmanager.SecretManagerServiceClient()

    # Define the name of the secret and the version
    project_id = os.environ.get('PROJECT_ID')
    if project_id is None:
        print(
            "ERROR! No PROJECT_ID set"
        )
    
    version_id = "latest"  # or a specific version number
    
    # Build the resource name of the secret
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"

    # Access the secret
    response = client.access_secret_version(request={"name": name})
    payload = response.payload.data.decode("UTF-8")

    return payload

def get_sensorpush_user():
    # Initialize the Secret Manager client
    client = secretmanager.SecretManagerServiceClient()

    # Define the name of the secret and the version
    project_id = os.environ.get('PROJECT_ID')
    if project_id is None:
        print(
            "ERROR! No PROJECT_ID set"
        )
    
    secret_id = "SENSORPUSH_USER"
    version_id = "latest"  # or a specific version number
    
    # Build the resource name of the secret
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"

    # Access the secret
    response = client.access_secret_version(request={"name": name})
    payload = response.payload.data.decode("UTF-8")

    return payload

def get_sensorpush_pwd():
    # Initialize the Secret Manager client
    client = secretmanager.SecretManagerServiceClient()

    # Define the name of the secret and the version
    project_id = os.environ.get('PROJECT_ID')
    if project_id is None:
        print(
            "ERROR! No PROJECT_ID set"
        )

    secret_id = "SENSORPUSH_PASSWORD"
    version_id = "latest"  # or a specific version number
    
    # Build the resource name of the secret
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"

    # Access the secret
    response = client.access_secret_version(request={"name": name})
    payload = response.payload.data.decode("UTF-8")

    return payload

@app.route('/test-sensorpush-connection')
def test_sensorpush_connection():
    user = get_secret('SENSORPUSH_USER')
    password = get_secret('SENSORPUSH_PASSWORD')

    if None in (user, password):
        print(
            "ERROR! Must set SENSORPUSH_USER and SENSORPUSH_PASSWORD"
        )
        raise SystemExit

    # setup_logger()
    pp = pprint.PrettyPrinter(indent=2)

    sensorpush = PySensorPush(user, password)

    # print("--Gateways--")
    # pp.pprint(sensorpush.gateways)

    # print("\n--Sensors--")
    # pp.pprint(sensorpush.sensors)

    # print("\n--Samples--")
    greenhousesensorid = os.environ.get('GREENHOUSE_SENSOR_ID')
    if greenhousesensorid is None:
        print(
            "ERROR! Must set GREENHOUSE_SENSOR_ID"
        )
        raise SystemExit

    samplelimit = int(os.environ.get('SAMPLE_LIMIT'))
    if samplelimit is None:
        samplelimit = 20
    
    # pp.pprint(sensorpush.samples({'sensors': [greenhouseSensor]}))
    sensordata=sensorpush.samples(limit=samplelimit)
    # pp.pprint(sensorpush.samples(limit=20))

    # Getting the samples for the specified sensor
    samples_for_sensor = sensordata.get('sensors', {}).get(greenhousesensorid, [])

    # Get the latest entry in the list
    latest_reading = samples_for_sensor[0]

    global last_reading

    if latest_reading == last_reading:
        return f"No new readings.\nLatest reading: {latest_reading}\nAll readings: {samples_for_sensor}"
    else:
        last_reading = latest_reading
        
        temptrigger = float(os.environ.get('TEMPERATURE_TRIGGER'))
        if temptrigger is None:
            temptrigger = 110

        # Check if the temperature is greater than the trigger
        if float(latest_reading["temperature"]) > temptrigger:
            return f"The most recent temperature in the greenhouse is greater than {temptrigger}.\nLatest entry: {latest_reading}\nAll readings: {samples_for_sensor}"
        else:
            return f"The most recent temperature in the greenhouse is not greater than {temptrigger}.\nLatest entry: {latest_reading}\nAll readings: {samples_for_sensor}"

@app.route('/test-ifttt-connection')
def test_ifttt_connection():
    # IFTTT Webhook key, available under "Documentation"
    # at  https://ifttt.com/maker_webhooks/.
    ifttt_key = get_secret('IFTTT_KEY')
    if ifttt_key is None:
        print(
            "ERROR! Must set IFTTT_KEY"
        )
        raise SystemExit
    
    # Create an instance of the IftttWebhook class,
    # passing the IFTTT Webhook key as parameter.
    ifttt = IftttWebhook(ifttt_key)

    # Trigger the IFTTT event defined by event_name with the content
    # defined by the value1, value2 and value3 parameters.
    # ifttt.trigger('greenhouse_temp_triggered', value1='value1', value2='value2', value3='value3')
    ifttt.trigger('greenhouse_temp_triggered')
    
    return "The greenhouse fans should be on now."

@app.route('/')
def hello():
    return "Hello World!"
    
if __name__ == '__main__':
    app.run()
