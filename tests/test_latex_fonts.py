import unittest

from latex_fonts import CJK_FONT_FAMILIES, CJK_FONT_PATHS, CSS_CJK_FONT_STACK


class LatexFontConfigurationTests(unittest.TestCase):
    def test_standard_serif_chinese_font_is_preferred(self):
        self.assertEqual(CJK_FONT_FAMILIES[0], "Noto Serif CJK SC")
        self.assertIn("NotoSerifCJK", CJK_FONT_PATHS[0].name)
        self.assertTrue(CSS_CJK_FONT_STACK.startswith("'Noto Serif CJK SC'"))

    def test_stylized_project_font_is_not_used_for_latex(self):
        configured_fonts = [str(path).lower() for path in CJK_FONT_PATHS]
        self.assertFalse(any("yuruka" in path for path in configured_fonts))


if __name__ == "__main__":
    unittest.main()
