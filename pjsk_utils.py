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
        """从 GitHub 更新角色配置文件"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}src/characters.json")
            if resp.status_code == 200:
                self.characters = resp.json()
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.characters, f, ensure_ascii=False, indent=4)
                return True
        return False

    def get_character_by_id(self, char_id):
        for char in self.characters:
            if char["id"] == char_id:
                return char
        return None

    def find_character(self, keyword):
        """根据 ID 或名称模糊查找"""
        for char in self.characters:
            if keyword == char["id"] or keyword.lower() in char["name"].lower():
                return char
        return None

    async def get_sticker_image(self, char):
        """获取并缓存贴纸底图"""
        img_path = STICKERS_DIR / char["img"]
        if not img_path.exists():
            img_path.parent.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient() as client:
                # 修正：去掉了 /stickers/，直接在 public/img/ 下
                url = f"{BASE_URL}public/img/{char['img']}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    with open(img_path, "wb") as f:
                        f.write(resp.content)
                else:
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
                return None, "未找到该角色或 ID"

        base_img = await self.get_sticker_image(char)
        if not base_img:
            return None, "素材下载失败"

        # 渲染逻辑
        width, height = base_img.size
        
        # 字体处理
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

        # 获取坐标和属性
        x = conf.get("x", 148)
        y = conf.get("y", 58)
        r = conf.get("r", 0)
        color = char.get("color", "#FB8AAC")
        stroke_width = 6 # 增加描边宽度，匹配原版效果

        # 计算文字大小
        # 使用 anchor="mm" 模式，中心对齐
        # 先创建一个包含文字和描边的透明图层
        # 文字可能很长，所以图层要足够大
        temp_w, temp_h = width * 2, height * 2
        text_layer = Image.new("RGBA", (temp_w, temp_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(text_layer)
        
        cx, cy = temp_w // 2, temp_h // 2
        
        # 绘制描边
        for ox in range(-stroke_width, stroke_width + 1):
            for oy in range(-stroke_width, stroke_width + 1):
                if ox*ox + oy*oy <= stroke_width*stroke_width: # 圆形描边更平滑
                    draw.text((cx + ox, cy + oy), text, font=font, fill="white", anchor="mm")
        
        # 绘制文字
        draw.text((cx, cy), text, font=font, fill=color, anchor="mm")

        # 旋转文字层 (逆时针旋转，原配置 r 为角度)
        # sekai-stickers 的 r 是弧度还是角度？看 JSON 通常是角度。
        # 原版是用之后顺时针旋转，所以这里用 -r
        rotated_text = text_layer.rotate(-r, resample=Image.BICUBIC, expand=False)
        
        # 将旋转后的层贴回底图
        # text_layer 的 (cx, cy) 对应 base_img 的 (x, y)
        offset_x = x - cx
        offset_y = y - cy
        
        base_img.alpha_composite(rotated_text, (int(offset_x), int(offset_y)))
        
        output = BytesIO()
        base_img.save(output, format="PNG")
        return output.getvalue(), None

pjsk_utils = PJSKUtils()
