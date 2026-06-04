import base64
import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Dict

import httpx
from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

BASE_DIR = Path("data/imagegen")
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "API_KEY": "",
    "BASE_URL": "https://api.openai.com/v1",
    "MODEL": "gpt-image-2",
    "TARGET_GROUP_ID": "",
    "SIZE": "1024x1024",
    "QUALITY": "medium",
    "OUTPUT_FORMAT": "png",
    "MODERATION": "auto",
    "RESPONSE_FORMAT": "",
    "TIMEOUT_SECONDS": 300,
    "RETRY_ATTEMPTS": 2,
    "PROXY_URL": "",
    "TRUST_ENV": True,
    "VERIFY_SSL": True,
}

IMAGE_PROMPT_PREFIX = (
    "生成的图片不涉及色情内容，如果对于某些人物形象不确定请联网搜索，避免生成不清晰的文字。"
)


def ensure_config_file():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )


def load_config() -> Dict[str, Any]:
    ensure_config_file()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{CONFIG_PATH} 不是合法 JSON：{e}") from e

    config = DEFAULT_CONFIG.copy()
    if isinstance(raw, dict):
        config.update(raw)
    return config


def get_config_value(config: Dict[str, Any], key: str):
    value = config.get(key)
    if value not in (None, ""):
        return value

    lower_value = config.get(key.lower())
    if lower_value not in (None, ""):
        return lower_value

    return DEFAULT_CONFIG.get(key)


def parse_bool(value, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def normalize_v1_base_url(base_url: str) -> str:
    base = (base_url or DEFAULT_CONFIG["BASE_URL"]).strip().rstrip("/")
    if base.endswith("/images/generations"):
        base = base[: -len("/images/generations")]
    if base.endswith("/images/generations"):
        return base
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def build_generation_url(base_url: str) -> str:
    base = normalize_v1_base_url(base_url)
    return f"{base}/images/generations"


def resolve_target_group_id(event: MessageEvent, config: Dict[str, Any]) -> int:
    raw_group_id = str(get_config_value(config, "TARGET_GROUP_ID") or "").strip()
    if raw_group_id:
        try:
            return int(raw_group_id)
        except ValueError as e:
            raise ValueError("TARGET_GROUP_ID 必须是群号数字") from e

    if isinstance(event, GroupMessageEvent):
        return int(event.group_id)

    raise ValueError(f"请先在 {CONFIG_PATH} 里填写 TARGET_GROUP_ID，私聊无法自动判断目标群。")


def extract_api_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]

    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if error:
        return str(error)
    return json.dumps(data, ensure_ascii=False)[:300]


def image_segment_from_url(url: str) -> MessageSegment:
    if url.startswith("data:image/") and ";base64," in url:
        return MessageSegment.image(base64.b64decode(url.split(";base64,", 1)[1]))
    return MessageSegment.image(url)


def build_client_kwargs(config: Dict[str, Any]) -> Dict[str, Any]:
    timeout_seconds = float(get_config_value(config, "TIMEOUT_SECONDS") or 300)
    proxy_url = str(get_config_value(config, "PROXY_URL") or "").strip()
    kwargs = {
        "timeout": httpx.Timeout(timeout_seconds, connect=30.0),
        "follow_redirects": True,
        "trust_env": parse_bool(get_config_value(config, "TRUST_ENV"), True),
        "verify": parse_bool(get_config_value(config, "VERIFY_SSL"), True),
    }

    if proxy_url:
        if "proxy" in inspect.signature(httpx.AsyncClient).parameters:
            kwargs["proxy"] = proxy_url
        else:
            kwargs["proxies"] = proxy_url
    return kwargs


def format_request_error(error: httpx.RequestError, url: str) -> str:
    detail = str(error).strip()
    if not detail:
        detail = "连接超时或底层网络未返回详细信息"
    return (
        f"{error.__class__.__name__}: {detail}\n"
        f"请求地址：{url}\n"
        "请检查 BASE_URL 是否能从服务器访问；如果服务器不能直连 OpenAI，请配置 PROXY_URL 或使用可访问的中转 BASE_URL。"
    )


def build_image_prompt(prompt: str) -> str:
    return f"{IMAGE_PROMPT_PREFIX}\n\n用户需求：{prompt}"


