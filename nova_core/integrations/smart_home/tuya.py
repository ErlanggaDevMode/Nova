import hmac
import hashlib
import time
import requests
import logging
import os
import json

logger = logging.getLogger("nova.tuya")

class TuyaClient:
    def __init__(self):
        self.client_id = os.getenv("TUYA_CLIENT_ID", "")
        self.client_secret = os.getenv("TUYA_CLIENT_SECRET", "")
        self.region = os.getenv("TUYA_REGION", "us").lower()
        
        regions = {
            "us": "https://openapi.tuyaus.com",
            "eu": "https://openapi.tuyaeu.com",
            "in": "https://openapi.tuyain.com",
            "cn": "https://openapi.tuyacn.com"
        }
        self.base_url = regions.get(self.region, "https://openapi.tuyaus.com")
        self.access_token = None

    def _get_signature(self, client_id, secret, t, token="", string_to_sign=""):
        message = client_id + token + str(t) + string_to_sign
        sign = hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest().upper()
        return sign

    def get_token(self) -> bool:
        if not self.client_id or not self.client_secret:
            logger.warning("Tuya credentials missing. Running stub IoT mode.")
            return False

        t = int(time.time() * 1000)
        sign = self._get_signature(self.client_id, self.client_secret, t)
        
        headers = {
            "client_id": self.client_id,
            "sign": sign,
            "t": str(t),
            "sign_method": "HMAC-SHA256"
        }
        
        try:
            url = f"{self.base_url}/v1.0/token?grant_type=1"
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    self.access_token = data["result"]["access_token"]
                    return True
            return False
        except Exception as e:
            logger.error(f"Tuya token request error: {e}")
            return False

    def send_device_command(self, device_id: str, command_name: str, value: bool) -> dict:
        """
        Sends commands to a Tuya smart home device.
        Falls back to a successful stub mock response if credentials are not configured.
        """
        if not self.client_id or not self.client_secret:
            logger.info(f"[TUYA STUB] Sent command '{command_name}' with value '{value}' to device '{device_id}'")
            return {"success": True, "stub": True, "device_id": device_id, "command": command_name, "value": value}

        if not self.access_token and not self.get_token():
            return {"success": False, "error": "Failed to authenticate with Tuya API"}

        t = int(time.time() * 1000)
        url_path = f"/v1.0/devices/{device_id}/commands"
        
        body = {
            "commands": [{"code": command_name, "value": value}]
        }
        body_str = json.dumps(body)
        
        # Calculate SHA256 of request body
        m = hashlib.sha256()
        m.update(body_str.encode("utf-8"))
        content_sha256 = m.hexdigest()

        # Build StringToSign
        string_to_sign = f"POST\n{content_sha256}\n\n{url_path}"
        sign = self._get_signature(self.client_id, self.client_secret, t, token=self.access_token, string_to_sign=string_to_sign)

        headers = {
            "client_id": self.client_id,
            "access_token": self.access_token,
            "sign": sign,
            "t": str(t),
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json"
        }

        try:
            res = requests.post(self.base_url + url_path, headers=headers, data=body_str, timeout=5)
            if res.status_code == 200:
                return res.json()
            return {"success": False, "status_code": res.status_code, "text": res.text}
        except Exception as e:
            return {"success": False, "error": str(e)}
