"""Этап 9: деплой.

Здесь проверяется то, что можно проверить без VPS: алерт о молчании Telegram,
конфигурация Caddy и compose, отсутствие секретов в образах.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from app.jobs import Jobs
from app.repo import Repo
from tests.conftest import OWNER_ID, TEST_SCHEMA, make_settings

S = TEST_SCHEMA
ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakePoller:
    """Поллер, у которого Telegram молчит столько, сколько скажет тест."""

    def __init__(self, silent_seconds: float) -> None:
        self.silent_seconds = silent_seconds

    def seconds_since_success(self) -> float:
        return self.silent_seconds


@pytest.fixture
async def jobs_with_poller(database, db_admin, fake_tg):
    from app.db import Database
    from app.telegram import TelegramClient

    _state, tg_server = fake_tg
    db = Database(database.bot_url, database.schema, min_size=1, max_size=2)
    await db.connect()
    settings = make_settings(tg_server.base_url, tg_server.base_url, database,
                             poller_silence_minutes=5.0)
    tg = TelegramClient(settings.telegram_bot_token, tg_server.base_url, 1)
    poller = FakePoller(0.0)
    try:
        yield Jobs(settings, Repo(db), tg, poller), poller
    finally:
        await tg.aclose()
        await db.close()


async def test_owner_is_alerted_once_when_telegram_goes_silent(jobs_with_poller, fake_tg, db_admin):
    jobs, poller = jobs_with_poller
    state, _tg = fake_tg

    assert (await jobs.tick()).poller_alert is False  # связь есть, молчим

    poller.silent_seconds = 6 * 60  # шесть минут при пороге пять
    assert (await jobs.tick()).poller_alert is True
    assert "Telegram не отвечает боту больше 5 мин" in state.texts_to(OWNER_ID)[-1]
    assert await db_admin.fetchval(f"select count(*) from {S}.events where type = 'poller_silent'") == 1

    # Второй виток при том же молчании второй раз не пишет.
    assert (await jobs.tick()).poller_alert is False
    assert len(state.texts_to(OWNER_ID)) == 1

    poller.silent_seconds = 1.0  # связь вернулась
    assert (await jobs.tick()).poller_alert is True
    assert "восстановилась" in state.texts_to(OWNER_ID)[-1]

    # И снова молчание: алерт приходит заново.
    poller.silent_seconds = 6 * 60
    assert (await jobs.tick()).poller_alert is True
    assert len(state.texts_to(OWNER_ID)) == 3


def test_compose_exposes_only_caddy():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    # Порты на хост есть только у Caddy: бот и кабинет живут во внутренней сети.
    ports = re.findall(r'^\s+- "(\d+):(\d+)"', compose, flags=re.MULTILINE)
    assert sorted(ports) == [("443", "443"), ("80", "80")]
    assert "restart: always" in compose
    for service in ("bot:", "cabinet:", "caddy:"):
        assert service in compose


def test_local_compose_stays_on_this_machine():
    compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")

    # Без Caddy и домена, порты только на loopback: кабинет не виден даже
    # соседям по Wi-Fi.
    services = re.findall(r"^  (\w+):$", compose, flags=re.MULTILINE)
    assert services == ["bot", "cabinet"]
    ports = re.findall(r'^\s+- "([^"]+)"', compose, flags=re.MULTILINE)
    assert sorted(ports) == ["127.0.0.1:3000:3000", "127.0.0.1:8000:8000"]
    # По http Secure-cookie не сохраняется, а проверка источника сверяет Origin
    # с cabinet_url: без этих двух строк локальный вход не работает.
    assert 'CABINET_COOKIE_SECURE: "false"' in compose
    assert "CABINET_URL: http://localhost:3000" in compose


def test_caddy_routes_api_to_bot_and_hides_healthz():
    caddyfile = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "handle /api/*" in caddyfile
    assert "reverse_proxy bot:8000" in caddyfile
    assert "reverse_proxy cabinet:3000" in caddyfile
    # Healthcheck для Docker, наружу его не отдаём.
    assert "handle /healthz" in caddyfile and "respond 404" in caddyfile
    # CVE-2025-55182: RCE через Server Actions. Кабинет их не использует,
    # поэтому запросы с Next-Action режутся на входе, а не только версией Next.
    assert "header Next-Action *" in caddyfile
    assert "method POST PUT PATCH DELETE" in caddyfile
    for header in ("Strict-Transport-Security", "X-Content-Type-Options nosniff",
                   "X-Frame-Options DENY", "Referrer-Policy"):
        assert header in caddyfile


def test_next_version_is_patched_against_server_actions_rce():
    """Next младше 15.5.7 уязвим к CVE-2025-55182 без аутентификации."""
    package = json.loads((ROOT / "cabinet" / "package.json").read_text(encoding="utf-8"))
    version = package["dependencies"]["next"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "версия закреплена точно, без ^ и ~"
    major, minor, patch = (int(part) for part in version.split("."))
    assert (major, minor, patch) >= (15, 5, 7)


def test_images_do_not_bake_secrets_or_knowledge():
    bot_dockerfile = (ROOT / "bot" / "Dockerfile").read_text(encoding="utf-8")
    cabinet_dockerfile = (ROOT / "cabinet" / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    # .env и база знаний в образ не попадают: первое секреты, второе меняется
    # без пересборки и монтируется томом.
    assert ".env" in dockerignore
    for dockerfile in (bot_dockerfile, cabinet_dockerfile):
        copies = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
        assert not any(".env" in line for line in copies)
        assert not any("knowledge" in line for line in copies)
    # Оба контейнера не под root.
    assert "USER app" in bot_dockerfile
    assert "USER cabinet" in cabinet_dockerfile
