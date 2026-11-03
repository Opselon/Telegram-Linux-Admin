"""Shared fixtures and fakes for the Bale adapter tests.

The Bale HTTP/SDK layer is never touched: ``FakeBot`` records sends/edits and
real ``bale.Message``/``bale.CallbackQuery``/``bale.User`` objects are built by
hand (they are plain data objects in the SDK).
"""

from __future__ import annotations

import asyncio
import datetime
import itertools
from types import SimpleNamespace

import bale
import pytest

from src.services import build_services
from src.services import store
from src.jobs.models import JobSpec
from src.services.migrations import run_migrations


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeBot:
    """Records outbound traffic; replaces the SDK transport in tests."""

    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []
        self.edits: list[SimpleNamespace] = []
        self.events: dict[str, object] = {}

    async def send_message(self, chat_id, text, components=None, **kwargs):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text,
                                         components=components))
        return SimpleNamespace(chat_id=chat_id, message_id=str(len(self.sent) + 100))

    async def edit_message(self, chat_id, message_id, text, components=None):
        self.edits.append(SimpleNamespace(chat_id=chat_id, message_id=message_id,
                                          text=text, components=components))
        return SimpleNamespace(chat_id=chat_id, message_id=message_id)

    def event(self, coro):
        self.events[coro.__name__] = coro
        return coro

    def listen(self, event_name):
        def wrapper(func):
            self.events[event_name] = func
            return func
        return wrapper

    # -- assertions helpers ----------------------------------------------------
    def texts(self) -> list[str]:
        return [item.text for item in self.sent]

    def edit_texts(self) -> list[str]:
        return [item.text for item in self.edits]

    def last_text(self) -> str:
        return self.sent[-1].text if self.sent else ""

    def last_components(self):
        return self.sent[-1].components if self.sent else None


class FakeManager:
    """Stands in for SSHManager.execute so no network/SSH is touched."""

    def __init__(self, stdout: str = "ok", error: Exception | None = None) -> None:
        self.stdout = stdout
        self.error = error
        self.calls: list[tuple] = []

    async def execute(self, user_id, alias, command, timeout=60):
        self.calls.append((user_id, alias, command, timeout))
        if self.error is not None:
            raise self.error
        return (self.stdout, "", 0)


