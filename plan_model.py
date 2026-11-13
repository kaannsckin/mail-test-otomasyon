"""
plan_model.py — Test planı (test_plan.yaml): yollar, istemciler, kademeler.

Checklist CSV'si dört etiketi (alan sunucu/istemci, gönderen sunucu/istemci)
çarparak kombinasyon üretiyordu; istemci adı otomasyonda hiçbir şeyi
değiştirmediği için aynı sunucu yolu defalarca koşuyordu. Plan bunu ayırır:

  * **Yol** — gönderen sunucu → alan sunucu. Mesaj yol başına BİR kez gönderilir.
  * **İstemci** — bir sunucudaki posta kutusuna belirli bir protokolle
    (imap / pop3 / activesync) erişen gerçek uygulama. İstemcinin protokolü
    otomatik okunabiliyorsa mesaj o kanaldan da okunup doğrulanır; ekrandaki
    görünüm ise Manuel Doğrulama'da istemci başına sorulur.
  * **Kademe** — duman < regresyon < tam. Hem yollar hem senaryolar kademelidir.
  * **Etki** — "ne güncellendi?" (sunucu ya da istemci anahtarı) → yalnızca
    etkilenen yollar ve istemciler koşar.

Plan, orkestratörün beklediği TestCombination nesnelerine çevrilir; main.py'nin
geri kalanı CSV ile aynı şekilde çalışır.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from csv_parser import SUPPORTED_SCENARIOS, TestCombination, TestScenario
from scenario_catalog import catalog, scenario_label

TIERS = ("duman", "regresyon", "tam")
TIER_LABELS = {"duman": "Duman (hızlı)", "regresyon": "Regresyon", "tam": "Tam"}
# Mesajı otomatik okuyabildiğimiz erişim protokolleri (activesync = Z-Push).
AUTOMATED_ACCESS = ("imap", "pop3", "activesync")
SEND_CHANNELS = ("smtp", "activesync")
KNOWN_ACCESS = ("imap", "pop3", "activesync", "web")
SERVERS = ("ems", "gmail", "outlook")


class PlanError(ValueError):
    """Plan dosyası okunamıyor ya da tutarsız."""


@dataclass
class Client:
    key: str
    name: str
    server: str
    access: str
    critical: bool = False
    version: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "server": self.server,
                "access": self.access, "critical": self.critical, "version": self.version}


@dataclass
class Route:
    name: str
    sender: str
    receiver: str
    tier: str = "tam"
    send_via: str = "smtp"


@dataclass
class Plan:
    clients: dict = field(default_factory=dict)
    routes: list = field(default_factory=list)
    tier_scenarios: dict = field(default_factory=dict)   # kademe → o kademede EKLENEN senaryolar
    client_scenarios: Optional[list] = None              # None → tüm senaryolar istemcide sorulur
    path: str = ""

    # ── sorgular ─────────────────────────────────────────────────────
    def scenarios_for(self, tier: str) -> list:
        """Kademe kümülatiftir: regresyon = duman + regresyon'da eklenenler."""
        out: list = []
        for t in TIERS[:TIERS.index(tier) + 1]:
            out += [s for s in self.tier_scenarios.get(t, []) if s not in out]
        return out

    def clients_on(self, server: str) -> list:
        return [c for c in self.clients.values() if c.server == server]

    def channels_for(self, server: str, clients: Optional[list] = None) -> list:
        """Alıcı sunucuda otomatik okunacak kanallar. IMAP her zaman birincildir
        (sunucudaki mesajın kendisi); istemcilerin kullandığı diğer otomatik
        protokoller ikincil kanal olarak eklenir."""
        channels = ["imap"]
        for client in (self.clients_on(server) if clients is None else clients):
            if client.access in AUTOMATED_ACCESS and client.access not in channels:
                channels.append(client.access)
        return channels

    def impact_keys(self) -> list:
        servers = sorted({r.sender for r in self.routes} | {r.receiver for r in self.routes})
        return servers + list(self.clients)

    # ── kombinasyon üretimi ──────────────────────────────────────────
    def combinations(self, tier: str = "tam", impact: Optional[str] = None) -> list:
        """Seçilen kademe ve etkiye göre çalıştırılacak yolları döndürür."""
        if tier not in TIERS:
            raise PlanError(f"Bilinmeyen kademe: {tier} ({', '.join(TIERS)})")
        only_client = None
        if impact:
            if impact in self.clients:
                only_client = self.clients[impact]
            elif impact not in self.impact_keys():
                raise PlanError(f"Bilinmeyen etki: {impact} — seçenekler: "
                                + ", ".join(self.impact_keys()))
        scenarios = self.scenarios_for(tier)
        combos = []
        for route in self.routes:
            if TIERS.index(route.tier) > TIERS.index(tier):
                continue
            if only_client is not None:
                # İstemci güncellendi: yalnızca o istemcinin posta kutusuna
                # GELEN yollar anlamlı (gönderim tarafı gerçek istemci değil).
                if route.receiver != only_client.server:
                    continue
                clients = [only_client]
            else:
                if impact and impact not in (route.sender, route.receiver):
                    continue
                clients = self.clients_on(route.receiver)
            keys = scenarios
            if only_client is not None and self.client_scenarios is not None:
                keys = [s for s in scenarios if s in self.client_scenarios]
            combos.append(self._combination(route, keys, clients))
        return combos

    def _combination(self, route: Route, scenario_keys: list, clients: list) -> TestCombination:
        channels = self.channels_for(route.receiver, clients)
        combo = TestCombination(
            label=route.name,
            receiver_server=route.receiver,
            receiver_client=", ".join(c.name for c in clients) or "—",
            sender_server=route.sender,
            sender_client="Z-Push" if route.send_via == "activesync" else "SMTP",
        )
        for key in scenario_keys:
            combo.scenarios[key] = TestScenario(
                combination=combo.label, receiver_server=combo.receiver_server,
                receiver_client=combo.receiver_client, sender_server=combo.sender_server,
                sender_client=combo.sender_client,
                scenario_type=scenario_label(key), scenario_key=key)
        # Plan'a özgü alanlar (CSV kombinasyonlarında yoktur → getattr ile okunur)
        combo.receive_channels = channels
        combo.send_channel = route.send_via
        combo.clients = [c.as_dict() for c in clients]
        combo.client_scenarios = self.client_scenarios
        combo.tier = route.tier
        return combo

    def summary(self) -> dict:
        return {
            "tiers": [{"key": t, "label": TIER_LABELS[t],
                       "scenario_count": len(self.scenarios_for(t)),
                       "route_count": sum(1 for r in self.routes
                                          if TIERS.index(r.tier) <= TIERS.index(t))}
                      for t in TIERS],
            "impacts": [{"key": k, "label": self.clients[k].name if k in self.clients else k.upper(),
                         "kind": "client" if k in self.clients else "server"}
                        for k in self.impact_keys()],
            "clients": [c.as_dict() for c in self.clients.values()],
            "routes": [{"name": r.name, "sender": r.sender, "receiver": r.receiver,
                        "tier": r.tier, "send_via": r.send_via,
                        "channels": self.channels_for(r.receiver)}
                       for r in self.routes],
        }


