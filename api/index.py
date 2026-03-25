"""
api/index.py — Vercel serverless entry point
Flask uygulamasını Vercel'in @vercel/python builder'ına bağlar.
"""
import sys
import os

# Repo kökünü Python path'e ekle (modüllerin bulunması için)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as handler  # noqa: F401  (Vercel 'handler' adını arar)
