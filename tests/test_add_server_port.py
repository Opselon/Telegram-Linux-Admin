"""Regression tests for SSH port configuration in the add_server conversation and database."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src import database
from src.bot import servers, state
from src.bot.state import PORT, USER
from telegram import Update, User, Chat, Message


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Sets up an isolated database for every test."""
    db_file = tmp_path / "test_port.db"
    key_file = tmp_path / "key.json"
    monkeypatch.setenv("TLA_DB_FILE", str(db_file))
    monkeypatch.setenv("TLA_ENCRYPTION_KEY_FILE", str(key_file))
    database.close_db_connection()
    database.initialize_database()
    yield
    database.close_db_connection()


def test_database_add_and_update_server_with_custom_port():
    """add_server stores the port, defaulting to 22, and update_server modifies it."""
    # Default port
    database.add_server(1001, "default-port", "10.0.0.1", "root", password="pw")
    srv = database.get_server(1001, "default-port")
    assert srv is not None
    assert srv["port"] == 22

    # Custom port
    database.add_server(1001, "custom-port", "10.0.0.2", "root", password="pw", port=2222)
    srv = database.get_server(1001, "custom-port")
    assert srv is not None
    assert srv["port"] == 2222

    # Update port
    ok = database.update_server(1001, "default-port", port=2200)
    assert ok is True
    srv = database.get_server(1001, "default-port")
    assert srv is not None
    assert srv["port"] == 2200


@pytest.mark.asyncio
async def test_get_port_valid_custom_number():
    """Entering a valid port number sets context.user_data['port'] and transitions to USER."""
    update = MagicMock(spec=Update)
    user = User(id=1001, is_bot=False, first_name="Test")
    chat = Chat(id=1001, type="private")
    message = MagicMock(spec=Message)
    message.text = "2222"
    message.chat = chat
    update.effective_user = user
    update.effective_message = message
    update.message = message

    context = MagicMock()
    context.user_data = {"alias": "myserver", "hostname": "1.2.3.4"}

    with patch("src.bot.servers._send_message_safely", new_callable=AsyncMock) as mock_send:
        next_state = await servers.get_port(update, context)

    assert next_state == USER
    assert context.user_data["port"] == 2222
    assert mock_send.called
    assert context.user_data["alias"] == "myserver"
    assert context.user_data["hostname"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_port_empty_or_whitespace_defaults_to_22():
    """Sending empty text defaults to 22 and proceeds to USER."""
    update = MagicMock(spec=Update)
    user = User(id=1001, is_bot=False, first_name="Test")
    chat = Chat(id=1001, type="private")
    message = MagicMock(spec=Message)
    message.text = "   "
    message.chat = chat
    update.effective_user = user
    update.effective_message = message
    update.message = message

    context = MagicMock()
    context.user_data = {"alias": "myserver", "hostname": "1.2.3.4"}

    with patch("src.bot.servers._send_message_safely", new_callable=AsyncMock) as mock_send:
        next_state = await servers.get_port(update, context)

    assert next_state == USER
    assert context.user_data["port"] == 22
    assert mock_send.called


@pytest.mark.asyncio
async def test_get_port_invalid_input_reprompts_and_stays_in_port_state():
    """Invalid input (letters or out-of-range) re-prompts and remains in PORT state."""
    update = MagicMock(spec=Update)
    user = User(id=1001, is_bot=False, first_name="Test")
    chat = Chat(id=1001, type="private")
    message = MagicMock(spec=Message)
    message.text = "not_a_port"
    message.chat = chat
    update.effective_user = user
    update.effective_message = message
    update.message = message

    context = MagicMock()
    context.user_data = {"alias": "myserver", "hostname": "1.2.3.4"}

    with patch("src.bot.servers._send_message_safely", new_callable=AsyncMock) as mock_send:
        next_state = await servers.get_port(update, context)

    assert next_state == PORT
    assert "port" not in context.user_data
    # Context data is preserved
    assert context.user_data["alias"] == "myserver"
    assert context.user_data["hostname"] == "1.2.3.4"
    assert mock_send.called


@pytest.mark.asyncio
async def test_save_server_records_custom_port():
    """save_server reads port from user_data and passes it to add_server."""
    update = MagicMock(spec=Update)
    user = User(id=1001, is_bot=False, first_name="Test")
    chat = Chat(id=1001, type="private")
    message = MagicMock(spec=Message)
    message.chat = chat
    update.effective_user = user
    update.effective_message = message
    update.message = message

    context = MagicMock()
    context.user_data = {
        "alias": "srv1",
        "hostname": "192.168.1.50",
        "user": "ubuntu",
        "password": "secretpassword",
        "port": 22022,
    }

    with patch("src.bot.servers._send_message_safely", new_callable=AsyncMock):
        await servers.save_server(update, context)

    saved = database.get_server(1001, "srv1")
    assert saved is not None
    assert saved["port"] == 22022
    assert saved["hostname"] == "192.168.1.50"
    assert saved["user"] == "ubuntu"
