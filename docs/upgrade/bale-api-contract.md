# Bale SDK Contract — verified against python-bale-bot v2.4.5 API reference

Source: https://docs.python-bale-bot.ir/en/v2.4.5/api.html (fetched 2026-11-04).
This is the **binding** method list; the adapter may only use what appears here
(cross-checked against the installed package source at implementation time).

## Verified Bot methods

`add_event`, `ban_chat_member`, `close`, `delete_message`, `delete_webhook`,
`edit_message(chat_id, message_id, text, *, components=None)`,
`forward_message`, `get_bot`, `get_chat`, `get_chat_administrators`,
`get_chat_member`, `get_chat_members_count`, `get_file`, `get_user`,
`invite_user`, `is_closed`, `leave_chat`, `listen(event_name)`,
`add_event(event, function)`, `remove_event`, `wait_for(event_name, *, check,
timeout)`, `on_error(event_name, error)`, `run(sleep_after_every_get_updates)`,
`send_message(chat_id, text, *, components=None, reply_to_message_id=None)`,
`send_document/send_photo/send_video (chat_id, file, *, caption=None,
reply_to_message_id=None)`, `send_location`, `send_contact`, `send_invoice`.

## Key constraints that shape the adapter

1. **No Mini App / WebApp support.** `bale.InlineKeyboard` fields are exactly
   `text`, `callback_data`, `url`, `switch_inline_query`,
   `switch_inline_query_current_chat`. There is no `web_app` field and no
   init-data/signature contract anywhere in the API. Therefore:
   - The shipped integration is the **URL-button fallback**: "Open Management
     App" → `InlineKeyboard(text=..., url=TLA_PUBLIC_URL)` — opens the
     responsive web app in Bale's in-app browser.
   - Identity binding uses **one-time link codes** (created from the web
     Security Center / API, consumed in the `/link` bot conversation), never
     client-asserted user IDs.
2. **Message limit:** `text` max **4096 characters** — adapters must truncate.
3. **Menus:** `Components(inline_keyboards=[InlineKeyboard(...)])` passed as
   `components=` to `send_message`; updates via `edit_message`.
4. **Events:** `EventType.READY/BEFORE_READY/UPDATE/MESSAGE/EDITED_MESSAGE/
   CALLBACK/...`; handler signature `on_update(update)`, `on_message(message)`,
   `on_callback(callback)`. `Update` carries `message`, `edited_message`,
   `callback_query`; `CallbackQuery` carries `callback_id`, `from_user`,
   `message`, `data` (+ `user` alias).
5. **Exceptions to contain:** `BaleError`, `InvalidToken`, `APIError`,
   `NetworkError`, `TimeOut`, `NotFound`, `Forbidden`, `RateLimited`,
   `HTTPException`. The lifecycle must return `{"bale": False}` on
   `InvalidToken` and never leak exceptions into the platform loop.
6. `Bot.run()` is a blocking convenience entry; the platform runtime drives
   the update loop itself (async) and calls `Bot.close()` on shutdown.

## Mini App verdict (for reviewers)

No native Bale Mini App mechanism is verifiable in the v2.4.5 SDK contract.
Claims of native support would be untestable; per the security model we ship
the documented URL fallback and the code-based account link. If Bale ships a
signed init-data contract later, `src/adapters/bale/miniapp.py` is the single
place to add verification.
