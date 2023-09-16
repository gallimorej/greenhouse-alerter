#!/usr/bin/python3

from flask import Flask
from google.cloud import secretmanager
from pysensorpush import PySensorPush
from ifttt_webhook import IftttWebhook
from datetime import datetime

import logging
import os
import pprint

app = Flask(__name__)

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

def get_latest_reading():
    user = get_secret('SENSORPUSH_USER')
    password = get_secret('SENSORPUSH_PASSWORD')

    if None in (user, password):
        print("ERROR! Must set SENSORPUSH_USER and SENSORPUSH_PASSWORD")
        raise SystemExit

    sensorpush = PySensorPush(user, password)
    greenhousesensorid = os.environ.get('GREENHOUSE_SENSOR_ID')
    
    if greenhousesensorid is None:
        print("ERROR! Must set GREENHOUSE_SENSOR_ID")
        raise SystemExit

    samplelimit = int(os.environ.get('SAMPLE_LIMIT', 20))
    sensordata = sensorpush.samples(limit=samplelimit)
    samples_for_sensor = sensordata.get('sensors', {}).get(greenhousesensorid, [])
    latest_reading = samples_for_sensor[0]

    return latest_reading

def is_greenhouse_too_hot():
    latest_reading = get_latest_reading()
    temptrigger = float(os.environ.get('TEMPERATURE_HIGH_TRIGGER', 100.0))
    if "temperature" in latest_reading:
        try:
            return float(latest_reading["temperature"]) > temptrigger
        except ValueError:
            print("Could not convert temperature to float.")
            return False
    else:
        print("Temperature key not found in latest_reading.")
        return False

def is_greenhouse_cool_enough():
    latest_reading = get_latest_reading()
    temptrigger = float(os.environ.get('TEMPERATURE_LOW_TRIGGER', 90.0))
    if "temperature" in latest_reading:
        try:
            return float(latest_reading["temperature"]) < temptrigger
        except ValueError:
            print("Could not convert temperature to float.")
            return False
    else:
        print("Temperature key not found in latest_reading.")
        return False
    
@app.route('/test-sensorpush-connection')
def test_sensorpush_connection():
    latest_reading = get_latest_reading()
    temptrigger = float(os.environ.get('TEMPERATURE_HIGH_TRIGGER', 100.0))

    if "temperature" in latest_reading:
        try:
            latest_temp = float(latest_reading["temperature"])

            return f"The most recent temperature in the greenhouse is {latest_temp}.\nLatest reading: {latest_reading}"
        except ValueError:
            return "Could not convert the temperature to a float."
    else:
        return "Temperature key not found in latest_reading."

def toggle_greenhouse_fans(event_name):
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
    ifttt.trigger(event_name)

def start_greenhouse_fans():
    toggle_greenhouse_fans('greenhouse_temp_over_limit')
    
def stop_greenhouse_fans():
    toggle_greenhouse_fans('greenhouse_temp_under_limit')

@app.route('/test-ifttt-connection')
def test_ifttt_connection():
    start_greenhouse_fans()
    
    return "The greenhouse fans should be on now."

@app.route('/check-greenhouse-temp')
def check_greenhouse_temp():
    # Figure out whether the temp is greater than the threshold
    # If it is, trigger the greenhouse fans
    
    if is_greenhouse_too_hot():
        start_greenhouse_fans()
    
    if is_greenhouse_cool_enough():
        stop_greenhouse_fans()
    
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return f"Checked greenhouse temperature at {current_time}"

@app.route('/')
def hello():
    return "Yo! This is the Greenhouse Alerter."
    
if __name__ == '__main__':
    app.run()
