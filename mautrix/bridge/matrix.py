# Copyright (c) 2021 Tulir Asokan
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from typing import Tuple, Optional, Union, TYPE_CHECKING
import logging
import asyncio
import time

from mautrix.types import (EventID, RoomID, UserID, Event, EventType, MessageEvent, MessageType,
                           MessageEventContent, StateEvent, Membership, MemberStateEventContent,
                           PresenceEvent, TypingEvent, ReceiptEvent, TextMessageEventContent,
                           EncryptedEvent, ReceiptType, SingleReceiptEventContent, StateUnsigned,
                           MediaRepoConfig)
from mautrix.errors import IntentError, MatrixError, MForbidden, DecryptionError, SessionNotFound
from mautrix.appservice import AppService
from mautrix.util import markdown
from mautrix.util.logging import TraceLogger
from mautrix.util.opt_prometheus import Histogram
from mautrix.util.message_send_checkpoint import (
    CHECKPOINT_TYPES,
    MessageSendCheckpoint,
    MessageSendCheckpointReportedBy,
    MessageSendCheckpointStatus,
    MessageSendCheckpointStep,
)

from .commands import CommandProcessor

if TYPE_CHECKING:
    from .config import BaseBridgeConfig
    from .user import BaseUser
    from .portal import BasePortal
    from .puppet import BasePuppet
    from .bridge import Bridge

try:
    from .e2ee import EncryptionManager
except ImportError:
    EncryptionManager = None

try:
    from mautrix.crypto.attachments import encrypt_attachment
except ImportError:
    encrypt_attachment = None

EVENT_TIME = Histogram("bridge_matrix_event", "Time spent processing Matrix events",
                       ["event_type"])


