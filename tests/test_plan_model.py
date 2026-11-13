"""
test_plan_model.py — Test planı (yollar · istemciler · kademeler · etki), POP3 kanalı
ve istemci başına manuel doğrulama.
"""

import csv
import email
import poplib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import main as main_mod  # noqa: E402
import manual_verify  # noqa: E402
from receiver import MailReceiver, Pop3Receiver, receiver_for  # noqa: E402
from reporter import generate_csv_results  # noqa: E402
from sender import MailSender  # noqa: E402
from test_catalog_scenarios import Loopback, _drop_headers, _server_cfg  # noqa: E402
from plan_model import PlanError, load_plan, parse_plan  # noqa: E402
from verifier import apply_channel_checks, channel_checks, fingerprint  # noqa: E402

DEFAULT_PLAN = PROJECT_ROOT / "test_plan.yaml"


@pytest.fixture
def plan():
    return load_plan(DEFAULT_PLAN)


# ═══════════════════════════════════════════════════════════════════
#  Plan modeli
# ═══════════════════════════════════════════════════════════════════

class TestPlanModel:

    def test_default_plan_loads(self, plan):
        assert set(plan.clients) == {"outlook-masaustu", "android-gmail", "ipad-mail"}
        assert plan.clients["outlook-masaustu"].critical is True
        assert len(plan.routes) == 5

    def test_tiers_are_cumulative(self, plan):
        duman, regresyon, tam = (plan.scenarios_for(t) for t in ("duman", "regresyon", "tam"))
        assert set(duman) < set(regresyon) < set(tam)
        assert len(tam) == 26                       # tüm senaryolar tam kademede
        assert len(set(tam)) == len(tam)

    def test_tier_limits_routes_and_scenarios(self, plan):
        sizes = {t: (len(c), sum(len(x.scenarios) for x in c))
                 for t in ("duman", "regresyon", "tam")
                 for c in [plan.combinations(t)]}
        assert sizes == {"duman": (2, 10), "regresyon": (4, 76), "tam": (5, 130)}

    def test_message_sent_once_per_route_not_per_client(self, plan):
        # EMS'te üç istemci var ama EMS'e giden yol tek kombinasyondur.
        ems_routes = [c for c in plan.combinations("tam") if c.receiver_server == "ems"]
        assert len(ems_routes) == 3
        assert all(len(c.clients) == 3 for c in ems_routes)

    def test_channels_follow_client_protocols(self, plan):
        by_label = {c.label: c for c in plan.combinations("tam")}
        # Outlook masaüstü POP3, iPad Z-Push kullandığı için EMS kutusu üç kanaldan okunur.
        assert by_label["EMS → EMS (kurum içi)"].receive_channels == ["imap", "pop3", "activesync"]
        assert by_label["EMS → Gmail (dışarıya)"].receive_channels == ["imap"]
        assert by_label["EMS → Gmail (dışarıya)"].clients == []

    def test_impact_server_selects_routes_touching_it(self, plan):
        assert [c.label for c in plan.combinations("tam", "gmail")] == [
            "Gmail → EMS (dışarıdan)", "EMS → Gmail (dışarıya)"]
        assert len(plan.combinations("tam", "ems")) == 5

    def test_impact_client_selects_inbound_routes_and_visual_scenarios(self, plan):
        combos = plan.combinations("tam", "ipad-mail")
        assert [c.receiver_server for c in combos] == ["ems", "ems", "ems"]
        assert all([cl["key"] for cl in c.clients] == ["ipad-mail"] for c in combos)
        assert all(set(c.scenarios) == set(plan.client_scenarios) for c in combos)
        # iPad activesync kullanır → bu koşuda POP3 kanalına gerek yok
        assert all(c.receive_channels == ["imap", "activesync"] for c in combos)

    def test_impact_outlook_desktop_reads_pop3(self, plan):
        combos = plan.combinations("duman", "outlook-masaustu")
        assert all(c.receive_channels == ["imap", "pop3"] for c in combos)

    def test_unknown_impact_and_tier(self, plan):
        with pytest.raises(PlanError, match="Bilinmeyen etki"):
            plan.combinations("tam", "blackberry")
        with pytest.raises(PlanError, match="Bilinmeyen kademe"):
            plan.combinations("hepsi")

    @pytest.mark.parametrize("data, message", [
        ([], "Plan kökü"),
        ({"yollar": []}, "en az bir yol"),
        ({"yollar": [{"gonderen": "yahoo", "alan": "ems"}]}, "bilinmeyen sunucu"),
        ({"yollar": [{"gonderen": "ems", "alan": "ems", "kademe": "hızlı"}]}, "kademe"),
        ({"yollar": [{"gonderen": "ems", "alan": "ems"}] * 2}, "tekrar ediyor"),
        ({"yollar": [{"gonderen": "ems", "alan": "ems"}],
          "kademeler": {"duman": ["olmayan_senaryo"]}}, "bilinmeyen senaryo"),
        ({"yollar": [{"gonderen": "ems", "alan": "ems"}],
          "istemciler": {"x": {"sunucu": "ems", "erisim": "telnet"}}}, "erisim"),
        ({"yollar": [{"gonderen": "ems", "alan": "ems"}],
          "istemciler": {"a:b": {"sunucu": "ems", "erisim": "imap"}}}, "kullanılamaz"),
    ])
    def test_invalid_plans_are_rejected(self, data, message):
        with pytest.raises(PlanError, match=message):
            parse_plan(data)

    def test_minimal_plan_defaults_to_all_scenarios(self):
        plan = parse_plan({"yollar": [{"gonderen": "ems", "alan": "gmail"}]})
        (combo,) = plan.combinations()
        assert combo.label == "EMS → GMAIL" and len(combo.scenarios) == 26
        assert plan.client_scenarios is None

    def test_missing_file(self, tmp_path):
        with pytest.raises(PlanError, match="okunamadı"):
            load_plan(tmp_path / "yok.yaml")


