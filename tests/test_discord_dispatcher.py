import unittest
from unittest.mock import patch, MagicMock
from src.alerts.discord_dispatcher import send_to_discord

class TestDiscordDispatcher(unittest.TestCase):

    @patch("src.alerts.discord_dispatcher.DISCORD_WEBHOOK_URL", None)
    def test_send_to_discord_no_url(self):
        # Should return False when webhook URL is not set
        res = send_to_discord("test message")
        self.assertFalse(res)

    @patch("src.alerts.discord_dispatcher.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/mock")
    @patch("urllib.request.urlopen")
    def test_send_to_discord_success(self, mock_urlopen):
        # Mock successful HTTP response
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_urlopen.return_value = mock_resp

        res = send_to_discord("hello discord")
        self.assertTrue(res)

        # Verify urllib.request.urlopen called with correct parameters
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.headers.get("Content-type"), "application/json")
        self.assertEqual(req.data, b'{"content": "hello discord"}')

    @patch("src.alerts.discord_dispatcher.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/mock")
    @patch("urllib.request.urlopen")
    def test_send_to_discord_failure(self, mock_urlopen):
        # Mock API error
        mock_urlopen.side_effect = Exception("API connection timed out")

        res = send_to_discord("hello failure")
        self.assertFalse(res)
