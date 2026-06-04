import base64
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
    "TIMEOUT_SECONDS": 180,
}


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


def build_generation_url(base_url: str) -> str:
    base = (base_url or DEFAULT_CONFIG["BASE_URL"]).strip().rstrip("/")
    if base.endswith("/images/generations"):
        return base
    if not base.endswith("/v1"):
        base = f"{base}/v1"
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


async def fetch_image_from_url(client: httpx.AsyncClient, url: str) -> bytes:
    if url.startswith("data:image/") and ";base64," in url:
        return base64.b64decode(url.split(";base64,", 1)[1])

    response = await client.get(url)
    if response.status_code >= 400:
        raise RuntimeError(f"图片 URL 下载失败：HTTP {response.status_code}")
    return response.content


async def call_gpt_image2(prompt: str, config: Dict[str, Any]) -> bytes:
    api_key = str(get_config_value(config, "API_KEY") or "").strip()
    if not api_key:
        raise ValueError(f"请先在 {CONFIG_PATH} 里填写 API_KEY")

    base_url = str(get_config_value(config, "BASE_URL") or DEFAULT_CONFIG["BASE_URL"]).strip()
    timeout_seconds = float(get_config_value(config, "TIMEOUT_SECONDS") or 180)

    payload = {
        "model": get_config_value(config, "MODEL") or "gpt-image-2",
        "prompt": prompt,
        "n": 1,
        "size": get_config_value(config, "SIZE") or "1024x1024",
        "quality": get_config_value(config, "QUALITY") or "medium",
        "output_format": get_config_value(config, "OUTPUT_FORMAT") or "png",
        "moderation": get_config_value(config, "MODERATION") or "auto",
    }
    payload = {k: v for k, v in payload.items() if v not in (None, "")}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(timeout_seconds, connect=20.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(
            build_generation_url(base_url),
            headers=headers,
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GPT Image2 请求失败：HTTP {response.status_code}，{extract_api_error(response)}")

        data = response.json()
        images = data.get("data", []) if isinstance(data, dict) else []
        if not images:
            raise RuntimeError("GPT Image2 没有返回图片数据")

        first_image = images[0]
        b64_json = first_image.get("b64_json") if isinstance(first_image, dict) else None
        if b64_json:
            return base64.b64decode(b64_json)

        image_url = first_image.get("url") if isinstance(first_image, dict) else None
        if image_url:
            return await fetch_image_from_url(client, image_url)

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
        image_bytes = await call_gpt_image2(prompt, config)
        await bot.send_group_msg(
            group_id=target_group_id,
            message=MessageSegment.image(image_bytes),
        )
    except httpx.RequestError as e:
        logger.opt(exception=True).error("GPT Image2 网络请求失败")
        await imagegen_cmd.finish(f"网络请求失败：{e}")
    except Exception as e:
        logger.opt(exception=True).error("GPT Image2 生图失败")
        await imagegen_cmd.finish(f"生图失败：{e}")

    if isinstance(event, GroupMessageEvent) and int(event.group_id) == target_group_id:
        return

    await imagegen_cmd.finish(f"图片已发送到群 {target_group_id}")