# ═══════════════════════════════════════════════════════════════════
#  POP3 kanalı
# ═══════════════════════════════════════════════════════════════════

def _raw(msg_id: str, body: str = "Merhaba ğüş") -> bytes:
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Message-ID"], msg["Subject"] = msg_id, "Konu"
    return msg.as_bytes().replace(b"\n", b"\r\n")


class FakePop:
    def __init__(self, messages):
        self.messages = messages
        self.deleted = self.quit_called = False

    def user(self, _u): return b"+OK"
    def pass_(self, _p): return b"+OK"
    def stat(self): return (len(self.messages), 0)

    def top(self, n, _lines):
        head = self.messages[n - 1].split(b"\r\n\r\n")[0]
        return (b"+OK", head.split(b"\r\n"), len(head))

    def retr(self, n):
        return (b"+OK", self.messages[n - 1].rstrip(b"\r\n").split(b"\r\n"), 0)

    def dele(self, _n): self.deleted = True
    def quit(self): self.quit_called = True


class TestPop3Receiver:

    def _receiver(self):
        return Pop3Receiver({**_server_cfg("ems"), "pop_host": "pop.ems.test"})

    def test_finds_message_by_message_id_without_deleting(self):
        pop = FakePop([_raw("<a@x>"), _raw("<hedef@x>", "Aranan ğüş"), _raw("<c@x>")])
        with patch("receiver.pop3_connect", return_value=pop):
            got = self._receiver().wait_for_message("<hedef@x>", wait_seconds=0, max_retries=1)
        assert got["headers"]["message_id"] == "<hedef@x>"
        assert "Aranan ğüş" in got["parts"][0]["text_preview"]
        assert email.message_from_bytes(got["raw_bytes"])["Message-ID"] == "<hedef@x>"
        assert pop.quit_called and not pop.deleted

    def test_not_found_returns_none_without_error(self):
        receiver = self._receiver()
        with patch("receiver.pop3_connect", return_value=FakePop([_raw("<a@x>")])):
            assert receiver.wait_for_message("<yok@x>", wait_seconds=0, max_retries=2,
                                             retry_interval=0) is None
        assert receiver.last_error is None

    def test_auth_failure_is_not_retried(self):
        pop = FakePop([])
        pop.pass_ = MagicMock(side_effect=poplib.error_proto("-ERR Authentication failed"))
        pop.close = MagicMock()
        receiver = self._receiver()
        with patch("receiver.pop3_connect", return_value=pop) as connect:
            assert receiver.wait_for_message("<a@x>", wait_seconds=0, max_retries=5,
                                             retry_interval=0) is None
        assert connect.call_count == 1 and receiver.is_auth_error(receiver.last_error)

    def test_defaults_to_imap_host_and_factory(self):
        cfg = _server_cfg("ems")
        assert Pop3Receiver(cfg).host == "imap.ems.test"
        assert isinstance(receiver_for(cfg, "pop3"), Pop3Receiver)
        assert type(receiver_for(cfg, "imap")) is MailReceiver
        with pytest.raises(ValueError):
            receiver_for(cfg, "web")

    def test_pop3_connect_uses_ssl_on_995_by_default(self):
        from mail_transport import pop3_connect
        with patch("mail_transport.poplib.POP3_SSL") as ssl_cls, \
             patch("mail_transport.poplib.POP3") as plain_cls:
            pop3_connect(_server_cfg("ems"))
            assert ssl_cls.call_args[0][:2] == ("imap.ems.test", 995)
            pop3_connect({**_server_cfg("ems"), "pop_use_ssl": False, "pop_host": "p.test"})
            assert plain_cls.call_args[0][:2] == ("p.test", 110)


