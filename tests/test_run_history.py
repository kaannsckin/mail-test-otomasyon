"""
test_run_history.py — Koşu geçmişi, kanıt paketi, referans koşu ve fark.
"""

import email
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import main as main_mod  # noqa: E402
import run_history  # noqa: E402
from test_catalog_scenarios import Loopback, _drop_headers  # noqa: E402
from test_catalog_scenarios import _cli as csv_cli  # noqa: E402
from test_plan_model import _cli as plan_cli  # noqa: E402

DEFAULT_PLAN = PROJECT_ROOT / "test_plan.yaml"
DROP_PRIORITY = _drop_headers("X-Priority", "X-MSMail-Priority", "Importance", "Priority")


def _result(scenario, passed=True, checks=None, combo="EMS → EMS", rid="aaaa0000"):
    return {"combination": combo, "scenario_key": scenario, "result_id": rid,
            "analysis": {"passed": passed, "summary": "özet", "checks": checks or []}}


def _run(results, run_id="20261113-100000-aaaaaa", labels=()):
    return {"run_id": run_id, "manifest": {"labels": list(labels)}, "results": results}


def _check(name, passed=True, detail=""):
    return {"name": name, "passed": passed, "detail": detail}


# ═══════════════════════════════════════════════════════════════════
#  Fark
# ═══════════════════════════════════════════════════════════════════

class TestDiff:

    def test_identical_runs(self):
        a = _run([_result("plain_text"), _result("priority_high")])
        diff = run_history.diff_runs(a, _run(a["results"], "20261114-100000-bbbbbb"))
        assert diff["identical"] and diff["counts"]["same"] == 2
        assert "fark yok" in run_history.diff_summary(diff)

    def test_regression_fix_new_and_missing(self):
        base = _run([_result("a", True), _result("b", False), _result("c", True)])
        new = _run([_result("a", False), _result("b", True), _result("d", True)],
                   "20261114-100000-bbbbbb")
        diff = run_history.diff_runs(base, new)
        kinds = {r["scenario_key"]: r["kind"] for r in diff["rows"]}
        assert kinds == {"a": "regression", "b": "fixed", "d": "new", "c": "missing"}
        assert diff["rows"][0]["scenario_key"] == "a"          # yeni hata en üstte
        assert "1 YENİ HATA" in run_history.diff_summary(diff)

    def test_same_route_names_do_not_collide(self):
        base = _run([_result("a", True, combo="EMS → EMS"), _result("a", True, combo="Gmail → EMS")])
        new = _run([_result("a", True, combo="EMS → EMS"), _result("a", False, combo="Gmail → EMS")])
        diff = run_history.diff_runs(base, new)
        assert diff["counts"]["regression"] == 1 and diff["counts"]["same"] == 1

    def test_behaviour_change_without_verdict_change(self):
        # Karar aynı (PASS) ama sunucu artık 8bit'i QP'ye çeviriyor.
        before = [_check("[Kesin] Gövde metni"), _check("[Bilgi] Kodlama beyanı", detail="CTE=8bit")]
        after = [_check("[Kesin] Gövde metni"),
                 _check("[Bilgi] Kodlama beyanı", detail="CTE=quoted-printable")]
        diff = run_history.diff_runs(_run([_result("enc_8bit", True, before)]),
                                     _run([_result("enc_8bit", True, after)]))
        (row,) = diff["rows"]
        assert row["kind"] == "changed" and not diff["identical"]
        assert row["changes"] == [{"check": "[Bilgi] Kodlama beyanı", "kind": "davranış değişti",
                                   "before": "CTE=8bit", "after": "CTE=quoted-printable"}]

    def test_check_level_breakage_names_the_check(self):
        before = [_check("[Kesin] POP3 kanalı: teslim"), _check("[Kesin] Konu korunumu")]
        after = [_check("[Kesin] POP3 kanalı: teslim", False, "bulunamadı"),
                 _check("[Kesin] Konu korunumu")]
        diff = run_history.diff_runs(_run([_result("a", True, before)]),
                                     _run([_result("a", False, after)]))
        (row,) = diff["rows"]
        assert row["kind"] == "regression"
        assert [(c["check"], c["kind"]) for c in row["changes"]] == [
            ("[Kesin] POP3 kanalı: teslim", "bozuldu")]

    def test_llm_checks_are_ignored_and_detail_noise_on_hard_checks_too(self):
        before = [_check("[LLM] Başlık", True), _check("[Kesin] Gövde metni", detail="1200 karakter")]
        after = [_check("[LLM] Üstbilgi", False), _check("[Kesin] Gövde metni", detail="1201 karakter")]
        diff = run_history.diff_runs(_run([_result("a", True, before)]),
                                     _run([_result("a", True, after)]))
        assert diff["identical"]

    def test_new_and_vanished_checks(self):
        diff = run_history.diff_runs(
            _run([_result("a", True, [_check("[Kesin] POP3 kanalı: teslim")])]),
            _run([_result("a", True, [_check("[Kesin] ACTIVESYNC kanalı: teslim")])]))
        kinds = {c["check"]: c["kind"] for c in diff["rows"][0]["changes"]}
        assert kinds == {"[Kesin] POP3 kanalı: teslim": "kontrol kayboldu",
                         "[Kesin] ACTIVESYNC kanalı: teslim": "yeni kontrol"}


