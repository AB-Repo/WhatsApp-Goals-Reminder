# /// script
# requires-python = "==3.11.*"
# dependencies = [
#   "codewords-client==0.4.10",
#   "fastapi==0.116.1",
#   "openai==1.99.7"
# ]
# [tool.env-checker]
# env_vars = [
#   "PORT=8000",
#   "LOGLEVEL=INFO",
#   "CODEWORDS_API_KEY",
#   "CODEWORDS_RUNTIME_URI",
#   "SERVICE_ID"
# ]
# ///

import base64
import json
import os
import random
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from codewords_client import AsyncCodewordsClient, logger, redis_client, run_service
from fastapi import FastAPI, Request
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHAT_MODEL = "gpt-4.1-mini"
DEFAULT_TZ = "Europe/London"
SEEN_TTL = 24 * 3600

INTENT_PROMPT = (
    "You are the brain of a personal WhatsApp assistant. Read the user's message and classify it "
    "into exactly one action. Respond with ONLY a JSON object (no markdown, no commentary) using "
    'this schema:\n\n{"action": "...", "text": "...", "ref": "...", "reply": "..."}\n\n'
    "Allowed actions and how to fill fields:\n"
    '- "add_task" -> user wants to create a task/todo/reminder. Put the task text in "text".\n'
    '- "add_note" -> user wants to save a note/idea/thought. Put the note text in "text".\n'
    '- "list_tasks" -> user wants to see their tasks. Leave other fields empty.\n'
    '- "list_notes" -> user wants to see their notes. Leave other fields empty.\n'
    '- "complete_task" -> user wants to mark a task done. Put the task number or a short description in "ref".\n'
    '- "delete_task" -> user wants to remove a task. Put the task number or a short description in "ref".\n'
    '- "set_goal" -> user states a personal goal to remember. Put the goal in "text".\n'
    '- "list_goals" -> user wants to see their goals. Leave other fields empty.\n'
    '- "remove_goal" -> user wants to remove a goal. Put the goal number or description in "ref".\n'
    '- "motivation" -> user wants a motivational message or quote right now.\n'
    '- "help" -> user asks what they can do.\n'
    '- "chat" -> anything else (general question or conversation). Put a short, friendly, helpful reply '
    '(1-3 sentences, same language as the user) in "reply".\n\n'
    'Always return all four keys. Use "" for unused fields.'
)

SLOT_GUIDANCE = {
    "morning": "an original, uplifting motivational thought or short quote to start the day, tied to their goals",
    "midday": "a gentle, specific nudge to take one concrete step toward a goal right now",
    "evening": "a warm reflective check-in — acknowledge their effort today and encourage rest",
}

IMAGE_PROMPTS = [
    "A serene mountain peak at sunrise with golden light breaking through clouds, photorealistic, uplifting, no text",
    "A calm ocean horizon at dawn with a soft pastel sky, photorealistic, peaceful, no text",
    "A winding forest trail with sunlight streaming through tall trees, photorealistic, inspiring, no text",
    "A person standing on a hilltop at dawn with arms open to the sky, silhouetted, hopeful, no text",
    "A field of wildflowers under a bright blue sky with drifting clouds, photorealistic, joyful, no text",
    "A lone climber reaching a snowy summit, wide shot, epic and triumphant, photorealistic, no text",
    "A tranquil lake reflecting a colorful sunrise and mountains, photorealistic, calm, no text",
    "A cozy window seat with warm morning light, a cup of tea and a notebook, photorealistic, peaceful, no text",
    "A vast starry night sky over a quiet landscape, inspiring and awe-filled, photorealistic, no text",
    "A winding road through rolling green hills toward a bright horizon, photorealistic, optimistic, no text",
]


def _wa_number(jid: str) -> str:
    return "".join(c for c in (jid or "").split("@")[0].split(":")[0] if c.isdigit())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_hour(tz: str) -> int:
    try:
        tzinfo = ZoneInfo(tz)
    except Exception:
        tzinfo = timezone.utc
    return datetime.now(tzinfo).hour