class TestChannelChecks:

    def test_identical_content_passes(self):
        raw = _raw("<a@x>")
        # Başlık sırası/satır sonu farkı içerik farkı sayılmaz
        other = raw.replace(b"\r\n", b"\n")
        assert fingerprint(raw) == fingerprint(other)
        checks = channel_checks("pop3", other, raw, None)
        assert [c["passed"] for c in checks] == [True, True]
        assert checks[0]["name"] == "[Kesin] POP3 kanalı: teslim"

    def test_different_content_fails_and_flips_verdict(self):
        checks = channel_checks("pop3", _raw("<a@x>", "Merhaba gus"), _raw("<a@x>"), None)
        assert checks[1]["passed"] is False
        merged = apply_channel_checks({"passed": True, "summary": "tamam", "checks": [],
                                       "issues": []}, checks)
        assert merged["passed"] is False and "Kanal hatası" in merged["summary"]

    def test_missing_on_channel_fails(self):
        (check,) = channel_checks("pop3", None, _raw("<a@x>"), None,
                                  error="POP3 bağlantı hatası: timed out")
        assert not check["passed"] and "timed out" in check["detail"]

    def test_expectation_failures_are_reported_per_channel(self):
        raw = _raw("<a@x>")
        checks = channel_checks("pop3", raw, raw, {"subject": "Başka Konu"})
        assert checks[-1]["name"] == "[Kesin] POP3 kanalı: Konu korunumu"


# ═══════════════════════════════════════════════════════════════════
#  Uçtan uca: plan ile koşu (loopback)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def plan_cfg(tmp_path, monkeypatch, full_config):
    monkeypatch.chdir(tmp_path)
    full_config["analysis"] = {"provider": "none"}
    full_config["test"].update({"plan": str(DEFAULT_PLAN), "large_attachment_mb": 0.05,
                                "test_attachment_path": "test_files/test_document.pdf",
                                "test_image_path": "test_files/test_image.png"})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(full_config, allow_unicode=True), encoding="utf-8")
    return path, full_config


def _cli(argv, loop, pop_mutator=None):
    def pop_wait(self, expected_msg_id, **_kw):
        got = loop.wait_for_message(self, expected_msg_id)
        if got and pop_mutator:
            raw = pop_mutator(got["raw_bytes"])
            if raw is None:
                return None
            got = self._extract_details(email.message_from_bytes(raw), raw, "pop3")
        return got
    with patch.object(sys, "argv", ["main.py", *argv]), \
         patch.object(MailSender, "_connect", side_effect=lambda: loop.smtp()), \
         patch.object(MailReceiver, "wait_for_message", autospec=True,
                      side_effect=lambda self, **kw: loop.wait_for_message(self, **kw)), \
         patch.object(Pop3Receiver, "wait_for_message", autospec=True, side_effect=pop_wait), \
         patch.object(main_mod.time, "sleep"):
        main_mod.main()


def _results(cfg):
    return manual_verify.load_results_json(
        manual_verify.results_json_path(cfg["test"]["results_csv"]))


