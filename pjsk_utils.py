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
                return None, "未找到该角色或 ID，或网络资源获取失败"

        base_img = await self.get_sticker_image(char)
        if not base_img:
            return None, "素材下载失败（可能是网络问题）"

        width, height = base_img.size
        conf = char.get("defaultText", {})
        
        # 获取原始配置参数
        orig_font_size = conf.get("s", 47)
        x = conf.get("x", 148)
        y = conf.get("y", 58)
        r = conf.get("r", 0)
        color = char.get("color", "#FB8AAC")
        base_stroke_width = 6

        # 设定文字框的最大边界 (经验值)
        max_text_width = width * 0.8  
        max_text_height = height * 0.55

        # 查找可用字体
        font_path_to_use = None
        if font_path and os.path.exists(font_path):
            font_path_to_use = font_path
        else:
            font_paths = [
                "data/pjsk/yuruka.otf",   # <--- 【新增这一行】把原版字体放在最前面！(如果是otf后缀请改成yuruka.otf)
                "data/pjsk/font.ttf",
                "data/pjsk/font.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/System/Library/Fonts/STHeiti Light.ttc",
                "msyh.ttc",
                "simhei.ttf",
                "arial.ttf"
            ]
            for p in font_paths:
                if os.path.exists(p):
                    font_path_to_use = p
                    break

        # 动态缩放与自动换行算法
        font_size = orig_font_size
        wrapped_text = text
        font = None
        temp_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

        while font_size >= 16:  # 最小字号限制为 16
            if font_path_to_use:
                try:
                    font = ImageFont.truetype(font_path_to_use, font_size)
                except Exception:
                    font = ImageFont.load_default()
            else:
                font = ImageFont.load_default()

            if font_path_to_use is None:
                wrapped_text = text
                break

            # 自动换行计算
            lines = []
            curr_line = ""
            for char_str in text:
                if char_str == '\n':
                    lines.append(curr_line)
                    curr_line = ""
                    continue
                
                # 计算加上这个字后的像素宽度
                try:
                    w = font.getlength(curr_line + char_str)
                except AttributeError:
                    w = font.getsize(curr_line + char_str)[0] # 兼容老版本 Pillow

                if w <= max_text_width:
                    curr_line += char_str
                else:
                    lines.append(curr_line)
                    curr_line = char_str
            if curr_line:
                lines.append(curr_line)
                
            wrapped_text = "\n".join(lines)

            # 计算这段多行文字的总高度
            try:
                bbox = temp_draw.multiline_textbbox((0, 0), wrapped_text, font=font, align="center")
                text_h = bbox[3] - bbox[1]
            except AttributeError:
                text_h = len(lines) * (font_size + 4) # 兼容老版本 Pillow

            # 如果放得下，或者已经缩到极小了，就退出循环
            if text_h <= max_text_height or font_size <= 16:
                break
                
            font_size -= 4 # 放不下就缩小 4 号字继续试

        # 动态计算描边宽度 (字越小，描边要越细，否则看不清字)
        current_stroke = max(2, int(base_stroke_width * (font_size / orig_font_size)))

        # 开始渲染
        temp_w, temp_h = width * 2, height * 2
        text_layer = Image.new("RGBA", (temp_w, temp_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(text_layer)
        cx, cy = temp_w // 2, temp_h // 2
        
        try:
            # 增加了 align="center" 确保多行文本居中对齐
            draw.text(
                (cx, cy), 
                wrapped_text, 
                font=font, 
                fill=color, 
                anchor="mm", 
                align="center", 
                stroke_width=current_stroke, 
                stroke_fill="white"
            )
        except NotImplementedError:
            return None, "环境缺少必要的 TrueType 字体，无法渲染文本"

        # 逆时针旋转文字
        rotated_text = text_layer.rotate(-r, resample=Image.Resampling.BICUBIC, expand=False)
        
        # 计算偏移并拼贴
        offset_x = x - cx
        offset_y = y - cy
        base_img.paste(rotated_text, (int(offset_x), int(offset_y)), mask=rotated_text)
        
        output = BytesIO()
        base_img.save(output, format="PNG")
        return output.getvalue(), None

pjsk_utils = PJSKUtils()