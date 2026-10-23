"""OCR トークンを手で組み立てるための補助(認識エンジンを使わないテスト用)。"""

from app.ocr.tokens import OcrToken


def tokens_from_lines(lines: list[tuple[str, str] | str], score: float = 0.99, *, dy: int = 40) -> list[OcrToken]:
    """[(左の文字列, 右の文字列)] を、左右に分かれた 1 行ずつのトークンにする。"""
    out = []
    for i, line in enumerate(lines):
        left, right = (line, "") if isinstance(line, str) else line
        y = 20 + i * dy
        if left:
            out.append(OcrToken(left, score, (20, y, 20 + 18 * len(left), y + 30)))
        if right:
            out.append(OcrToken(right, score, (400, y, 400 + 14 * len(right), y + 30)))
    return out
