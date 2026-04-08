"""
test_mail_addresses.py — resolve_mail_address() fonksiyonu unit testleri.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from main import resolve_mail_address


# ── Yeni format (mail_addresses) ───────────────────────────────────

def test_resolve_new_format_ems():
    config = {
        "mail_addresses": {
            "ems": {"username": "testuser", "domain": "meb.gov.tr", "address": "testuser@meb.gov.tr"}
        }
    }
    assert resolve_mail_address(config, "ems") == "testuser@meb.gov.tr"


def test_resolve_new_format_gmail():
    config = {
        "mail_addresses": {
            "gmail": {"username": "user", "domain": "gmail.com", "address": "user@gmail.com"}
        }
    }
    assert resolve_mail_address(config, "gmail") == "user@gmail.com"


def test_resolve_new_format_outlook():
    config = {
        "mail_addresses": {
            "outlook": {"username": "ali", "domain": "hotmail.com", "address": "ali@hotmail.com"}
        }
    }
    assert resolve_mail_address(config, "outlook") == "ali@hotmail.com"


def test_resolve_case_insensitive():
    """server_name büyük harf olarak gelse de çalışmalı."""
    config = {
        "mail_addresses": {
            "ems": {"username": "user", "domain": "meb.gov.tr", "address": "user@meb.gov.tr"}
        }
    }
    assert resolve_mail_address(config, "EMS") == "user@meb.gov.tr"
    assert resolve_mail_address(config, "Ems") == "user@meb.gov.tr"


# ── Legacy fallback (test_address) ─────────────────────────────────

def test_resolve_legacy_test_address():
    """mail_addresses yoksa eski test_address alanına fallback etmeli."""
    config = {
        "ems": {"test_address": "eski@meb.gov.tr", "smtp_host": "ems.local"}
    }
    assert resolve_mail_address(config, "ems") == "eski@meb.gov.tr"


def test_resolve_legacy_gmail():
    config = {
        "gmail": {"test_address": "eski@gmail.com"}
    }
    assert resolve_mail_address(config, "gmail") == "eski@gmail.com"


def test_resolve_prefers_new_format_over_legacy():
    """mail_addresses varsa eski test_address'e bakmamalı."""
    config = {
        "mail_addresses": {
            "ems": {"address": "yeni@meb.gov.tr"}
        },
        "ems": {"test_address": "eski@meb.gov.tr"},
    }
    assert resolve_mail_address(config, "ems") == "yeni@meb.gov.tr"


# ── Eksik / boş senaryolar ─────────────────────────────────────────

def test_resolve_empty_config():
    assert resolve_mail_address({}, "ems") == ""


def test_resolve_missing_server():
    config = {"mail_addresses": {"gmail": {"address": "a@gmail.com"}}}
    assert resolve_mail_address(config, "outlook") == ""


def test_resolve_empty_address():
    config = {"mail_addresses": {"ems": {"address": ""}}}
    assert resolve_mail_address(config, "ems") == ""


def test_resolve_no_address_key():
    """mail_addresses'de 'address' key'i yoksa boş döner."""
    config = {"mail_addresses": {"ems": {"username": "user", "domain": "meb.gov.tr"}}}
    assert resolve_mail_address(config, "ems") == ""