def _pick_slot(hour: int) -> str:
    if hour < 11:
        return "morning"
    if hour < 16:
        return "midday"
    return "evening"


def _resolve_ref(items: list, ref: str):
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.isdigit():
        idx = int(ref) - 1
        return idx if 0 <= idx < len(items) else None
    low = ref.lower()
    for i, item in enumerate(items):
        if low in str(item).lower():
            return i
    return None


# ---------------------------------------------------------------------------
# Redis state helpers
# ---------------------------------------------------------------------------
async def _load_json(redis, ns, key, default):
    raw = await redis.get(f"{ns}:{key}")
    if not raw:
        return default
    try:
        return json.loads(raw if isinstance(raw, str) else raw.decode())
    except (json.JSONDecodeError, TypeError):
        return default


async def _save_json(redis, ns, key, value):
    await redis.set(f"{ns}:{key}", json.dumps(value))


async def _get_config() -> dict:
    async with redis_client() as (redis, ns):
        cfg = await _load_json(redis, ns, "config", {})
    cfg.setdefault("owner_number", "")
    cfg.setdefault("tz", DEFAULT_TZ)
    return cfg


async def _get_goals() -> list:
    async with redis_client() as (redis, ns):
        return await _load_json(redis, ns, "goals", [])


async def _set_goals(goals: list) -> None:
    async with redis_client() as (redis, ns):
        await _save_json(redis, ns, "goals", goals)


async def _get_tasks() -> list:
    async with redis_client() as (redis, ns):
        return await _load_json(redis, ns, "tasks", [])


async def _set_tasks(tasks: list) -> None:
    async with redis_client() as (redis, ns):
        await _save_json(redis, ns, "tasks", tasks)


async def _get_notes() -> list:
    async with redis_client() as (redis, ns):
        return await _load_json(redis, ns, "notes", [])


async def _set_notes(notes: list) -> None:
    async with redis_client() as (redis, ns):
        await _save_json(redis, ns, "notes", notes)


# ---------------------------------------------------------------------------
# WhatsApp (Personal/DM) + LLM helpers
# ---------------------------------------------------------------------------
def _clean_template(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    text = text.replace("**", "").replace("*", "").replace("__", "").replace("_", "")
    return "".join(ch for ch in text if ch.isascii()).strip()


async def _send_business_api(phone: str, text: str, as_template: bool) -> bool:
    clean = phone.lstrip("+")
    if as_template:
        inputs = {
            "to_phone_number": clean,
            "message_type": "template",
            "template_name": "message",
            "template_language": "en",
            "template_components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "parameter_name": "message", "text": _clean_template(text)},
                        {"type": "text", "parameter_name": "number", "text": clean},
                    ],
                }
            ],
        }
    else:
        inputs = {"to_phone_number": clean, "message_type": "text", "message_text": text}
    async with AsyncCodewordsClient() as cw:
        resp = await cw.run(service_id="whatsapp_trigger", path="/send_message", inputs=inputs)
    data = resp.json() if hasattr(resp, "json") else {}
    return bool(data.get("success"))


async def _register_inbound(phone: str) -> bool:
    clean = phone.lstrip("+")
    service_path = f"{os.environ['SERVICE_ID']}/whatsapp_event"
    async with AsyncCodewordsClient() as cw:
        resp = await cw.run(
            service_id="whatsapp_trigger",
            path="/register",
            inputs={
                "event_type": "messages",
                "phone_number": clean,
                "api_key": os.environ["CODEWORDS_API_KEY"],
                "service_path": service_path,
            },
        )
        resp.raise_for_status()
    return True