class TestPlanRun:

    def test_smoke_tier_runs_two_routes_five_scenarios(self, plan_cfg):
        path, cfg = plan_cfg
        _cli(["--config", str(path), "--tier", "duman"], Loopback())
        data = _results(cfg)
        assert data["run"]["plan"] == "test_plan.yaml" and data["run"]["tier"] == "duman"
        assert len(data["results"]) == 10
        assert {r["combination"] for r in data["results"]} == {
            "EMS → EMS (kurum içi)", "Gmail → EMS (dışarıdan)"}
        assert all(r["analysis"]["passed"] for r in data["results"])

    def test_pop3_channel_is_verified_for_every_message(self, plan_cfg):
        path, cfg = plan_cfg
        _cli(["--config", str(path), "--tier", "duman", "--combo", "0"], Loopback())
        for r in _results(cfg)["results"]:
            names = [c["name"] for c in r["analysis"]["checks"]]
            assert "[Kesin] POP3 kanalı: teslim" in names, r["scenario_key"]
            assert "[Kesin] POP3 kanalı: içerik IMAP ile aynı" in names

    def test_pop3_only_damage_fails_the_scenario(self, plan_cfg):
        path, cfg = plan_cfg
        with pytest.raises(SystemExit) as exc:
            _cli(["--config", str(path), "--tier", "duman", "--combo", "0",
                  "--scenario", "priority_high"], Loopback(),
                 pop_mutator=_drop_headers("X-Priority", "X-MSMail-Priority",
                                           "Importance", "Priority"))
        assert exc.value.code == main_mod.EXIT_TEST_FAILURES
        (r,) = _results(cfg)["results"]
        failed = [c["name"] for c in r["analysis"]["checks"] if not c["passed"]]
        # IMAP'te sağlam; yalnızca POP3 yolunda öncelik başlıkları kaybolmuş.
        assert failed == ["[Kesin] POP3 kanalı: Öncelik düzeyi"]

    def test_message_missing_on_pop3_fails(self, plan_cfg):
        path, cfg = plan_cfg
        with pytest.raises(SystemExit):
            _cli(["--config", str(path), "--tier", "duman", "--combo", "0",
                  "--scenario", "plain_text"], Loopback(), pop_mutator=lambda raw: None)
        (r,) = _results(cfg)["results"]
        assert any(c["name"] == "[Kesin] POP3 kanalı: teslim" and not c["passed"]
                   for c in r["analysis"]["checks"])

    def test_clients_attached_only_for_client_relevant_scenarios(self, plan_cfg):
        path, cfg = plan_cfg
        _cli(["--config", str(path), "--tier", "duman", "--combo", "0"], Loopback())
        by_key = {r["scenario_key"]: r for r in _results(cfg)["results"]}
        assert [c["key"] for c in by_key["priority_high"]["clients"]] == [
            "outlook-masaustu", "android-gmail", "ipad-mail"]
        # enc_quoted_printable istemci_senaryolari'nda yok → manuel sorulmaz
        assert by_key["enc_quoted_printable"]["manual_scope"] == "none"
        assert not manual_verify.is_eligible(by_key["enc_quoted_printable"])
        assert len(manual_verify.slots(by_key["priority_high"])) == 3

    def test_impact_runs_only_that_client(self, plan_cfg):
        path, cfg = plan_cfg
        _cli(["--config", str(path), "--tier", "duman", "--impact", "ipad-mail"], Loopback())
        results = _results(cfg)["results"]
        assert {r["scenario_key"] for r in results} == {
            "plain_text", "attachment", "inline_image", "priority_high"}
        assert all([c["key"] for c in r["clients"]] == ["ipad-mail"] for r in results)

    def test_manual_prompt_asks_per_client(self, plan_cfg):
        path, cfg = plan_cfg
        answers = iter(["g", "", "g", "", "g", "", "",          # Outlook: 3 adım + genel not
                        "g", "", "k", "ünlem yok", "g", "", "", # Android
                        "a"])                                    # iPad: atla
        with patch.object(sys.stdin, "isatty", return_value=True, create=True), \
             patch("builtins.input", side_effect=lambda _p="": next(answers)):
            _cli(["--config", str(path), "--tier", "duman", "--combo", "0",
                  "--scenario", "priority_high", "--manual"], Loopback())
        store = manual_verify.load_store(manual_verify.store_path(cfg["test"]["results_csv"]))
        by_client = {k.split(":")[1]: v for k, v in store.items()}
        assert set(by_client) == {"outlook-masaustu", "android-gmail"}
        assert by_client["android-gmail"]["overall"] == "fail"
        assert by_client["outlook-masaustu"]["client_name"] == "Outlook Masaüstü"
        row = next(csv.DictReader(open(cfg["test"]["results_csv"], encoding="utf-8-sig")))
        assert row["Manuel Sonuç"] == "KALDI"
        assert "Gmail (Android): KALDI" in row["Manuel Not"]
        assert "Outlook Masaüstü: GEÇTİ" in row["Manuel Not"]

    @pytest.mark.parametrize("argv, message", [
        (["--tier", "duman", "--impact", "yok"], "Bilinmeyen etki"),
        (["--plan", "olmayan_plan.yaml"], "Test planı bulunamadı"),
    ])
    def test_plan_errors_exit_cleanly(self, plan_cfg, caplog, argv, message):
        path, _ = plan_cfg
        with pytest.raises(SystemExit) as exc, caplog.at_level("ERROR"):
            _cli(["--config", str(path), *argv], Loopback())
        assert exc.value.code == main_mod.EXIT_ERROR and message in caplog.text

    def test_tier_without_plan_is_rejected(self, plan_cfg, caplog):
        path, cfg = plan_cfg
        del cfg["test"]["plan"]
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        with pytest.raises(SystemExit) as exc, caplog.at_level("ERROR"):
            _cli(["--config", str(path), "--tier", "duman"], Loopback())
        assert exc.value.code == main_mod.EXIT_ERROR
        assert "yalnızca test planıyla" in caplog.text

    def test_explicit_csv_overrides_configured_plan(self, plan_cfg):
        path, cfg = plan_cfg
        _cli(["--config", str(path), "--csv", str(PROJECT_ROOT / "mail_test_checklist.csv"),
              "--combo", "0", "--scenario", "priority_low"], Loopback())
        (r,) = _results(cfg)["results"]
        assert r["combination"] == "EMS/iOS ← Gmail/Android" and "clients" not in r