class FakeLifecycleBot:
    """A fake ``bale.Bot`` for lifecycle tests (no HTTP client)."""

    def __init__(self, token: str, *, fail_me: bool = False,
                 crash: bool = False) -> None:
        self.token = token
        self.fail_me = fail_me
        self.crash = crash
        self.closed = False
        self.registered: list[str] = []
        self._running = False
        self.started = asyncio.Event()
        self.updater = SimpleNamespace(start=self._updater_start,
                                       stop=self._updater_stop,
                                       interval=None, running=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return None

    async def get_me(self):
        if self.fail_me:
            import bale as _bale
            raise _bale.InvalidToken("invalid token")
        return SimpleNamespace(user_id="999000")

    async def _updater_start(self):
        if self.crash:
            raise RuntimeError("polling loop exploded")
        self._running = True
        self.started.set()
        while self._running:
            await asyncio.sleep(0.01)

    async def _updater_stop(self):
        self._running = False

    async def close(self):
        self.closed = True
        self._running = False

    def event(self, coro):
        self.registered.append(coro.__name__)
        return coro


# ---------------------------------------------------------------------------
# bale object factories
# ---------------------------------------------------------------------------

_ids = itertools.count(1)


def make_user(user_id: int = 100200, first_name: str = "Bale") -> bale.User:
    return bale.User(user_id, False, first_name)


def make_message(text: str, user_id: int = 100200,
                 chat_id: int | None = None) -> bale.Message:
    chat_id = user_id if chat_id is None else chat_id
    return bale.Message(
        message_id=str(next(_ids)),
        date=datetime.datetime.now(),
        text=text,
        caption=None,
        forward_from=None,
        forward_from_chat=None,
        forward_from_message_id=None,
        from_user=make_user(user_id),
        document=None,
        contact=None,
        edit_date=None,
        location=None,
        chat=bale.Chat(chat_id, bale.Chat.PRIVATE, None, None, None, None,
                       None, None),
        video=None,
        photos=None,
        sticker=None,
        reply_to_message=None,
        invoice=None,
        audio=None,
        successful_payment=None,
        animation=None,
        new_chat_members=None,
        left_chat_member=None,
    )


def make_callback(data: str, user_id: int = 100200,
                  message: bale.Message | None = None) -> bale.CallbackQuery:
    return bale.CallbackQuery(id=str(next(_ids)), from_user=make_user(user_id),
                              data=data, message=message, inline_message_id=None)


def callback_data_of(markup) -> list[str]:
    if markup is None:
        return []
    return [item.item.callback_data for item in markup.keyboards
            if getattr(item.item, "callback_data", None)]


# ---------------------------------------------------------------------------
# service graph helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def services(tmp_path, monkeypatch):
    """A fresh service graph over a throwaway database (same shape as
    tests/test_services_core.py)."""
    monkeypatch.setenv("TLA_DB_FILE", str(tmp_path / "bale.db"))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(tmp_path / "keys.key"))
    monkeypatch.setenv("TLA_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("TLA_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("TLA_ACCESS_MODE", "open")
    from src import database

    database.close_db_connection()
    run_migrations(snapshot=False)
    yield build_services()
    database.close_db_connection()


def bind_account(services, bale_user_id: str = "100200", role: str = "owner",
                 username: str | None = None):
    """Creates a platform account and binds a Bale user to it."""
    username = username or f"web:{role}:{bale_user_id}"
    account = store.ensure_account(store.KIND_WEB, username,
                                   display_name=username.split(":")[-1],
                                   role=role)
    store.bind_bale_user(bale_user_id, account.id, "Bale")
    return account


def make_adapter(bot: FakeBot | None = None, services=None):
    """A BaleAdapter with fast job polling (no 1.5 s of test sleeps)."""
    from src.adapters.bale.handlers import BaleAdapter

    adapter = BaleAdapter(bot if bot is not None else FakeBot(), services)
    adapter.JOB_POLL_DELAYS = (0.0, 0.0)
    return adapter


def install_fake_manager(services, stdout: str = "ok",
                         error: Exception | None = None) -> FakeManager:
    fake = FakeManager(stdout=stdout, error=error)
    services.manager = fake
    services.servers.manager = fake
    services.commands.manager = fake
    return fake


def create_server(services, actor, alias: str = "web1") -> dict:
    """Creates a server the actor may see.

    Actors holding ``servers.create`` create their own.  Otherwise (read_only,
    operator without create) this models an admin grant: an owner creates the
    server and ownership is transferred to the actor's account — the same
    ``owner_account_id`` the service layer checks.
    """
    if actor.has("servers.create"):
        return services.servers.create_server(actor, alias=alias, hostname="h",
                                              user="u")
    from src import database

    grantor = store.ensure_account(store.KIND_WEB, f"web:grantor:{alias}",
                                   role="owner")
    server = services.servers.create_server(store.actor_from_account(grantor),
                                            alias=alias, hostname="h", user="u")
    with database.transaction() as conn:
        conn.execute(
            "UPDATE servers SET owner_account_id = ?, owner_id = ? WHERE id = ?",
            (actor.account_id, actor.owner_id, server["id"]))
    return server


async def submit_hanging_job(services, actor):
    """A queued job that stays active (for /jobs and cancel tests)."""

    async def _runner(spec: JobSpec) -> str:
        await asyncio.sleep(30)
        return ""

    spec = JobSpec(user_id=actor.owner_id, alias="web1", command="sleep 30")
    return await services.queue.submit_nowait(spec, _runner)