# ═══════════════════════════════════════════════════════════════════
#  Depolama
# ═══════════════════════════════════════════════════════════════════

class TestStorage:

    def test_run_id_format(self):
        rid = run_history.new_run_id()
        assert run_history.RUN_ID_RE.match(rid) and rid != run_history.new_run_id()

    def test_evidence_written_with_hash_and_size_limit(self, tmp_path):
        small, big = b"From: a\r\n\r\nkucuk", b"x" * (3 * 1024 * 1024)
        out = run_history.save_evidence(tmp_path, "abcd1234",
                                        {"sent": small, "imap": big, "pop3": None, "eas": b""},
                                        max_mb=2)
        assert set(out) == {"sent", "imap"}
        assert out["sent"]["file"] == "abcd1234.sent.eml"
        assert (tmp_path / "evidence" / "abcd1234.sent.eml").read_bytes() == small
        # Sınırı aşan mesaj saklanmaz ama özeti kaydedilir
        assert "file" not in out["imap"]
        assert out["imap"]["sha256"] == hashlib.sha256(big).hexdigest()

    def test_finalize_list_load_and_baseline(self, tmp_path):
        ids = ["20261113-100000-aaaaaa", "20261114-100000-bbbbbb"]
        for rid, labels in zip(ids, (["EMS 9.4"], ["EMS 9.5"])):
            run_history.finalize_run(
                run_history.runs_dir(tmp_path) / rid,
                [{**_result("a"), "send_meta": {"msg_id": "<a>", "expect": {"x": 1}},
                  "evidence": {"imap": {"file": "aaaa0000.imap.eml"}}}],
                {"started_at": "2026-11-13 10:00:00", "labels": labels, "tier": "duman",
                 "plan": "test_plan.yaml", "clients": [{"key": "ipad-mail"}]}, {})
        (run_history.runs_dir(tmp_path) / "bozuk-ad").mkdir()
        runs = run_history.list_runs(tmp_path)
        assert [r["run_id"] for r in runs] == ids[::-1]            # yeniden eskiye
        assert runs[0]["labels"] == ["EMS 9.5"] and runs[0]["evidence_files"] == 1
        assert runs[0]["totals"] == {"total": 1, "passed": 1, "failed": 0, "skipped": 0}
        loaded = run_history.load_run(tmp_path, ids[0])
        assert loaded["manifest"]["tool_version"] == run_history.TOOL_VERSION
        assert "expect" not in loaded["results"][0]["send_meta"]
        assert run_history.get_baseline(tmp_path) == ""
        assert run_history.set_baseline(tmp_path, ids[0]) is True
        assert run_history.set_baseline(tmp_path, "20260101-000000-ffffff") is False
        assert run_history.set_baseline(tmp_path, "../../etc") is False
        assert [r["baseline"] for r in run_history.list_runs(tmp_path)] == [False, True]

    def test_labels_are_cleaned(self):
        assert run_history.clean_labels([" EMS  9.5 ", "", "EMS 9.5", "x" * 200, 7]) == [
            "EMS 9.5", "x" * 60, "7"]