# ────────────────────────────────────────────────────────────────────
#  Yükleme ve doğrulama
# ────────────────────────────────────────────────────────────────────
def _server(value, where: str) -> str:
    key = str(value or "").strip().lower()
    if key not in SERVERS:
        raise PlanError(f"{where}: bilinmeyen sunucu {value!r} ({', '.join(SERVERS)})")
    return key


def _scenario_list(value, where: str) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, str) and value.strip().lower() in ("tumu", "tümü", "all", "*"):
        return [item["key"] for item in catalog()]
    if not isinstance(value, list):
        raise PlanError(f"{where}: senaryo listesi olmalı")
    unknown = [s for s in value if s not in SUPPORTED_SCENARIOS]
    if unknown:
        raise PlanError(f"{where}: bilinmeyen senaryo: {', '.join(map(str, unknown))}")
    return list(dict.fromkeys(value))


def parse_plan(data, path: str = "") -> Plan:
    if not isinstance(data, dict):
        raise PlanError("Plan kökü 'anahtar: değer' yapısında olmalı")
    plan = Plan(path=path)

    for key, raw in (data.get("istemciler") or {}).items():
        if not isinstance(raw, dict):
            raise PlanError(f"istemciler.{key}: sözlük olmalı")
        access = str(raw.get("erisim") or "").strip().lower()
        if access not in KNOWN_ACCESS:
            raise PlanError(f"istemciler.{key}.erisim: {access!r} geçersiz "
                            f"({', '.join(KNOWN_ACCESS)})")
        key = str(key)
        if ":" in key or "," in key:
            raise PlanError(f"istemciler.{key}: anahtarda ':' ve ',' kullanılamaz")
        plan.clients[key] = Client(
            key=key, name=str(raw.get("ad") or key),
            server=_server(raw.get("sunucu"), f"istemciler.{key}.sunucu"),
            access=access, critical=bool(raw.get("kritik")),
            version=str(raw.get("surum") or ""))

    routes = data.get("yollar")
    if not isinstance(routes, list) or not routes:
        raise PlanError("Plan en az bir yol (yollar:) içermeli")
    for i, raw in enumerate(routes, start=1):
        if not isinstance(raw, dict):
            raise PlanError(f"yollar[{i}]: sözlük olmalı")
        sender = _server(raw.get("gonderen"), f"yollar[{i}].gonderen")
        receiver = _server(raw.get("alan"), f"yollar[{i}].alan")
        tier = str(raw.get("kademe") or "tam").strip().lower()
        if tier not in TIERS:
            raise PlanError(f"yollar[{i}].kademe: {tier!r} geçersiz ({', '.join(TIERS)})")
        send_via = str(raw.get("gonderim") or "smtp").strip().lower()
        if send_via not in SEND_CHANNELS:
            raise PlanError(f"yollar[{i}].gonderim: {send_via!r} geçersiz "
                            f"({', '.join(SEND_CHANNELS)})")
        name = str(raw.get("ad") or f"{sender.upper()}"
                   + (" (Z-Push)" if send_via == "activesync" else "")
                   + f" → {receiver.upper()}")
        if any(r.name == name for r in plan.routes):
            raise PlanError(f"yollar[{i}]: yol adı tekrar ediyor: {name}")
        plan.routes.append(Route(name=name, sender=sender, receiver=receiver, tier=tier,
                                 send_via=send_via))

    tiers = data.get("kademeler") or {}
    if not isinstance(tiers, dict):
        raise PlanError("kademeler: sözlük olmalı")
    for tier in tiers:
        if tier not in TIERS:
            raise PlanError(f"kademeler.{tier}: bilinmeyen kademe ({', '.join(TIERS)})")
    for tier in TIERS:
        plan.tier_scenarios[tier] = _scenario_list(tiers.get(tier), f"kademeler.{tier}")
    if not tiers:
        plan.tier_scenarios["tam"] = [item["key"] for item in catalog()]
    if not plan.scenarios_for("tam"):
        raise PlanError("kademeler altında hiç senaryo tanımlı değil")

    if data.get("istemci_senaryolari") is not None:
        plan.client_scenarios = _scenario_list(data["istemci_senaryolari"], "istemci_senaryolari")
    return plan


def load_plan(path) -> Plan:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise PlanError(f"Plan dosyası okunamadı ({path}): {e}") from e
    except yaml.YAMLError as e:
        raise PlanError(f"Plan dosyası geçerli YAML değil ({path}): {e}") from e
    return parse_plan(data, str(path))


def resolve_plan_path(raw: str) -> Optional[Path]:
    """Göreli plan yolunu çalışma dizini ve repo köküne göre çözer."""
    if not raw:
        return None
    p = Path(str(raw))
    for candidate in ([p] if p.is_absolute() else [Path.cwd() / p, Path(__file__).parent / p]):
        if candidate.is_file():
            return candidate
    return None
