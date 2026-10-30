"""合成レシート画像と正解ラベルの生成(評価・回帰テスト用)。

実在の店舗・個人情報は含まない。合成データでの精度は実レシートでの精度を意味しない(必ず区別して報告する)。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

GENERATOR_VERSION = "synth-0.2.0"

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Debian/Ubuntu fonts-noto-cjk (OFL-1.1)
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",  # macOS(ローカル実行用。生成画像はリポジトリに含めない)
]

STORES = ["居酒屋 テスト", "カフェ サンプル", "スーパー 架空", "ベーカリー 見本", "定食屋 例"]
ITEMS = ["生ビール", "ハイボール", "ウーロン茶", "唐揚げ", "枝豆", "シーザーサラダ", "焼き鳥盛合せ", "だし巻き玉子",
         "ブレンドコーヒー", "カフェラテ", "チーズケーキ", "クロワッサン", "牛乳", "食パン", "バナナ", "おにぎり鮭",
         "緑茶", "ポテトフライ", "刺身盛合せ", "日替わり定食"]


def find_font(path: str | None = None) -> str:
    for p in [path, *FONT_CANDIDATES]:
        if p and Path(p).exists():
            return p
    raise FileNotFoundError("日本語フォントが見つかりません。--font で Noto Sans CJK などのパスを指定してください")


def yen(v: int) -> str:
    return f"-¥{-v:,}" if v < 0 else f"¥{v:,}"


def bare(v: int) -> str:
    """¥ を印字しないレシートの金額(桁区切りだけ)。"""
    return f"-{-v:,}" if v < 0 else f"{v:,}"


# 実レシートのヘッダーにある、金額ではない数字(架空の住所・番号)
def header_noise(rng: random.Random) -> list[tuple[str, str]]:
    lines = [(f"東京都架空区見本町{rng.randint(1, 9)}-{rng.randint(1, 30)}-{rng.randint(1, 20)}", ""),
             (f"TEL 03-{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}", "")]
    if rng.random() < 0.6:
        lines.append((f"店舗# {rng.randint(100, 9999)} 端末# {rng.randint(1, 99)}", ""))
    if rng.random() < 0.5:
        lines.append((f"登録番号 T{rng.randint(10**12, 10**13 - 1)}", ""))
    return lines


@dataclass
class Sample:
    lines: list[tuple[str, str]]  # (左, 右)
    label: dict[str, Any]


def make_sample(rng: random.Random) -> Sample:
    tax_mode = rng.choice(["exclusive", "inclusive"])
    reduced = rng.random() < 0.4  # 8% と 10% の混在
    layout = rng.choice(["inline", "two_line"])
    style = "bare" if rng.random() < 0.3 else "yen"  # ¥ を印字しないレシート
    money = bare if style == "bare" else yen
    items: list[dict[str, Any]] = []
    lines: list[tuple[str, str]] = [(rng.choice(STORES), "")]
    if rng.random() < 0.7:
        lines += header_noise(rng)
    lines += [(f"2026年{rng.randint(1, 12)}月{rng.randint(1, 28)}日 {rng.randint(10, 22)}:"
               f"{rng.randint(0, 59):02d}", ""), ("", "")]
    for name in rng.sample(ITEMS, rng.randint(2, 7)):
        qty = rng.choice([1, 1, 1, 2, 3])
        unit = rng.randrange(100, 1500, 10)
        tg = "8" if reduced and rng.random() < 0.5 else ("10" if reduced else None)
        mark = "※" if tg == "8" else ""
        amount = unit * qty
        if layout == "two_line" and qty > 1:
            lines.append((f"{name}{mark}", ""))
            lines.append((f"  @{unit:,} x{qty}", money(amount)))
        elif qty > 1:
            lines.append((f"{name}{mark} {qty}個", money(amount)))
        else:
            lines.append((f"{name}{mark}", money(amount)))
        items.append({"name": name, "quantity": qty, "line_minor": amount, "tax_group": tg})
    adjustments: list[dict[str, Any]] = []
    s8 = sum(i["line_minor"] for i in items if i["tax_group"] == "8")
    s10 = sum(i["line_minor"] for i in items if i["tax_group"] != "8")
    lines.append(("", ""))
    subtotal = s8 + s10
    if tax_mode == "exclusive":
        lines.append(("小計", money(subtotal)))
        tax8, tax10 = s8 * 8 // 100, s10 * 10 // 100
        if s8:
            lines.append(("消費税(8%)", money(tax8)))
            adjustments.append({"kind": "tax", "amount_minor": tax8})
        lines.append(("消費税(10%)", money(tax10)))
        adjustments.append({"kind": "tax", "amount_minor": tax10})
        total = subtotal + tax8 + tax10
    else:
        total = subtotal
        if rng.random() < 0.4:
            d = -rng.randrange(50, 300, 10)
            lines.append(("値引", money(d)))
            adjustments.append({"kind": "discount", "amount_minor": d})
            total += d
    lines.append(("合計", money(total)))
    if tax_mode == "inclusive":
        lines.append((f"(内消費税等 {money(total * 10 // 110)})", ""))
    if rng.random() < 0.5:
        paid = ((total // 1000) + 1) * 1000
        lines += [("お預り", money(paid)), ("お釣り", money(paid - total))]
    else:
        lines.append((rng.choice(["QUICPay", "iD", "交通系IC", "VISA", "PayPay"]), money(total)))
    if reduced:
        lines.append(("※は軽減税率対象商品", ""))
    label = {"label_status": "synthetic", "generator": GENERATOR_VERSION, "items": items, "adjustments": adjustments,
             "total_minor": total, "tax_mode": tax_mode, "price_style": style}
    return Sample(lines, label)


def render(sample: Sample, font_path: str, rng: random.Random, difficulty: str) -> Image.Image:
    size = 28
    font = ImageFont.truetype(font_path, size)
    width = 560
    img = Image.new("L", (width, 60 + int(size * 1.6) * len(sample.lines)), 255)
    d = ImageDraw.Draw(img)
    for i, (left, right) in enumerate(sample.lines):
        y = 30 + int(size * 1.6) * i
        d.text((24, y), left, fill=0, font=font)
        if right:
            w = d.textlength(right, font=font)
            d.text((width - 24 - w, y), right, fill=0, font=font)
    if difficulty == "hard":
        img = img.rotate(rng.uniform(-2.5, 2.5), expand=True, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 1.0)))
        noise = Image.effect_noise(img.size, rng.uniform(10, 25))
        img = Image.blend(img, noise, 0.12)
        img = img.point(lambda v: int(v * rng.uniform(0.75, 0.95)))
    return img.convert("RGB")


def generate(out: Path, count: int, seed: int, font: str | None = None, difficulty: str = "easy") -> list[Path]:
    rng = random.Random(seed)
    font_path = find_font(font)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in range(count):
        s = make_sample(rng)
        s.label["generator_seed"] = seed
        s.label["difficulty"] = difficulty
        stem = out / f"synth-{seed}-{n:04d}"
        render(s, font_path, rng, difficulty).save(stem.with_suffix(".png"))
        stem.with_suffix(".json").write_text(json.dumps(s.label, ensure_ascii=False, indent=1))
        paths.append(stem.with_suffix(".png"))
    return paths
