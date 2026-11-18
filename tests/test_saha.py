"""
test_saha.py — Saha denemesinde çıkan ayar hataları: şifrede kontrol karakteri,
şemasız Z-Push adresi, kurulum betiğinin şifre düzeltme kipi.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import main as main_mod  # noqa: E402
import saha_kurulum  # noqa: E402
from activesync import EasClient  # noqa: E402
from mail_transport import control_chars, credential_problem, normalize_eas_url  # noqa: E402


class TestCredentialProblem:

    @pytest.mark.parametrize("password", ["gizli\x1b[D123", "a\tb", "x\x7f", "satır\nsonu"])
    def test_control_characters_are_caught(self, password):
        problem = credential_problem({"password": password})
        assert problem and "görünmez" in problem and "--sifre" in problem

    @pytest.mark.parametrize("password", ["Şifre!2026 ğüş", "p@ss word", "", None])
    def test_normal_passwords_pass(self, password):
        assert credential_problem({"password": password, "username": "a@b"}) is None

    def test_control_chars_listing(self):
        assert control_chars("a\x1b[Db\x1b") == ["\\x1b"]

    def test_run_refuses_to_connect_with_broken_password(self, full_config):
        full_config["ems"]["password"] = "gizli\x1b[A"
        with pytest.raises(ValueError, match="EMS: Şifre görünmez kontrol karakteri"):
            main_mod.get_server_config(full_config, "EMS")


class TestEasUrl:

    @pytest.mark.parametrize("raw, expected", [
        ("testepostaconf.meb.gov.tr", "https://testepostaconf.meb.gov.tr/Microsoft-Server-ActiveSync"),
        ("https://posta.test/", "https://posta.test/Microsoft-Server-ActiveSync"),
        ("HTTPS://posta.test/ozel/yol", "https://posta.test/ozel/yol"),
        ("posta.test:8443", "https://posta.test:8443/Microsoft-Server-ActiveSync"),
        ("  ", ""),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_eas_url(raw) == expected

    def test_client_accepts_bare_host(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, headers={"MS-ASProtocolVersions": "14.0"})
        client = EasClient({"eas_url": "zpush.test", "username": "u", "password": "p"},
                           transport=httpx.MockTransport(handler))
        client.options()
        assert seen == ["https://zpush.test/Microsoft-Server-ActiveSync"]


class TestKurulum:

    def test_secret_prompt_rejects_control_characters(self, capsys):
        answers = iter(["gizli\x1b[D123", "gizli123"])
        with patch.object(saha_kurulum.getpass, "getpass", side_effect=lambda _p: next(answers)):
            assert saha_kurulum.ask_secret("Şifre") == "gizli123"
        assert "görünmez karakter" in capsys.readouterr().out

    def test_fix_passwords_only_touches_secrets(self, tmp_path):
        cfg = {"ems": {"username": "test@meb", "password": "eski\x1b[D", "smtp_host": "h",
                       "eas_url": "zpush.test"},
               "gmail": {"username": "x@gmail.com", "password": ""}}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        secrets = iter(["yeni-şifre"])
        with patch.object(saha_kurulum.getpass, "getpass", side_effect=lambda _p: next(secrets)), \
             patch("builtins.input", side_effect=lambda _p="": "h"):      # 2FA yok
            saha_kurulum.fix_passwords(path)
        out = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert out["ems"]["password"] == "yeni-şifre" and out["ems"]["smtp_host"] == "h"
        assert out["ems"]["auth_method"] == "password"
        assert out["ems"]["eas_url"] == "https://zpush.test/Microsoft-Server-ActiveSync"
        assert out["gmail"]["password"] == ""                 # tanımsız Gmail sorulmadı

    def test_mfa_with_totp_secret(self):
        cfg = {}
        with patch("builtins.input", side_effect=lambda _p="": "e"), \
             patch.object(saha_kurulum.getpass, "getpass", side_effect=lambda _p: "JBSW Y3DP"):
            saha_kurulum.ask_mfa(cfg)
        assert cfg == {"auth_method": "totp_password", "mfa_method": "totp", "totp_secret": "JBSWY3DP"}