# ═══════════════════════════════════════════════════════════════════
#  Uçtan uca: iki sürüm, referans, fark, kanıt
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def cfg(tmp_path, monkeypatch, full_config):
    monkeypatch.chdir(tmp_path)
    full_config["analysis"] = {"provider": "none"}
    full_config["test"].update({"plan": str(DEFAULT_PLAN),
                                "test_attachment_path": "test_files/test_document.pdf",
                                "test_image_path": "test_files/test_image.png"})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(full_config, allow_unicode=True), encoding="utf-8")
    return path, Path(full_config["test"]["results_csv"]).parent


ARGS = ["--tier", "duman", "--combo", "0", "--scenario", "priority_high,enc_quoted_printable"]


class TestEndToEnd:

    def test_run_is_persisted_with_evidence_and_labels(self, cfg):
        path, reports = cfg
        plan_cli(["--config", str(path), *ARGS, "--label", "EMS 9.4.2", "--label=Z-Push 2.7.1"],
                 Loopback())
        (run,) = run_history.list_runs(reports)
        assert run["labels"] == ["EMS 9.4.2", "Z-Push 2.7.1"] and run["tier"] == "duman"
        assert run["totals"]["passed"] == 2
        run_dir = run_history.runs_dir(reports) / run["run_id"]
        assert (run_dir / "report.html").is_file() and (run_dir / "results.csv").is_file()
        loaded = run_history.load_run(reports, run["run_id"])
        for r in loaded["results"]:
            assert set(r["evidence"]) == {"sent", "imap", "pop3"}
            sent = (run_dir / "evidence" / r["evidence"]["sent"]["file"]).read_bytes()
            got = (run_dir / "evidence" / r["evidence"]["imap"]["file"]).read_bytes()
            assert sent == got                                   # loopback: bayt bayt aynı
            assert f"#{r['result_id']}" in " ".join(
                str(email.header.make_header(email.header.decode_header(
                    email.message_from_bytes(sent)["Subject"]))).split())
            assert r["evidence"]["sent"]["sha256"] == hashlib.sha256(sent).hexdigest()

    def test_evidence_can_be_disabled(self, cfg):
        path, reports = cfg
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["test"]["evidence"] = False
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        plan_cli(["--config", str(path), *ARGS], Loopback())
        (run,) = run_history.list_runs(reports)
        assert run["evidence_files"] == 0
        assert not (run_history.runs_dir(reports) / run["run_id"] / "evidence").exists()

    def test_upgrade_regression_is_reported_against_baseline(self, cfg, caplog):
        path, reports = cfg
        # "Güncelleme öncesi": sağlam → referans
        plan_cli(["--config", str(path), *ARGS, "--label", "EMS 9.4.2", "--baseline"], Loopback())
        baseline = run_history.get_baseline(reports)
        assert baseline
        # "Güncelleme sonrası": sunucu POP3 yolunda öncelik başlıklarını siliyor
        with pytest.raises(SystemExit), caplog.at_level("INFO"):
            plan_cli(["--config", str(path), *ARGS, "--label", "EMS 9.5.0"], Loopback(),
                     pop_mutator=DROP_PRIORITY)
        assert "1 YENİ HATA" in caplog.text
        assert "⛔ YENİ HATA: EMS → EMS (kurum içi) / priority_high" in caplog.text
        newest = run_history.list_runs(reports)[0]
        assert newest["run_id"] != baseline and newest["labels"] == ["EMS 9.5.0"]
        diff = json.loads((run_history.runs_dir(reports) / newest["run_id"]
                           / "diff_vs_baseline.json").read_text(encoding="utf-8"))
        assert diff["counts"] == {"regression": 1, "changed": 0, "fixed": 0,
                                  "new": 0, "missing": 0, "same": 1}
        (change,) = diff["rows"][0]["changes"]
        assert change["check"] == "[Kesin] POP3 kanalı: Öncelik düzeyi"
        assert change["kind"] == "bozuldu" and "silinmiş" in change["after"]
        assert run_history.get_baseline(reports) == baseline      # referans değişmedi

    def test_unchanged_upgrade_reports_no_difference(self, cfg, caplog):
        path, reports = cfg
        plan_cli(["--config", str(path), *ARGS, "--baseline"], Loopback())
        with caplog.at_level("INFO"):
            plan_cli(["--config", str(path), *ARGS], Loopback())
        assert "fark yok — 2 senaryo aynı" in caplog.text

    def test_csv_mode_runs_are_recorded_too(self, cfg):
        path, reports = cfg
        csv_cli(["--config", str(path), "--csv", str(PROJECT_ROOT / "mail_test_checklist.csv"),
                 "--combo", "0", "--scenario", "priority_low"], Loopback())
        (run,) = run_history.list_runs(reports)
        assert run["plan"] == "" and run["totals"]["total"] == 1


