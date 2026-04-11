import os
import json
import httpx
import asyncio
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

# 基础配置
BASE_URL = "https://raw.githubusercontent.com/TheOriginalAyaka/sekai-stickers/main/"
DATA_DIR = Path("data/pjsk")
STICKERS_DIR = DATA_DIR / "stickers"
CONFIG_FILE = DATA_DIR / "characters.json"

# 确保目录存在
STICKERS_DIR.mkdir(parents=True, exist_ok=True)

class PJSKUtils:
    def __init__(self):
        self.characters = []
        self._load_config()

    def _load_config(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self.characters = json.load(f)

    async def update_config(self):
        """从 GitHub 更新角色配置文件 (增加 Timeout)"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(f"{BASE_URL}src/characters.json")
                if resp.status_code == 200:
                    self.characters = resp.json()
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump(self.characters, f, ensure_ascii=False, indent=4)
                    return True
            except Exception:
                pass
        return False

    def get_character_by_id(self, char_id):
        for char in self.characters:
            if str(char["id"]) == str(char_id):
                return char
        return None

    def find_character(self, keyword):
        """根据 ID 或名称模糊查找"""
        for char in self.characters:
            # 修复：确保输入的字符串 ID 能和 JSON 里的数字 ID 匹配
            if keyword == str(char["id"]) or keyword.lower() in char["name"].lower():
                return char
        return None

    async def get_sticker_image(self, char):
        """获取并缓存贴纸底图"""
        img_path = STICKERS_DIR / char["img"]
        if not img_path.exists():
            img_path.parent.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(timeout=15.0) as client:
                url = f"{BASE_URL}public/img/{char['img']}"
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        with open(img_path, "wb") as f:
                            f.write(resp.content)
                    else:
                        return None
                except Exception:
                    return None
        return Image.open(img_path).convert("RGBA")

    async def create_sticker(self, char_id, text, font_path=None):
        """创建表情包的完整流程"""
        char = self.find_character(char_id)
        if not char:
            if not self.characters:
                await self.update_config()
                char = self.find_character(char_id)
            if not char:
                return None, "未找到该角色或 ID，或 GitHub 资源获取失败"

        base_img = await self.get_sticker_image(char)
        if not base_img:
            return None, "素材下载失败（可能是网络问题）"

        width, height = base_img.size
        conf = char.get("defaultText", {})
        font_size = conf.get("s", 47)
        
        try:
            if font_path and os.path.exists(font_path):
                font = ImageFont.truetype(font_path, font_size)
            else:
                font_paths = [
                    "/System/Library/Fonts/Hiragino Sans GB.ttc",
                    "/System/Library/Fonts/STHeiti Light.ttc",
                    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "arial.ttf"
                ]
                font = None
                for p in font_paths:
                    if os.path.exists(p):
                        font = ImageFont.truetype(p, font_size)
                        break
                if not font:
                    font = ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        x = conf.get("x", 148)
        y = conf.get("y", 58)
        r = conf.get("r", 0)
        color = char.get("color", "#FB8AAC")
        stroke_width = 6

        temp_w, temp_h = width * 2, height * 2
        text_layer = Image.new("RGBA", (temp_w, temp_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(text_layer)
        
        cx, cy = temp_w // 2, temp_h // 2
        
        # 使用 Pillow 原生支持的描边
        try:
            draw.text((cx, cy), text, font=font, fill=color, anchor="mm", stroke_width=stroke_width, stroke_fill="white")
        except NotImplementedError:
            return None, "环境缺少必要的 TrueType 字体，无法渲染文本"

        # 逆时针旋转
        rotated_text = text_layer.rotate(-r, resample=Image.Resampling.BICUBIC, expand=False)
        
        # 计算偏移
        offset_x = x - cx
        offset_y = y - cy
        
        # 修复越界：使用 paste 及 mask 处理溢出和负坐标
        base_img.paste(rotated_text, (int(offset_x), int(offset_y)), mask=rotated_text)
        
        output = BytesIO()
        base_img.save(output, format="PNG")
        return output.getvalue(), None

pjsk_utils = PJSKUtils()