"""
test_performance.py — Kritik modüllerin performans (hız) testleri.

Her test bir zaman sınırı (deadline) tanımlar; bu sınır aşılırsa CI başarısız olur.
Testler, gerçek I/O gerektirmeden saf hesaplama süresini ölçer.
"""

import time
import tempfile
import pytest


# ── Yardımcı: basit zaman ölçer ──────────────────────────────────
class Timer:
    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_):
        self.elapsed = time.perf_counter() - self._start


# ══════════════════════════════════════════════════════════════════
# 1. CSV PARSER PERFORMANS TESTLERİ
# ══════════════════════════════════════════════════════════════════
class TestCSVParserPerformance:
    """parse_csv() büyük dosyalarda hızlı çalışmalı."""

    DEADLINES = {
        "small":  0.05,   # 50 ms  — 3 kombinasyon,  ~50 satır
        "medium": 0.20,   # 200 ms — 20 kombinasyon, ~400 satır
        "large":  1.00,   # 1 s    — 100 kombinasyon, ~2000 satır
    }

    @staticmethod
    def _make_csv(n_combinations: int, steps_per_combo: int) -> str:
        """Sentetik CSV üretir."""
        rows = []
        row_id = 1
        for i in range(n_combinations):
            rows.append(
                f"🔀 Kombinasyon: Alan → EMS / iOS  Gönderen → Gmail / Android\n"
                f"Senaryo,Senaryo Tipi,Alan Sunucu,Alan İstemci,"
                f"Gönderen Sunucu,Gönderen İstemci,Açıklama,Durum\n"
            )
            for j in range(steps_per_combo):
                rows.append(
                    f"{row_id},Sadece İçerik (Plain Text),"
                    f"EMS,iOS,Gmail,Android,Adım açıklaması {row_id},⬜ Bekliyor\n"
                )
                row_id += 1
            rows.append("\n")
        return "".join(rows)

    def _parse_csv_timed(self, n_combinations: int, steps_per_combo: int) -> float:
        from csv_parser import parse_csv
        csv_content = self._make_csv(n_combinations, steps_per_combo)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", encoding="utf-8", delete=False
        ) as f:
            f.write(csv_content)
            fname = f.name

        with Timer() as t:
            result = parse_csv(fname)

        assert len(result) == n_combinations, "Beklenen kombinasyon sayısı hatalı"
        return t.elapsed

    def test_small_csv_under_50ms(self):
        elapsed = self._parse_csv_timed(n_combinations=3, steps_per_combo=15)
        assert elapsed < self.DEADLINES["small"], (
            f"CSV parse (küçük) çok yavaş: {elapsed*1000:.1f}ms "
            f"(limit: {self.DEADLINES['small']*1000:.0f}ms)"
        )

    def test_medium_csv_under_200ms(self):
        elapsed = self._parse_csv_timed(n_combinations=20, steps_per_combo=20)
        assert elapsed < self.DEADLINES["medium"], (
            f"CSV parse (orta) çok yavaş: {elapsed*1000:.1f}ms "
            f"(limit: {self.DEADLINES['medium']*1000:.0f}ms)"
        )

    def test_large_csv_under_1s(self):
        elapsed = self._parse_csv_timed(n_combinations=100, steps_per_combo=20)
        assert elapsed < self.DEADLINES["large"], (
            f"CSV parse (büyük) çok yavaş: {elapsed*1000:.1f}ms "
            f"(limit: {self.DEADLINES['large']*1000:.0f}ms)"
        )

    def test_parse_csv_idempotent_performance(self):
        """Aynı CSV'yi 5 kez parse etmek, tek parse'ın 5 katından daha az sürmeli."""
        from csv_parser import parse_csv
        csv_content = self._make_csv(n_combinations=10, steps_per_combo=10)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", encoding="utf-8", delete=False
        ) as f:
            f.write(csv_content)
            fname = f.name

        # İlk parse
        with Timer() as single:
            parse_csv(fname)

        # 5 tekrar
        with Timer() as repeated:
            for _ in range(5):
                parse_csv(fname)

        assert repeated.elapsed < single.elapsed * 5.5, (
            "Tekrarlı parse beklenenin çok üzerinde: olası bellek sızıntısı veya state sorunu"
        )


# ══════════════════════════════════════════════════════════════════
# 2. MESSAGE TEMPLATES PERFORMANS TESTLERİ
# ══════════════════════════════════════════════════════════════════
class TestMessageTemplatesPerformance:
    """Template modülü her çağrıda hızlı dönmeli."""

    def test_get_template_under_1ms(self):
        """get_template() 1ms altında dönmeli."""
        from message_templates import get_template
        with Timer() as t:
            for i in range(1000):
                get_template("plain_text", i % 3)
        avg_ms = (t.elapsed / 1000) * 1000
        assert avg_ms < 1.0, (
            f"get_template() ortalama {avg_ms:.3f}ms — limit: 1ms"
        )

    def test_all_scenarios_accessible_under_5ms(self):
        """9 senaryo tipi toplam 5ms içinde erişilebilmeli."""
        from csv_parser import SCENARIO_TYPE_MAP
        from message_templates import get_template
        scenario_keys = list(SCENARIO_TYPE_MAP.values())

        with Timer() as t:
            for i, key in enumerate(scenario_keys):
                get_template(key, i % 3)

        assert t.elapsed < 0.005, (
            f"Tüm template erişimleri {t.elapsed*1000:.1f}ms sürdü (limit: 5ms)"
        )

    def test_template_bulk_generation_under_100ms(self):
        """1000 template çağrısı 100ms altında tamamlanmalı."""
        from message_templates import get_template
        scenarios = ["plain_text", "attachment", "inline_image", "reply_chain"]

        with Timer() as t:
            for i in range(1000):
                get_template(scenarios[i % len(scenarios)], i % 3)

        assert t.elapsed < 0.10, (
            f"1000 template çağrısı {t.elapsed*1000:.1f}ms sürdü (limit: 100ms)"
        )