class BaseMatrixHandler:
    log: TraceLogger = logging.getLogger("mau.mx")
    az: AppService
    commands: CommandProcessor
    config: 'BaseBridgeConfig'
    bridge: 'Bridge'
    e2ee: Optional[EncryptionManager]
    media_config: MediaRepoConfig

    user_id_prefix: str
    user_id_suffix: str

    def __init__(self, command_processor: Optional[CommandProcessor] = None,
                 bridge: Optional['Bridge'] = None) -> None:
        self.az = bridge.az
        self.config = bridge.config
        self.bridge = bridge
        self.commands = command_processor or CommandProcessor(bridge=bridge)
        self.media_config = MediaRepoConfig(upload_size=50 * 1024 * 1024)
        self.az.matrix_event_handler(self.int_handle_event)

        self.e2ee = None
        if self.config["bridge.encryption.allow"]:
            if not EncryptionManager:
                self.log.error("Encryption enabled in config, but dependencies not installed.")
                return
            if not encrypt_attachment:
                self.log.warning("Encryption enabled in config, but media encryption dependencies "
                                 "not installed.")
            self.e2ee = EncryptionManager(
                bridge=bridge,
                user_id_prefix=self.user_id_prefix, user_id_suffix=self.user_id_suffix,
                homeserver_address=self.config["homeserver.address"],
                db_url=self.config["appservice.database"],
                key_sharing_config=self.config["bridge.encryption.key_sharing"])

        self.management_room_text = self.config.get(
            "bridge.management_room_text",
            {
                "welcome": "Hello, I'm a bridge bot.",
                "welcome_connected": "Use `help` for help.",
                "welcome_unconnected": "Use `help` for help on how to log in.",
            },
        )
        self.management_room_multiple_messages = self.config.get(
            "bridge.management_room_multiple_messages",
            False,
        )

    async def wait_for_connection(self) -> None:
        self.log.info("Ensuring connectivity to homeserver")
        errors = 0
        tried_to_register = False
        while True:
            try:
                await self.az.intent.whoami()
                break
            except MForbidden as e:
                if not tried_to_register:
                    self.log.debug("Whoami endpoint returned M_FORBIDDEN, "
                                   "trying to register bridge bot before retrying...")
                    await self.az.intent.ensure_registered()
                    tried_to_register = True
                else:
                    raise
            except Exception:
                errors += 1
                if errors <= 6:
                    self.log.exception("Connection to homeserver failed, retrying in 10 seconds")
                    await asyncio.sleep(10)
                else:
                    raise
        try:
            self.media_config = await self.az.intent.get_media_repo_config()
        except Exception:
            self.log.warning("Failed to fetch media repo config", exc_info=True)

    async def init_as_bot(self) -> None:
        self.log.debug("Initializing appservice bot")
        displayname = self.config["appservice.bot_displayname"]
        if displayname:
            try:
                await self.az.intent.set_displayname(
                    displayname if displayname != "remove" else "")
            except Exception:
                self.log.exception("Failed to set bot displayname")

        avatar = self.config["appservice.bot_avatar"]
        if avatar:
            try:
                await self.az.intent.set_avatar_url(avatar if avatar != "remove" else "")
            except Exception:
                self.log.exception("Failed to set bot avatar")

    async def init_encryption(self) -> None:
        if self.e2ee:
            await self.e2ee.start()

    @staticmethod
    async def allow_message(user: 'BaseUser') -> bool:
        return user.is_whitelisted

    @staticmethod
    async def allow_command(user: 'BaseUser') -> bool:
        return user.is_whitelisted

    @staticmethod
    async def allow_bridging_message(user: 'BaseUser', portal: 'BasePortal') -> bool:
        return await user.is_logged_in()

    async def handle_leave(self, room_id: RoomID, user_id: UserID, event_id: EventID) -> None:
        pass

    async def handle_kick(self, room_id: RoomID, user_id: UserID, kicked_by: UserID, reason: str,
                          event_id: EventID) -> None:
        pass

    async def handle_ban(self, room_id: RoomID, user_id: UserID, banned_by: UserID, reason: str,
                         event_id: EventID) -> None:
        pass

    async def handle_unban(self, room_id: RoomID, user_id: UserID, unbanned_by: UserID,
                           reason: str, event_id: EventID) -> None:
        pass

    async def handle_join(self, room_id: RoomID, user_id: UserID, event_id: EventID) -> None:
        pass

    async def handle_member_info_change(self, room_id: RoomID, user_id: UserID,
                                        content: MemberStateEventContent,
                                        prev_content: MemberStateEventContent,
                                        event_id: EventID) -> None:
        pass

    async def handle_puppet_invite(self, room_id: RoomID, puppet: 'BasePuppet',
                                   invited_by: 'BaseUser', event_id: EventID) -> None:
        pass

    async def handle_invite(self, room_id: RoomID, user_id: UserID, inviter: 'BaseUser',
                            event_id: EventID) -> None:
        pass

    async def handle_reject(self, room_id: RoomID, user_id: UserID, reason: str, event_id: EventID
                            ) -> None:
        pass

    async def handle_disinvite(self, room_id: RoomID, user_id: UserID, disinvited_by: UserID,
                               reason: str, event_id: EventID) -> None:
        pass

    async def handle_event(self, evt: Event) -> None:
        """Called by :meth:`int_handle_event` for message events other than m.room.message."""

    async def handle_state_event(self, evt: StateEvent) -> None:
        """Called by :meth:`int_handle_event` for state events other than m.room.membership."""

    async def handle_ephemeral_event(self, evt: Union[ReceiptEvent, PresenceEvent, TypingEvent]
                                     ) -> None:
        if evt.type == EventType.RECEIPT:
            await self.handle_receipt(evt)

    async def send_permission_error(self, room_id: RoomID) -> None:
        await self.az.intent.send_notice(
            room_id,
            text="You are not whitelisted to use this bridge.\n\n"
                 "If you are the owner of this bridge, see the bridge.permissions "
                 "section in your config file.",
            html="<p>You are not whitelisted to use this bridge.</p>"
                 "<p>If you are the owner of this bridge, see the "
                 "<code>bridge.permissions</code> section in your config file.</p>")

    async def accept_bot_invite(self, room_id: RoomID, inviter: 'BaseUser') -> None:
        tries = 0
        while tries < 5:
            try:
                await self.az.intent.join_room(room_id)
                break
            except (IntentError, MatrixError):
                tries += 1
                wait_for_seconds = (tries + 1) * 10
                if tries < 5:
                    self.log.exception(f"Failed to join room {room_id} with bridge bot, "
                                       f"retrying in {wait_for_seconds} seconds...")
                    await asyncio.sleep(wait_for_seconds)
                else:
                    self.log.exception(f"Failed to join room {room_id}, giving up.")
                    return

        if not await self.allow_command(inviter):
            await self.send_permission_error(room_id)
            await self.az.intent.leave_room(room_id)
            return

        await self.send_welcome_message(room_id, inviter)

    async def send_welcome_message(self, room_id: RoomID, inviter: 'BaseUser') -> None:
        has_two_members, bridge_bot_in_room = await self._is_direct_chat(room_id)
        is_management = has_two_members and bridge_bot_in_room

        welcome_messages = [self.management_room_text.get("welcome")]

        if is_management:
            if await inviter.is_logged_in():
                welcome_messages.append(self.management_room_text.get("welcome_connected"))
            else:
                welcome_messages.append(self.management_room_text.get("welcome_unconnected"))

            additional_help = self.management_room_text.get("additional_help")
            if additional_help:
                welcome_messages.append(additional_help)
        else:
            cmd_prefix = self.commands.command_prefix
            welcome_messages.append(f"Use `{cmd_prefix} help` for help.")

        if self.management_room_multiple_messages:
            for m in welcome_messages:
                await self.az.intent.send_notice(room_id, text=m, html=markdown.render(m))
        else:
            combined = "\n".join(welcome_messages)
            combined_html = "".join(map(markdown.render, welcome_messages))
            await self.az.intent.send_notice(room_id, text=combined, html=combined_html)

    async def int_handle_invite(self, room_id: RoomID, user_id: UserID, invited_by: UserID,
                                event_id: EventID) -> None:
        self.log.debug(f"{invited_by} invited {user_id} to {room_id}")
        inviter = await self.bridge.get_user(invited_by)
        if inviter is None:
            self.log.exception(f"Failed to find user with Matrix ID {invited_by}")
            return
        elif user_id == self.az.bot_mxid:
            await self.accept_bot_invite(room_id, inviter)
            return
        elif not await self.allow_command(inviter):
            return

        puppet = await self.bridge.get_puppet(user_id)
        if puppet:
            await self.handle_puppet_invite(room_id, puppet, inviter, event_id)
            return

        await self.handle_invite(room_id, user_id, inviter, event_id)

    def is_command(self, message: MessageEventContent) -> Tuple[bool, str]:
        text = message.body
        prefix = self.config["bridge.command_prefix"]
        is_command = text.startswith(prefix)
        if is_command:
            text = text[len(prefix) + 1:].lstrip()
        return is_command, text

    async def handle_message(self, room_id: RoomID, user_id: UserID, message: MessageEventContent,
                             event_id: EventID) -> None:
        async def bail(error_text: str, step=MessageSendCheckpointStep.REMOTE) -> None:
            self.log.debug(error_text)
            await MessageSendCheckpoint(
                event_id=event_id,
                room_id=room_id,
                step=step,
                timestamp=int(time.time() * 1000),
                status=MessageSendCheckpointStatus.PERM_FAILURE,
                reported_by=MessageSendCheckpointReportedBy.BRIDGE,
                event_type=EventType.ROOM_MESSAGE,
                message_type=message.msgtype,
                info=error_text
            ).send(
                self.log,
                self.bridge.config["homeserver.message_send_checkpoint_endpoint"],
                self.az.as_token,
            )

        sender = await self.bridge.get_user(user_id)
        if not sender or not await self.allow_message(sender):
            await bail(
                f"Ignoring message {event_id} from {user_id} to {room_id}: User is not whitelisted."
            )
            return
        self.log.debug(f"Received Matrix event {event_id} from {sender.mxid} in {room_id}")
        self.log.trace("Event %s content: %s", event_id, message)

        if isinstance(message, TextMessageEventContent):
            message.trim_reply_fallback()

        is_command, text = self.is_command(message)
        portal = await self.bridge.get_portal(room_id)
        if not is_command and portal:
            if await self.allow_bridging_message(sender, portal):
                await portal.handle_matrix_message(sender, message, event_id)
            else:
                await bail(
                    f"Ignoring event {event_id} from {sender.mxid}: not allowed to send to portal"
                )
            return

        if message.msgtype != MessageType.TEXT:
            await bail(f"Event {event_id} is not m.text. Ignoring.")
            return
        elif not await self.allow_command(sender):
            await bail(
                f"Ignoring command {event_id} from {sender.mxid}: not allowed to perform command",
                step=MessageSendCheckpointStep.COMMAND,
            )
            return

        has_two_members, bridge_bot_in_room = await self._is_direct_chat(room_id)
        is_management = has_two_members and bridge_bot_in_room

        if is_command or is_management:
            try:
                command, arguments = text.split(" ", 1)
                args = arguments.split(" ")
            except ValueError:
                # Not enough values to unpack, i.e. no arguments
                command = text
                args = []

            try:
                await self.commands.handle(room_id, event_id, sender, command, args, message,
                                           portal, is_management, bridge_bot_in_room)
            except Exception as e:
                await bail(repr(e), step=MessageSendCheckpointStep.COMMAND)
            else:
                await MessageSendCheckpoint(
                    event_id=event_id,
                    room_id=room_id,
                    step=MessageSendCheckpointStep.COMMAND,
                    timestamp=int(time.time() * 1000),
                    status=MessageSendCheckpointStatus.SUCCESS,
                    reported_by=MessageSendCheckpointReportedBy.BRIDGE,
                    event_type=EventType.ROOM_MESSAGE,
                    message_type=message.msgtype,
                ).send(
                    self.log,
                    self.bridge.config["homeserver.message_send_checkpoint_endpoint"],
                    self.az.as_token,
                )
        else:
            await bail(
                f"Ignoring event {event_id} from {sender.mxid}: not a command and not a portal room"
            )

    async def _is_direct_chat(self, room_id: RoomID) -> Tuple[bool, bool]:
        try:
            members = await self.az.intent.get_room_members(room_id)
            return len(members) == 2, self.az.bot_mxid in members
        except MatrixError:
            return False, False

    async def handle_receipt(self, evt: ReceiptEvent) -> None:
        for event_id, receipts in evt.content.items():
            for user_id, data in receipts[ReceiptType.READ].items():
                user = await self.bridge.get_user(user_id, create=False)
                if not user or not await user.is_logged_in():
                    continue

                portal = await self.bridge.get_portal(evt.room_id)
                if not portal:
                    continue

                await self.handle_read_receipt(user, portal, event_id, data)

    async def handle_read_receipt(self, user: 'BaseUser', portal: 'BasePortal', event_id: EventID,
                                  data: SingleReceiptEventContent) -> None:
        pass

    def filter_matrix_event(self, evt: Event) -> bool:
        if not isinstance(evt, (MessageEvent, StateEvent, ReceiptEvent)):
            return False
        return evt.sender == self.az.bot_mxid

    async def try_handle_sync_event(self, evt: Event) -> None:
        try:
            if isinstance(evt, (ReceiptEvent, PresenceEvent, TypingEvent)):
                await self.handle_ephemeral_event(evt)
            else:
                self.log.trace("Unknown event type received from sync: %s", evt)
        except Exception:
            self.log.exception("Error handling manually received Matrix event")

    async def send_encryption_error_notice(self, evt: EncryptedEvent,
                                           error: DecryptionError) -> None:
        await self.az.intent.send_notice(evt.room_id,
                                         f"\u26a0 Your message was not bridged: {error}")

    async def handle_encrypted(self, evt: EncryptedEvent) -> None:
        if not self.e2ee:
            await self.handle_encrypted_unsupported(evt)
            return
        try:
            decrypted = await self.e2ee.decrypt(evt, wait_session_timeout=5)
        except SessionNotFound as e:
            await self._handle_encrypted_wait(evt, e, wait=10)
        except DecryptionError as e:
            self.log.warning(f"Failed to decrypt {evt.event_id}: {e}")
            self.log.trace("%s decryption traceback:", evt.event_id, exc_info=True)
            await self.send_encryption_error_notice(evt, e)
        else:
            await self.int_handle_event(decrypted)

    async def handle_encrypted_unsupported(self, evt: EncryptedEvent) -> None:
        self.log.debug("Got encrypted message %s from %s, but encryption is not enabled",
                       evt.event_id, evt.sender)
        await self.az.intent.send_notice(evt.room_id, "🔒️ This bridge has not been configured "
                                                      "to support encryption")

    async def _handle_encrypted_wait(self, evt: EncryptedEvent, err: SessionNotFound, wait: int
                                     ) -> None:
        self.log.debug(f"Couldn't find session {err.session_id} trying to decrypt {evt.event_id},"
                       " waiting even longer")
        msg = ("\u26a0 Your message was not bridged: the bridge hasn't received the decryption "
               f"keys. The bridge will retry for {wait} seconds. If this error keeps happening, "
               "try restarting your client.")
        try:
            event_id = await self.az.intent.send_notice(evt.room_id, msg)
        except IntentError:
            self.log.debug("IntentError while sending encryption error", exc_info=True)
            self.log.error("Got IntentError while trying to send encryption error message. "
                           "This likely means the bridge bot is not in the room, which can "
                           "happen if you force-enable e2ee on the homeserver without enabling "
                           "it by default on the bridge (bridge -> encryption -> default).")
            return
        got_keys = await self.e2ee.crypto.wait_for_session(evt.room_id, err.sender_key,
                                                           err.session_id, timeout=wait)
        if got_keys:
            self.log.debug(f"Got session {err.session_id} after waiting more, "
                           f"trying to decrypt {evt.event_id} again")
            try:
                decrypted = await self.e2ee.decrypt(evt, wait_session_timeout=0)
            except DecryptionError as e:
                self.log.warning(f"Failed to decrypt {evt.event_id}: {e}")
                self.log.trace("%s decryption traceback:", evt.event_id, exc_info=True)
                msg = f"\u26a0 Your message was not bridged: {e}"
            else:
                await self.az.intent.redact(evt.room_id, event_id)
                await self.int_handle_event(decrypted)
                return
        else:
            self.log.warning(f"Didn't get {err.session_id}, giving up on {evt.event_id}")
            msg = ("\u26a0 Your message was not bridged: the bridge hasn't received the decryption"
                   " keys. If this error keeps happening, try restarting your client.")
        content = TextMessageEventContent(msgtype=MessageType.NOTICE, body=msg)
        content.set_edit(event_id)
        await self.az.intent.send_message(evt.room_id, content)

    async def handle_encryption(self, evt: StateEvent) -> None:
        await self.az.state_store.set_encryption_info(evt.room_id, evt.content)
        portal = await self.bridge.get_portal(evt.room_id)
        if portal:
            portal.encrypted = True
            await portal.save()
            if portal.is_direct:
                portal.log.debug("Received encryption event in direct portal: %s", evt.content)
                await portal.enable_dm_encryption()

    async def send_message_send_checkpoint(self, evt: Event):
        if not self.bridge.config["homeserver.message_send_checkpoint_endpoint"]:
            return
        if evt.type not in CHECKPOINT_TYPES:
            return
        # Exclude encrypted events because they will be decrypted and handled as normal events.
        if evt.type == EventType.ROOM_ENCRYPTED:
            return

        self.log.debug(f"Sending message send checkpoint for {evt.event_id} to API server.")

        await MessageSendCheckpoint(
            event_id=evt.event_id,
            room_id=evt.room_id,
            step=MessageSendCheckpointStep.BRIDGE,
            timestamp=int(time.time() * 1000),
            status=MessageSendCheckpointStatus.SUCCESS,
            reported_by=MessageSendCheckpointReportedBy.BRIDGE,
            event_type=evt.type,
            message_type=evt.content.msgtype if evt.type == EventType.ROOM_MESSAGE else None,
        ).send(
            self.log,
            self.bridge.config["homeserver.message_send_checkpoint_endpoint"],
            self.az.as_token,
        )

    async def int_handle_event(self, evt: Event) -> None:
        if isinstance(evt, StateEvent) and evt.type == EventType.ROOM_MEMBER and self.e2ee:
            await self.e2ee.handle_member_event(evt)
        if self.filter_matrix_event(evt):
            return
        self.log.trace("Received event: %s", evt)
        start_time = time.time()

        asyncio.create_task(self.send_message_send_checkpoint(evt))

        if evt.type == EventType.ROOM_MEMBER:
            evt: StateEvent
            unsigned = evt.unsigned or StateUnsigned()
            prev_content = unsigned.prev_content or MemberStateEventContent()
            prev_membership = prev_content.membership if prev_content else Membership.JOIN
            if evt.content.membership == Membership.INVITE:
                await self.int_handle_invite(evt.room_id, UserID(evt.state_key), evt.sender,
                                             evt.event_id)
            elif evt.content.membership == Membership.LEAVE:
                if prev_membership == Membership.BAN:
                    await self.handle_unban(evt.room_id, UserID(evt.state_key), evt.sender,
                                            evt.content.reason, evt.event_id)
                elif prev_membership == Membership.INVITE:
                    if evt.sender == evt.state_key:
                        await self.handle_reject(evt.room_id, UserID(evt.state_key),
                                                 evt.content.reason, evt.event_id)
                    else:
                        await self.handle_disinvite(evt.room_id, UserID(evt.state_key), evt.sender,
                                                    evt.content.reason, evt.event_id)
                elif evt.sender == evt.state_key:
                    await self.handle_leave(evt.room_id, UserID(evt.state_key), evt.event_id)
                else:
                    await self.handle_kick(evt.room_id, UserID(evt.state_key), evt.sender,
                                           evt.content.reason, evt.event_id)
            elif evt.content.membership == Membership.BAN:
                await self.handle_ban(evt.room_id, UserID(evt.state_key), evt.sender,
                                      evt.content.reason, evt.event_id)
            elif evt.content.membership == Membership.JOIN:
                if prev_membership != Membership.JOIN:
                    await self.handle_join(evt.room_id, UserID(evt.state_key), evt.event_id)
                else:
                    await self.handle_member_info_change(evt.room_id, UserID(evt.state_key),
                                                         evt.content, prev_content, evt.event_id)
        elif evt.type in (EventType.ROOM_MESSAGE, EventType.STICKER):
            evt: MessageEvent
            if evt.type != EventType.ROOM_MESSAGE:
                evt.content.msgtype = MessageType(str(evt.type))
            await self.handle_message(evt.room_id, evt.sender, evt.content, evt.event_id)
        elif evt.type == EventType.ROOM_ENCRYPTED:
            await self.handle_encrypted(evt)
        elif evt.type == EventType.ROOM_ENCRYPTION:
            await self.handle_encryption(evt)
        else:
            if evt.type.is_state and isinstance(evt, StateEvent):
                await self.handle_state_event(evt)
            elif evt.type.is_ephemeral and isinstance(evt, (PresenceEvent, TypingEvent,
                                                            ReceiptEvent)):
                await self.handle_ephemeral_event(evt)
            else:
                await self.handle_event(evt)

        await self.log_event_handle_duration(evt, time.time() - start_time)

    async def log_event_handle_duration(self, evt: Event, duration: float) -> None:
        EVENT_TIME.labels(event_type=str(evt.type)).observe(duration)