async def call_gpt_image2(prompt: str, config: Dict[str, Any]) -> MessageSegment:
    api_key = str(get_config_value(config, "API_KEY") or "").strip()
    if not api_key:
        raise ValueError(f"请先在 {CONFIG_PATH} 里填写 API_KEY")

    base_url = str(get_config_value(config, "BASE_URL") or DEFAULT_CONFIG["BASE_URL"]).strip()
    generation_url = build_generation_url(base_url)
    retry_attempts = max(1, int(get_config_value(config, "RETRY_ATTEMPTS") or 2))

    payload = {
        "model": get_config_value(config, "MODEL") or "gpt-image-2",
        "prompt": prompt,
        "n": 1,
        "size": get_config_value(config, "SIZE") or "1024x1024",
        "quality": get_config_value(config, "QUALITY") or "medium",
        "output_format": get_config_value(config, "OUTPUT_FORMAT") or "png",
        "moderation": get_config_value(config, "MODERATION") or "auto",
        "response_format": get_config_value(config, "RESPONSE_FORMAT"),
    }
    payload = {k: v for k, v in payload.items() if v not in (None, "")}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(**build_client_kwargs(config)) as client:
        for attempt in range(1, retry_attempts + 1):
            try:
                response = await client.post(generation_url, headers=headers, json=payload)
                if response.status_code >= 400:
                    raise RuntimeError(f"GPT Image2 请求失败：HTTP {response.status_code}，{extract_api_error(response)}")

                data = response.json()
                images = data.get("data", []) if isinstance(data, dict) else []
                if not images:
                    raise RuntimeError("GPT Image2 没有返回图片数据")

                first_image = images[0]
                b64_json = first_image.get("b64_json") if isinstance(first_image, dict) else None
                if b64_json:
                    return MessageSegment.image(base64.b64decode(b64_json))

                image_url = first_image.get("url") if isinstance(first_image, dict) else None
                if image_url:
                    logger.info("GPT Image2 返回图片 URL，将直接交给 NapCat 发送。")
                    return image_segment_from_url(image_url)
            except httpx.RequestError as e:
                logger.warning(
                    f"GPT Image2 网络请求失败，第 {attempt}/{retry_attempts} 次：{format_request_error(e, generation_url)}"
                )
                if attempt >= retry_attempts:
                    raise RuntimeError(format_request_error(e, generation_url)) from e
                await asyncio.sleep(min(10, 2 ** attempt))

    raise RuntimeError("GPT Image2 返回格式中没有 b64_json 或 url")


imagegen_cmd = on_command(
    "img",
    aliases={"画图", "生图", "imagegen"},
    priority=5,
    block=True,
)


@imagegen_cmd.handle()
async def handle_imagegen(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    prompt = args.extract_plain_text().strip()
    if not prompt:
        await imagegen_cmd.finish(
            "用法：/img [提示词]\n"
            "示例：/img 一只穿宇航服的猫，电影感灯光\n"
            f"配置文件：{CONFIG_PATH}"
        )

    try:
        config = load_config()
        target_group_id = resolve_target_group_id(event, config)
    except Exception as e:
        await imagegen_cmd.finish(str(e))

    await imagegen_cmd.send("正在调用 GPT Image2 生成图片，请稍候...")

    try:
        image_message = await call_gpt_image2(build_image_prompt(prompt), config)
        result_message = (
            MessageSegment.at(event.get_user_id())
            + "\n图片生成成功！\n"
            + f"prompt: {prompt}\n"
            + image_message
        )
        await bot.send_group_msg(
            group_id=target_group_id,
            message=result_message,
        )
    except httpx.RequestError as e:
        logger.opt(exception=True).error("GPT Image2 网络请求失败")
        await imagegen_cmd.finish(f"网络请求失败：{e.__class__.__name__}: {e}")
    except Exception as e:
        logger.opt(exception=True).error("GPT Image2 生图失败")
        await imagegen_cmd.finish(f"生图失败：{e}")

    if isinstance(event, GroupMessageEvent) and int(event.group_id) == target_group_id:
        return

    await imagegen_cmd.finish(f"图片已发送到群 {target_group_id}")