# ═══════════════════════════════════════════════════════════════════
#  Web uçları
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def history_app(flask_client):
    import app as app_module
    base = [_result("a", True), _result("b", True)]
    new = [_result("a", False), _result("b", True)]
    ids = ["20261113-100000-aaaaaa", "20261114-100000-bbbbbb"]
    for rid, results, label in zip(ids, (base, new), ("EMS 9.4", "EMS 9.5")):
        run_dir = run_history.runs_dir(app_module.REPORTS_DIR) / rid
        run_history.finalize_run(run_dir, results, {"labels": [label]}, {})
        (run_dir / "report.html").write_text("<html>rapor</html>", encoding="utf-8")
        (run_dir / "evidence").mkdir()
        (run_dir / "evidence" / "aaaa0000.imap.eml").write_bytes(b"From: a\r\n\r\nx")
        (run_dir / "gizli.txt").write_text("gizli", encoding="utf-8")
    return flask_client, ids


class TestEndpoints:

    def test_list_and_set_baseline(self, history_app):
        client, ids = history_app
        data = client.get("/api/runs").get_json()
        assert [r["run_id"] for r in data["runs"]] == ids[::-1] and data["baseline"] == ""
        assert client.post(f"/api/runs/{ids[0]}/baseline").get_json()["baseline"] == ids[0]
        assert client.get("/api/runs").get_json()["baseline"] == ids[0]
        assert client.post("/api/runs/20260101-000000-ffffff/baseline").status_code == 404

    def test_diff_against_baseline(self, history_app):
        client, ids = history_app
        assert "Referans koşu yok" in client.get(f"/api/runs/diff?new={ids[1]}").get_json()["error"]
        client.post(f"/api/runs/{ids[0]}/baseline")
        data = client.get(f"/api/runs/diff?new={ids[1]}").get_json()
        assert data["diff"]["counts"]["regression"] == 1
        assert data["diff"]["base_labels"] == ["EMS 9.4"] and "1 YENİ HATA" in data["summary"]
        assert client.get("/api/runs/diff?new=yok").status_code == 404

    def test_files_are_served_only_from_allowlist(self, history_app):
        client, ids = history_app
        assert b"rapor" in client.get(f"/api/runs/{ids[0]}/files/report.html").data
        eml = client.get(f"/api/runs/{ids[0]}/files/evidence/aaaa0000.imap.eml")
        assert eml.data == b"From: a\r\n\r\nx" and "attachment" in eml.headers["Content-Disposition"]
        for bad in ("gizli.txt", "evidence/../gizli.txt", "../baseline.txt",
                    "evidence/aaaa0000.imap.exe", "results.json"):
            assert client.get(f"/api/runs/{ids[0]}/files/{bad}").status_code == 404, bad
        assert client.get("/api/runs/..%2F..%2Fx/files/report.html").status_code == 404

    def test_run_start_passes_labels_and_baseline_safely(self, flask_client):
        import app as app_module
        proc = MagicMock()
        proc.stdout, proc.pid, proc.returncode = iter(()), 4242, 0
        with patch.object(app_module.subprocess, "Popen", return_value=proc) as popen:
            resp = flask_client.post("/api/run/start", json={
                "labels": ["EMS 9.5.0", "--dry-run", " "], "baseline": True})
        assert resp.get_json()["ok"]
        cmd = popen.call_args[0][0]
        # '-' ile başlayan etiket ayrı bir seçenek olarak yorumlanamaz
        assert "--label=EMS 9.5.0" in cmd and "--label=--dry-run" in cmd
        assert "--dry-run" not in cmd and "--baseline" in cmd

    def test_run_start_rejects_bad_labels(self, flask_client):
        assert flask_client.post("/api/run/start", json={"labels": "EMS"}).status_code == 400
        assert flask_client.post("/api/run/start", json={"labels": [1]}).status_code == 400
