import os
import re
import io
import time
import json
import random
import secrets
import asyncio
import logging
import html
from datetime import datetime, timezone
from typing import Optional, Union, Dict, Any, List, Tuple
import httpx
from PIL import Image

from config import settings
from psycopg.rows import tuple_row
from core.cache import BoundedTTLCache
from core.state import FORM_REGISTRY, FORMS_STORAGE_DIR, get_redis_client, refresh_form_registry_from_disk

from core.durable_work import audit, enqueue_action, transition_submission
from core.phone_links import VERIFIED_PHONE_LINK_SOURCES, normalize_vn_phone, phone_normalization_sql
from core.submission_ids import normalize_submission_id
from services.admin_moderator import (
    query_submission_dossier,
    render_dossier_message,
)
from services.normalizer import compute_laplacian_blur_score, normalize_image_to_webp
from services.telegram_ui import submission_form_line, with_survey_copy
from services.vehicle_tracker import VehiclePersistenceTracker, stored_vehicle_candidate

logger = logging.getLogger("telegram_polling")
DEV_STORAGE_FILE = os.path.join(os.getcwd(), "data", "dev_submissions.json")

# Shared live support conversation registry between Admin Group and User Private DMs
ACTIVE_SUPPORT_CONVERSATIONS: Dict[int, dict] = {}  # user_id -> conversation metadata
ADMIN_GROUP_ACTIVE_SESSION: Dict[str, int] = {}     # legacy chat/admin identifier -> user_id
ADMIN_ACTIVE_CONVERSATIONS: Dict[int, int] = {}     # admin_user_id -> user_id (isolated per-admin routing)
# Strong refs for fire-and-forget review uploads; asyncio only weakly tracks tasks,
# and webhook runners are short-lived instances, so a module-level set keeps the
# media/document send alive after the callback response is returned.
BACKGROUND_REVIEW_TASKS: set = set()

BOT_INTERACTION_TTL = 600
BOT_INTERACTIONS = BoundedTTLCache(max_size=2048, ttl_seconds=BOT_INTERACTION_TTL)
ARGUMENT_PROMPT = "↩️ Trả lời tin nhắn này"
HISTORY_COMMANDS = ("/surveys", "/my_surveys", "/tracuu", "/history")
# A Telegram media group tops out at 10 photos; above this many the dossier
# links the Drive images folder instead of uploading the set again.
DOSSIER_PHOTO_LIMIT = 9


async def submission_images_folder_url(db_pool, submission_id: Any) -> Optional[str]:
    """Drive folder URL holding this submission's images, or None when Drive has none."""
    sid = str(submission_id or "").strip()
    if not db_pool or not sid:
        return None
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT drive_id FROM drive_name_ledger "
                    "WHERE entity_type='export' AND state='ACTIVE' AND object_name=%s "
                    "AND mime_type='application/vnd.google-apps.folder' "
                    "ORDER BY (parent_name='Images') DESC, last_seen_at DESC LIMIT 1",
                    (sid,),
                )
                row = await cur.fetchone()
    except Exception as exc:
        logger.debug(f"Drive folder lookup failed for #{sid}: {exc}")
        return None
    drive_id = str((row[0] if row else "") or "").strip()
    return f"https://drive.google.com/drive/folders/{drive_id}" if drive_id else None


# Use the original delivery question, not the driver identity or current form settings.
TARGET_ANSWER_SQL = "s.form_data -> (s.form_data -> '_delivery_policy' ->> 'question_id')"
TARGET_PHONE_JOIN = f"""
    CROSS JOIN LATERAL (SELECT CASE
        WHEN s.form_data -> '_delivery_policy' ->> 'source' = 'question'
         AND s.form_data -> '_delivery_policy' ->> 'inferred' IS DISTINCT FROM 'true'
         AND (s.form_data -> '_delivery_policy' ->> 'recorded_at_submission' = 'true' OR NOT EXISTS (
             SELECT 1 FROM outbox_events e WHERE e.aggregate_id = s.submission_id AND e.flow_target = 'ALL'
             AND e.terminal_reason = 'LEGACY_RECONCILED'
         ))
        THEN CASE WHEN jsonb_typeof({TARGET_ANSWER_SQL}) = 'array'
             THEN ({TARGET_ANSWER_SQL}) ->> 0 ELSE ({TARGET_ANSWER_SQL}) #>> '{{}}' END
    END AS phone) AS target
"""
HISTORY_ORDER = "ORDER BY s.created_at DESC NULLS LAST, s.submission_id DESC LIMIT 5"