# ═══════════════════════════════════════════════════════════════════
#  İstemci başına manuel kayıt + web uçları
# ═══════════════════════════════════════════════════════════════════

def _planned_result(rid="abcd1234", passed=True):
    return {"result_id": rid, "combination": "EMS → EMS (kurum içi)",
            "scenario_key": "priority_high", "subject": f"[T] #{rid}", "received": True,
            "send_meta": {"msg_id": "<a@b>"}, "receiver_server": "ems",
            "manual_steps": ["Açılıyor mu?", "Ünlem görünüyor mu?"],
            "clients": [{"key": "outlook-masaustu", "name": "Outlook Masaüstü",
                         "access": "pop3", "critical": True, "version": "2021"},
                        {"key": "ipad-mail", "name": "iPad Mail", "access": "activesync",
                         "critical": False, "version": ""}],
            "analysis": {"passed": passed, "confidence": "HIGH", "checks": [],
                         "summary": "özet", "issues": [], "recommendations": []},
            "test_time": "2026-11-13 10:00:00"}


class TestPerClientManual:

    def test_agreement_counts_each_client_slot(self):
        r = _planned_result()
        ok = [{"status": "pass"}, {"status": "pass"}]
        store = {"abcd1234:ipad-mail": manual_verify.build_entry(
            r, [{"status": "pass"}, {"status": "fail"}], client=r["clients"][1])}
        stats = manual_verify.agreement([r], store)
        assert (stats["eligible"], stats["verified"], stats["pending"]) == (2, 1, 1)
        assert stats["false_pass"] == 1
        store["abcd1234:outlook-masaustu"] = manual_verify.build_entry(r, ok, client=r["clients"][0])
        assert manual_verify.agreement([r], store)["pending"] == 0

    def test_report_merges_clients_into_one_cell(self, tmp_path):
        r = _planned_result()
        store = {
            "abcd1234:outlook-masaustu": manual_verify.build_entry(
                r, [{"status": "pass"}, {"status": "pass"}], client=r["clients"][0]),
            "abcd1234:ipad-mail": manual_verify.build_entry(
                r, [{"status": "pass"}, {"status": "fail", "note": "simge yok"}],
                "iPadOS 19", client=r["clients"][1]),
            "ffff0000:ipad-mail": {"overall": "fail"},      # başka koşunun kaydı
        }
        manual = manual_verify.for_results(store, [r])
        assert "ffff0000:ipad-mail" not in manual
        out = tmp_path / "r.csv"
        generate_csv_results([r], str(out), manual=manual)
        row = next(csv.DictReader(open(out, encoding="utf-8-sig")))
        assert row["Manuel Sonuç"] == "KALDI" and row["Oto/Manuel Uyumu"] == "ARAÇ KAÇIRDI"
        assert "Outlook Masaüstü: GEÇTİ" in row["Manuel Not"]
        assert "iPad Mail: KALDI (iPadOS 19)" in row["Manuel Not"]
        assert "iPad Mail · Ünlem görünüyor mu? — simge yok" in row["Manuel Not"]


