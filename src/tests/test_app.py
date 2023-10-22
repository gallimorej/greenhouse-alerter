import unittest
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

from flask_testing import TestCase

from app import app, toggle_greenhouse_fans


class TestFlaskRoutes(TestCase):
    def create_app(self):
        return app

    @patch('app.get_secret')  # Replace the path with your actual import
    @patch('app.PySensorPush')  # Replace the path with your actual import
    def test_test_sensorpush_connection(self, MockPySensorPush, MockGetSecret):
        MockGetSecret.return_value = "some_value"
        mock_sensor = Mock()
        mock_sensor.samples.return_value = {'sensors': {'some_id': [{'temperature': '70.0'}]}}
        MockPySensorPush.return_value = mock_sensor

        with patch.dict('os.environ', {'TEMPERATURE_HIGH_TRIGGER': '75', 'GREENHOUSE_SENSOR_ID': 'some_id', 'SAMPLE_LIMIT': '20'}):
            response = self.client.get('/test-sensorpush-connection')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"The most recent temperature in the greenhouse is 70.0", response.data)

    @patch('app.get_secret')
    @patch('app.IftttWebhook')
    def test_test_ifttt_connection(self, MockIftttWebhook, MockGetSecret):
        MockGetSecret.return_value = "some_key"
        mock_ifttt = Mock()
        MockIftttWebhook.return_value = mock_ifttt

        response = self.client.get('/test-ifttt-connection')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"The greenhouse fans should be on now.", response.data)
        
    @patch('app.is_greenhouse_too_hot', return_value=True)
    @patch('app.is_greenhouse_cool_enough', return_value=False)
    @patch('app.start_greenhouse_fans')
    def test_check_temp_too_hot(self, mock_start_fans, mock_is_cool_enough, mock_is_too_hot):
        response = self.client.get('/check-greenhouse-temp')
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Checked greenhouse temperature at {current_time}", str(response.data))
        mock_start_fans.assert_called_once()
    
    @patch('app.is_greenhouse_too_hot', return_value=False)
    @patch('app.is_greenhouse_cool_enough', return_value=True)
    @patch('app.stop_greenhouse_fans')
    def test_check_temp_cool_enough(self, mock_stop_fans, mock_is_cool_enough, mock_is_too_hot):
        response = self.client.get('/check-greenhouse-temp')
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Checked greenhouse temperature at {current_time}", str(response.data))
        mock_stop_fans.assert_called_once()
    
    @patch('app.get_secret')
    @patch('app.IftttWebhook')
    def test_toggle_greenhouse_fans_on(self, MockIftttWebhook, MockGetSecret):
        # Set up mock objects
        mock_ifttt = MagicMock()
        MockIftttWebhook.return_value = mock_ifttt
        MockGetSecret.return_value = 'some_key'

        # Call the function
        toggle_greenhouse_fans('greenhouse_temp_over_limit')

        # Verify that the IftttWebhook constructor was called with the correct key
        MockIftttWebhook.assert_called_once_with('some_key')

        # Verify that the IftttWebhook.trigger method was called with the correct event name
        mock_ifttt.trigger.assert_called_once_with('greenhouse_temp_over_limit')


    @patch('app.is_greenhouse_too_hot', return_value=False)
    @patch('app.is_greenhouse_cool_enough', return_value=False)
    def test_check_temp_not_hot_enough(self, mock_is_cool_enough, mock_is_too_hot):
        response = self.client.get('/check-greenhouse-temp')
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Checked greenhouse temperature at {current_time}", str(response.data))
        self.assertNotIn(b"Starting greenhouse fans", response.data)
        self.assertNotIn(b"Stopping greenhouse fans", response.data)

if __name__ == '__main__':
    unittest.main()
