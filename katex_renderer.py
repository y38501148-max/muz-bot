import json
import re
import tempfile
from math import ceil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
KATEX_DIR = BASE_DIR / "vendor" / "katex"
KATEX_CSS = KATEX_DIR / "katex.min.css"
KATEX_JS = KATEX_DIR / "katex.min.js"
INLINE_MATH_PATTERN = re.compile(r"(?<!\\)\$(?!\$)(.*?)(?<!\\)\$(?!\$)", re.DOTALL)
CJK_FONT_PATHS = (
    BASE_DIR / "yuruka.otf",
    BASE_DIR / "data" / "pjsk" / "yuruka.otf",
    Path.home() / "yuruka.otf",
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
)


class KatexEngineUnavailable(RuntimeError):
    pass


class KatexRenderError(ValueError):
    pass


def find_cjk_font_uri() -> str:
    for font_path in CJK_FONT_PATHS:
        if font_path.exists():
            return font_path.resolve().as_uri()
    return ""


def build_font_face_css() -> str:
    font_uri = find_cjk_font_uri()
    if not font_uri:
        return ""
    return (
        "@font-face {"
        "font-family: 'LocalCJK';"
        f"src: url('{font_uri}') format('opentype');"
        "font-display: swap;"
        "}"
    )


def ensure_katex_assets():
    missing_assets = [path for path in (KATEX_CSS, KATEX_JS) if not path.exists()]
    if missing_assets:
        paths = ", ".join(str(path) for path in missing_assets)
        raise KatexEngineUnavailable(f"KaTeX 静态资源缺失：{paths}")


def build_html(lines: list[str]) -> str:
    ensure_katex_assets()
    lines_json = json.dumps(lines, ensure_ascii=False)
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="{KATEX_CSS.resolve().as_uri()}">
  <script src="{KATEX_JS.resolve().as_uri()}"></script>
  <style>
    {build_font_face_css()}
    html, body {{
      margin: 0;
      padding: 0;
      background: transparent;
    }}
    #content {{
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      gap: 22px;
      padding: 22px 28px;
      color: #000;
      background: transparent;
      font-size: 30px;
      line-height: 1.9;
      font-family: 'LocalCJK', 'Noto Sans CJK SC', 'Source Han Sans SC',
        'WenQuanYi Zen Hei', 'PingFang SC', 'Microsoft YaHei', 'SimHei',
        'Arial Unicode MS', sans-serif;
      white-space: pre-wrap;
    }}
    .line {{
      white-space: pre-wrap;
      text-align: center;
    }}
    .katex {{
      font-size: 1.15em;
    }}
  </style>
</head>
<body>
  <div id="content"></div>
  <script>
    const lines = {lines_json};
    const content = document.getElementById("content");

    function containsCjk(text) {{
      return /[\\u3400-\\u9fff\\uf900-\\ufaff\\u3040-\\u30ff]/.test(text);
    }}

    function isEscaped(text, index) {{
      let slashCount = 0;
      for (let cursor = index - 1; cursor >= 0 && text[cursor] === "\\\\"; cursor--) {{
        slashCount += 1;
      }}
      return slashCount % 2 === 1;
    }}

    function findInlineDollar(text, start) {{
      for (let index = start; index < text.length; index++) {{
        if (text[index] !== "$" || isEscaped(text, index)) {{
          continue;
        }}
        if (text[index + 1] === "$" || text[index - 1] === "$") {{
          continue;
        }}
        return index;
      }}
      return -1;
    }}

    function appendText(parent, text) {{
      if (!text) {{
        return;
      }}
      parent.appendChild(document.createTextNode(text));
    }}

    function appendInlineMath(parent, math) {{
      const span = document.createElement("span");
      katex.render(math, span, {{
        displayMode: false,
        throwOnError: true,
        strict: "warn",
        trust: false,
      }});
      parent.appendChild(span);
    }}

    function renderMixedLine(parent, line) {{
      let cursor = 0;
      while (cursor < line.length) {{
        const start = findInlineDollar(line, cursor);
        if (start < 0) {{
          appendText(parent, line.slice(cursor));
          return;
        }}
        const end = findInlineDollar(line, start + 1);
        if (end < 0) {{
          throw new Error("Unmatched $ delimiter");
        }}
        appendText(parent, line.slice(cursor, start));
        appendInlineMath(parent, line.slice(start + 1, end));
        cursor = end + 1;
      }}
    }}

    function renderLine(line) {{
      const div = document.createElement("div");
      div.className = "line";
      if (line.includes("$")) {{
        renderMixedLine(div, line);
      }} else if (containsCjk(line)) {{
        div.textContent = line;
      }} else {{
        const math = line.includes("&") ? "\\\\begin{{aligned}}" + line + "\\\\end{{aligned}}" : line;
        katex.render(math, div, {{
          displayMode: true,
          throwOnError: true,
          strict: "warn",
          trust: false,
        }});
      }}
      content.appendChild(div);
    }}

    try {{
      lines.forEach(renderLine);
    }} catch (error) {{
      document.body.dataset.error = error && error.message ? error.message : String(error);
    }}
  </script>
</body>
</html>
"""


async def render_katex_png(lines: list[str]) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise KatexEngineUnavailable("Python 依赖 playwright 未安装") from e

    html = build_html(lines)
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as temp_file:
        temp_file.write(html)
        temp_path = Path(temp_file.name)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = await browser.new_page(
                    viewport={"width": 1600, "height": 1200},
                    device_scale_factor=2,
                )
                await page.goto(temp_path.resolve().as_uri(), wait_until="networkidle")
                error = await page.evaluate("document.body.dataset.error || ''")
                if error:
                    raise KatexRenderError(error)

                locator = page.locator("#content")
                box = await locator.bounding_box()
                if box:
                    await page.set_viewport_size(
                        {
                            "width": max(400, ceil(box["width"]) + 80),
                            "height": max(240, ceil(box["height"]) + 80),
                        }
                    )
                return await locator.screenshot(type="png", omit_background=True)
            finally:
                await browser.close()
    finally:
        temp_path.unlink(missing_ok=True)