@pytest.fixture
def plan_app(flask_client, tmp_path):
    import app as app_module
    app_module.CONFIG_PATH.write_text(
        yaml.safe_dump({"test": {"plan": str(DEFAULT_PLAN)}}), encoding="utf-8")
    manual_verify.write_results_json(
        [_planned_result()], app_module.REPORTS_DIR / "test_results.json", {"finished": True})
    return flask_client, app_module


class TestPlanEndpoints:

    def test_plan_inactive_by_default(self, flask_client):
        assert flask_client.get("/api/plan").get_json() == {"ok": True, "active": False}

    def test_plan_summary(self, plan_app):
        client, _ = plan_app
        data = client.get("/api/plan").get_json()
        assert data["active"] and data["file"] == "test_plan.yaml"
        assert [t["key"] for t in data["tiers"]] == ["duman", "regresyon", "tam"]
        assert {"key": "ipad-mail", "label": "iPad Mail", "kind": "client"} in data["impacts"]

    def test_combinations_come_from_plan(self, plan_app):
        client, _ = plan_app
        data = client.get("/api/combinations?tier=duman").get_json()
        assert data["plan"] and len(data["combinations"]) == 2
        assert data["combinations"][0]["channels"] == ["imap", "pop3", "activesync"]
        bad = client.get("/api/combinations?impact=yok").get_json()
        assert not bad["ok"] and "Bilinmeyen etki" in bad["error"]

    def test_broken_plan_is_reported(self, plan_app, tmp_path):
        client, app_module = plan_app
        broken = tmp_path / "bozuk.yaml"
        broken.write_text("yollar: []", encoding="utf-8")
        app_module.CONFIG_PATH.write_text(yaml.safe_dump({"test": {"plan": str(broken)}}))
        data = client.get("/api/plan").get_json()
        assert not data["ok"] and "en az bir yol" in data["error"]

    def test_queue_has_one_item_per_client(self, plan_app):
        client, _ = plan_app
        items = client.get("/api/manual/queue").get_json()["items"]
        assert [i["id"] for i in items] == ["abcd1234:outlook-masaustu", "abcd1234:ipad-mail"]
        assert items[0]["code"] == "abcd1234" and items[0]["client"]["critical"] is True
        assert items[1]["receiver_client"] == "iPad Mail"

    def test_submit_per_client(self, plan_app):
        client, _ = plan_app
        resp = client.post("/api/manual/submit", json={
            "id": "abcd1234:ipad-mail", "steps": [{"status": "pass"}, {"status": "fail"}]})
        data = resp.get_json()
        assert data["entry"]["client"] == "ipad-mail" and data["stats"]["pending"] == 1
        assert client.post("/api/manual/submit", json={
            "id": "abcd1234", "steps": [{"status": "pass"}, {"status": "pass"}]}).status_code == 404
        assert client.post("/api/manual/submit", json={
            "id": "abcd1234:blackberry", "steps": []}).status_code == 404

    def test_run_start_passes_tier_and_impact(self, plan_app):
        client, app_module = plan_app
        proc = MagicMock()
        proc.stdout, proc.pid, proc.returncode = iter(()), 4242, 0
        with patch.object(app_module.subprocess, "Popen", return_value=proc) as popen:
            resp = client.post("/api/run/start", json={"tier": "duman", "impact": "ipad-mail"})
        assert resp.get_json()["ok"]
        cmd = popen.call_args[0][0]
        assert cmd[cmd.index("--tier") + 1] == "duman"
        assert cmd[cmd.index("--impact") + 1] == "ipad-mail"

    @pytest.mark.parametrize("body", [{"tier": "hepsi"}, {"impact": "--config"}])
    def test_run_start_rejects_bad_tier_or_impact(self, plan_app, body):
        client, _ = plan_app
        assert client.post("/api/run/start", json=body).status_code == 400

    def test_tier_needs_a_plan(self, flask_client):
        resp = flask_client.post("/api/run/start", json={"tier": "duman"})
        assert resp.status_code == 400 and "test planı gerekli" in resp.get_json()["error"]