async def _generate_image() -> str:
    """Generate a motivational image. Returns a public URL, or empty string on failure."""
    prompt = random.choice(IMAGE_PROMPTS)
    client = AsyncOpenAI(base_url=urljoin(os.environ["CODEWORDS_RUNTIME_URI"], "run/gemini/v1"))
    try:
        resp = await client.images.generate(
            model="imagen-4.0-generate-001",
            prompt=prompt,
            n=1,
            response_format="b64_json",
        )
        b64 = getattr(resp.data[0], "b64_json", None) if resp.data else None
        if b64:
            async with AsyncCodewordsClient() as cw:
                return await cw.upload_file_content(
                    filename="motivation.png", file_content=base64.b64decode(b64)
                )
    except Exception as exc:
        logger.warning("Imagen 4 generation failed, trying flash", error=str(exc))
    try:
        resp = await client.chat.completions.create(
            model="gemini-2.5-flash-image",
            messages=[{"role": "user", "content": f"Generate: {prompt}"}],
        )
        images = getattr(resp.choices[0].message, "images", None)
        if images:
            data_uri = images[0]["image_url"]["url"]
            b64 = data_uri.split(",", 1)[1]
            async with AsyncCodewordsClient() as cw:
                return await cw.upload_file_content(
                    filename="motivation.png", file_content=base64.b64decode(b64)
                )
    except Exception as exc:
        logger.warning("Flash image generation failed", error=str(exc))
    return ""


async def _send_image(phone: str, image_url: str, caption: str) -> bool:
    clean = phone.lstrip("+")
    async with AsyncCodewordsClient() as cw:
        resp = await cw.run(
            service_id="whatsapp_trigger",
            path="/send_message",
            inputs={
                "to_phone_number": clean,
                "message_type": "image",
                "image_link": image_url,
                "image_caption": caption,
            },
        )
    data = resp.json() if hasattr(resp, "json") else {}
    return bool(data.get("success"))


