"""
test_size_rejected.py — SIZE'ı ilan edip parametresini reddeden sunucu.

Yerel koşuda EMS SMTP'si her gönderimi şu yanıtla düşürüyordu:
  555 5.5.2 MAIL FROM/RCPT TO parameters not recognized or not implemented.
  Unknown Mail command argument : size=1355
smtplib, EHLO'da SIZE gören her sunucuya MAIL FROM'da ``size=N`` ekler; sunucu
SIZE'ı ilan edip parametreyi tanımıyordu. Gönderici artık SIZE'sız bir kez
yeniden dener ve o sunucuya sonraki gönderimlerde SIZE eklemez.
"""
from __future__ import annotations

import smtplib
import socket
import threading

import pytest

from sender import MailSender


class _SizeRejectingServer:
    """Gerçek soket üzerinde konuşan küçük SMTP sunucusu (TLS'siz)."""

    def __init__(self, reject_size: bool = True, reject_code: int = 555):
        self.reject_size, self.reject_code = reject_size, reject_code
        self.mail_cmds: list[str] = []
        self.delivered = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        f = conn.makefile("rb")
        send = lambda s: conn.sendall(s.encode() + b"\r\n")  # noqa: E731
        send("220 test ESMTP")
        try:
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.decode().strip()
                up = cmd.upper()
                if up.startswith("EHLO"):
                    send("250-test")
                    send("250-SIZE 10240000")
                    send("250 AUTH PLAIN LOGIN")
                elif up.startswith("AUTH"):
                    send("235 2.7.0 ok")
                elif up.startswith("MAIL FROM"):
                    self.mail_cmds.append(cmd)
                    if self.reject_size and "SIZE=" in up:
                        send(f"{self.reject_code} 5.5.2 MAIL FROM/RCPT TO parameters not "
                             f"recognized or not implemented.Unknown Mail command argument : "
                             f"{cmd.split()[-1]}")
                    else:
                        send("250 ok")
                elif up.startswith("RCPT"):
                    send("250 ok")
                elif up == "DATA":
                    send("354 go")
                    while f.readline() not in (b".\r\n", b""):
                        pass
                    self.delivered += 1
                    send("250 queued")
                elif up == "RSET":
                    send("250 ok")
                elif up == "QUIT":
                    send("221 bye")
                    return
                else:
                    send("502 unknown")
        finally:
            conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def server():
    srv = _SizeRejectingServer()
    yield srv
    srv.close()


def _sender(port: int) -> MailSender:
    return MailSender({"smtp_host": "127.0.0.1", "smtp_port": port, "smtp_use_tls": False,
                       "username": "u@ems.local", "password": "p",
                       "test_address": "u@ems.local"})


def test_size_rejection_retried_without_size(server, caplog):
    s = _sender(server.port)
    s.send_plain_text("alici@gmail.com", "konu", "gövde")
    assert server.delivered == 1
    assert "SIZE=" in server.mail_cmds[0].upper()
    assert "SIZE=" not in server.mail_cmds[1].upper()
    assert "SIZE'sız" in caplog.text


def test_later_sends_skip_size_up_front(server):
    s = _sender(server.port)
    s.send_plain_text("alici@gmail.com", "1", "gövde")
    s.send_plain_text("alici@gmail.com", "2", "gövde")
    assert server.delivered == 2
    assert len(server.mail_cmds) == 3, "ikinci gönderimde reddedilen deneme tekrarlanmamalı"


def test_501_size_rejection_also_retried():
    srv = _SizeRejectingServer(reject_code=501)
    try:
        _sender(srv.port).send_plain_text("alici@gmail.com", "konu", "gövde")
        assert srv.delivered == 1
    finally:
        srv.close()


def test_server_accepting_size_untouched():
    srv = _SizeRejectingServer(reject_size=False)
    try:
        _sender(srv.port).send_plain_text("alici@gmail.com", "konu", "gövde")
        assert srv.delivered == 1 and len(srv.mail_cmds) == 1
        assert "SIZE=" in srv.mail_cmds[0].upper()
    finally:
        srv.close()


def test_unrelated_sender_refusal_not_retried(monkeypatch):
    s = _sender(1)
    calls = []

    class Dead:
        esmtp_features = {"size": "100"}

        def sendmail(self, *a, **k):
            calls.append(a)
            raise smtplib.SMTPSenderRefused(553, b"5.7.1 Sender address rejected", "x")

        def rset(self):
            raise AssertionError("rset çağrılmamalı")

    with pytest.raises(smtplib.SMTPSenderRefused):
        s._sendmail(Dead(), ["a@b"], b"x")
    assert len(calls) == 1