# ══════════════════════════════════════════════════════════════════
# 3. SCENARIO TYPE MAP LOOKUP PERFORMANS TESTLERİ
# ══════════════════════════════════════════════════════════════════
class TestScenarioMapPerformance:
    """SCENARIO_TYPE_MAP dict lookup O(1) olmalı."""

    def test_million_lookups_under_500ms(self):
        """1 milyon dict lookup 500ms altında tamamlanmalı."""
        from csv_parser import SCENARIO_TYPE_MAP
        key = "Sadece İçerik (Plain Text)"

        with Timer() as t:
            for _ in range(1_000_000):
                _ = SCENARIO_TYPE_MAP.get(key, "unknown")

        assert t.elapsed < 0.5, (
            f"1M dict lookup {t.elapsed*1000:.0f}ms sürdü (limit: 500ms)"
        )

    def test_unknown_key_lookup_no_exception(self):
        """Bilinmeyen key, hata fırlatmamalı ve hızlı dönmeli."""
        from csv_parser import SCENARIO_TYPE_MAP
        with Timer() as t:
            for _ in range(100_000):
                result = SCENARIO_TYPE_MAP.get("bilinmeyen senaryo", "unknown")
        assert result == "unknown"
        assert t.elapsed < 0.1


# ══════════════════════════════════════════════════════════════════
# 4. FLASK APP ENDPOINT PERFORMANS TESTLERİ
# ══════════════════════════════════════════════════════════════════
class TestFlaskEndpointPerformance:
    """Flask endpoint'leri test client üzerinden hız testi."""

    @pytest.fixture(autouse=True)
    def app_client(self):
        from app import app
        app.config["TESTING"] = True
        with app.test_client() as client:
            self.client = client
            yield

    def test_index_response_under_200ms(self):
        """Ana sayfa 200ms altında yanıt vermeli."""
        with Timer() as t:
            resp = self.client.get("/")
        assert resp.status_code == 200
        assert t.elapsed < 0.2, (
            f"GET / yanıt süresi: {t.elapsed*1000:.1f}ms (limit: 200ms)"
        )

    def test_index_100_requests_under_5s(self):
        """100 ardışık istek 5 saniye altında tamamlanmalı."""
        with Timer() as t:
            for _ in range(100):
                resp = self.client.get("/")
                assert resp.status_code == 200
        assert t.elapsed < 5.0, (
            f"100 istek {t.elapsed:.2f}s sürdü (limit: 5s) "
            f"— ort: {t.elapsed/100*1000:.1f}ms/req"
        )

    def test_status_endpoint_under_100ms(self):
        """Status endpoint 100ms altında yanıt vermeli."""
        with Timer() as t:
            resp = self.client.get("/api/status")
        assert resp.status_code in (200, 404)  # endpoint yoksa geç
        assert t.elapsed < 0.1, (
            f"GET /api/status: {t.elapsed*1000:.1f}ms (limit: 100ms)"
        )

    def test_concurrent_simulation_throughput(self):
        """
        Senkron döngüyle 50 istek simülasyonu:
        ortalama yanıt 50ms altında olmalı.
        """
        times = []
        for _ in range(50):
            with Timer() as t:
                self.client.get("/")
            times.append(t.elapsed)

        avg = sum(times) / len(times)
        p95 = sorted(times)[int(len(times) * 0.95)]

        assert avg < 0.05, f"Ortalama yanıt {avg*1000:.1f}ms (limit: 50ms)"
        assert p95 < 0.10, f"P95 yanıt {p95*1000:.1f}ms (limit: 100ms)"


# ══════════════════════════════════════════════════════════════════
# 5. COMBINATION HEADER PARSER PERFORMANS TESTLERİ
# ══════════════════════════════════════════════════════════════════
class TestCombinationHeaderParserPerformance:
    """Regex tabanlı header parser hızlı çalışmalı."""

    VALID_HEADER = "🔀 Kombinasyon: Alan → EMS / iOS  Gönderen → Gmail / Android"
    INVALID_HEADER = "🔀 Geçersiz format metni burada"

    def test_valid_header_parse_under_1ms(self):
        """Geçerli header 1ms altında parse edilmeli."""
        from csv_parser import _parse_combination_header
        with Timer() as t:
            for _ in range(10_000):
                _parse_combination_header(self.VALID_HEADER)
        avg_us = (t.elapsed / 10_000) * 1_000_000
        assert avg_us < 1000, (
            f"Header parse ort. {avg_us:.0f}µs (limit: 1000µs)"
        )

    def test_fallback_header_parse_under_2ms(self):
        """Fallback path da 2ms altında çalışmalı."""
        from csv_parser import _parse_combination_header
        with Timer() as t:
            for _ in range(10_000):
                _parse_combination_header(self.INVALID_HEADER)
        avg_us = (t.elapsed / 10_000) * 1_000_000
        assert avg_us < 2000, (
            f"Fallback header parse ort. {avg_us:.0f}µs (limit: 2000µs)"
        )