class TelegramPollingRunner:
    """
    Asynchronous Telegram Bot long-polling daemon.
    In default/admin mode, polls updates for administrative moderation and inspection.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        polling_timeout: int = 30,
        db_pool=None,
        role: str = "admin",
    ):
        if bot_token:
            self.bot_token = bot_token
        elif role == "admin":
            self.bot_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        else:
            self.bot_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN

        self.role = role
        self.polling_timeout = polling_timeout
        self.offset = 0
        self.is_running = False
        self.http_client: Optional[httpx.AsyncClient] = None
        self.db_pool = db_pool
        self.backoff_delay = 1.0
        self.max_backoff = 60.0
        # Chat-scoped registry of the last prompt message per logical content key,
        # so a new instance can strip the old one's inline buttons.
        self.prompt_registry: Dict[str, List[int]] = {}
        self.vehicle_confirmations: Dict[str, tuple] = {}

    async def get_client(self) -> httpx.AsyncClient:
        if self.http_client is None or self.http_client.is_closed:
            self.http_client = httpx.AsyncClient(timeout=float(self.polling_timeout + 15))
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            self.http_client = None

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
        return_message: bool = False,
    ):
        """Dispatches an HTML or text message to a given chat_id via Telegram Bot API."""
        if not self.bot_token or self.bot_token == "mock_tg_token":
            logger.info(f"[MOCK TG {self.role.upper()} DISPATCH] to {chat_id}:\n{text}")
            return {"message_id": None} if return_message else True

        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                logger.warning("Telegram rate limit 429 encountered during polling dispatch")
            if resp.status_code != 200:
                return False
            if not return_message:
                return True
            return resp.json().get("result") or {}
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
            return False

    async def _save_interaction(self, key: str, payload: dict) -> bool:
        key = f"tg-interaction:{self.role}:{key}"
        redis = get_redis_client()
        if redis is None:
            # Single-process polling/local fallback. Webhook workers share Redis.
            BOT_INTERACTIONS[key] = payload
            return True
        try:
            return bool(await redis.set(key, json.dumps(payload), ex=BOT_INTERACTION_TTL))
        except Exception as exc:
            logger.warning("Could not save Telegram interaction (%s)", type(exc).__name__)
            return False

    async def _take_interaction(self, key: str) -> Optional[dict]:
        key = f"tg-interaction:{self.role}:{key}"
        redis = get_redis_client()
        if redis is None:
            payload = BOT_INTERACTIONS.get(key)
            if payload:
                BOT_INTERACTIONS[key] = None
            return payload
        try:
            raw = await redis.getdel(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("Could not load Telegram interaction (%s)", type(exc).__name__)
            return None

    async def _prompt_for_argument(self, chat_id, user_id, command, argument):
        if type(user_id) is not int or user_id <= 0:
            return
        sent = await self.send_message(
            chat_id, f"{ARGUMENT_PROMPT} bằng {argument}.", return_message=True,
            reply_markup={"force_reply": True},
        )
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if message_id and not await self._save_interaction(f"reply:{chat_id}:{user_id}:{message_id}", {"cmd": command}):
            await self.send_message(chat_id, f"Chưa thể nhận phản hồi. Vui lòng gõ <code>{command}</code> cùng tham số.")

    async def _argument_reply(self, chat_id, user_id, reply, text):
        if not reply or not reply.get("message_id") or not text.strip() or text.lstrip().startswith("/"):
            return False, None
        pending = await self._take_interaction(f"reply:{chat_id}:{user_id}:{reply['message_id']}")
        if pending:
            return True, f"{pending['cmd']} {text.strip()}"
        # Replies to expired prompts (or another operator's prompt) are not support messages.
        return bool((reply.get("from") or {}).get("is_bot") and (reply.get("text") or "").startswith(ARGUMENT_PROMPT)), None

    async def handle_forms(self, chat_id, user_id=None):
        from core.ctv import build_ctv_form_url, get_ctv_assigned_form_ids
        from core.quotas import (
            count_approved_responses_by_form,
            form_response_limit_status,
            resolve_ctv_code_for_telegram,
        )

        refresh_form_registry_from_disk()

        def recency(form):
            try:
                return os.stat(os.path.join(FORMS_STORAGE_DIR, f"{form.form_id}.json")).st_mtime_ns
            except OSError:
                return 0

        public_forms = [
            form for form in FORM_REGISTRY.values()
            if form.settings.access_control == "public"
        ]
        limited_ids = [
            form.form_id for form in public_forms
            if form.settings.response_limit and int(form.settings.response_limit) > 0
        ]
        approved_counts = (
            await count_approved_responses_by_form(self.db_pool, limited_ids)
            if self.db_pool and limited_ids
            else None
        )
        open_forms = [
            form for form in public_forms
            if not form_response_limit_status(
                form,
                None if approved_counts is None else approved_counts.get(str(form.form_id), 0),
            )[0]
        ]
        ctv_code = ""
        if self.role != "admin":
            if self.db_pool and user_id:
                try:
                    ctv_code = await resolve_ctv_code_for_telegram(self.db_pool, user_id) or ""
                    if ctv_code:
                        assigned_ids = await get_ctv_assigned_form_ids(self.db_pool, ctv_code, strict=True)
                        open_forms = [form for form in open_forms if form.form_id in assigned_ids]
                    else:
                        open_forms = [form for form in open_forms if not getattr(form.settings, "ctv_ref_enabled", True)]
                except Exception:
                    logger.exception("Failed to load assigned forms for Telegram user %s", user_id)
                    await self.send_message(chat_id, "⚠️ Tạm thời không thể tải danh sách khảo sát được phân công. Vui lòng thử lại sau.")
                    return
            else:
                open_forms = [form for form in open_forms if not getattr(form.settings, "ctv_ref_enabled", True)]
        recent = sorted(open_forms, key=lambda form: (recency(form), form.form_id), reverse=True)[:5]
        if not recent:
            empty_msg = "Bạn chưa được phân công biểu mẫu nào đang mở." if self.role != "admin" else "Chưa có biểu mẫu nào đang mở."
            await self.send_message(chat_id, empty_msg)
            return
        links = [
            f'• <a href="{build_ctv_form_url(ctv_code if getattr(form.settings, "ctv_ref_enabled", True) else "", form.form_id)}">{html.escape(form.title[:200])}</a>'
            for form in recent
        ]
        await self.send_message(chat_id, "📋 <b>Biểu mẫu đang mở gần đây:</b>\n" + "\n".join(links))

    async def delete_message(self, chat_id: Union[int, str], message_id: int) -> bool:
        """Deletes a single Telegram message (used to replace repeated /check responses)."""
        if not message_id or not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            return True
        try:
            client = await self.get_client()
            resp = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/deleteMessage",
                json={"chat_id": str(chat_id), "message_id": int(message_id)},
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Failed to delete message {message_id}: {e}")
            return False

    async def clear_previous_prompt(self, key: str, chat_id: Union[int, str]) -> None:
        """Deletes the previous response (message or media group) for this content key."""
        previous = self.prompt_registry.pop(key, None)
        if not previous:
            return
        ids = previous if isinstance(previous, list) else [previous]
        for message_id in ids:
            if message_id:
                await self.delete_message(chat_id, message_id)

    async def send_tracked(self, key: str, chat_id: Union[int, str], text: str, reply_markup: Optional[dict] = None):
        """Sends a message, first stripping buttons from the previous message with the same key."""
        await self.clear_previous_prompt(key, chat_id)
        result = await self.send_message(chat_id, text, reply_markup=reply_markup, return_message=True)
        if isinstance(result, dict) and result.get("message_id"):
            self.prompt_registry[key] = [result["message_id"]]
        return result

    async def send_dossier_bundle(
        self,
        chat_id: Union[int, str],
        key: str,
        dossier: dict,
        caption: str,
        txt_bytes: bytes,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Send photos first, then the dossier document carrying the review buttons.

        Above `DOSSIER_PHOTO_LIMIT` photos the bundle links the submission's
        Drive images folder instead of re-uploading the whole set to Telegram.
        """
        await self.clear_previous_prompt(key, chat_id)
        if not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            logger.info(f"[MOCK TG DISPATCH] dossier bundle for {dossier.get('submission_id')}: {caption}")
            return True

        sub_id = str(dossier.get("submission_id") or "submission")
        photo_paths = list(dossier.get("photo_paths") or [])
        if len(photo_paths) > DOSSIER_PHOTO_LIMIT:
            images_url = await submission_images_folder_url(self.db_pool, sub_id)
            if images_url:
                caption = (
                    f"{caption}\n\n🖼 <b>{len(photo_paths)} ảnh:</b> "
                    f"<a href=\"{images_url}\">Mở thư mục ảnh trên Drive</a>"
                )
                photo_paths = []
            else:
                caption = (
                    f"{caption}\n\n🖼 Chỉ gửi {DOSSIER_PHOTO_LIMIT}/{len(photo_paths)} ảnh "
                    "do giới hạn Telegram; thư mục ảnh Drive chưa có (chạy Xuất Drive để tạo)."
                )
                photo_paths = photo_paths[:DOSSIER_PHOTO_LIMIT]

        files: Dict[str, Any] = {}
        media: List[dict] = []
        for idx, path in enumerate(photo_paths):
            try:
                with open(path, "rb") as fp:
                    raw_b = fp.read()
                if len(raw_b) > 300_000:
                    try:
                        im = Image.open(io.BytesIO(raw_b))
                        if max(im.size) > 1200:
                            im.thumbnail((1200, 1200), Image.Resampling.BILINEAR)
                        out_io = io.BytesIO()
                        im.convert("RGB").save(out_io, format="JPEG", quality=82, optimize=True)
                        raw_b = out_io.getvalue()
                    except Exception:
                        pass
                files[f"photo_{idx}"] = (os.path.basename(path), raw_b, "image/jpeg")
                media.append({"type": "photo", "media": f"attach://photo_{idx}"})
            except Exception as e:
                logger.warning(f"Failed reading dossier photo {path}: {e}")

        files["dossier_txt"] = (f"{sub_id}.txt", txt_bytes, "text/plain")
        if media:
            media[0]["caption"] = caption
            media[0]["parse_mode"] = "HTML"

        client = await self.get_client()
        try:
            if not media:
                resp = await client.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendDocument",
                    data={
                        "chat_id": str(chat_id),
                        "caption": caption,
                        "parse_mode": "HTML",
                        **({"reply_markup": json.dumps(reply_markup)} if reply_markup else {}),
                    },
                    files={"document": files["dossier_txt"]},
                    timeout=30.0,
                )
                if resp.status_code != 200:
                    logger.error(f"Dossier document send failed: {resp.status_code} {resp.text[:200]}")
                    return False
                result = resp.json().get("result") or {}
                ids = [result.get("message_id")]
            else:
                # Telegram forbids mixing documents with photos in one media group.
                photo_files = {k: v for k, v in files.items() if k != "dossier_txt"}

                async def send_images():
                    if len(media) == 1:
                        return await client.post(
                            f"https://api.telegram.org/bot{self.bot_token}/sendPhoto",
                            data={"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
                            files={"photo": next(iter(photo_files.values()))},
                            timeout=30.0,
                        )
                    return await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/sendMediaGroup",
                        data={"chat_id": str(chat_id), "media": json.dumps(media)},
                        files=photo_files,
                        timeout=30.0,
                    )

                async def send_data():
                    return await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/sendDocument",
                        data={
                            "chat_id": str(chat_id),
                            **({"reply_markup": json.dumps(reply_markup)} if reply_markup else {}),
                        },
                        files={"document": files["dossier_txt"]},
                        timeout=30.0,
                    )

                # Strict order: images first, then the data message so the
                # approve/reject buttons always sit under the data response.
                ids = []
                try:
                    images_res = await send_images()
                    if getattr(images_res, "status_code", None) != 200:
                        logger.error(f"Dossier media group send failed: {images_res}")
                    else:
                        messages = images_res.json().get("result") or []
                        if isinstance(messages, dict):
                            messages = [messages]
                        ids = [m.get("message_id") for m in messages]
                except Exception as images_err:
                    logger.error(f"Dossier media group send failed: {images_err}")
                try:
                    data_res = await send_data()
                    if getattr(data_res, "status_code", None) != 200:
                        logger.error(f"Dossier txt send failed: {data_res}")
                    else:
                        doc_msg = data_res.json().get("result") or {}
                        if doc_msg.get("message_id"):
                            ids.append(doc_msg["message_id"])
                except Exception as data_err:
                    logger.error(f"Dossier txt send failed: {data_err}")
                if not ids:
                    return False
            self.prompt_registry[key] = [mid for mid in ids if mid]
            return True
        except Exception as e:
            logger.error(f"Failed sending dossier bundle: {e}")
            return False

    async def answer_callback_query(
        self, callback_query_id: str, text: Optional[str] = None, show_alert: bool = False
    ) -> bool:
        """Acknowledges an inline callback query with optional pop-up alert."""
        if not self.bot_token or self.bot_token == "mock_tg_token":
            return True
        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        try:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to answer callback query {callback_query_id}: {e}")
            return False

    def prepare_media_group_payload(self, photo_paths: List[str]) -> Optional[dict]:
        """Loads and pre-compresses photos into lightweight memory buffers for instant Telegram transmission."""
        if not photo_paths:
            return None
        selected = [p for p in photo_paths[:10] if os.path.exists(p) and os.path.getsize(p) > 0]
        if not selected:
            return None

        files = {}
        media = []
        for idx, p in enumerate(selected):
            key = f"photo_{idx}"
            try:
                with open(p, "rb") as fp:
                    raw_b = fp.read()
                # Fast downscale if oversized (> 300KB) to ensure sub-second transmission
                if len(raw_b) > 300_000:
                    try:
                        im = Image.open(io.BytesIO(raw_b))
                        if max(im.size) > 1200:
                            im.thumbnail((1200, 1200), Image.Resampling.BILINEAR)
                        out_io = io.BytesIO()
                        im.convert("RGB").save(out_io, format="JPEG", quality=82, optimize=True)
                        raw_b = out_io.getvalue()
                    except Exception:
                        pass
                files[key] = (os.path.basename(p), raw_b, "image/jpeg")
                media.append({"type": "photo", "media": f"attach://{key}"})
            except Exception as e:
                logger.warning(f"Error reading photo for media group: {e}")

        if not files:
            return None
        return {"files": files, "media": media}

    async def send_prepared_media_group(self, chat_id: Union[int, str], payload: dict) -> bool:
        """Dispatches an already prepared in-memory media group to Telegram."""
        if not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            return True
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMediaGroup"
        try:
            client = await self.get_client()
            resp = await client.post(
                url,
                data={"chat_id": str(chat_id), "media": json.dumps(payload["media"])},
                files=payload["files"],
                timeout=30.0,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to send prepared media group: {e}")
            return False

    async def send_media_group(self, chat_id: Union[int, str], photo_paths: List[str]) -> bool:
        """Convenience helper to prepare and send media group."""
        payload = self.prepare_media_group_payload(photo_paths)
        if payload:
            return await self.send_prepared_media_group(chat_id, payload)
        return False

    async def edit_message_reply_markup(
        self,
        chat_id: Union[int, str],
        message_id: int,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Updates or removes inline keyboard buttons on an existing message."""
        if not message_id or not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            return True

        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageReplyMarkup"
        payload = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        try:
            resp = await client.post(url, json=payload, timeout=10.0)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to edit message reply markup: {e}")
            return False

    def schedule_submission_review(self, chat_id: Union[int, str], sub_id: str) -> None:
        """
        Runs the heavy dossier upload AFTER the callback was answered.

        Telegram shows a spinner on the inline button until answerCallbackQuery
        lands, so awaiting the media group (which can take 20-30s) made the bot
        look frozen. The send itself keeps strict image-then-data ordering inside
        send_dossier_bundle; failures are logged instead of killing the webhook.
        """
        async def _run() -> None:
            try:
                await self.execute_submission_review(chat_id, sub_id)
            except Exception as e:
                logger.error(f"Background submission review failed for #{sub_id}: {e}", exc_info=True)

        task = asyncio.create_task(_run())
        BACKGROUND_REVIEW_TASKS.add(task)
        task.add_done_callback(BACKGROUND_REVIEW_TASKS.discard)

    async def execute_submission_review(self, chat_id: Union[int, str], sub_id: str, *, audit_actor: Optional[int] = None) -> None:
        """
        Unified review handler invoked identically by /check command and [📋 Mở Phiếu] button.
        Loads media group and review text in parallel, waiting for both to be ready before dispatching.
        """
        clean_sub_id = normalize_submission_id(sub_id)
        if not self.db_pool and (settings.ENVIRONMENT == "production" or audit_actor is not None):
            await self.send_message(chat_id, "Chưa thể mở hồ sơ: cơ sở dữ liệu không khả dụng.")
            return
        dossier = await query_submission_dossier(clean_sub_id, db_pool=self.db_pool)
        if "error" in dossier:
            await self.send_message(chat_id, f"❌ {dossier['error']}")
            return

        cur_status = dossier.get("status", "PENDING")
        ctv = dossier.get("ctv_code") or dossier.get("collaborator_id") or "N/A"
        form_line = submission_form_line(dossier.get("form_id"))
        caption = (
            f"📋 <b>HỒ SƠ KHẢO SÁT #{html.escape(str(clean_sub_id))}</b>\n"
            + (f"{form_line}\n" if form_line else "")
            + f"• <b>CTV:</b> <code>{html.escape(str(ctv))}</code>"
        )
        # Raw nested facts duplicate the rest of the dossier; keep the txt focused.
        raw_data = {k: v for k, v in dossier.items() if k not in ("photo_paths", "raw_data")}
        txt_bytes = json.dumps(raw_data, ensure_ascii=False, indent=2, default=str).encode("utf-8")

        if cur_status in ("APPROVED", "REJECTED"):
            status_text = "Đã DUYỆT (APPROVED)" if cur_status == "APPROVED" else "Đã TỪ CHỐI (REJECTED)"
            buttons = {
                "inline_keyboard": [
                    [{"text": f"🔒 {status_text} - Đã hoàn tất", "callback_data": "resolved_notice"}],
                ]
            }
            if audit_actor is not None:
                token = secrets.token_urlsafe(12)
                snapshot = {"submission_id": clean_sub_id, "status": cur_status, "moderated_at": dossier.get("moderated_at")}
                if await self._save_interaction(f"override:{chat_id}:{audit_actor}:{token}", snapshot):
                    label = "Đổi sang TỪ CHỐI" if cur_status == "APPROVED" else "Đổi sang DUYỆT"
                    buttons["inline_keyboard"].append([{"text": label, "callback_data": f"admin_override:{token}"}])
                else:
                    await self.send_message(chat_id, "Chưa thể tạo yêu cầu đổi quyết định. Vui lòng thử lại.")
        else:
            buttons = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Duyệt (Approve)", "callback_data": f"admin_req_approve:{clean_sub_id}"},
                        {"text": "❌ Từ chối (Reject)", "callback_data": f"admin_req_reject:{clean_sub_id}"},
                    ],
                ]
            }

        buttons = with_survey_copy(buttons, clean_sub_id)
        await self.send_dossier_bundle(
            chat_id,
            f"dossier:{chat_id}:{dossier.get('submission_id')}",
            dossier,
            caption,
            txt_bytes,
            reply_markup=buttons,
        )

    async def _handle_audit_override(self, cq_id, chat_id, msg_id, admin_id, action, token):
        pending = action == "admin_override"
        key = f"override:{chat_id}:{admin_id}" if pending else f"confirm:{chat_id}:{admin_id}:{msg_id}"
        snapshot = await self._take_interaction(f"{key}:{token}") if msg_id else None
        if not snapshot:
            await self.answer_callback_query(cq_id, "Yêu cầu đã hết hạn hoặc không thuộc về bạn. Mở lại /audit.", show_alert=True)
            return
        sid, previous = snapshot["submission_id"], snapshot["status"]
        target = "REJECTED" if previous == "APPROVED" else "APPROVED"
        if pending:
            confirm = secrets.token_urlsafe(12)
            if not await self._save_interaction(f"confirm:{chat_id}:{admin_id}:{msg_id}:{confirm}", snapshot):
                await self.answer_callback_query(cq_id, "Chưa thể tạo xác nhận. Mở lại /audit.", show_alert=True)
                return
            await self.edit_message_reply_markup(chat_id, msg_id, reply_markup={"inline_keyboard": [[
                {"text": f"Xác nhận đổi từ {previous} sang {target}", "callback_data": f"admin_override_ok:{confirm}"},
                {"text": "Hủy", "callback_data": f"admin_override_cancel:{confirm}"},
            ]]})
            await self.answer_callback_query(cq_id, "Vui lòng xác nhận đổi quyết định.")
            return
        if action == "admin_override_cancel":
            await self.edit_message_reply_markup(chat_id, msg_id, reply_markup={"inline_keyboard": []})
            await self.answer_callback_query(cq_id, "Đã hủy thao tác.")
            return
        reason = "Quản trị viên đổi quyết định qua /audit"
        success, status = await self._update_submission_status(
            sid, target, reason=reason, actor=f"admin:{admin_id}", expected_statuses=(previous,), override=snapshot,
        )
        if not success:
            await self._answer_failed_decision(cq_id, chat_id, msg_id, sid, status)
            return
        await self.answer_callback_query(cq_id, f"Đã đổi quyết định #{sid}: {target}")
        await self.edit_message_reply_markup(chat_id, msg_id, reply_markup={"inline_keyboard": [[
            {"text": f"🔒 {target}", "callback_data": "resolved_notice"},
        ]]})
        if await self._legacy_delivery_allowed(sid):
            await self._notify_parties_of_decision(sid, target, reason=reason)

    async def log_chat_history(
        self,
        telegram_user_id: int,
        chat_id: int,
        message_id: int,
        role: str,
        direction: str,
        message_text: str,
    ) -> None:
        """Records message in telegram_chat_history for Decree 13 regulatory compliance."""
        if not self.db_pool:
            return
        try:
            async with self.db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO telegram_chat_history 
                            (telegram_user_id, chat_id, message_id, role, direction, message_text)
                        VALUES (%s, %s, %s, %s, %s, %s);
                        """,
                        (telegram_user_id, chat_id, message_id, role, direction, message_text),
                    )
                await conn.commit()
        except Exception as e:
            logger.debug(f"Failed to log chat history: {e}")

    async def get_stats_message(self) -> str:
        """Calculates survey submission counts across database or dev test storage."""
        total = 0
        approved = 0
        rejected = 0
        if self.db_pool is None and settings.ENVIRONMENT == "production":
            return "Chưa thể đọc thống kê: cơ sở dữ liệu không khả dụng."

        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT 
                                COUNT(*),
                                COUNT(*) FILTER (WHERE status = 'APPROVED'),
                                COUNT(*) FILTER (WHERE status = 'REJECTED')
                            FROM survey_submissions;
                            """
                        )
                        row = await cur.fetchone()
                        if row:
                            total = row[0] or 0
                            approved = row[1] or 0
                            rejected = row[2] or 0
            except Exception as e:
                logger.warning("Could not read survey stats from database (%s)", type(e).__name__)
                return "Chưa thể đọc thống kê. Vui lòng thử lại sau."

        if self.db_pool is None and os.path.exists(DEV_STORAGE_FILE):
            try:
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    dev_data = json.load(f)
                    total = len(dev_data)
                    approved = sum(1 for item in dev_data if item.get("status") == "APPROVED")
                    rejected = sum(1 for item in dev_data if item.get("status") == "REJECTED")
            except Exception as e:
                logger.warning(f"Could not read dev storage for stats: {e}")

        pending = total - (approved + rejected)
        if pending < 0:
            pending = 0

        return (
            f"📊 <b>Thống Kê Khảo Sát Hệ Thống (Admin Stats):</b>\n\n"
            f"• <b>Tổng số phiếu đã tiếp nhận:</b> {total}\n"
            f"• <b>Đã duyệt hợp lệ (Approved):</b> {approved}\n"
            f"• <b>Bị từ chối (Rejected):</b> {rejected}\n"
            f"• <b>Chờ kiểm tra thủ công (Review):</b> {pending}\n\n"
            f"<i>Cập nhật thời gian thực từ cơ sở dữ liệu SSOT.</i>"
        )

    async def handle_command(
        self,
        chat_id: Union[int, str],
        text: str,
        reply_to_message: Optional[dict] = None,
        from_user_id: Optional[int] = None,
    ) -> None:
        """Routes and executes admin slash commands."""
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/check", "/query", "/audit", "/stats", "/quota", "/quotas", "/hanmuc", *HISTORY_COMMANDS):
            if not await self._can_moderate(chat_id, from_user_id, allow_local=False):
                await self.send_message(chat_id, "Không có quyền tra cứu tại đây.")
                return

        if cmd in ("/check", "/query", "/audit"):
            sub_id = None
            if argument:
                sub_id = argument.lstrip("#")
            elif reply_to_message:
                reply_text = reply_to_message.get("text") or reply_to_message.get("caption") or ""
                m = re.search(r"#?((?:VN|DEV)-[A-Za-z0-9_-]+)", reply_text)
                if m:
                    sub_id = m.group(1).lstrip("#")

            if not sub_id:
                await self._prompt_for_argument(chat_id, from_user_id, cmd, "mã phiếu khảo sát")
                return

            if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", sub_id):
                await self.send_message(
                    chat_id,
                    "⚠️ <b>Mã phiếu không đúng định dạng.</b>\n"
                    "Chỉ chấp nhận chữ cái, số và dấu gạch ngang (tối đa 64 ký tự).",
                )
                return

            if cmd == "/audit":
                await self.execute_submission_review(chat_id, sub_id, audit_actor=from_user_id)
            else:
                await self.execute_submission_review(chat_id, sub_id)
        elif cmd == "/stats":
            await self.send_message(chat_id, await self.get_stats_message())
        elif cmd == "/forms":
            await self.handle_forms(chat_id, from_user_id)
        elif cmd in HISTORY_COMMANDS:
            if argument:
                await self.handle_admin_surveys_lookup(chat_id, argument)
            else:
                await self._prompt_for_argument(chat_id, from_user_id, cmd, "mã CTV hoặc số điện thoại")
        elif cmd in ("/quota", "/quotas", "/hanmuc"):
            if argument:
                await self.handle_quota_command(chat_id, f"{cmd} {argument}", from_user_id)
            else:
                await self._prompt_for_argument(chat_id, from_user_id, cmd, "mã CTV")
        elif cmd in ("/start", "/help"):
            help_msg = (
                "🤖 <b>Bot Điều Hành Trust Bridge (@TrustBridgeVNAdmin_Bot)</b>\n\n"
                "Chào mừng bạn đến với hệ thống kiểm định khảo sát Trust Bridge!\n\n"
                "Lệnh điều hành:\n"
                "• <code>/check &lt;mã_phiếu&gt;</code> - Mở hồ sơ & ảnh phiếu khảo sát để xét duyệt\n"
                "• <code>/audit &lt;mã_phiếu&gt;</code> - Xem hồ sơ và đổi quyết định đã chốt\n"
                "• <code>/forms</code> - Xem biểu mẫu đang mở\n"
                "• <code>/surveys &lt;mã_ctv hoặc số_điện_thoại&gt;</code> - Tra cứu phiếu gần đây\n"
                "• <code>/quota &lt;mã_ctv&gt;</code> - Xem mục tiêu của một CTV\n"
                "• <code>/stats</code> - Xem thống kê tiến độ duyệt phiếu\n"
                "• <code>/close</code> - Kết thúc phiên chat hỗ trợ trực tiếp\n\n"
                "👉 <i>Mẹo: Bạn có thể reply (trả lời) trực tiếp tin nhắn thông báo bằng lệnh <code>/check</code>!</i>"
            )
            await self.send_message(chat_id, help_msg)
        else:
            help_msg = (
                "🤖 <b>Bot Điều Hành Trust Bridge (@TrustBridgeVNAdmin_Bot)</b>\n\n"
                "Lệnh điều hành:\n"
                "• <code>/check &lt;mã_phiếu&gt;</code> - Mở hồ sơ & ảnh phiếu để xét duyệt\n"
                "• <code>/quota &lt;mã_ctv&gt;</code> - Xem mục tiêu của một CTV\n\n"
                "👉 <i>Mẹo: Bạn có thể reply (trả lời) trực tiếp tin nhắn thông báo bằng lệnh <code>/check</code>!</i>"
            )
            await self.send_message(chat_id, help_msg)

    async def handle_admin_surveys_lookup(self, chat_id, ref):
        if not self.db_pool:
            await self.send_message(chat_id, "Chưa thể tra cứu phiếu: cơ sở dữ liệu không khả dụng.")
            return
        from core.quotas import resolve_ctv_code_from_ref

        phone = normalize_vn_phone(ref) if re.fullmatch(r"[+0-9 .()-]+", ref) else None
        if not phone and not re.fullmatch(r"[\w@-]{1,64}", ref):
            await self.send_message(chat_id, "Mã CTV hoặc số điện thoại không hợp lệ.")
            return
        code = None if phone else await resolve_ctv_code_from_ref(self.db_pool, ref) or ref.lstrip("@")
        try:
            async with self.db_pool.connection() as conn:
                async with conn.cursor(row_factory=tuple_row) as cur:
                    if phone:
                        await cur.execute(
                            f"SELECT s.submission_id, s.status, s.created_at FROM survey_submissions s {TARGET_PHONE_JOIN} "
                            f"WHERE {phone_normalization_sql('s.driver_phone')}=%s OR {phone_normalization_sql('target.phone')}=%s "
                            f"OR EXISTS (SELECT 1 FROM ctv_directory c WHERE {phone_normalization_sql('c.phone')}=%s "
                            "AND (c.telegram_user_id=s.collaborator_id OR UPPER(c.code)=UPPER(s.ctv_code))) "
                            f"{HISTORY_ORDER}", (phone, phone, phone),
                        )
                    else:
                        await cur.execute(
                            "SELECT s.submission_id, s.status, s.created_at FROM survey_submissions s "
                            "WHERE UPPER(s.ctv_code)=UPPER(%s) OR s.collaborator_id::text=%s OR EXISTS ("
                            "SELECT 1 FROM ctv_directory c WHERE UPPER(c.code)=UPPER(%s) "
                            "AND c.telegram_user_id=s.collaborator_id) "
                            f"{HISTORY_ORDER}", (code, ref, code),
                        )
                    rows = await cur.fetchall()
        except Exception as exc:
            logger.warning("Could not read admin survey history (%s)", type(exc).__name__)
            await self.send_message(chat_id, "Chưa thể tra cứu phiếu. Vui lòng thử lại sau.")
            return
        if not rows:
            await self.send_message(chat_id, "Không tìm thấy phiếu khảo sát phù hợp.")
            return
        lines = ["📋 <b>Phiếu khảo sát gần đây:</b>"]
        for sid, status, created in rows:
            date = created.strftime("%d/%m/%Y %H:%M") if isinstance(created, datetime) else "—"
            lines.append(f"• <code>{html.escape(sid)}</code> · {html.escape(status)} ({date})")
        await self.send_message(chat_id, "\n".join(lines))

    async def handle_quota_command(
        self,
        chat_id: Union[int, str],
        text: str,
        from_user_id: Optional[int] = None,
    ) -> None:
        """Shows quotas for a CTV code (admin) or the caller's own CTV (driver bot)."""
        from core.quotas import build_ctv_quota_report, resolve_ctv_code_for_telegram, resolve_ctv_code_from_ref

        parts = text.strip().split(maxsplit=1)
        ref = parts[1].strip().lstrip("@") if len(parts) > 1 else ""
        code = None
        if ref:
            code = await resolve_ctv_code_from_ref(self.db_pool, ref)
            if not code:
                await self.send_message(
                    chat_id,
                    f"⚠️ Không tìm thấy CTV <code>{html.escape(ref)}</code> trong danh bạ.\n"
                    "Vui lòng dùng <code>/quota &lt;mã_ctv&gt;</code>.",
                )
                return
        elif from_user_id:
            code = await resolve_ctv_code_for_telegram(self.db_pool, int(from_user_id))

        if not code:
            await self.send_message(
                chat_id,
                "⚠️ <b>Chưa xác định được mã CTV.</b>\n\n"
                "• Quản trị viên: <code>/quota &lt;mã_ctv&gt;</code>\n"
                "• CTV: liên kết tài khoản bằng link mời CTV trước, hoặc nhờ quản trị viên tra cứu.",
            )
            return

        report = await build_ctv_quota_report(self.db_pool, code)
        await self.send_message(chat_id, report)
    async def handle_callback_query(self, cq: dict) -> None:
        """Processes inline keyboard button interactions for admin audits."""
        cq_id = cq.get("id", "")
        data = cq.get("data", "")
        cq_msg = cq.get("message") or {}
        cq_chat = cq_msg.get("chat") or {}
        cq_chat_id = cq_chat.get("id")
        from_user = cq.get("from") or {}
        msg_id = cq_msg.get("message_id")
        if not cq_chat_id:
            await self.answer_callback_query(cq_id)
            return

        # Telegram authenticates the update, not the room's authority to moderate.
        admin_id = from_user.get("id")
        if data.startswith(("admin_review:", "admin_query:", "admin_override:", "admin_override_ok:", "admin_override_cancel:")):
            if not await self._can_moderate(cq_chat_id, admin_id, allow_local=False):
                await self.answer_callback_query(cq_id, "Không có quyền tra cứu tại đây.", show_alert=True)
                return
        if data.startswith(("admin_req_", "admin_cancel_decision:", "admin_approve:",
                            "admin_reject:", "admin_warn:", "admin_change_veh:", "admin_cv:", "admin_cv_cancel:")):
            if not await self._can_moderate(cq_chat_id, admin_id):
                await self.answer_callback_query(cq_id, "Không có quyền xử lý phiếu tại đây.", show_alert=True)
                return

        if data.startswith("admin_review:") or data.startswith("admin_query:"):
            sub_id = normalize_submission_id(data.split(":", 1)[1])
            # Answer immediately (Telegram caps callback acks at ~10s and keeps the
            # button spinner until then); the 20-30s media+document send follows.
            await self.answer_callback_query(cq_id, "Đang mở phiếu...")
            self.schedule_submission_review(cq_chat_id, sub_id)
        elif data.startswith(("admin_override:", "admin_override_ok:", "admin_override_cancel:")):
            action, token = data.split(":", 1)
            await self._handle_audit_override(cq_id, cq_chat_id, msg_id, admin_id, action, token)
        elif data.startswith("admin_req_approve:"):
            sub_id = normalize_submission_id(data.split(":", 1)[1])
            confirm_buttons = {
                "inline_keyboard": [
                    [
                        {"text": f"⚠️ Xác nhận DUYỆT phiếu #{sub_id}?", "callback_data": "none"},
                    ],
                    [
                        {"text": "✅ ĐỒNG Ý DUYỆT", "callback_data": f"admin_approve:{sub_id}"},
                        {"text": "↩️ HỦY BỎ", "callback_data": f"admin_cancel_decision:{sub_id}"},
                    ],
                ]
            }
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=with_survey_copy(confirm_buttons, sub_id))
            await self.answer_callback_query(cq_id, "Vui lòng xác nhận để hoàn tất duyệt phiếu.")

        elif data.startswith("admin_req_reject:"):
            sub_id = normalize_submission_id(data.split(":", 1)[1])
            confirm_buttons = {
                "inline_keyboard": [
                    [
                        {"text": f"⚠️ Xác nhận TỪ CHỐI phiếu #{sub_id}?", "callback_data": "none"},
                    ],
                    [
                        {"text": "❌ ĐỒNG Ý TỪ CHỐI", "callback_data": f"admin_reject:{sub_id}"},
                        {"text": "↩️ HỦY BỎ", "callback_data": f"admin_cancel_decision:{sub_id}"},
                    ],
                ]
            }
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=with_survey_copy(confirm_buttons, sub_id))
            await self.answer_callback_query(cq_id, "Vui lòng xác nhận để hoàn tất từ chối phiếu.")

        elif data.startswith("admin_cancel_decision:"):
            sub_id = normalize_submission_id(data.split(":", 1)[1])
            restore_buttons = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Duyệt (Approve)", "callback_data": f"admin_req_approve:{sub_id}"},
                        {"text": "❌ Từ chối (Reject)", "callback_data": f"admin_req_reject:{sub_id}"},
                    ],
                ]
            }
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=with_survey_copy(restore_buttons, sub_id))
            await self.answer_callback_query(cq_id, "Đã hủy thao tác.")

        elif data.startswith("admin_approve:"):
            sub_id = normalize_submission_id(data.split(":", 1)[1])
            reason = "Quản trị viên phê duyệt thủ công"
            success, final_status = await self._update_submission_status(sub_id, "APPROVED", reason=reason, actor=f"admin:{admin_id}")
            if not success:
                await self._answer_failed_decision(cq_id, cq_chat_id, msg_id, sub_id, final_status)
                return

            await self.answer_callback_query(cq_id, f"Đã duyệt #{sub_id}")
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=with_survey_copy({"inline_keyboard": [[{"text": "🔒 Đã DUYỆT (APPROVED) - Hoàn tất", "callback_data": "resolved_notice"}]]}, sub_id))
            if await self._legacy_delivery_allowed(sub_id):
                await self.send_message(cq_chat_id, f"✅ <b>Quản trị viên đã DUYỆT thủ công phiếu #{sub_id}.</b>", reply_markup=with_survey_copy(None, sub_id))
                await self._notify_parties_of_decision(sub_id, "APPROVED", reason=reason)

        elif data.startswith("admin_reject:"):
            sub_id = normalize_submission_id(data.split(":", 1)[1])
            reason = "Quản trị viên từ chối thủ công"
            success, final_status = await self._update_submission_status(sub_id, "REJECTED", reason=reason, actor=f"admin:{admin_id}")
            if not success:
                await self._answer_failed_decision(cq_id, cq_chat_id, msg_id, sub_id, final_status)
                return

            await self.answer_callback_query(cq_id, f"Đã từ chối #{sub_id}")
            if msg_id:
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=with_survey_copy({"inline_keyboard": [[{"text": "🔒 Đã TỪ CHỐI (REJECTED) - Hoàn tất", "callback_data": "resolved_notice"}]]}, sub_id))
            if await self._legacy_delivery_allowed(sub_id):
                await self.send_message(cq_chat_id, f"❌ <b>Quản trị viên đã TỪ CHỐI phiếu #{sub_id}.</b>", reply_markup=with_survey_copy(None, sub_id))
                await self._notify_parties_of_decision(sub_id, "REJECTED", reason=reason)

        elif data == "resolved_notice":
            await self.answer_callback_query(cq_id, "Phiếu này đã được xử lý hoàn tất và bị khóa, không nhận phản hồi thêm.", show_alert=True)
        elif data.startswith("admin_warn:"):
            sub_id = normalize_submission_id(data.split(":", 2)[1])
            note = await self._queue_driver_warning(sub_id, admin_id)
            await self.answer_callback_query(cq_id, note, show_alert=True)

        elif data.startswith("admin_change_veh:"):
            sub_id = normalize_submission_id(data.split(":", 2)[1])
            await self._request_vehicle_change(cq_id, cq_chat_id, sub_id, admin_id)

        elif data.startswith(("admin_cv:", "admin_cv_cancel:")):
            action, token = data.split(":", 1)
            await self._confirm_vehicle_change(cq_id, cq_chat_id, msg_id, admin_id, token, action == "admin_cv")

        elif data.startswith("admin_reply_chat:"):
            target_user_id = int(data.split(":", 1)[1])
            admin_id = from_user.get("id")
            admin_name = from_user.get("first_name", "Admin")

            # 1. Check if this support request is already locked by another admin!
            existing = ACTIVE_SUPPORT_CONVERSATIONS.get(target_user_id)
            if existing and existing.get("locked_admin_id"):
                locked_id = existing["locked_admin_id"]
                locked_name = existing.get("locked_admin_name", "quản trị viên khác")
                if locked_id != admin_id:
                    await self.answer_callback_query(
                        cq_id,
                        f"⚠️ Yêu cầu hỗ trợ này đã được khóa và đang xử lý bởi {locked_name}! Chỉ {locked_name} mới có quyền nhắn tin.",
                        show_alert=True
                    )
                    return

            # 2. Enforce 1-admin-1-convo rule: check if this admin is already holding another active conversation!
            if admin_id and admin_id in ADMIN_ACTIVE_CONVERSATIONS:
                held_user_id = ADMIN_ACTIVE_CONVERSATIONS[admin_id]
                if held_user_id != target_user_id and held_user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                    await self.answer_callback_query(
                        cq_id,
                        f"⚠️ Bạn đang trong phiên hỗ trợ với thành viên khác (ID: {held_user_id})!\n\n"
                        f"Mỗi quản trị viên chỉ được giữ 1 phiên hỗ trợ tại một thời điểm.\n"
                        f"Vui lòng gõ /close để hoàn tất phiên hiện tại trước khi nhận phiên mới.",
                        show_alert=True,
                    )
                    return
                elif held_user_id != target_user_id:
                    del ADMIN_ACTIVE_CONVERSATIONS[admin_id]
            # Lock session to this admin only!
            ACTIVE_SUPPORT_CONVERSATIONS[target_user_id] = {
                "user_id": target_user_id,
                "admin_chat_id": cq_chat_id,
                "locked_admin_id": admin_id,
                "locked_admin_name": admin_name,
                "opened_at": time.time(),
            }
            if admin_id:
                ADMIN_ACTIVE_CONVERSATIONS[admin_id] = target_user_id
                ADMIN_GROUP_ACTIVE_SESSION[str(admin_id)] = target_user_id
            ADMIN_GROUP_ACTIVE_SESSION[str(cq_chat_id)] = target_user_id
            ADMIN_GROUP_ACTIVE_SESSION[cq_chat_id] = target_user_id
            # Update ticket message buttons in group to show locked state!
            if msg_id:
                locked_ticket_markup = {
                    "inline_keyboard": [
                        [
                            {"text": f"🔒 Đang hỗ trợ bởi {admin_name}", "callback_data": f"admin_locked_info:{target_user_id}"},
                        ],
                    ]
                }
                await self.edit_message_reply_markup(cq_chat_id, msg_id, reply_markup=locked_ticket_markup)

            await self.send_message(
                cq_chat_id,
                f"🟢 <b>ĐÃ MỞ & KHÓA PHIÊN HỘI THOẠI TRỰC TIẾP (ID: <code>{target_user_id}</code>) CHO {admin_name}!</b>\n\n"
                f"👉 Phiên chat này đã được <b>khóa riêng cho {admin_name}</b>. Tin nhắn từ quản trị viên khác trong nhóm sẽ không chuyển tiếp tới người dùng.\n"
                f"👉 {admin_name} gõ <code>/close</code> để kết thúc phiên bất cứ lúc nào.",
            )
            await self.answer_callback_query(cq_id, f"Đã khóa phiên hỗ trợ cho {admin_name}")
            driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
            if driver_token and driver_token != "mock_tg_token":
                client = await self.get_client()
                url = f"https://api.telegram.org/bot{driver_token}/sendMessage"
                user_msg = (
                    "🟢 <b>Quản trị viên đã tham gia cuộc hội thoại hỗ trợ trực tiếp!</b>\n\n"
                    "Bạn có thể nhắn tin trao đổi tại đây. Quản trị viên sẽ đọc và trả lời bạn ngay trong khung chat này.\n"
                    "👉 Gõ <code>/close</code> khi đã giải quyết xong để kết thúc phiên hỗ trợ."
                )
                try:
                    await client.post(url, json={"chat_id": target_user_id, "text": user_msg, "parse_mode": "HTML"})
                except Exception as ex:
                    logger.error(f"Failed to notify user about open chat: {ex}")

        elif data.startswith("admin_close_ticket:"):
            target_user_id = int(data.split(":", 1)[1])
            convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(target_user_id, None)
            locked_admin = convo.get("locked_admin_id") if convo else None
            if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
                del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
            if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
                del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
            if str(cq_chat_id) in ADMIN_GROUP_ACTIVE_SESSION:
                del ADMIN_GROUP_ACTIVE_SESSION[str(cq_chat_id)]
            await self.send_message(
                cq_chat_id,
                f"🔒 Đã đóng yêu cầu hỗ trợ của thành viên <code>{target_user_id}</code>."
            )
            await self.answer_callback_query(cq_id, "Đã đóng ticket")
        else:
            await self.answer_callback_query(cq_id)

    async def _can_moderate(self, chat_id, admin_id, *, allow_local=True) -> bool:
        if self.role != "admin" or type(admin_id) is not int or admin_id <= 0:
            return False
        if str(chat_id) == str(settings.TELEGRAM_ADMIN_GROUP_ID):
            return True
        if not self.db_pool:
            return allow_local and settings.ENVIRONMENT != "production" and not settings.DURABLE_MODERATION_ENABLED
        if chat_id != admin_id:
            return False
        try:
            async with self.db_pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM authorized_users WHERE telegram_user_id=%s AND 'ADMIN' = ANY(roles) AND is_active",
                    (admin_id,),
                )
                return bool(await cur.fetchone())
        except Exception as exc:
            logger.warning("Could not verify admin account (%s)", type(exc).__name__)
            return False

    async def _queue_driver_warning(self, sub_id: str, admin_id: int) -> str:
        if not self.db_pool:
            return "Chưa ghi nhận cảnh cáo: không kết nối được cơ sở dữ liệu."
        try:
            async with self.db_pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
                await cur.execute(
                    "SELECT s.submission_id, s.driver_phone, s.created_at, "
                    "to_jsonb(s)->>'work_owner', to_jsonb(s)->>'decision_version' "
                    "FROM survey_submissions s WHERE UPPER(s.submission_id)=UPPER(%s) FOR UPDATE",
                    (sub_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return "Không tìm thấy phiếu; chưa ghi nhận cảnh cáo."
                sid, phone, submitted_at, owner, decision_version = row
                if owner != "outbox_v2":
                    return "Cần đối soát phiếu theo luồng cũ trước khi ghi nhận cảnh cáo."
                await cur.execute(
                    "SELECT telegram_user_id, updated_at FROM phone_telegram_links WHERE phone_normalized=%s "
                    "AND source IN (%s,%s) AND telegram_user_id>0 AND updated_at<=%s FOR SHARE",
                    (normalize_vn_phone(phone), *VERIFIED_PHONE_LINK_SOURCES, submitted_at),
                )
                binding = await cur.fetchone()
                chat_id = binding[0] if binding else None
                key = f"telegram-driver-warning:{decision_version}"
                await cur.execute(
                    "SELECT payload->'policy'->>'skip_reason' FROM outbox_events WHERE aggregate_kind='submission' AND aggregate_id=%s "
                    "AND event_type='survey.warning' AND flow_target='TELEGRAM_NOTIFY' AND action_key=%s",
                    (sid, key),
                )
                existing = await cur.fetchone()
                if existing:
                    if existing[0] == "UNLINKED_RECIPIENT":
                        return "Cảnh cáo đã được ghi nhận trước đó nhưng không gửi: chưa có liên kết Telegram phù hợp."
                    return "Cảnh cáo đã được ghi nhận trước đó; không gửi thêm."
                payload = {
                    "message_class": "DRIVER_WARNING", "decision_independent": True,
                    "decision_version": int(decision_version), "recipient_kind": "driver",
                    "bot_role": "driver" if settings.TELEGRAM_DRIVER_BOT_TOKEN else "primary",
                    "chat_id": chat_id, "text": f"Cảnh cáo từ Ban Quản Trị về phiếu khảo sát #{sid}.",
                    "policy": {"enabled": bool(binding), "version": "warning-v1",
                               "skip_reason": None if binding else "UNLINKED_RECIPIENT"},
                }
                if binding:
                    payload["recipient_binding"] = {
                        "phone": normalize_vn_phone(phone), "telegram_user_id": chat_id,
                        "verified_at": binding[1].isoformat(), "submitted_at": submitted_at.isoformat(),
                    }
                event_id = await enqueue_action(conn, sid, "TELEGRAM_NOTIFY", "survey.warning", payload, action_key=key)
                await audit(conn, "DRIVER_WARNING", sid, f"admin:{admin_id}", {
                    "intent_id": event_id, "recipient_kind": "driver", "recipient_linked": bool(binding),
                })
                await conn.commit()
            if not binding:
                return "Đã ghi nhận cảnh cáo nhưng không gửi: tài xế chưa có liên kết Telegram đã xác thực phù hợp."
            return "Đã ghi nhận và xếp hàng gửi cảnh cáo; chưa xác nhận đã gửi."
        except Exception as exc:
            logger.error("Could not record driver warning (%s)", type(exc).__name__)
            return "Chưa ghi nhận được cảnh cáo. Vui lòng thử lại."

    @staticmethod
    async def _vehicle_change_candidate(conn, sub_id, *, lock=False):
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(
                "SELECT s.submission_id, s.driver_phone, s.raw_extracted_facts, s.vehicle_type, s.status, "
                "to_jsonb(s)->>'work_owner', to_jsonb(s)->>'decision_version' "
                "FROM survey_submissions s WHERE UPPER(s.submission_id)=UPPER(%s)" + (" FOR UPDATE" if lock else ""),
                (sub_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None, "Không tìm thấy phiếu."
        sid, phone, facts, vehicle_type, status, owner, version = row
        if owner != "outbox_v2" and settings.DURABLE_MODERATION_ENABLED:
            return None, "Cần đối soát phiếu theo luồng cũ trước khi đổi xe."
        if status == "APPROVED":
            return None, "Phiếu đã duyệt; vui lòng cung cấp yêu cầu đổi xe mới."
        candidate = stored_vehicle_candidate(phone, facts, vehicle_type)
        if not candidate:
            return None, "Cần bổ sung hoặc xác minh lại biển số và loại xe trong hồ sơ trước khi đổi xe."
        return (sid, version, *candidate), ""

    async def _request_vehicle_change(self, cq_id, chat_id, sub_id, admin_id):
        if not self.db_pool:
            await self.answer_callback_query(cq_id, "Chưa thể kiểm tra hồ sơ đổi xe.", show_alert=True)
            return
        try:
            async with self.db_pool.connection() as conn:
                candidate, note = await self._vehicle_change_candidate(conn, sub_id)
        except Exception as exc:
            logger.error("Could not inspect vehicle candidate (%s)", type(exc).__name__)
            candidate, note = None, "Chưa thể kiểm tra hồ sơ đổi xe."
        if not candidate:
            await self.answer_callback_query(cq_id, note, show_alert=True)
            return
        sid, _, _, plate, kind = candidate
        now = time.monotonic()
        self.vehicle_confirmations = {k: v for k, v in self.vehicle_confirmations.items() if v[0] > now}
        token = secrets.token_urlsafe(12)
        self.vehicle_confirmations[token] = (now + 600, chat_id, admin_id, candidate)
        await self.answer_callback_query(cq_id, "Vui lòng xác nhận trước khi đổi xe.")
        await self.send_message(chat_id, f"Xác nhận đổi xe theo phiếu #{html.escape(sid)} sang {plate} ({kind})?",
                                reply_markup=with_survey_copy({"inline_keyboard": [[
                                    {"text": "Xác nhận đổi xe", "callback_data": f"admin_cv:{token}"},
                                    {"text": "Hủy bỏ", "callback_data": f"admin_cv_cancel:{token}"},
                                ]]}, sid))

    async def _confirm_vehicle_change(self, cq_id, chat_id, msg_id, admin_id, token, confirmed):
        prompt = self.vehicle_confirmations.get(token)
        if not prompt or prompt[0] <= time.monotonic() or prompt[1:3] != (chat_id, admin_id):
            await self.answer_callback_query(cq_id, "Xác nhận không hợp lệ hoặc đã hết hạn.", show_alert=True)
            return
        self.vehicle_confirmations.pop(token)
        note = "Đã hủy thao tác đổi xe."
        if confirmed:
            sid, _, phone, plate, kind = prompt[3]
            try:
                async with self.db_pool.connection() as conn:
                    candidate, note = await self._vehicle_change_candidate(conn, sid, lock=True)
                    if candidate == prompt[3]:
                        changed, count, note = await VehiclePersistenceTracker().approve_vehicle_change(
                            phone, plate, kind, admin_id, reason=f"Telegram moderation: {sid}", conn=conn,
                        )
                        if changed:
                            await audit(conn, "VEHICLE_CHANGE", sid, f"admin:{admin_id}", {"changes_30d": count})
                            await conn.commit()
                    elif candidate:
                        note = "Hồ sơ đổi xe đã thay đổi. Vui lòng kiểm tra và xác nhận lại."
            except Exception as exc:
                logger.error("Could not commit vehicle change (%s)", type(exc).__name__)
                note = "Chưa ghi nhận được việc đổi xe. Vui lòng thử lại."
        await self.answer_callback_query(cq_id, note, show_alert=True)
        if msg_id:
            await self.edit_message_reply_markup(chat_id, msg_id, reply_markup={"inline_keyboard": []})

    def _clear_active_appeal(self, sub_id: str) -> None:
        """Drop any in-memory appeal lock for this submission (base-safe)."""
        active = getattr(self, "active_appeals", None)
        if not active:
            return
        canonical = normalize_submission_id(sub_id)
        for uid, sid in list(active.items()):
            if normalize_submission_id(sid) == canonical:
                active.pop(uid, None)

    async def _update_submission_status(
        self, sub_id: str, new_status: str, reason: str = "", *, actor: str = "system",
        requester_telegram_id: Optional[int] = None, expected_statuses: Optional[Tuple[str, ...]] = None,
        override: Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """Lock, authorize and commit before acknowledging any transition."""
        sub_id = normalize_submission_id(sub_id)
        if not sub_id:
            return False, "NOT_FOUND"
        if expected_statuses is None:
            expected_statuses = ("REJECTED",) if new_status == "APPEAL_PENDING" else ("MANUAL_REVIEW", "APPEAL_PENDING")
        durable_required = (settings.DURABLE_APPEALS_ENABLED if requester_telegram_id is not None
                            else settings.DURABLE_MODERATION_ENABLED)
        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor(row_factory=tuple_row) as cur:
                        if requester_telegram_id is None:
                            await cur.execute(
                                "SELECT status, COALESCE(to_jsonb(s)->>'work_owner', 'legacy'), moderated_at, submission_id "
                                "FROM survey_submissions s WHERE UPPER(submission_id)=UPPER(%s) FOR UPDATE",
                                (sub_id,),
                            )
                        else:
                            await self._select_driver_submission(cur, sub_id, requester_telegram_id, lock=True)
                        row = await cur.fetchone()
                        if row:
                            current_status = str(row[0] or "").upper()
                            work_owner = row[1] if len(row) > 1 else "legacy"
                            if override is not None:
                                moderated_at = row[2].isoformat() if row[2] else None
                                if current_status not in expected_statuses or moderated_at != override["moderated_at"]:
                                    return False, "DECISION_CHANGED"
                            if work_owner == "outbox_v2":
                                changed, status = await transition_submission(
                                    conn, sub_id, new_status, reason, actor=actor,
                                    expected_statuses=expected_statuses,
                                )
                                if not changed:
                                    return False, status
                            elif durable_required:
                                return False, "LEGACY_RECONCILIATION_REQUIRED"
                            elif current_status not in expected_statuses:
                                return False, current_status
                            else:
                                await cur.execute(
                                    "UPDATE survey_submissions SET status=%s, rejection_reason=%s, moderated_at=NOW() "
                                    "WHERE UPPER(submission_id)=UPPER(%s);",
                                    (new_status, reason or None, sub_id),
                                )
                            if override is not None:
                                await audit(conn, "VERDICT_OVERRIDE", row[3], actor, {"from": current_status, "to": new_status})
                            await conn.commit()
                            if new_status != "APPEAL_PENDING":
                                self._clear_active_appeal(sub_id)
                            return True, new_status
                        # No DB row: TEST/DEV submissions live in dev storage only.
                        # Fall through instead of reporting a phantom success.
            except Exception as e:
                logger.error("Submission transition failed (%s)", type(e).__name__)
                return False, "UNAVAILABLE"
        if override is not None:
            return False, "NOT_FOUND" if self.db_pool else "UNAVAILABLE"
        if requester_telegram_id is not None or durable_required or settings.ENVIRONMENT == "production":
            return False, "NOT_FOUND" if self.db_pool else "UNAVAILABLE"

        found_in_dev = False
        for fpath in (
            os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
            os.path.join(os.getcwd(), "data", "dev_submissions.json"),
        ):
            if os.path.exists(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        records = json.load(f)
                    changed = False
                    for r in records if isinstance(records, list) else []:
                        if normalize_submission_id(r.get("submission_id")) != sub_id:
                            continue
                        # dev_test_submissions.json also holds action-checklist stubs
                        # ({submission_id, action_status}); never treat them as submissions.
                        if not (r.get("status") or r.get("reason") or r.get("rejection_reason") or r.get("submitted_at")):
                            continue
                        found_in_dev = True
                        current_status = str(r.get("status") or "").upper()
                        if current_status not in expected_statuses:
                            return False, current_status
                        r["status"] = new_status
                        r["reason"] = reason
                        r["rejection_reason"] = reason
                        r["moderated_at"] = datetime.now(timezone.utc).isoformat()
                        changed = True
                    if changed:
                        with open(fpath, "w", encoding="utf-8") as f_out:
                            json.dump(records, f_out, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.error(f"Failed to update submission status in {fpath}: {e}")
        if found_in_dev:
            if new_status != "APPEAL_PENDING":
                self._clear_active_appeal(sub_id)
            return True, new_status
        return False, "NOT_FOUND"
    async def _answer_failed_decision(self, cq_id, chat_id, msg_id, sub_id, status):
        if status == "LEGACY_RECONCILIATION_REQUIRED":
            text = f"Phiếu #{sub_id} cần đối soát luồng xử lý cũ trước khi duyệt hoặc từ chối."
        elif status == "DECISION_CHANGED":
            text = f"Quyết định của phiếu #{sub_id} đã thay đổi. Mở lại /audit để xem trạng thái mới nhất."
        elif status in ("APPROVED", "REJECTED"):
            text = f"⚠️ Phiếu #{sub_id} đã được xử lý ({status})."
            if msg_id:
                await self.edit_message_reply_markup(chat_id, msg_id, reply_markup=with_survey_copy({"inline_keyboard": [[
                    {"text": f"🔒 Đã xử lý ({status})", "callback_data": "resolved_notice"}
                ]]}, sub_id))
        else:
            text = f"Chưa thay đổi được trạng thái của phiếu #{sub_id}."
        await self.answer_callback_query(cq_id, text, show_alert=True)

    @staticmethod
    async def _select_driver_submission(cur, sub_id, requester_telegram_id, *, lock=False):
        await cur.execute(
            f"SELECT s.status, COALESCE(to_jsonb(s)->>'work_owner', 'legacy') "
            f"FROM survey_submissions s JOIN phone_telegram_links link "
            f"ON link.phone_normalized={phone_normalization_sql('s.driver_phone')} "
            "WHERE UPPER(s.submission_id)=UPPER(%s) AND link.telegram_user_id=%s "
            "AND link.telegram_user_id>0 AND link.source IN (%s,%s) AND link.updated_at<=s.created_at "
            + ("FOR UPDATE OF s FOR SHARE OF link" if lock else ""),
            (sub_id, requester_telegram_id, *VERIFIED_PHONE_LINK_SOURCES),
        )

    async def _legacy_delivery_allowed(self, sub_id: str) -> bool:
        if not self.db_pool:
            return settings.ENVIRONMENT != "production" and not (
                settings.DURABLE_MODERATION_ENABLED or settings.DURABLE_APPEALS_ENABLED
            )
        try:
            async with self.db_pool.connection() as conn:
                async with conn.cursor(row_factory=tuple_row) as cur:
                    await cur.execute(
                        "SELECT to_jsonb(s)->>'work_owner' FROM survey_submissions s "
                        "WHERE UPPER(submission_id)=UPPER(%s)", (sub_id,),
                    )
                    row = await cur.fetchone()
                    return bool(row and row[0] in (None, "legacy"))
        except Exception as exc:
            logger.warning("Could not verify legacy notification ownership (%s)", type(exc).__name__)
            return False

    async def _notify_parties_of_decision(
        self, sub_id: str, new_status: str, reason: str = ""
    ) -> None:
        """Dispatches decision updates directly to Collaborator (CTV) and Driver."""
        clean_sub_id = normalize_submission_id(sub_id)
        dossier = await query_submission_dossier(clean_sub_id, db_pool=self.db_pool)
        if not dossier or "error" in dossier:
            logger.warning(f"Could not load dossier for #{clean_sub_id} to dispatch decision notification.")
            return

        driver_phone = dossier.get("driver_phone")
        driver_name = dossier.get("driver_name")
        collab_id = dossier.get("ctv_code") or dossier.get("collaborator_id")
        gross_rev = dossier.get("gross_income") or 0
        effective_reason = reason or dossier.get("rejection_reason") or ""
        form_id = dossier.get("form_id")

        raw = dossier.get("raw_data") or {}
        answers = (raw.get("form_data") or {}) if isinstance(raw, dict) else {}
        policy = answers.get("_delivery_policy") or {}
        form_settings = getattr(FORM_REGISTRY.get(form_id), "settings", None)
        source = policy.get("source") or (getattr(form_settings, "tg_response_source", None) or "ctv").strip().lower()
        recipient_kind = "ctv"
        recipient = collab_id
        if source == "none":
            logger.info(f"TG response disabled for form '{form_id}'; skipping verdict for #{clean_sub_id}.")
            return
        if source == "question":
            qid = policy.get("question_id") if policy else getattr(form_settings, "tg_response_question_id", None)
            recipient = answers.get(qid) if qid else None
            if isinstance(recipient, list):
                recipient = recipient[0] if recipient else None
            recipient_kind = "driver"
            if not recipient:
                logger.info(
                    f"TG response question '{qid}' has no answer for #{clean_sub_id}; nothing to notify."
                )
                return

        try:
            try:
                from routers.survey import dispatch_submission_telegram_notifications
            except ImportError:
                from main import dispatch_submission_telegram_notifications
            await dispatch_submission_telegram_notifications(
                submission_id=clean_sub_id,
                status_str=new_status,
                driver_phone=driver_phone,
                driver_name=driver_name,
                collaborator_id=recipient,
                reason_str=effective_reason,
                gross_revenue=gross_rev,
                form_id=form_id,
                recipient_kind=recipient_kind,
                ctv_ref=collab_id,
                submitted_at=dossier.get("submitted_at"),
            )
            logger.info(
                f"Dispatched decision verdict for #{clean_sub_id} to {recipient_kind} "
                f"({recipient}) and driver ({driver_phone}); collab on record: {collab_id}"
            )
        except Exception as e:
            logger.error(f"Failed to dispatch decision notification for #{clean_sub_id}: {e}")
    async def close_active_support(
        self,
        admin_chat_id: int,
        user_id: int,
        closed_by: str = "admin",
        admin_user_id: Optional[int] = None,
    ) -> None:
        """Closes live support conversation from admin side for the specific admin session."""
        convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(user_id, None)
        locked_admin = convo.get("locked_admin_id") if convo else admin_user_id
        if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
            del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
        if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
        if str(admin_chat_id) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(admin_chat_id)]

        await self.send_message(
            admin_chat_id,
            f"🔒 <b>Đã đóng phiên hội thoại hỗ trợ với người dùng <code>{user_id}</code>.</b>"
        )
        driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        if driver_token and driver_token != "mock_tg_token":
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{driver_token}/sendMessage"
            msg = (
                "🔒 <b>Phiên hỗ trợ trực tiếp với Ban Quản Trị đã kết thúc.</b>\n\n"
                "Cảm ơn bạn! Nếu có bất kỳ câu hỏi nào trong tương lai, bạn có thể gửi lại yêu cầu bất cứ lúc nào."
            )
            try:
                await client.post(url, json={"chat_id": user_id, "text": msg, "parse_mode": "HTML"})
            except Exception as e:
                logger.error(f"Failed to notify user about closed session: {e}")


    async def handle_update(self, update: dict) -> None:
        """Dispatches an incoming update payload to message or callback handlers."""
        if "my_chat_member" in update:
            mcm = update["my_chat_member"]
            chat = mcm.get("chat") or {}
            chat_id = chat.get("id")
            title = chat.get("title") or "Nhóm Quản Trị"
            logger.info(f"🌟 [{self.role.upper()} BOT JOINED CHAT] '{title}' (ID: {chat_id})")
            if chat_id and self.role == "admin":
                await self.send_message(
                    chat_id,
                    f"👋 <b>Trust Bridge Admin Bot đã kết nối thành công vào nhóm!</b>\n\n"
                    f"• <b>Tên nhóm:</b> {title}\n"
                    f"• <b>Group Chat ID:</b> <code>{chat_id}</code>\n\n"
                    f"👉 Hãy lưu mã <code>{chat_id}</code> vào cấu hình <code>TELEGRAM_ADMIN_GROUP_ID</code> trên máy chủ để nhận thông báo kiểm định!",
                )

        if "message" in update and update["message"]:
            msg = update["message"]
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = msg.get("text") or ""
            from_user = msg.get("from") or {}
            admin_name = from_user.get("first_name", "Quản trị viên")

            if chat_id and text:
                from_user_id = from_user.get("id")
                if len(text) <= 4096:
                    handled, resumed = await self._argument_reply(chat_id, from_user_id, msg.get("reply_to_message"), text)
                    if handled:
                        if resumed:
                            await self.handle_command(chat_id, resumed, from_user_id=from_user_id)
                        return

                # Check for /close command in admin group - closes ONLY the active session of this specific admin!
                if text.strip().lower() in ("/close", "/end", "/ketthuc"):
                    active_user = (
                        (ADMIN_ACTIVE_CONVERSATIONS.get(from_user_id) if from_user_id else None)
                        or (ADMIN_GROUP_ACTIVE_SESSION.get(str(from_user_id)) if from_user_id else None)
                        or ADMIN_GROUP_ACTIVE_SESSION.get(str(chat_id))
                    )
                    if active_user:
                        await self.close_active_support(chat_id, active_user, closed_by="admin", admin_user_id=from_user_id)
                        return
                    else:
                        await self.send_message(chat_id, "ℹ️ Hiện bạn không có phiên hội thoại hỗ trợ nào đang mở.")
                        return

                # Check if message is replying to a support ticket, or if session is active
                reply_to = msg.get("reply_to_message") or {}
                reply_text = reply_to.get("text") or reply_to.get("caption") or ""
                target_user = None

                if reply_text:
                    m = re.search(r"(?:Chat ID|User ID|ID):\s*(?:<code>)?(\d+)(?:</code>)?", reply_text, re.IGNORECASE)
                    if m:
                        target_user = int(m.group(1))

                # Guard cross-admin leakage:
                if target_user:
                    convo = ACTIVE_SUPPORT_CONVERSATIONS.get(target_user)
                    if convo and convo.get("locked_admin_id"):
                        if from_user_id and from_user_id != convo["locked_admin_id"]:
                            logger.info(
                                f"Blocked cross-convo reply: admin {admin_name} ({from_user_id}) tried to message user {target_user} "
                                f"locked to {convo.get('locked_admin_name')} ({convo.get('locked_admin_id')})"
                            )
                            return
                    else:
                        # 1-admin-1-convo: If admin attempts to start a new convo via reply, ensure they don't already hold one!
                        if from_user_id and from_user_id in ADMIN_ACTIVE_CONVERSATIONS:
                            held = ADMIN_ACTIVE_CONVERSATIONS[from_user_id]
                            if held != target_user and held in ACTIVE_SUPPORT_CONVERSATIONS:
                                logger.info(
                                    f"Blocked admin {admin_name} ({from_user_id}) from taking user {target_user} "
                                    f"because they already hold user {held}"
                                )
                                await self.send_message(
                                    chat_id,
                                    f"⚠️ {admin_name}, bạn đang trong phiên hỗ trợ với thành viên <code>{held}</code>!\n"
                                    f"Mỗi quản trị viên chỉ được hỗ trợ 1 thành viên tại một thời điểm. "
                                    f"Vui lòng gõ <code>/close</code> phiên hiện tại trước khi nhận phiên mới.",
                                )
                                return
                else:
                    # Message is typed into group without replying: route EXCLUSIVELY by this admin's active session!
                    if from_user_id and from_user_id in ADMIN_ACTIVE_CONVERSATIONS:
                        target_user = ADMIN_ACTIVE_CONVERSATIONS[from_user_id]
                    elif from_user_id and str(from_user_id) in ADMIN_GROUP_ACTIVE_SESSION:
                        target_user = ADMIN_GROUP_ACTIVE_SESSION[str(from_user_id)]
                    elif not ACTIVE_SUPPORT_CONVERSATIONS and (ADMIN_GROUP_ACTIVE_SESSION.get(str(chat_id)) or ADMIN_GROUP_ACTIVE_SESSION.get(chat_id)):
                        # Fall back to group-level only if no conflicting active conversations exist
                        target_user = ADMIN_GROUP_ACTIVE_SESSION.get(str(chat_id)) or ADMIN_GROUP_ACTIVE_SESSION.get(chat_id)

                if target_user and not text.startswith("/"):
                    convo = ACTIVE_SUPPORT_CONVERSATIONS.get(target_user)

                    # If session is locked to a specific admin, only relay messages from that admin!
                    if convo and convo.get("locked_admin_id"):
                        if from_user_id and from_user_id != convo["locked_admin_id"]:
                            logger.info(
                                f"Ignored group message from non-assigned admin {admin_name} ({from_user_id}) "
                                f"for session locked to {convo.get('locked_admin_name')} ({convo.get('locked_admin_id')})"
                            )
                            return

                    ACTIVE_SUPPORT_CONVERSATIONS[target_user] = convo or {
                        "user_id": target_user,
                        "admin_chat_id": chat_id,
                        "locked_admin_id": from_user_id,
                        "locked_admin_name": admin_name,
                        "opened_at": time.time(),
                    }
                    if from_user_id:
                        ADMIN_ACTIVE_CONVERSATIONS[from_user_id] = target_user
                        ADMIN_GROUP_ACTIVE_SESSION[str(from_user_id)] = target_user
                    ADMIN_GROUP_ACTIVE_SESSION[str(chat_id)] = target_user
                    driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
                    if driver_token and driver_token != "mock_tg_token":
                        client = await self.get_client()
                        url = f"https://api.telegram.org/bot{driver_token}/sendMessage"
                        relay_text = f"💬 <b>Phản hồi từ Ban Quản Trị ({admin_name}):</b>\n\n{text}"
                        try:
                            resp = await client.post(url, json={"chat_id": target_user, "text": relay_text, "parse_mode": "HTML"})
                            if resp.status_code == 200:
                                logger.info(f"Relayed admin message to user {target_user}")
                                await self.send_message(
                                    chat_id,
                                    f"✅ <i>Đã gửi phản hồi tới người dùng (ID: <code>{target_user}</code>). Phiên hội thoại đang mở, gõ <code>/close</code> để kết thúc.</i>"
                                )
                            else:
                                logger.error(f"Failed to relay admin message: {resp.text}")
                                await self.send_message(
                                    chat_id,
                                    f"⚠️ Không thể chuyển tiếp tin nhắn tới người dùng (Mã lỗi HTTP {resp.status_code})."
                                )
                        except Exception as e:
                            logger.error(f"Failed to relay message to user: {e}")
                    return
                if len(text) <= 4096 and text.startswith("/"):
                    await self.handle_command(
                        chat_id, text, reply_to_message=msg.get("reply_to_message"), from_user_id=from_user_id
                    )
        elif "callback_query" in update and update["callback_query"]:
            await self.handle_callback_query(update["callback_query"])
    async def poll_once(self) -> int:
        """Executes a single long-polling request against getUpdates."""
        if not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            logger.warning(
                "TELEGRAM_BOT_TOKEN for %s is not configured. Sleeping %.1fs...",
                self.role,
                self.backoff_delay,
            )
            await asyncio.sleep(self.backoff_delay)
            self.backoff_delay = min(self.backoff_delay * 1.5, self.max_backoff)
            return 0

        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {
            "offset": self.offset,
            "timeout": self.polling_timeout,
            "allowed_updates": json.dumps(["message", "callback_query", "my_chat_member"]),
        }

        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 409:
                logger.warning(
                    f"[{self.role.upper()} BOT] Telegram 409 Conflict: another getUpdates instance running. Backing off {self.backoff_delay:.1f}s"
                )
                await asyncio.sleep(self.backoff_delay)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_sec = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_delay
                logger.warning(f"[{self.role.upper()} BOT] Telegram 429 Too Many Requests hit! Backing off for {sleep_sec:.1f}s")
                await asyncio.sleep(sleep_sec)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code >= 500:
                logger.warning(f"[{self.role.upper()} BOT] Telegram server error {resp.status_code}. Backing off for {self.backoff_delay:.1f}s")
                await asyncio.sleep(self.backoff_delay)
                self.backoff_delay = min(self.backoff_delay * 2, self.max_backoff)
                return 0
            elif resp.status_code != 200:
                logger.warning(f"[{self.role.upper()} BOT] Telegram getUpdates returned status {resp.status_code}. Backing off {self.backoff_delay:.1f}s")
                await asyncio.sleep(self.backoff_delay)
                self.backoff_delay = min(self.backoff_delay * 1.5, self.max_backoff)
                return 0

            self.backoff_delay = 1.0
            data = resp.json()
            updates = data.get("result", [])
            for upd in updates:
                upd_id = upd.get("update_id", 0)
                if upd_id >= self.offset:
                    self.offset = upd_id + 1
                try:
                    await self.handle_update(upd)
                except Exception as ex:
                    logger.error(f"[{self.role.upper()} BOT] Error handling update {upd_id}: {ex}")

            return len(updates)
        except httpx.TimeoutException:
            return 0
        except Exception as e:
            logger.error(f"[{self.role.upper()} BOT] Telegram long-polling connection error: {e}")
            await asyncio.sleep(3.0)
            return 0

    async def configure_bot_commands(self) -> None:
        """Registers commands appropriate for this bot's role."""
        if not self.bot_token or self.bot_token.lower() in ("mock_tg_token", "mock", "none", ""):
            return
        commands = [
            {"command": "start", "description": "Bắt đầu / liên kết CTV"},
            {"command": "forms", "description": "Biểu mẫu đang mở"},
            {"command": "surveys", "description": "Khảo sát của tôi"},
            {"command": "quota", "description": "Mục tiêu nộp phiếu của tôi"},
            {"command": "linkphone", "description": "Liên kết số điện thoại với Telegram"},
            {"command": "appeal", "description": "Phúc khảo một phiếu"},
            {"command": "support", "description": "Yêu cầu hỗ trợ"},
            {"command": "help", "description": "Hướng dẫn sử dụng"},
            {"command": "close", "description": "Kết thúc phiên hỗ trợ"},
        ]
        if self.role == "admin":
            commands = [
                {"command": "check", "description": "Mở hồ sơ phiếu"},
                {"command": "audit", "description": "Đổi quyết định đã chốt"},
                {"command": "forms", "description": "Biểu mẫu đang mở"},
                {"command": "surveys", "description": "Tra cứu phiếu theo CTV hoặc số điện thoại"},
                *[item for item in commands if item["command"] in ("help", "close")],
                {"command": "quota", "description": "Mục tiêu theo mã CTV"},
                {"command": "stats", "description": "Thống kê tiến độ duyệt phiếu"},
            ]
        try:
            client = await self.get_client()
            await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/setMyCommands",
                json={"commands": commands},
                timeout=10.0,
            )
        except Exception as e:
            logger.debug(f"setMyCommands failed: {e}")

    async def start_polling(self) -> None:
        """Main long-polling loop."""
        self.is_running = True
        await self.configure_bot_commands()
        masked = f"{self.bot_token[:8]}...{self.bot_token[-4:]}" if self.bot_token and len(self.bot_token) > 12 else "(none)"
        logger.info(f"[{self.role.upper()} BOT] Long-polling runner started (token={masked})")
        try:
            while self.is_running:
                await self.poll_once()
        finally:
            await self.aclose()

    def stop(self) -> None:
        """Signals the long-polling loop to terminate."""
        self.is_running = False


class DriverBotRunner(TelegramPollingRunner):
    """
    Dedicated Collaborator / Driver 1-on-1 Direct Message Bot (@TrustBridgeVN_bot).
    Features:
    - 📱 One-tap Phone Number Verification (Contact Share)
    - 📋 View Submissions & Payout Status (/surveys, /my_surveys)
    - 📸 In-chat Replacement of Blurry Images (Laplacian Blur Check & Instant Resubmit)
    - ⚖️ 1-Click Appeal of Rejected Submissions (Max 3 / 8h Rate Limited)
    - 🆘 Direct Admin Help Request (/support, /hotro)
    - 🛡️ Decree 13 Right to Personal Data Erasure (/scrub_my_data)
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        polling_timeout: int = 30,
        db_pool=None,
    ):
        token = bot_token or getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        super().__init__(bot_token=token, polling_timeout=polling_timeout, db_pool=db_pool, role="driver")
        # In-memory tracking for pending replacement photo uploads: chat_id -> {sub_id, slot, timestamp}
        self.pending_fixes: Dict[int, dict] = {}
        # In-memory tracking for appeal rate limits: phone -> list of timestamps
        self.appeal_history: Dict[str, List[float]] = {}
        # In-memory tracking for support rate limits: user_id -> list of timestamps
        self.support_history: Dict[Union[int, str], List[float]] = {}
        # In-memory tracking for pending support message input: chat_id -> True or dict
        self.pending_support: Dict[int, Any] = {}
        # In-memory tracking for active help tickets awaiting admin response
        self.pending_help_tickets: Dict[int, dict] = {}
        # In-memory tracking for active appeals: user_id -> sub_id
        self.active_appeals: Dict[int, str] = {}

    def get_driver_reply_keyboard(self) -> dict:
        """Returns the persistent 1-tap quick action keyboard for drivers & collaborators."""
        return {
            "keyboard": [
                [{"text": "📱 Xác Thực Số Điện Thoại", "request_contact": True}],
                [{"text": "📋 Khảo Sát Của Tôi"}],
                [{"text": "🆘 Yêu Cầu Hỗ Trợ"}, {"text": "❓ Trợ Giúp"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
    async def get_driver_phone(self, telegram_user_id: int) -> Optional[str]:
        """Resolve only the sender's verified standalone phone binding."""
        from core.phone_links import get_verified_phone_for_telegram_user

        return await get_verified_phone_for_telegram_user(telegram_user_id, db_pool=self.db_pool)


    async def download_telegram_photo(self, file_id: str) -> bytes:
        """Downloads high-res photo bytes from Telegram Bot API."""
        client = await self.get_client()
        url = f"https://api.telegram.org/bot{self.bot_token}/getFile"
        res = await client.get(url, params={"file_id": file_id})
        data = res.json()
        file_path = data["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        photo_resp = await client.get(download_url)
        return photo_resp.content

    async def handle_contact_sharing(self, chat_id: int, user_id: int, contact: dict, from_user: dict) -> None:
        """Verify a private own-contact, then enroll or replace its registered CTV account."""
        sender_id = from_user.get("id")
        contact_id = contact.get("user_id")
        if (
            chat_id != user_id or type(sender_id) is not int or type(contact_id) is not int
            or sender_id <= 0 or sender_id != user_id or contact_id != sender_id
        ):
            await self.send_message(
                chat_id,
                "⚠️ Chỉ có thể xác thực danh bạ của chính bạn. "
                "Vui lòng dùng nút [📱 Xác Thực Số Điện Thoại] để chia sẻ số của tài khoản này.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        from core.phone_links import normalize_vn_phone, upsert_phone_telegram_link

        phone = normalize_vn_phone(contact.get("phone_number"))
        if not phone:
            await self.send_message(chat_id, "⚠️ Số điện thoại không hợp lệ; chưa xác thực liên kết.")
            return
        from core.ctv_onboarding import complete_ctv_onboarding

        name = " ".join(str(contact.get(part) or from_user.get(part) or "") for part in ("first_name", "last_name")).strip() or "Thành viên"
        try:
            ctv_code = await complete_ctv_onboarding(self.db_pool, sender_id, phone, name)
        except ValueError:
            await self.send_message(
                chat_id,
                "⚠️ Chưa thể liên kết CTV. Cần xác thực đúng số đã đăng ký, CTV còn hoạt động và không bị trùng số. "
                "Đổi số điện thoại hoặc giải quyết tranh chấp cần quản trị viên hỗ trợ; chưa thay đổi danh bạ CTV.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return
        except Exception as exc:
            logger.warning("CTV contact verification failed (%s)", type(exc).__name__)
            await self.send_message(chat_id, "⚠️ Chưa thể lưu xác thực. Vui lòng thử lại sau.")
            return
        stored = bool(ctv_code) or await upsert_phone_telegram_link(
            phone, sender_id, source="verified_contact", db_pool=self.db_pool
        )
        if not stored:
            await self.send_message(
                chat_id,
                "⚠️ Chưa thể xác thực liên kết. Hệ thống không tự chuyển số giữa các tài khoản; "
                "nếu số đã đổi chủ, vui lòng liên hệ quản trị viên để thu hồi và xác minh lại.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        if ctv_code:
            await self.send_message(
                chat_id,
                f"✅ <b>Đã xác thực và liên kết CTV <code>{html.escape(ctv_code)}</code>.</b>\n\n"
                f"• <b>Số điện thoại:</b> <code>{phone[:3]}****{phone[-3:]}</code>\n"
                "Kết quả duyệt / từ chối phiếu do bạn giới thiệu sẽ được thông báo tại đây.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return
        masked_phone = phone[:3] + "****" + phone[-3:]
        welcome_text = (
            "✅ <b>Xác thực số điện thoại thành công!</b>\n\n"
            f"• <b>Họ tên:</b> {html.escape(name)}\n"
            f"• <b>Số điện thoại liên kết:</b> <code>{masked_phone}</code>\n"
            "• <b>Liên kết:</b> Đã xác thực cho tài khoản Telegram này.\n\n"
            "Thông báo khảo sát được cấu hình gửi theo số điện thoại có thể dùng liên kết này. "
            "Liên kết không cấp quyền tra cứu lịch sử trên web.\n\n"
            "👉 Bấm <b>[📋 Khảo Sát Của Tôi]</b> để xem các phiếu của số đã xác thực."
        )
        await self.send_message(chat_id, welcome_text, reply_markup=self.get_driver_reply_keyboard())

    async def _store_replacement_photo(
        self,
        sub_id: str,
        slot_name: str,
        raw_bytes: bytes,
        requester_telegram_id: Optional[int] = None,
        approve_reason: Optional[str] = None,
    ) -> bool:
        """
        Persists a Telegram-replaced photo into the raw store as WebP.

        Telegram always delivers JPEG, so the bytes are normalized before they hit
        disk. Only a concrete `slot_<n>` target can be replaced; the generic
        `slot_photo` placeholder carries no index and is left unpersisted.
        """
        from services.submission_media import write_replacement

        token = str(slot_name or "").strip()
        flat_match = re.fullmatch(r"flat_(\d+)", token)
        slot_match = re.fullmatch(r"(?:(?P<qid>[A-Za-z0-9_.-]+)__)?slot_(?P<slot>\d+)", token)
        if not flat_match and not slot_match:
            return False
        if not self.db_pool:
            # Without the authoritative transaction store there is no way to
            # authenticate the requester, binding epoch, or committed transition.
            raise PermissionError("authoritative submission store is unavailable")
        webp_bytes = await asyncio.to_thread(normalize_image_to_webp, raw_bytes)
        if not webp_bytes:
            return False
        flat_idx = int(flat_match.group(1)) if flat_match else None
        slot_idx = int(slot_match.group("slot")) if slot_match else None
        qid = slot_match.group("qid") if slot_match else None
        await write_replacement(
            self.db_pool,
            sub_id,
            slot_idx,
            webp_bytes,
            question_id=qid,
            flat_index=flat_idx,
            requester_telegram_id=requester_telegram_id,
            approve_reason=approve_reason,
        )
        return True

    async def handle_photo_upload(self, chat_id: int, user_id: int, photos: list) -> None:
        """Handles in-chat replacement of blurry survey photos."""
        if not chat_id or chat_id != user_id:
            return
        highest_res = photos[-1]
        file_id = highest_res["file_id"]

        fix_info = self.pending_fixes.get(chat_id)
        sub_id = fix_info.get("sub_id") if fix_info else None
        slot_name = fix_info.get("slot", "flat_0") if fix_info else "Ảnh khảo sát"

        try:
            photo_bytes = await self.download_telegram_photo(file_id)
            pil_img = Image.open(io.BytesIO(photo_bytes))
            is_sharp, blur_score = compute_laplacian_blur_score(pil_img, threshold=35.0)

            if not is_sharp:
                await self.send_message(
                    chat_id,
                    f"⚠️ <b>Ảnh gửi chưa đạt chuẩn độ nét!</b>\n\n"
                    f"• <b>Độ nét hiện tại:</b> <code>{blur_score:.1f}</code> / 35.0 (Chưa đạt)\n"
                    f"• <b>Mục cần bổ sung:</b> <b>{slot_name}</b>\n\n"
                    f"Vui lòng lau sạch ống kính camera, giữ chắc tay và chụp lại trong điều kiện đủ ánh sáng.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return

            # Image is sharp! Authorize the concrete requester and commit the
            # manifest revision plus approval in the same database transaction.
            if sub_id:
                reason = f"Bổ sung ảnh đạt chuẩn qua Telegram DM (Độ nét: {blur_score:.1f})"
                try:
                    stored = await self._store_replacement_photo(
                        sub_id,
                        slot_name,
                        photo_bytes,
                        requester_telegram_id=user_id,
                        approve_reason=reason,
                    )
                except PermissionError:
                    await self.send_message(
                        chat_id,
                        "⚠️ Bạn không có quyền bổ sung ảnh cho phiếu này. "
                        "Vui lòng dùng tài khoản đã xác thực đúng số điện thoại của phiếu.",
                        reply_markup=self.get_driver_reply_keyboard(),
                    )
                    return
                except (ValueError, FileNotFoundError) as exc:
                    note = ("Phiếu cần đối soát luồng cũ trước khi bổ sung ảnh."
                            if str(exc) == "LEGACY_RECONCILIATION_REQUIRED"
                            else "Phiếu này không còn nhận bổ sung ảnh hoặc mục ảnh không hợp lệ.")
                    await self.send_message(
                        chat_id,
                        f"⚠️ {note}",
                        reply_markup=self.get_driver_reply_keyboard(),
                    )
                    return
                if not stored:
                    await self.send_message(
                        chat_id,
                        "⚠️ Mục ảnh không hợp lệ; chưa có thay đổi nào được lưu.",
                        reply_markup=self.get_driver_reply_keyboard(),
                    )
                    return
                if chat_id in self.pending_fixes:
                    del self.pending_fixes[chat_id]

                confirm_text = (
                    f"🎉 <b>Bổ sung ảnh thành công và đạt chuẩn!</b>\n\n"
                    f"• <b>Mã phiếu:</b> <code>#{sub_id}</code>\n"
                    f"• <b>Độ nét đo được:</b> <code>{blur_score:.1f}</code> (Đạt chuẩn > 35.0)\n"
                    f"• <b>Trạng thái mới:</b> <b>ĐÃ DUYỆT THÀNH CÔNG ✅</b>"
                )
                await self.send_message(chat_id, confirm_text, reply_markup=self.get_driver_reply_keyboard())

                # Alert Admin Group via Admin Bot
                admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
                admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-1001234567890")
                if admin_token and admin_token != "mock_tg_token" and await self._legacy_delivery_allowed(sub_id):
                    client = await self.get_client()
                    alert_url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
                    alert_text = (
                        f"🔄 <b>Thành viên đã bổ sung ảnh đạt chuẩn qua Telegram DM!</b>\n\n"
                        f"• <b>Mã phiếu:</b> #{sub_id}\n"
                        f"• <b>Độ nét:</b> {blur_score:.1f}\n"
                        f"• <b>Trạng thái:</b> Chuyển thành APPROVED ✅"
                    )
                    try:
                        await client.post(alert_url, json={"chat_id": admin_group, "text": alert_text, "parse_mode": "HTML"})
                    except Exception as ex:
                        logger.debug(f"Could not dispatch admin alert: {ex}")
            else:
                await self.send_message(
                    chat_id,
                    f"📸 <b>Đã nhận ảnh với độ nét đạt chuẩn ({blur_score:.1f})!</b>\n\n"
                    f"Hiện tại không có phiếu nào đang chờ bổ sung ảnh. Nếu bạn muốn kiểm tra các phiếu đã nộp, bấm <b>[📋 Khảo Sát Của Tôi]</b>.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
        except Exception as e:
            logger.error(f"Failed to process photo upload: {e}")
            await self.send_message(
                chat_id,
                "⚠️ Đã có lỗi khi tải hoặc phân tích ảnh. Vui lòng thử lại sau.",
                reply_markup=self.get_driver_reply_keyboard(),
            )

    async def handle_driver_surveys_lookup(self, chat_id: int, user_id: int) -> None:
        """Private history only within the sender's current verified ownership period."""
        if chat_id != user_id:
            await self.send_message(chat_id, "Vui lòng mở cuộc trò chuyện riêng với bot để xem lịch sử khảo sát.")
            return
        from core.phone_links import get_verified_phone_binding_for_telegram_user
        binding = await get_verified_phone_binding_for_telegram_user(user_id, db_pool=self.db_pool, sources=("verified_contact",))
        if not binding:
            await self.send_message(
                chat_id,
                "ℹ️ <b>Bạn chưa xác thực số điện thoại.</b>\n\n"
                "Hãy dùng nút [📱 Xác Thực Số Điện Thoại] để chia sẻ danh bạ của chính bạn. "
                "Số nhập qua /linkphone không cho phép tra cứu lịch sử.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return
        phone, verified_since = binding
        submissions = []

        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"""
                            SELECT s.submission_id, s.status, s.rejection_reason, s.created_at
                            FROM survey_submissions AS s
                            {TARGET_PHONE_JOIN}
                            JOIN phone_telegram_links AS link
                              ON link.phone_normalized = {phone_normalization_sql("target.phone")}
                            WHERE link.phone_normalized = %s AND link.telegram_user_id = %s
                              AND link.source = 'verified_contact' AND link.updated_at = %s
                              AND s.created_at >= link.updated_at
                            {HISTORY_ORDER}
                            """,
                            (phone, user_id, verified_since),
                        )
                        rows = await cur.fetchall()
                        for row in rows:
                            submissions.append({
                                "submission_id": row[0],
                                "status": row[1],
                                "reason": row[2] or "",
                                "created_at": row[3].strftime("%d/%m/%Y %H:%M") if hasattr(row[3], "strftime") else str(row[3])[:16],
                            })
            except Exception as e:
                logger.warning("Could not read ownership-scoped bot history (%s)", type(e).__name__)
                await self.send_message(chat_id, "Chưa thể kiểm tra lịch sử an toàn. Vui lòng thử lại sau.")
                return

        # Check dev test storage as fallback
        if not self.db_pool and settings.ENVIRONMENT != "production":
            try:
                with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                    records = json.load(f)
                    for r in records:
                        data = r.get("form_data") or {}
                        policy = data.get("_delivery_policy") or {}
                        if policy.get("source") != "question" or policy.get("inferred"):
                            continue
                        target_phone = data.get(policy.get("question_id"))
                        if isinstance(target_phone, list):
                            target_phone = target_phone[0] if target_phone else None
                        if normalize_vn_phone(target_phone) != phone:
                            continue
                        try:
                            submitted_at = datetime.fromisoformat(str(r.get("submitted_at") or "").replace("Z", "+00:00"))
                        except (TypeError, ValueError):
                            continue
                        if submitted_at.utcoffset() is None or submitted_at < verified_since:
                            continue
                        submissions.append({
                            "submission_id": r.get("submission_id", "DEV"),
                            "status": r.get("status", "SUBMITTED"),
                            "reason": r.get("reason", ""),
                            "created_at": submitted_at.isoformat(),
                        })
            except Exception:
                pass
            submissions = sorted(submissions, key=lambda sub: datetime.fromisoformat(sub["created_at"]), reverse=True)[:5]

        if not submissions:
            msg = (
                f"📋 <b>Chưa tìm thấy phiếu khảo sát nào cho số điện thoại <code>{phone}</code>.</b>\n\n"
                "Chỉ hiển thị phiếu từ khi liên kết được xác thực. Phiếu cũ cần quản trị viên hỗ trợ."
            )
            await self.send_message(chat_id, msg, reply_markup=self.get_driver_reply_keyboard())
            return

        lines = ["📋 <b>Danh Sách Phiếu Khảo Sát Của Bạn:</b>\n"]
        buttons = []

        for idx, sub in enumerate(submissions, 1):
            sub_id = sub["submission_id"]
            st = sub["status"]
            if st == "APPROVED":
                badge = "ĐÃ DUYỆT ✅"
            elif st == "REJECTED":
                badge = "TỪ CHỐI ❌"
            elif st == "APPEAL_PENDING":
                badge = "CHỜ PHÚC KHẢO ⚖️"
            else:
                badge = "ĐANG XỬ LÝ ⏳"

            item_text = f"{idx}. <b>#{html.escape(sub_id)}</b> ({html.escape(sub['created_at'][:16])})\n   • Trạng thái: <b>{badge}</b>"
            if sub["reason"]:
                item_text += f"\n   • Lý do: <i>{html.escape(str(sub['reason'])[:300])}</i>"
            lines.append(item_text)

            if st == "REJECTED" and await self._get_submission_status(sub_id, requester_telegram_id=user_id) == "REJECTED":
                buttons.append([
                    {"text": f"📸 Gửi lại ảnh #{sub_id}", "callback_data": f"driver_fix:{sub_id}:flat_0"},
                ])
            buttons.append([
                {"text": f"Sao chép #{sub_id}", "copy_text": {"text": sub_id}},
            ])

        inline_markup = {"inline_keyboard": buttons} if buttons else None
        await self.send_tracked(f"surveys:{user_id}", chat_id, "\n\n".join(lines), reply_markup=inline_markup)

    async def _get_submission_status(self, sub_id: str, *, requester_telegram_id: Optional[int] = None) -> Optional[str]:
        """Case-insensitive submission lookup across DB and dev stores."""
        canonical = normalize_submission_id(sub_id)
        if not canonical:
            return None
        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor(row_factory=tuple_row) as cur:
                        if requester_telegram_id is None:
                            await cur.execute(
                                "SELECT status FROM survey_submissions WHERE UPPER(submission_id)=UPPER(%s) LIMIT 1",
                                (canonical,),
                            )
                        else:
                            await self._select_driver_submission(cur, canonical, requester_telegram_id)
                        row = await cur.fetchone()
                        if row:
                            return str(row[0] or "MANUAL_REVIEW").upper()
            except Exception as e:
                logger.debug(f"Submission status lookup failed for #{canonical}: {e}")
        if requester_telegram_id is not None:
            return None

        for fpath in (
            os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
            DEV_STORAGE_FILE,
        ):
            if not os.path.exists(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    records = json.load(f)
                for r in records if isinstance(records, list) else []:
                    if normalize_submission_id(r.get("submission_id")) != canonical:
                        continue
                    # Skip action-checklist stubs; they are not submissions.
                    if not (r.get("status") or r.get("reason") or r.get("rejection_reason") or r.get("submitted_at")):
                        continue
                    return str(r.get("status") or "MANUAL_REVIEW").upper()
            except Exception as e:
                logger.debug(f"Dev submission status lookup failed in {fpath}: {e}")
        return None

    async def _get_submission_form(self, sub_id: str, *, requester_telegram_id: Optional[int] = None) -> Optional[str]:
        """Form id for a submission, honoring the same ownership rule as its status."""
        canonical = normalize_submission_id(sub_id)
        if not canonical:
            return None
        if self.db_pool:
            try:
                async with self.db_pool.connection() as conn:
                    async with conn.cursor(row_factory=tuple_row) as cur:
                        if requester_telegram_id is None:
                            await cur.execute(
                                "SELECT form_id FROM survey_submissions WHERE UPPER(submission_id)=UPPER(%s) LIMIT 1",
                                (canonical,),
                            )
                        else:
                            await cur.execute(
                                f"SELECT s.form_id FROM survey_submissions s JOIN phone_telegram_links link "
                                f"ON link.phone_normalized={phone_normalization_sql('s.driver_phone')} "
                                "WHERE UPPER(s.submission_id)=UPPER(%s) AND link.telegram_user_id=%s "
                                "AND link.telegram_user_id>0 AND link.source IN (%s,%s) AND link.updated_at<=s.created_at",
                                (canonical, requester_telegram_id, *VERIFIED_PHONE_LINK_SOURCES),
                            )
                        row = await cur.fetchone()
                        if row:
                            return str(row[0] or "").strip() or None
            except Exception as e:
                logger.debug(f"Submission form lookup failed for #{canonical}: {e}")
        if requester_telegram_id is not None:
            return None
        for fpath in (
            os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
            DEV_STORAGE_FILE,
        ):
            if not os.path.exists(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    records = json.load(f)
                for r in records if isinstance(records, list) else []:
                    if normalize_submission_id(r.get("submission_id")) != canonical:
                        continue
                    if not (r.get("status") or r.get("reason") or r.get("rejection_reason") or r.get("submitted_at")):
                        continue
                    return str(r.get("form_id") or "").strip() or None
            except Exception as e:
                logger.debug(f"Dev submission form lookup failed in {fpath}: {e}")
        return None

    async def _prune_stale_active_appeals(self) -> None:
        """Drop in-memory appeals whose submission is no longer APPEAL_PENDING."""
        for uid, sid in list(self.active_appeals.items()):
            status = await self._get_submission_status(sid)
            if status != "APPEAL_PENDING":
                self.active_appeals.pop(uid, None)
                logger.info(f"Pruned stale active appeal for user {uid}, submission {normalize_submission_id(sid)} (status={status})")

    async def handle_driver_appeal(self, chat_id: int, user_id: int, sub_id: str) -> None:
        """Handles driver appeal request with 2-hour rate limit (max 5) and active guard."""
        if not chat_id or chat_id != user_id:
            await self.send_message(chat_id, "Vui lòng gửi phúc khảo trong cuộc trò chuyện riêng với bot.")
            return
        canonical = normalize_submission_id(sub_id)
        if not canonical:
            await self.send_message(
                chat_id,
                "❌ <b>Không tìm thấy phiếu khảo sát.</b>\n\nVui lòng kiểm tra lại mã phiếu.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        # Reconcile in-memory checks against the DB/dev source of truth first.
        await self._prune_stale_active_appeals()
        status = await self._get_submission_status(canonical, requester_telegram_id=user_id)
        if status is None:
            await self.send_message(
                chat_id,
                f"❌ <b>Không tìm thấy phiếu #{canonical}.</b>\n\n"
                "Mã phiếu không tồn tại trong hệ thống. Vui lòng kiểm tra lại.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        appeal_form_line = submission_form_line(
            await self._get_submission_form(canonical, requester_telegram_id=user_id)
        )
        appeal_form_suffix = f"\n\n{appeal_form_line}" if appeal_form_line else ""

        if status == "APPROVED":
            await self.send_message(
                chat_id,
                f"✅ <b>Phiếu #{canonical} đã được DUYỆT.</b>\n\n"
                "Phiếu đã hoàn tất nên không thể gửi phúc khảo."
                f"{appeal_form_suffix}",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        if status == "APPEAL_PENDING" or self.active_appeals.get(user_id) == canonical:
            await self.send_message(
                chat_id,
                f"⚠️ <b>Phiếu #{canonical} đã có một yêu cầu kiểm tra/phúc khảo đang hoạt động!</b>\n\n"
                f"Bạn không thể gửi thêm yêu cầu mới khi yêu cầu cũ chưa hoàn tất.\n"
                f"👉 Gõ <code>/cancel {canonical}</code> nếu bạn muốn hủy yêu cầu này."
                f"{appeal_form_suffix}",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        if status == "MANUAL_REVIEW":
            await self.send_message(
                chat_id,
                f"⏳ <b>Phiếu #{canonical} đang được Ban Quản Trị kiểm tra.</b>\n\n"
                "Vui lòng chờ kết quả; nếu phiếu bị từ chối, bạn có thể gửi phúc khảo ngay sau đó."
                f"{appeal_form_suffix}",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        if status != "REJECTED":
            await self.send_message(
                chat_id,
                f"⚠️ <b>Phiếu #{canonical} chưa thể phúc khảo ở trạng thái hiện tại ({status}).</b>"
                f"{appeal_form_suffix}",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        sub_id = canonical
        phone = await self.get_driver_phone(user_id) or str(user_id)
        now = time.time()
        window = 2 * 3600  # 2 hours

        # 2. Rate limit check: max 5 appeals in 2h
        history = [t for t in self.appeal_history.get(phone, []) if now - t < window]
        if len(history) >= 5:
            await self.send_message(
                chat_id,
                "🚫 <b>Bạn đã gửi tối đa 5 lần phúc khảo trong vòng 2 giờ qua!</b>\n\n"
                "Vui lòng đợi Ban Quản Trị xử lý các yêu cầu trước đó hoặc gửi yêu cầu trợ giúp qua lệnh <code>/support</code>.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        history.append(now)
        self.appeal_history[phone] = history

        success, final_status = await self._update_submission_status(
            sub_id, "APPEAL_PENDING", reason="Thành viên gửi yêu cầu phúc khảo qua Telegram DM",
            actor=f"driver:{user_id}", requester_telegram_id=user_id,
        )
        if not success:
            self.appeal_history[phone].remove(now)
            note = ("Phiếu cần đối soát luồng cũ trước khi phúc khảo."
                    if final_status == "LEGACY_RECONCILIATION_REQUIRED"
                    else f"Không thể gửi phúc khảo cho phiếu #{sub_id} (trạng thái: {final_status}).")
            await self.send_message(
                chat_id,
                f"⚠️ <b>{note}</b>",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return
        self.active_appeals[user_id] = sub_id

        # Notify Member
        await self.send_message(
            chat_id,
            f"📨 <b>Yêu cầu phúc khảo cho phiếu #{sub_id} đã được gửi thành công!</b>\n\n"
            f"Ban Quản Trị sẽ trực tiếp kiểm tra lại hồ sơ và phản hồi kết quả sớm nhất tại đây.\n"
            f"<i>(Bạn có thể gõ <code>/cancel {sub_id}</code> để hủy yêu cầu này bất cứ lúc nào).</i>",
            reply_markup=self.get_driver_reply_keyboard(),
        )

        # Notify Admin Group via Admin Bot
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-1001234567890")
        if admin_token and admin_token != "mock_tg_token" and await self._legacy_delivery_allowed(sub_id):
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
            payload = {
                "chat_id": admin_group,
                "text": (
                    f"⚖️ <b>YÊU CẦU PHÚC KHẢO MỚI TỪ TÀI XẾ!</b>\n\n"
                    f"• <b>Mã phiếu:</b> <code>#{sub_id}</code>\n"
                    f"• <b>SĐT tài xế:</b> <code>{phone}</code>\n"
                    f"• <b>Thời gian:</b> Vừa gửi\n\n"
                    f"Vui lòng xem xét và bấm nút duyệt hoặc bác bỏ bên dưới:"
                ),
                "parse_mode": "HTML",
                "reply_markup": with_survey_copy({
                    "inline_keyboard": [
                        [
                            {"text": f"✅ Duyệt #{sub_id}", "callback_data": f"admin_req_approve:{sub_id}"},
                            {"text": f"❌ Bác bỏ #{sub_id}", "callback_data": f"admin_req_reject:{sub_id}"},
                        ],
                        [
                            {"text": f"🔍 Xem hồ sơ #{sub_id}", "callback_data": f"admin_query:{sub_id}"},
                        ],
                    ]
                }, sub_id),
            }
            try:
                await client.post(url, json=payload)
            except Exception as e:
                logger.error(f"Failed to alert admin group about appeal: {e}")

    async def handle_admin_help_request(
        self, chat_id: int, user_id: int, message_text: str, from_user: dict
    ) -> None:
        """Forwards support inquiries from drivers or collaborators to the Telegram Admin Group."""
        # 1. Prevent duplicate active help request / session
        if user_id in ACTIVE_SUPPORT_CONVERSATIONS or user_id in self.pending_help_tickets:
            await self.send_message(
                chat_id,
                "⚠️ <b>Bạn đã có một yêu cầu hỗ trợ đang hoạt động với Ban Quản Trị!</b>\n\n"
                "Bạn có thể nhắn tin trực tiếp để tiếp tục trao đổi, hoặc gõ <code>/cancel</code> để hủy yêu cầu hiện tại trước khi gửi yêu cầu mới.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        clean_msg = message_text.strip()
        if not clean_msg:
            self.pending_support[chat_id] = True
            prompt = (
                "💬 <b>Bạn cần Ban Quản Trị hỗ trợ vấn đề gì?</b>\n\n"
                "Hãy nhập nội dung thắc mắc hoặc khó khăn của bạn ngay tại đây (ví dụ: <i>Cần kiểm tra trạng thái trả thưởng phiếu VN-123456</i>).\n\n"
                "Hệ thống sẽ chuyển tiếp trực tiếp tin nhắn của bạn tới Ban Quản Trị."
            )
            await self.send_message(chat_id, prompt, reply_markup=self.get_driver_reply_keyboard())
            return

        # 2. Rate limit check: max 5 requests per 2 hours
        now = time.time()
        window = 2 * 3600
        s_history = [t for t in self.support_history.get(user_id, []) if now - t < window]
        if len(s_history) >= 5:
            await self.send_message(
                chat_id,
                "🚫 <b>Bạn đã gửi tối đa 5 yêu cầu hỗ trợ trong vòng 2 giờ qua!</b>\n\n"
                "Vui lòng chờ Ban Quản Trị xử lý các yêu cầu trước đó hoặc thử lại sau.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
            return

        s_history.append(now)
        self.support_history[user_id] = s_history
        self.pending_help_tickets[user_id] = {"chat_id": chat_id, "timestamp": now, "message": clean_msg}

        phone = await self.get_driver_phone(user_id) or "Chưa liên kết SĐT"
        name = from_user.get("first_name", "") + (" " + from_user.get("last_name", "") if from_user.get("last_name") else "")
        if not name.strip():
            name = f"Thành viên #{user_id}"
        username = from_user.get("username")

        if chat_id in self.pending_support:
            del self.pending_support[chat_id]

        # 1. Forward to Telegram Admin Group via Admin Bot
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-1001234567890")
        if admin_token and admin_token != "mock_tg_token":
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
            user_ref = f"@{username}" if username else f"ID: <code>{user_id}</code>"
            admin_card = (
                f"🆘 <b>YÊU CẦU TRỢ GIÚP TỪ THÀNH VIÊN!</b>\n\n"
                f"• <b>Người gửi:</b> {name} ({user_ref})\n"
                f"• <b>Số điện thoại:</b> <code>{phone}</code>\n"
                f"• <b>Chat ID:</b> <code>{chat_id}</code>\n"
                f"• <b>Nội dung yêu cầu:</b>\n"
                f"<i>\"{clean_msg}\"</i>"
            )
            buttons = [
                [
                    {"text": f"💬 Trả lời {name[:16]}", "callback_data": f"admin_reply_chat:{user_id}"},
                    {"text": "❌ Đóng", "callback_data": f"admin_close_ticket:{user_id}"},
                ]
            ]
            if username:
                buttons.append([{"text": "📱 Mở Telegram riêng", "url": f"https://t.me/{username}"}])
            payload: Dict[str, Any] = {
                "chat_id": admin_group,
                "text": admin_card,
                "parse_mode": "HTML",
            }
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            try:
                await client.post(url, json=payload)
            except Exception as e:
                logger.error(f"Failed to forward help request to admin group: {e}")

        # 2. Confirm to user
        confirm_text = (
            f"📨 <b>Đã gửi yêu cầu hỗ trợ tới Ban Quản Trị!</b>\n\n"
            f"• <b>Nội dung:</b> <i>\"{clean_msg}\"</i>\n\n"
            f"Đội ngũ hỗ trợ sẽ xem xét và phản hồi bạn trong thời gian sớm nhất."
        )
        await self.send_message(chat_id, confirm_text, reply_markup=self.get_driver_reply_keyboard())
    async def handle_cancel_request(self, chat_id: int, user_id: int, target_arg: str = "") -> None:
        """Allows cancelling active review/appeal requests, support tickets, or photo fix sessions."""
        if not chat_id or chat_id != user_id:
            await self.send_message(chat_id, "Vui lòng hủy yêu cầu trong cuộc trò chuyện riêng với bot.")
            return
        cancelled_items = []
        admin_group = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-5445563473")
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN

        # 1. Cancel active appeal
        sub_id = normalize_submission_id(target_arg) if target_arg else normalize_submission_id(self.active_appeals.get(user_id))
        if sub_id:
            changed, status = await self._update_submission_status(
                sub_id, "REJECTED", reason="Người dùng đã hủy yêu cầu phúc khảo",
                actor=f"driver:{user_id}", requester_telegram_id=user_id,
                expected_statuses=("APPEAL_PENDING",),
            )
            if not changed:
                note = ("Phiếu cần đối soát luồng cũ trước khi hủy phúc khảo."
                        if status == "LEGACY_RECONCILIATION_REQUIRED"
                        else "Không có phúc khảo đang chờ để hủy hoặc bạn không có quyền hủy.")
                await self.send_message(chat_id, note, reply_markup=self.get_driver_reply_keyboard())
                if target_arg:
                    return
            else:
                cancelled_items.append(f"yêu cầu phúc khảo phiếu <code>#{sub_id}</code>")
                if admin_token and admin_token != "mock_tg_token" and await self._legacy_delivery_allowed(sub_id):
                    client = await self.get_client()
                    try:
                        await client.post(f"https://api.telegram.org/bot{admin_token}/sendMessage", json={
                            "chat_id": admin_group,
                            "text": f"ℹ️ <b>Người dùng đã HỦY yêu cầu phúc khảo cho phiếu #{sub_id}.</b>",
                            "parse_mode": "HTML",
                        })
                    except Exception:
                        pass

        # 2. Cancel active support ticket or live chat
        if user_id in ACTIVE_SUPPORT_CONVERSATIONS or user_id in self.pending_help_tickets:
            if user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(user_id, {})
                locked_admin = convo.get("locked_admin_id")
                if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
                    del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
                if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
                    del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
                admin_chat = convo.get("admin_chat_id") or admin_group
                if str(admin_chat) in ADMIN_GROUP_ACTIVE_SESSION:
                    del ADMIN_GROUP_ACTIVE_SESSION[str(admin_chat)]
            if user_id in self.pending_help_tickets:
                del self.pending_help_tickets[user_id]
            cancelled_items.append("yêu cầu hỗ trợ Ban Quản Trị")

            if admin_token and admin_token != "mock_tg_token":
                client = await self.get_client()
                url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
                try:
                    await client.post(url, json={
                        "chat_id": admin_group,
                        "text": f"ℹ️ <b>Người dùng (ID: <code>{user_id}</code>) đã HỦY yêu cầu hỗ trợ.</b>",
                        "parse_mode": "HTML",
                    })
                except Exception:
                    pass

        # 3. Cancel pending photo fix
        if chat_id in self.pending_fixes:
            fix = self.pending_fixes.pop(chat_id)
            cancelled_items.append(f"phiên gửi lại ảnh cho phiếu <code>#{fix.get('sub_id')}</code>")

        # 4. Cancel pending support prompt
        if chat_id in self.pending_support:
            del self.pending_support[chat_id]

        if cancelled_items:
            details = ", ".join(cancelled_items)
            await self.send_message(
                chat_id,
                f"✅ <b>Đã hủy thành công:</b> {details}.\n\nBây giờ bạn có thể thao tác bình thường hoặc gửi yêu cầu mới.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
        else:
            await self.send_message(
                chat_id,
                "ℹ️ Hiện tại bạn không có yêu cầu hỗ trợ, phúc khảo hoặc gửi ảnh nào đang chờ để hủy.",
                reply_markup=self.get_driver_reply_keyboard(),
            )
    async def close_user_support(self, chat_id: int, user_id: int, closed_by: str = "user") -> None:
        """Closes live support conversation from user side."""
        convo = ACTIVE_SUPPORT_CONVERSATIONS.pop(user_id, {})
        locked_admin = convo.get("locked_admin_id")
        if locked_admin and locked_admin in ADMIN_ACTIVE_CONVERSATIONS:
            del ADMIN_ACTIVE_CONVERSATIONS[locked_admin]
        if locked_admin and str(locked_admin) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(locked_admin)]
        admin_chat_id = convo.get("admin_chat_id") or getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-5445563473")
        if str(admin_chat_id) in ADMIN_GROUP_ACTIVE_SESSION:
            del ADMIN_GROUP_ACTIVE_SESSION[str(admin_chat_id)]
        await self.send_message(
            chat_id,
            "🔒 <b>Cuộc trò chuyện hỗ trợ đã kết thúc theo yêu cầu của bạn.</b>\n\n"
            "Cảm ơn bạn! Chúc bạn một ngày làm việc thuận lợi.",
            reply_markup=self.get_driver_reply_keyboard(),
        )
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
        if admin_token and admin_token != "mock_tg_token":
            client = await self.get_client()
            url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
            msg = f"🔒 <b>Người dùng <code>{user_id}</code> đã kết thúc cuộc trò chuyện hỗ trợ.</b>"
            try:
                await client.post(url, json={"chat_id": admin_chat_id, "text": msg, "parse_mode": "HTML"})
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")


    async def handle_driver_command(self, chat_id: int, user_id: int, text: str, from_user: dict) -> None:
        """Dispatches text commands and persistent keyboard button presses."""
        clean = text.strip()
        cmd = clean.split()[0].lower() if clean else ""

        if cmd == "/start" or clean == "Bắt đầu":
            parts = clean.split(" ", 1)
            payload = parts[1].strip() if len(parts) > 1 else ""
            if payload and not payload.startswith(("support", "fix_")):
                if chat_id != user_id or from_user.get("id") != user_id:
                    return
                from core.ctv_onboarding import begin_ctv_onboarding

                try:
                    ctv_code = await begin_ctv_onboarding(self.db_pool, user_id, payload)
                except ValueError:
                    await self.send_message(
                        chat_id,
                        "⚠️ Link mời CTV không hợp lệ, tài khoản đã thuộc CTV khác hoặc CTV đã ngừng hoạt động. "
                        "Vui lòng xin quản trị viên link trong cột Telegram Bot của CTV_Info.",
                    )
                    return
                except Exception as exc:
                    logger.warning("CTV invitation lookup failed (%s)", type(exc).__name__)
                    await self.send_message(chat_id, "⚠️ Chưa thể kiểm tra link mời. Vui lòng thử lại sau.")
                    return
                await self.send_message(
                    chat_id,
                    f"📱 <b>Xác thực số điện thoại cho CTV <code>{html.escape(ctv_code)}</code></b>\n\n"
                    "Bấm <b>[📱 Xác Thực Số Điện Thoại]</b> bên dưới và chia sẻ số của chính tài khoản Telegram này. "
                    "CTV đã có số đăng ký phải xác thực đúng số đó; muốn đổi số cần quản trị viên hỗ trợ.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return
            if payload.startswith("support"):
                sub_ref = normalize_submission_id(payload[7:].lstrip("_"))
                ref_text = f" (cho phiếu <code>#{sub_ref}</code>)" if sub_ref else ""
                self.pending_support[chat_id] = True
                prompt = (
                    f"💬 <b>Bạn cần Ban Quản Trị hỗ trợ vấn đề gì{ref_text}?</b>\n\n"
                    f"Hãy nhập nội dung thắc mắc hoặc khó khăn của bạn ngay tại đây.\n\n"
                    f"Hệ thống sẽ chuyển tiếp trực tiếp tin nhắn của bạn tới Ban Quản Trị trong nhóm điều hành."
                )
                await self.send_message(chat_id, prompt, reply_markup=self.get_driver_reply_keyboard())
                return

            if payload.startswith("fix_"):
                # Deep link: /start fix_<sub_id>_<slot>
                fix_data = payload[4:].split("_", 1)
                sub_id = normalize_submission_id(fix_data[0])
                slot_name = fix_data[1] if len(fix_data) > 1 else "flat_0"
                if chat_id != user_id or not await self._get_submission_status(sub_id, requester_telegram_id=user_id):
                    await self.send_message(chat_id, "Bạn chỉ có thể bổ sung ảnh của mình trong cuộc trò chuyện riêng đã xác thực.")
                    return
                self.pending_fixes[chat_id] = {"sub_id": sub_id, "slot": slot_name, "timestamp": time.time()}
                fix_form_line = submission_form_line(
                    await self._get_submission_form(sub_id, requester_telegram_id=user_id)
                )
                await self.send_message(
                    chat_id,
                    f"📸 <b>Bổ Sung Ảnh Cho Phiếu #{sub_id}:</b>\n\n"
                    + (f"{fix_form_line}\n" if fix_form_line else "")
                    + f"Ảnh mục <b>{html.escape(slot_name)}</b> trước đó chưa đạt tiêu chuẩn độ nét.\n"
                    f"👉 <i>Vui lòng chụp lại rõ nét và gửi thẳng ảnh vào khung chat này ngay bây giờ!</i>",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return
            welcome_text = (
                "👋 <b>Kênh Hỗ Trợ Khảo Sát Trust Bridge (CTV)</b>\n\n"
                "Kênh thông tin và hỗ trợ dành cho <b>Cộng tác viên</b>:\n"
                "• Nhận kết quả duyệt / từ chối phiếu do bạn giới thiệu\n"
                "• Bổ sung ảnh chưa đạt trực tiếp trong chat để kiểm tra lại\n"
                "• Tra cứu phiếu và gửi phúc khảo bằng <code>/appeal &lt;mã_phiếu&gt;</code>\n"
                "• Kết nối trực tiếp với Ban Quản Trị\n\n"
                "👉 <i>Hãy mở link mời riêng do quản trị viên gửi từ cột Telegram Bot của CTV_Info, "
                "sau đó bấm <b>[📱 Xác Thực Số Điện Thoại]</b> để hoàn tất liên kết CTV.</i>"
            )
            await self.send_message(chat_id, welcome_text, reply_markup=self.get_driver_reply_keyboard())

        elif cmd == "/forms":
            await self.handle_forms(chat_id, user_id)

        elif cmd in HISTORY_COMMANDS or clean == "📋 Khảo Sát Của Tôi":
            if from_user.get("id") != user_id or type(user_id) is not int or user_id <= 0:
                return
            await self.handle_driver_surveys_lookup(chat_id, user_id)

        elif cmd in ("/quota", "/quotas", "/hanmuc"):
            await self.handle_quota_command(chat_id, clean, user_id)

        elif cmd in ("/linkphone", "/lienket_sdt", "/linksdt"):
            parts = clean.split(maxsplit=1)
            if len(parts) < 2:
                await self._prompt_for_argument(chat_id, user_id, cmd, "số điện thoại (hoặc chia sẻ danh bạ để xác thực)")
                return
            from core.phone_links import normalize_vn_phone, upsert_phone_telegram_link
            normalized = normalize_vn_phone(parts[1])
            if not normalized:
                await self.send_message(
                    chat_id,
                    "⚠️ Số điện thoại không hợp lệ. Ví dụ: <code>/linkphone 0932090967</code>.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )
                return
            ok = await upsert_phone_telegram_link(
                normalized, user_id, source="command", db_pool=self.db_pool
            )
            masked = normalized[:3] + "****" + normalized[-3:]
            await self.send_message(
                chat_id,
                (
                    f"Đã ghi nhận <code>{masked}</code> (chưa xác thực). "
                    if ok else "Không thay đổi liên kết số điện thoại. "
                )
                + "Số nhập qua /linkphone không xác minh quyền sở hữu, không cho phép nhận kết quả hay tra cứu. "
                "Hãy dùng nút [📱 Xác Thực Số Điện Thoại] để chia sẻ danh bạ của chính bạn.",
                reply_markup=self.get_driver_reply_keyboard(),
            )

        elif cmd in ("/support", "/hotro", "/help_admin", "/lienhe") or "hỗ trợ" in clean.lower():
            if clean.startswith("/"):
                parts = clean.split(" ", 1)
                msg_body = parts[1].strip() if len(parts) > 1 else ""
            else:
                msg_body = ""
            await self.handle_admin_help_request(chat_id, user_id, msg_body, from_user)

        elif cmd in ("/cancel", "/huy", "/cancel_request") or clean == "❌ Hủy Yêu Cầu":
            parts = clean.split(" ", 1)
            target = parts[1].strip() if len(parts) > 1 else ""
            await self.handle_cancel_request(chat_id, user_id, target)


        elif cmd in ("/help", "/trogiup") or clean == "❓ Trợ Giúp":
            help_text = (
                "❓ <b>Hướng Dẫn Sử Dụng (Dành Cho Cộng Tác Viên):</b>\n\n"
                "1. <b>Liên kết CTV:</b> mở link mời riêng do quản trị viên gửi từ cột Telegram Bot của CTV_Info, "
                "rồi bấm [📱 Xác Thực Số Điện Thoại] và chia sẻ số của chính bạn. Đổi Telegram: xác thực lại đúng số đã đăng ký từ tài khoản mới, không cần tài khoản cũ. Đổi số: liên hệ quản trị viên.\n"
                "2. <b>Xem phiếu đã nộp:</b> Bấm [📋 Khảo Sát Của Tôi] hoặc gõ <code>/surveys</code>.\n"
                "• <code>/forms</code> - Xem biểu mẫu đang mở gần đây.\n"
                "3. <b>Xem mục tiêu:</b> Gõ <code>/quota</code> để xem mục tiêu nộp phiếu của bạn.\n"
                "3b. <b>Liên kết số điện thoại:</b> Gõ <code>/linkphone 09xxxxxxxx</code> hoặc chia sẻ danh bạ.\n"
                "4. <b>Bổ sung ảnh chưa đạt:</b> Bấm [📸 Gửi lại ảnh] rồi gửi ảnh mới trực tiếp vào chat.\n"
                "5. <b>Phúc khảo:</b> Gõ <code>/appeal &lt;mã_phiếu&gt;</code> nếu kết quả chưa chính xác.\n"
                "6. <b>Cần trợ giúp:</b> Gõ <code>/support &lt;nội dung&gt;</code> hoặc bấm [🆘 Yêu Cầu Hỗ Trợ].\n"
                "7. <b>Kết thúc phiên hỗ trợ:</b> Gõ <code>/close</code> khi đã trao đổi xong với Ban Quản Trị."
            )
            await self.send_message(chat_id, help_text, reply_markup=self.get_driver_reply_keyboard())

        elif cmd == "/appeal":
            parts = clean.split(maxsplit=1)
            if len(parts) < 2:
                await self._prompt_for_argument(chat_id, user_id, cmd, "mã phiếu cần phúc khảo")
                return
            await self.handle_driver_appeal(chat_id, user_id, normalize_submission_id(parts[1]))
        elif cmd in ("/close", "/end", "/ketthuc"):
            if user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                await self.close_user_support(chat_id, user_id, closed_by="user")
            else:
                await self.send_message(
                    chat_id,
                    "ℹ️ Hiện tại không có phiên hội thoại hỗ trợ trực tiếp nào đang mở.",
                    reply_markup=self.get_driver_reply_keyboard(),
                )


        else:
            if self.pending_support.get(chat_id):
                await self.handle_admin_help_request(chat_id, user_id, clean, from_user)
            else:
                await self.send_message(
                    chat_id,
                    "Bạn cần hỗ trợ gì? Hãy chọn một tính năng trên bàn phím bên dưới nhé!",
                    reply_markup=self.get_driver_reply_keyboard(),
                )

    async def handle_update(self, update: dict) -> None:
        """Dispatches driver direct messages, contact shares, photos, and callbacks."""
        if "message" in update and update["message"]:
            msg = update["message"]
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            from_user = msg.get("from") or {}
            user_id = from_user.get("id", chat_id)
            if chat.get("type") != "private" or type(user_id) is not int or user_id <= 0 or chat_id != user_id or from_user.get("id") != user_id:
                return

            # 1. Contact Card Shared
            if "contact" in msg and msg["contact"]:
                await self.handle_contact_sharing(chat_id, user_id, msg["contact"], from_user)
                return

            # 2. Photo Uploaded
            if "photo" in msg and msg["photo"]:
                await self.handle_photo_upload(chat_id, user_id, msg["photo"])
                return

            # 3. Text Command or Button
            text = msg.get("text") or ""
            if chat_id and text:
                if len(text) <= 4096:
                    handled, resumed = await self._argument_reply(chat_id, user_id, msg.get("reply_to_message"), text)
                    if handled:
                        if resumed:
                            await self.handle_driver_command(chat_id, user_id, resumed, from_user)
                        return
                # 1. Check if user wants to close active support conversation
                if text.strip().lower() in ("/close", "/end", "/ketthuc") and user_id in ACTIVE_SUPPORT_CONVERSATIONS:
                    await self.close_user_support(chat_id, user_id, closed_by="user")
                    return

                # 2. Check if user is in an active support conversation open with admin
                if user_id in ACTIVE_SUPPORT_CONVERSATIONS and not text.startswith("/"):
                    convo = ACTIVE_SUPPORT_CONVERSATIONS[user_id]
                    admin_chat_id = convo.get("admin_chat_id") or getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-5445563473")
                    admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
                    user_display = from_user.get("first_name", f"User {user_id}")
                    if admin_token and admin_token != "mock_tg_token":
                        client = await self.get_client()
                        url = f"https://api.telegram.org/bot{admin_token}/sendMessage"
                        relay_text = f"👤 <b>Tin nhắn từ {user_display} (ID: <code>{user_id}</code>):</b>\n\n{text}"
                        try:
                            await client.post(url, json={"chat_id": admin_chat_id, "text": relay_text, "parse_mode": "HTML"})
                        except Exception as e:
                            logger.error(f"Failed to relay user message to admin group: {e}")
                    return

                if self.pending_support.get(chat_id) and not text.startswith("/"):
                    await self.handle_admin_help_request(chat_id, user_id, text, from_user)
                else:
                    await self.handle_driver_command(chat_id, user_id, text, from_user)
        elif "callback_query" in update and update["callback_query"]:
            cq = update["callback_query"]
            cq_id = cq.get("id", "")
            data = cq.get("data", "")
            cq_msg = cq.get("message") or {}
            chat_id = cq_msg.get("chat", {}).get("id")
            user_id = (cq.get("from") or {}).get("id")
            if data.startswith(("driver_appeal:", "driver_fix:")) and (not user_id or chat_id != user_id):
                await self.answer_callback_query(cq_id, "Vui lòng dùng cuộc trò chuyện riêng đã xác thực.", show_alert=True)
                return

            if data.startswith("driver_appeal:"):
                sub_id = normalize_submission_id(data.split(":", 1)[1])
                await self.handle_driver_appeal(chat_id, user_id, sub_id)
                await self.answer_callback_query(cq_id)

            elif data.startswith("driver_fix:"):
                parts = data.split(":")
                sub_id = normalize_submission_id(parts[1])
                slot_name = parts[2] if len(parts) > 2 else "flat_0"
                if not await self._get_submission_status(sub_id, requester_telegram_id=user_id):
                    await self.answer_callback_query(cq_id, "Bạn không có quyền bổ sung ảnh cho phiếu này.", show_alert=True)
                    return
                self.pending_fixes[chat_id] = {"sub_id": sub_id, "slot": slot_name, "timestamp": time.time()}
                await self.answer_callback_query(cq_id, "Đang chờ gửi ảnh mới")
                await self.send_message(
                    chat_id,
                    f"📸 <b>Đang chờ nhận ảnh cho phiếu #{sub_id} ({html.escape(slot_name)}):</b>\n\n"
                    f"👉 Vui lòng mở camera, chụp rõ nét và gửi thẳng ảnh vào khung chat này ngay nhé!",
                    reply_markup=self.get_driver_reply_keyboard(),
                )

            elif data.startswith("driver_support:"):
                sub_id = normalize_submission_id(data.split(":", 1)[1])
                await self.answer_callback_query(cq_id, f"Đang gửi yêu cầu hỗ trợ cho #{sub_id}")
                await self.handle_admin_help_request(
                    chat_id, user_id, f"Cần hỗ trợ về phiếu khảo sát #{sub_id}", cq.get("from", {})
                )
            else:
                await self.answer_callback_query(cq_id)


async def configure_bot_menus() -> None:
    """Registers both command menus for webhook deployments."""
    admin = TelegramPollingRunner(role="admin")
    driver = DriverBotRunner()
    runners = (admin,) if admin.bot_token == driver.bot_token else (admin, driver)
    for runner in runners:
        try:
            await runner.configure_bot_commands()
        finally:
            await runner.aclose()


async def run_polling_loop() -> None:
    """Spawns both Admin Bot and Driver Bot concurrently if tokens are provided."""
    pool = None
    try:
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(conninfo=settings.DATABASE_URL, min_size=1, max_size=5, open=False)
        await pool.open()
    except Exception as e:
        logger.info("Database pool not available in standalone bot: %s", e)
        pool = None

    admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    driver_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None)

    runners = []
    tasks = []

    # 1. Admin Bot
    if admin_token and admin_token != "mock_tg_token":
        admin_runner = TelegramPollingRunner(bot_token=admin_token, db_pool=pool, role="admin")
        runners.append(admin_runner)
        tasks.append(admin_runner.start_polling())

    # 2. Driver / Collaborator Bot
    if driver_token and driver_token != "mock_tg_token" and driver_token != admin_token:
        driver_runner = DriverBotRunner(bot_token=driver_token, db_pool=pool)
        runners.append(driver_runner)
        tasks.append(driver_runner.start_polling())
    elif not tasks:
        # Fallback to single runner with default settings
        fallback_runner = TelegramPollingRunner(db_pool=pool, role="admin")
        runners.append(fallback_runner)
        tasks.append(fallback_runner.start_polling())

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Telegram polling loops cancelled.")
    finally:
        for r in runners:
            r.stop()
            await r.aclose()
        if pool:
            try:
                await pool.close()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run_polling_loop())
