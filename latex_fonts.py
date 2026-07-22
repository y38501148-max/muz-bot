from pathlib import Path


# Keep LaTeX output on a conventional Chinese serif face. Project-specific
# display fonts (such as the PJSK sticker font) must not leak into formulas.
CJK_FONT_PATHS = (
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Regular.otf"),
    Path("/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
)

CJK_FONT_FAMILIES = (
    "Noto Serif CJK SC",
    "Source Han Serif SC",
    "FandolSong",
    "AR PL UMing CN",
    "Songti SC",
    "STSong",
    "SimSun",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "PingFang SC",
    "Microsoft YaHei",
    "SimHei",
    "Arial Unicode MS",
    "DejaVu Serif",
)

CSS_CJK_FONT_STACK = ", ".join(f"'{font_family}'" for font_family in CJK_FONT_FAMILIES)