async def _llm_complete(messages: list, temperature: float, max_tokens: int) -> str:
    client = AsyncOpenAI()
    resp = await client.chat.completions.create(
        model=CHAT_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_intent(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "action": str(data.get("action", "chat") or "chat").strip(),
                "text": str(data.get("text", "") or "").strip(),
                "ref": str(data.get("ref", "") or "").strip(),
                "reply": str(data.get("reply", "") or "").strip(),
            }
    except json.JSONDecodeError:
        pass
    return {"action": "chat", "text": "", "ref": "", "reply": ""}


async def _classify(text: str) -> dict:
    raw = await _llm_complete(
        [
            {"role": "system", "content": INTENT_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
        max_tokens=250,
    )
    return _parse_intent(raw)


async def _generate_message(slot: str, goals: list, tasks: list) -> str:
    open_tasks = [t.get("text", "") for t in tasks if not t.get("done")]
    goals_text = ", ".join(goals) if goals else "(none set yet)"
    tasks_text = "; ".join(open_tasks) if open_tasks else "(none)"
    guidance = SLOT_GUIDANCE.get(slot, SLOT_GUIDANCE["morning"])
    system = (
        "You are a warm, thoughtful personal coach texting the user on WhatsApp. "
        "Write ONE short message (2-3 sentences). Plain text only — no emojis, no line breaks, no markdown. "
        "Vary your wording every time; never repeat a stock quote. Write like a kind, human friend."
    )
    user = (
        f"Message type: {slot} ({guidance}).\n"
        f"Their goals: {goals_text}\n"
        f"Their open tasks: {tasks_text}\n\n"
        "Write the WhatsApp message now."
    )
    return await _llm_complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.9,
        max_tokens=200,
    )


async def _execute_intent(intent: dict) -> str:
    action = intent["action"]
    text = intent["text"]
    ref = intent["ref"]

    if action == "add_task":
        tasks = await _get_tasks()
        tasks.append({"id": uuid.uuid4().hex[:8], "text": text, "done": False, "created_at": _now_iso()})
        await _set_tasks(tasks)
        open_count = sum(1 for t in tasks if not t.get("done"))
        return f'✅ Task added: "{text}"\nYou have {open_count} open task(s).'

    if action == "add_note":
        notes = await _get_notes()
        notes.insert(0, {"id": uuid.uuid4().hex[:8], "text": text, "created_at": _now_iso()})
        await _set_notes(notes)
        return f'📝 Note saved: "{text}"'

    if action == "list_tasks":
        tasks = await _get_tasks()
        if not tasks:
            return 'You have no tasks yet. Add one anytime — e.g. "add task: go for a run".'
        lines = [f'{i}. {"✅" if t.get("done") else "⬜"} {t.get("text", "")}' for i, t in enumerate(tasks, 1)]
        return "Your tasks:\n" + "\n".join(lines)

    if action == "list_notes":
        notes = await _get_notes()
        if not notes:
            return 'No notes saved yet. Save one with "note: ...".'
        lines = [f'{i}. {n.get("text", "")}' for i, n in enumerate(notes, 1)]
        return "Your notes:\n" + "\n".join(lines)

    if action == "complete_task":
        tasks = await _get_tasks()
        idx = _resolve_ref([t.get("text", "") for t in tasks], ref)
        if idx is None:
            return 'Couldn\'t find that task. Reply "list tasks" to see them.'
        tasks[idx]["done"] = True
        await _set_tasks(tasks)
        return f'🎉 Marked done: "{tasks[idx].get("text", "")}"'

    if action == "delete_task":
        tasks = await _get_tasks()
        idx = _resolve_ref([t.get("text", "") for t in tasks], ref)
        if idx is None:
            return 'Couldn\'t find that task. Reply "list tasks" to see them.'
        removed = tasks.pop(idx)
        await _set_tasks(tasks)
        return f'🗑️ Removed task: "{removed.get("text", "")}"'

    if action == "set_goal":
        goals = await _get_goals()
        goals.append(text)
        await _set_goals(goals)
        return f'🎯 Goal noted: "{text}"'

    if action == "list_goals":
        goals = await _get_goals()
        if not goals:
            return 'No goals set yet. Tell me a goal and I\'ll help you stay on track — e.g. "goal: run a 10k".'
        lines = [f"{i}. {g}" for i, g in enumerate(goals, 1)]
        return "Your goals:\n" + "\n".join(lines)

    if action == "remove_goal":
        goals = await _get_goals()
        idx = _resolve_ref(goals, ref)
        if idx is None:
            return 'Couldn\'t find that goal. Reply "list goals" to see them.'
        removed = goals.pop(idx)
        await _set_goals(goals)
        return f'Removed goal: "{removed}"'

    if action == "motivation":
        cfg = await _get_config()
        goals = await _get_goals()
        tasks = await _get_tasks()
        slot = _pick_slot(_local_hour(cfg.get("tz", DEFAULT_TZ)))
        return await _generate_message(slot, goals, tasks)

    if action == "help":
        return (
            "Here's what I can do — just message me:\n"
            '• Add a task: "add task: ..."\n'
            '• Save a note: "note: ..."\n'
            '• Tasks: "list tasks", "done 1"\n'
            '• Goals: "goal: ...", "list goals"\n'
            '• Get motivated: "motivate me"\n'
            "Or just chat with me."
        )

    if intent.get("reply"):
        return intent["reply"]
    return 'I\'m not sure what you\'d like me to do. Reply "help" to see what I can do.'


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="WhatsApp Personal Assistant",
    description=(
        "Your personal assistant over WhatsApp (Personal/DM messaging). Message the CodeWords "
        "number to add tasks, save notes, set goals, and get motivated. Sends scheduled "
        "motivational messages and goal reminders."
    ),
    version="1.1.0",
)


class ConfigureRequest(BaseModel):
    owner_number: str = Field(..., description="Your phone number (international format, digits only)")
    tz: str = Field(default=DEFAULT_TZ, description="IANA timezone, e.g. Europe/London")
    goals: list[str] = Field(default_factory=list, description="Optional initial personal goals")


class ConfigureResponse(BaseModel):
    status: str = Field(...)
    owner_number: str = Field(...)
    registration_success: bool = Field(...)


class DispatchRequest(BaseModel):
    mode: str = Field(default="auto", description="auto | morning | midday | evening | motivation | reminder | checkin")
    dry_run: bool = Field(default=False, description="If True, generate but do NOT send to WhatsApp")


class DispatchResponse(BaseModel):
    status: str = Field(...)
    slot: str = Field(...)
    message: str = Field(...)
    image_url: str = Field(default="")
    sent: bool = Field(...)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/configure", response_model=ConfigureResponse)
async def configure(body: ConfigureRequest) -> ConfigureResponse:
    owner = _wa_number(body.owner_number)
    async with redis_client() as (redis, ns):
        await _save_json(redis, ns, "config", {"owner_number": owner, "tz": body.tz})
    if body.goals:
        await _set_goals([g.strip() for g in body.goals if g.strip()])
    await _register_inbound(owner)
    logger.info("Assistant configured", owner=owner, registered=True)
    return ConfigureResponse(status="configured", owner_number=owner, registration_success=True)


@app.post("/", response_model=DispatchResponse)
async def dispatch(body: DispatchRequest) -> DispatchResponse:
    """Generate and send a motivational message or goal reminder (also used by the schedule)."""
    logger.info("STEPLOG START scheduled")
    cfg = await _get_config()
    goals = await _get_goals()
    tasks = await _get_tasks()

    mode = (body.mode or "auto").strip().lower()
    if mode in ("morning", "midday", "evening"):
        slot = mode
    elif mode == "reminder":
        slot = "midday"
    elif mode == "checkin":
        slot = "evening"
    elif mode == "motivation":
        slot = "morning"
    else:
        slot = _pick_slot(_local_hour(cfg.get("tz", DEFAULT_TZ)))

    message = await _generate_message(slot, goals, tasks)
    image_url = await _generate_image()

    if body.dry_run:
        return DispatchResponse(status="dry_run", slot=slot, message=message, image_url=image_url, sent=False)

    sent = False
    owner = cfg.get("owner_number", "")
    if owner:
        if image_url:
            sent = await _send_image(owner, image_url, message)
            if not sent:
                sent = await _send_business_api(owner, f"{message} {image_url}", as_template=True)
        else:
            sent = await _send_business_api(owner, message, as_template=True)

    status = "sent" if sent else "not_configured"
    logger.info("Dispatch complete", slot=slot, sent=sent, has_image=bool(image_url))
    return DispatchResponse(status=status, slot=slot, message=message, image_url=image_url, sent=sent)


@app.post("/whatsapp_event")
async def whatsapp_event(request: Request) -> dict:
    """Receive messages sent to the CodeWords WhatsApp number and respond."""
    logger.info("STEPLOG START message_you")
    payload = await request.json()
    if payload.get("object") != "whatsapp_business_account":
        return {"status": "skipped", "reason": "invalid object"}

    cfg = await _get_config()
    owner = cfg.get("owner_number", "")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            for message in value.get("messages", []):
                from_phone = _wa_number(message.get("from", ""))
                text = (message.get("text") or {}).get("body", "")
                if not text:
                    continue
                if owner and from_phone and from_phone != owner:
                    continue
                msg_id = message.get("id", "")
                async with redis_client() as (redis, ns):
                    if msg_id and not await redis.set(f"{ns}:seen:{msg_id}", "1", nx=True, ex=SEEN_TTL):
                        continue

                text = text.strip()
                logger.info("STEPLOG START understand")
                intent = await _classify(text)
                logger.info("STEPLOG START save_state")
                reply = await _execute_intent(intent)
                logger.info("STEPLOG START reply_wa")
                await _send_business_api(from_phone, reply, as_template=False)

    return {"status": "ok"}


if __name__ == "__main__":
    run_service(app)
