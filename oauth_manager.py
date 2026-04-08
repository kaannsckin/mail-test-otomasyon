"""
oauth_manager.py — Microsoft (MSAL) ve Google OAuth2 akışlarını yöneten modül.
Token alma, yenileme ve SMTP/IMAP için XOAUTH2 formatında auth string üretimi yapar.
"""
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import msal
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    # requirements.txt henüz yüklenmemiş olabilir (pip install bekliyor)
    msal = None
    Credentials = None

logger = logging.getLogger(__name__)

# Scopes
MS_SCOPES = ["https://outlook.office.com/SMTP.Send", "https://outlook.office.com/IMAP.Access.asUser.All", "offline_access"]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]

class OAuthManager:
    def __init__(self, config_dir: Path):
        self.token_path = config_dir / "tokens.json"
        self._tokens = self._load_tokens()

    def _load_tokens(self) -> Dict[str, Any]:
        if self.token_path.exists():
            try:
                with open(self.token_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Token yükleme hatası: {e}")
        return {}

    def _save_tokens(self):
        try:
            with open(self.token_path, "w") as f:
                json.dump(self._tokens, f, indent=2)
        except Exception as e:
            logger.error(f"Token kaydetme hatası: {e}")

    # ------------------------------------------------------------------ #
    #  Microsoft (MSAL) Akışı
    # ------------------------------------------------------------------ #
    def get_ms_auth_url(self, client_id: str, tenant_id: str = "common", redirect_uri: str = "") -> str:
        """Microsoft yetkilendirme URL'sini döndürür."""
        if not msal: return ""
        app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant_id}")
        return app.get_authorization_request_url(MS_SCOPES, redirect_uri=redirect_uri)

    def complete_ms_auth(self, client_id: str, code: str, redirect_uri: str, tenant_id: str = "common") -> bool:
        """Yetkilendirme kodunu token ile takas eder."""
        if not msal: return False
        app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant_id}")
        result = app.acquire_token_by_authorization_code(code, scopes=MS_SCOPES, redirect_uri=redirect_uri)
        
        if "access_token" in result:
            self._tokens["outlook"] = result
            self._save_tokens()
            return True
        else:
            logger.error(f"MS Auth Hatası: {result.get('error_description')}")
            return False

    def get_ms_token(self, client_id: str, tenant_id: str = "common") -> Optional[str]:
        """Geçerli bir Microsoft access token döner (gerekirse yeniler)."""
        if not msal: return None
        token_data = self._tokens.get("outlook")
        if not token_data: return None

        app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant_id}")
        accounts = app.get_accounts()
        
        # Cache'den dene
        result = app.acquire_token_silent(MS_SCOPES, account=accounts[0]) if accounts else None
        
        if not result and "refresh_token" in token_data:
            # Refresh token ile zorla
            result = app.acquire_token_by_refresh_token(token_data["refresh_token"], scopes=MS_SCOPES)

        if result and "access_token" in result:
            self._tokens["outlook"] = result
            self._save_tokens()
            return result["access_token"]
        
        return None

    # ------------------------------------------------------------------ #
    #  Google Akışı
    # ------------------------------------------------------------------ #
    def get_google_auth_url(self, client_id: str, client_secret: str, redirect_uri: str) -> str:
        """Google yetkilendirme URL'sini döndürür."""
        from google_auth_oauthlib.flow import Flow
        client_config = {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri]
            }
        }
        flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
        url, _ = flow.authorization_url(prompt='consent', access_type='offline')
        return url

    def complete_google_auth(self, client_id: str, client_secret: str, code: str, redirect_uri: str) -> bool:
        """Google kodunu takas eder."""
        from google_auth_oauthlib.flow import Flow
        client_config = {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
        flow.fetch_token(code=code)
        
        creds = flow.credentials
        self._tokens["gmail"] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes
        }
        self._save_tokens()
        return True

    def get_google_token(self) -> Optional[str]:
        """Geçerli bir Google access token döner."""
        if not Credentials: return None
        token_data = self._tokens.get("gmail")
        if not token_data: return None

        creds = Credentials(**token_data)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._tokens["gmail"] = {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": creds.scopes
            }
            self._save_tokens()
        
        return creds.token

    # ------------------------------------------------------------------ #
    #  XOAUTH2 Generator
    # ------------------------------------------------------------------ #
    @staticmethod
    def generate_xoauth2_string(user: str, token: str) -> str:
        """base64(user=USER^Aauth=Bearer TOKEN^A^A) formatında string üretir."""
        auth_string = f"user={user}\1auth=Bearer {token}\1\1"
        return base64.b64encode(auth_string.encode()).decode()

def get_oauth_manager():
    """App dir'den gelen konfigürasyon ile bir manager döner."""
    config_dir_str = os.environ.get("MAIL_AUTO_CONFIG_DIR")
    config_dir = Path(config_dir_str or ".").resolve()
    return OAuthManager(config_dir)
