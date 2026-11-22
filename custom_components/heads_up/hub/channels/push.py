"""Push channel: Companion app notifications through the legacy notify services (DESIGN §23.8).

Only `notify.mobile_app_<device>` forwards `data` (tag, actions, clear_notification); the notify
entity does not (N-61). One tag per message, so a device replaces instead of stacking and a clear
reaches every device (N-62). The service is worked out from the registration's current
`device_name` each time (N-78). Presses come back as `mobile_app_notification_action` events whose
button id carries message, recipient and action; nothing else in the event is trusted (N-79).
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from ...const import (
    DEFAULT_OPTIONS,
    OPT_INSTALL_TOKEN,
    OPT_PUSH_CHANNEL_ID,
    OPT_PUSH_LEVELS,
    OPT_PUSH_LOCKSCREEN,
    OPT_PUSH_STICKY,
)
from ...models import Banner
from ..recipients import notify_service, registration_data
from .base import Capabilities, Outcome, Result, Target

if TYPE_CHECKING:
    from ...manager import BannerManager
    from ..engine import HubEngine

_LOGGER = logging.getLogger(__name__)
ACTION_EVENT = "mobile_app_notification_action"
ID_PREFIX = "hu"
OWN_PREFIX = "b-"  # keeps a message's own button keys apart from dismiss, snooze and ack
MAX_BUTTONS = {"android": 3, "ios": 10}  # N-62
WARN_EVERY = 3600  # seconds between log warnings per device
IMPORTANCE = {"passive": "low", "normal": "default", "time-sensitive": "high", "critical": "max"}
# The Android counterpart of the iOS critical alert: a channel that plays from the alarm stream, so
# it sounds in vibrate and silent mode (Companion critical notifications, N-96). Do Not Disturb is
# overridden only if the user allows it for the channel in the Android settings.
ANDROID_ALARM_CHANNEL = "alarm_stream"

# Text the hub itself puts on a notification, in the languages of the integration.
TEXT = {
    "dismiss": {"en": "Dismiss", "de": "Schließen", "fr": "Fermer", "es": "Descartar"},
    "snooze": {"en": "Snooze", "de": "Später", "fr": "Reporter", "es": "Posponer"},
    "ack": {"en": "Acknowledge", "de": "Bestätigen", "fr": "Confirmer", "es": "Confirmar"},
    "tap_to_read": {
        "en": "Open Home Assistant to read this message.",
        "de": "Öffne Home Assistant, um die Nachricht zu lesen.",
        "fr": "Ouvrez Home Assistant pour lire ce message.",
        "es": "Abre Home Assistant para leer este mensaje.",
    },
    "new_message": {
        "en": "New message",
        "de": "Neue Nachricht",
        "fr": "Nouveau message",
        "es": "Nuevo mensaje",
    },
}


def button_id(message_id: str, recipient: str, action: str) -> str:
    return f"{ID_PREFIX}:{message_id}:{recipient}:{action}"


def parse_button_id(value: str) -> tuple[str, str, str] | None:
    """(message, recipient, action) of a hub button id, or None for any other action."""
    parts = value.split(":")
    if len(parts) < 4 or parts[0] != ID_PREFIX:
        return None
    return parts[1], ":".join(parts[2:-1]), parts[-1]


def tag_for(token: str, message_id: str) -> str:
    return f"hu-{token}-{message_id}"


class PushChannel:
    name = "push"
    available = True
    capabilities = Capabilities(title=True, buttons=True, clear=True, replace=True, per_user=True)

    def __init__(self, hass: HomeAssistant, hub: HubEngine, manager: BannerManager) -> None:
        self._hass = hass
        self._hub = hub
        self._manager = manager
        self._warned: dict[str, float] = {}
        self._unsub = hass.bus.async_listen(ACTION_EVENT, self._on_action)

    def stop(self) -> None:
        self._unsub()

    # Settings ---------------------------------------------------------------------------

    def _opt(self, key: str) -> Any:
        return self._manager.options.get(key, DEFAULT_OPTIONS[key])

    def _token(self) -> str:
        return str(self._manager.options.get(OPT_INSTALL_TOKEN) or "local")

    def _text(self, key: str) -> str:
        table = TEXT[key]
        return table.get(self._hass.config.language, table["en"])

    # Payload ----------------------------------------------------------------------------

    def level_for(self, message: Banner, target: Target) -> str:
        if target.quiet:
            return "passive"
        return str(self._opt(OPT_PUSH_LEVELS).get(message.severity, "normal"))

    def payload(self, message: Banner, target: Target, platform: str) -> tuple[dict[str, Any], str]:
        """The `notify` call data for one device, and a note when something was left out."""
        ios = platform == "ios"
        level = self.level_for(message, target)
        token = self._token()
        data: dict[str, Any] = {
            "tag": tag_for(token, message.id),
            "group": f"hu-{token}",
            "channel": f"{self._opt(OPT_PUSH_CHANNEL_ID)}_{level.replace('-', '_')}",
            "importance": IMPORTANCE[level],
            "push": {"interruption-level": level},
        }
        if level == "critical" and ios:
            data["push"]["sound"] = {"name": "default", "critical": 1, "volume": 1.0}
        if not ios and level in ("time-sensitive", "critical"):
            # Without these an idle Android phone may delay the notification (N-96).
            data["priority"] = "high"
            data["ttl"] = 0
        if not ios and level == "critical":
            data["channel"] = ANDROID_ALARM_CHANNEL
        if self._opt(OPT_PUSH_STICKY):
            data["sticky"] = "true"
        if uri := _click_target(message.tap_action):
            data["clickAction"] = uri
            data["url"] = uri
        actions, note = self._buttons(message, target, platform)
        if actions:
            data["actions"] = actions
        if message.image:
            # Android and iOS both read `image` (Companion docs, N-93). A picture on the lock
            # screen would defeat the lock screen setting, so it is left out unless it is `full`.
            if self._opt(OPT_PUSH_LOCKSCREEN) == "full":
                data["image"] = message.image
            else:
                note = ";".join(n for n in (note, "image_hidden") if n)
        title, body = self._lockscreen(message)
        if message.progress is not None and self._opt(OPT_PUSH_LOCKSCREEN) == "full":
            # https://companion.home-assistant.io/docs/notifications/live-activities/
            # Android requires a title. Keep activities apart from the ordinary push group.
            title = title or "Heads Up"
            data.pop("group")
            # Companion activity tags accept at most 64 ASCII letters, digits, "-" or "_".
            # Hash the installation-scoped tag so even the longest message ID fits.
            activity_id = hashlib.sha256(data["tag"].encode()).hexdigest()[:32]
            data.update(
                tag=f"hu_live_{activity_id}",
                live_update=True,
                progress=message.progress,
                progress_max=100,
                alert_once=True,
            )
        out: dict[str, Any] = {"message": body, "data": data}
        if title:
            out["title"] = title
        return out, note

    def _lockscreen(self, message: Banner) -> tuple[str | None, str]:
        mode = self._opt(OPT_PUSH_LOCKSCREEN)
        if mode == "generic":
            return "Heads Up", self._text("new_message")
        if mode == "title_only":
            return message.title or "Heads Up", self._text("tap_to_read")
        return message.title, message.message

    def _buttons(
        self, message: Banner, target: Target, platform: str
    ) -> tuple[list[dict[str, Any]], str]:
        who = target.person or target.key
        own: list[dict[str, Any]] = []
        for b in message.actions:
            button: dict[str, Any] = {
                "action": button_id(message.id, who, OWN_PREFIX + b["key"]),
                "title": b["label"],
            }
            # N-97: both are booleans of the Companion action object.
            if b.get("destructive"):
                button["destructive"] = True
            if b.get("require_auth"):
                button["authenticationRequired"] = True
            own.append(button)
        extra: list[dict[str, Any]] = []
        if target.ack_button:
            extra.append({"action": button_id(message.id, who, "ack"), "title": self._text("ack")})
        if message.snoozable:
            extra.append(
                {"action": button_id(message.id, who, "snooze"), "title": self._text("snooze")}
            )
        if message.dismissible:
            extra.append(
                {"action": button_id(message.id, who, "dismiss"), "title": self._text("dismiss")}
            )
        buttons = own + extra
        limit = MAX_BUTTONS.get(platform, MAX_BUTTONS["android"])
        if len(buttons) <= limit:
            return buttons, ""

        # Too many for the platform: keep what a household needs most. Acknowledge and Dismiss
        # first, then the message's own buttons in order, Snooze last; the kept ones stay in
        # display order.
        def keep_rank(button: dict[str, Any]) -> int:
            kind = button["action"].rsplit(":", 1)[-1]
            ranks = {"ack": 0, "dismiss": 1, "snooze": 10_000}
            if kind in ranks:
                return ranks[kind]
            return 2 + own.index(button)

        kept = sorted(buttons, key=keep_rank)[:limit]
        return [b for b in buttons if b in kept], f"buttons_clipped:{len(buttons) - limit}"

    # Delivery ---------------------------------------------------------------------------

    async def deliver(self, message: Banner, target: Target) -> Outcome:
        return await self._deliver(message, target, update=False)

    async def update(self, message: Banner, target: Target) -> Outcome:
        return await self._deliver(message, target, update=True)

    async def _deliver(self, message: Banner, target: Target, *, update: bool) -> Outcome:
        registration = self._registration(target)
        if registration is None:
            return Outcome(Result.FAILED, "device_missing")
        platform = str(registration.get("os_name", "")).lower()
        platform = "ios" if platform == "ios" else "android"
        service = notify_service(str(registration.get("device_name", "")))
        payload, note = self.payload(message, target, platform)
        # A normal notification and an activity can coexist on iOS. On a mode change,
        # retire the previous presentation using the tag recorded for this device.
        previous = self._hub.ledger.rows.get((message.id, self.name, target.key))
        previous_tag = previous.ref if previous else None
        new_tag = payload["data"]["tag"]
        # iOS critical ordinary notifications also need a clear before replacement (N-62).
        critical_replace = (
            update
            and platform == "ios"
            and not payload["data"].get("live_update")
            and self.level_for(message, target) == "critical"
        )
        if (previous_tag and previous_tag != new_tag) or critical_replace:
            cleared = await self.clear(message.id, target, previous_tag or new_tag)
            if cleared.result is not Result.SENT:
                return cleared
        outcome = await self._call(target, service, payload)
        if outcome.result is Result.SENT:
            return Outcome(Result.SENT, note or None, ref=payload["data"]["tag"])
        return outcome

    async def clear(self, message_id: str, target: Target, ref: str | None) -> Outcome:
        registration = self._registration(target)
        if registration is None:
            return Outcome(Result.FAILED, "device_missing")
        token = self._token()
        service = notify_service(str(registration.get("device_name", "")))
        payload = {
            "message": "clear_notification",
            "data": {"tag": ref or tag_for(token, message_id)},
        }
        return await self._call(target, service, payload)

    def _registration(self, target: Target) -> dict[str, Any] | None:
        device_id = target.key.removeprefix("device:")
        data = registration_data(self._hass, device_id)
        return dict(data) if data is not None else None

    async def _call(self, target: Target, service: str, payload: dict[str, Any]) -> Outcome:
        try:
            await self._hass.services.async_call("notify", service, payload, blocking=True)
        except ServiceNotFound:
            self._warn(target.key, "notify.%s does not exist", service)
            return Outcome(Result.FAILED, "service_not_found")
        except HomeAssistantError as err:
            self._warn(target.key, "Push through notify.%s failed: %s", service, err)
            return Outcome(Result.FAILED, "send_failed")
        return Outcome(Result.SENT)

    def _warn(self, key: str, message: str, *args: object) -> None:
        # One warning per device per hour; the trace keeps every failure.
        now = time.monotonic()
        if now - self._warned.get(key, -WARN_EVERY) >= WARN_EVERY:
            self._warned[key] = now
            _LOGGER.warning(message, *args)

    # Presses ----------------------------------------------------------------------------

    @callback
    def _on_action(self, event: Event[Any]) -> None:
        parsed = parse_button_id(str(event.data.get("action", "")))
        if parsed is None:
            return  # an action of some other automation
        message_id, recipient, action = parsed
        outcome = self._press(message_id, recipient, action, event.context.user_id)
        self._hub.note(message_id, "push", recipient, "press", outcome)

    def _press(self, message_id: str, recipient: str, action: str, event_user: str | None) -> str:
        manager = self._manager
        banner = manager.banners.get(message_id)
        if banner is None:
            return "stale_message"
        if not any(
            r.state.value == "sent" and recipient in (r.person, r.recipient)
            for r in self._hub.ledger.for_message(message_id)
            if r.channel == "push"
        ):
            return "unknown_recipient"
        # The id names the recipient, so the press counts for their user. A user on the event is
        # a cross-check only (N-72, D-110).
        person_user = self._hub.resolver.user_of_person(recipient) if "." in recipient else None
        if person_user and event_user and person_user != event_user:
            return "user_mismatch"
        user_id = person_user or event_user
        if user_id is None:
            return "no_user"
        from ...manager import HeadsUpError

        try:
            if action == "dismiss":
                manager.dismiss(message_id, user_id)
            elif action == "snooze":
                manager.snooze(message_id, user_id, manager.quick_snooze_seconds)
            elif action == "ack":
                manager.acknowledge(message_id, user_id)
            elif action.startswith(OWN_PREFIX):
                manager.press(message_id, user_id, action.removeprefix(OWN_PREFIX), "push")
            else:
                return "unknown_action"
        except HeadsUpError as err:
            return err.code
        return "ok"


def _click_target(tap_action: dict[str, Any] | None) -> str | None:
    """A path or URL the notification opens, from a navigate or url tap action."""
    if not tap_action:
        return None
    if tap_action.get("action") == "navigate":
        return tap_action.get("navigation_path")
    if tap_action.get("action") == "url":
        return tap_action.get("url_path")
    return None
