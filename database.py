import sqlite3
import json
import logging
import uuid
from datetime import datetime
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path(os.environ.get("MAIL_AUTO_CONFIG_DIR", "")).resolve() \
             if os.environ.get("MAIL_AUTO_CONFIG_DIR") else Path(__file__).parent
DB_PATH = _BASE_DIR / "reports" / "automation.db"

def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # Ana test akışı (Özet tablo)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_uuid TEXT UNIQUE,
                start_time DATETIME,
                total_tests INTEGER,
                passed_tests INTEGER,
                failed_tests INTEGER
            )
        """)
        
        # Test kırılımları (Senaryo detayları)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_uuid TEXT,
                combination_label TEXT,
                scenario_type TEXT,
                scenario_key TEXT,
                test_time DATETIME,
                passed BOOLEAN,
                confidence TEXT,
                issues TEXT,
                screenshot_path TEXT,
                baseline_path TEXT,
                diff_path TEXT,
                diff_percent REAL,
                is_visual_match BOOLEAN,
                FOREIGN KEY(run_uuid) REFERENCES test_runs(run_uuid)
            )
        """)
        conn.commit()

def save_run(run_uuid: str, all_results: list):
    if not all_results:
        return
        
    try:
        init_db()
        total = len(all_results)
        passed = sum(1 for r in all_results if r.get("analysis", {}).get("passed"))
        failed = total - passed
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            # Ana koşuyu kaydet
            cursor.execute("""
                INSERT INTO test_runs (run_uuid, start_time, total_tests, passed_tests, failed_tests)
                VALUES (?, ?, ?, ?, ?)
            """, (run_uuid, datetime.now().isoformat(), total, passed, failed))
            
            # Detayları kaydet
            for res in all_results:
                analysis = res.get("analysis", {})
                is_passed = analysis.get("passed")
                confidence = analysis.get("confidence", "N/A")
                issues = json.dumps(analysis.get("issues", []))
                cursor.execute("""
                    INSERT INTO test_results 
                    (run_uuid, combination_label, scenario_type, scenario_key, test_time, passed, confidence, issues, screenshot_path, baseline_path, diff_path, diff_percent, is_visual_match)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_uuid,
                    res.get("combination", ""),
                    res.get("scenario_type", ""),
                    res.get("scenario_key", ""),
                    res.get("test_time", datetime.now().isoformat()),
                    is_passed,
                    confidence,
                    issues,
                    res.get("analysis", {}).get("screenshot", ""),
                    res.get("analysis", {}).get("baseline", ""),
                    res.get("analysis", {}).get("diff_screenshot", ""),
                    res.get("analysis", {}).get("diff_percent", 0.0),
                    res.get("analysis", {}).get("is_visual_match", True)
                ))
            conn.commit()
            logger.info(f"Test sonuçları veritabanına kaydedildi. (UUID: {run_uuid})")
    except Exception as e:
        logger.error(f"DB Kayıt Hatası: {e}", exc_info=True)

def get_recent_runs(limit=10):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM test_runs ORDER BY id DESC LIMIT ?", (limit,))
        runs = [dict(row) for row in cursor.fetchall()]
        return runs

def get_dashboard_stats():
    init_db()
    stats = {"total_runs": 0, "total_tests": 0, "total_passed": 0, "total_failed": 0, "pass_rate": 0}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) as runs FROM test_runs")
        row = cursor.fetchone()
        if row: stats["total_runs"] = row["runs"]
        
        cursor.execute("SELECT SUM(total_tests) as t_all, SUM(passed_tests) as t_pass, SUM(failed_tests) as t_fail FROM test_runs")
        row = cursor.fetchone()
        if row and row["t_all"] is not None:
            stats["total_tests"] = row["t_all"]
            stats["total_passed"] = row["t_pass"]
            stats["total_failed"] = row["t_fail"]
            
            if stats["total_tests"] > 0:
                stats["pass_rate"] = round((stats["total_passed"] / stats["total_tests"]) * 100)
                
    return stats
