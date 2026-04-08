"""
notifications.py — Mail otomasyon hataları için bildirim kanalları.
"""
import requests
import logging
import json

logger = logging.getLogger(__name__)

def send_alert(summary: str, details: str, config: dict):
    """
    Belirlenen kanallar üzerinden alarm gönderir.
    """
    auto_cfg = config.get("automation", {})
    notif_cfg = auto_cfg.get("notifications", {})
    
    if not auto_cfg.get("enabled", False):
        return

    # 1. Slack / Teams Webhook
    webhook_url = notif_cfg.get("webhook_url")
    if webhook_url:
        _send_webhook(webhook_url, summary, details)
    
    # Gelecekte SMTP bildirim desteği buraya eklenebilir.

def _send_webhook(url: str, summary: str, details: str):
    try:
        # Standart Slack formatı (Teams de genellikle uyumludur veya basit text kabul eder)
        payload = {
            "text": f"🚨 *Mail Otomasyon Alarmı*",
            "attachments": [
                {
                    "color": "#ef4444",
                    "title": summary,
                    "text": details,
                    "footer": "Mail Otomasyon Botu",
                    "ts": None
                }
            ]
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code >= 400:
            logger.error(f"Webhook hatası ({resp.status_code}): {resp.text}")
        else:
            logger.info("Bildirim webhook üzerinden başarıyla gönderildi.")
    except Exception as e:
        logger.error(f"Webhook gönderim hatası: {e}")
