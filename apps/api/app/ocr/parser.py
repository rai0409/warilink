"""日本語レシートのルールベース解析(OCR トークン → 明細の下書き)。

方針:
- 結果は常に「下書き」。金額の確定は利用者の確認後、Phase 1 のエンジンで検算する。
- 合計が合わなくても補正しない。差額と警告を返し、画面で訂正してもらう。
- 文字の誤認識(金額・商品名)と、行の役割の誤判定(小計を商品と判断した等)を区別できるよう、
  各行に元の OCR 行番号と判定理由を残す。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from app.ocr.tokens import OcrToken

PARSER_VERSION = "rule-ja-0.1.0"
LOW_CONFIDENCE = 0.90

# ---------------------------------------------------------------- 正規化・行の組み立て


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("\\", "¥").replace("￥", "¥").replace("円", "円")
    t = re.sub(r"(?<=\d)\s+(?=,\d{3})|(?<=\d,)\s+(?=\d)", "", t)  # "1 ,300" / "1, 300"
    return t.strip()


@dataclass
class Line:
    index: int
    text: str
    tokens: list[OcrToken]
    bbox: tuple[int, int, int, int]
    score: float


def group_lines(tokens: list[OcrToken]) -> list[Line]:
    """縦方向の重なりで同じ行のトークンをまとめ、行内は左から右へ並べる。"""
    rows: list[list[OcrToken]] = []
    for tok in sorted(tokens, key=lambda t: ((t.bbox[1] + t.bbox[3]) / 2, t.bbox[0])):
        placed = False
        if rows:
            row = rows[-1]
            y0 = min(t.bbox[1] for t in row)
            y1 = max(t.bbox[3] for t in row)
            overlap = min(y1, tok.bbox[3]) - max(y0, tok.bbox[1])
            h = min(y1 - y0, tok.bbox[3] - tok.bbox[1])
            if h > 0 and overlap / h >= 0.5:
                row.append(tok)
                placed = True
        if not placed:
            rows.append([tok])
    lines = []
    for i, row in enumerate(rows):
        row.sort(key=lambda t: t.bbox[0])
        text = " ".join(normalize(t.text) for t in row if t.text.strip())
        bbox = (min(t.bbox[0] for t in row), min(t.bbox[1] for t in row),
                max(t.bbox[2] for t in row), max(t.bbox[3] for t in row))
        lines.append(Line(i, text, row, bbox, min(t.score for t in row)))
    return lines


# ---------------------------------------------------------------- 金額・数量

_MONEY = re.compile(
    r"(?P<neg>[-−‐▲△])?\s*(?P<yen>¥)?\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<unit>\s*円)?(?P<mark>\s*[※*軽])?"
)
_QTY_PATTERNS = [
    re.compile(r"[@＠]\s*¥?\s*(?P<unit>\d{1,3}(?:,\d{3})+|\d+)\s*[x×X*]\s*(?P<qty>\d{1,3})"),
    re.compile(r"(?P<qty>\d{1,3})\s*(?:個|点|コ|ケ|本|杯|枚|皿|人前|袋|パック)\s*[x×X*]?\s*(?:[@＠]?\s*¥?\s*(?P<unit>\d{1,3}(?:,\d{3})+|\d+))?"),
    re.compile(r"[x×X*]\s*(?P<qty>\d{1,3})(?!\d)"),
]
_REDUCED_MARK = re.compile(r"[※*]|軽(?:減)?")


def _to_int(num: str) -> int:
    return int(num.replace(",", ""))


@dataclass
class Money:
    value: int
    start: int
    explicit: bool  # ¥・円・桁区切りのいずれかがあり、金額らしい
    reduced_mark: bool


def find_amount(text: str) -> Money | None:
    """行末寄りの金額を探す。数量(「2」)と金額(「¥1,300」)が連結した「2¥1,300」にも対応する。"""
    candidates: list[Money] = []
    for m in _MONEY.finditer(text):
        num = m.group("num")
        explicit = bool(m.group("yen") or m.group("unit") or "," in num)
        value = _to_int(num)
        if m.group("neg"):
            value = -value  # 長音記号「ー」は商品名(コーヒー等)と区別できないため負号として扱わない
        candidates.append(Money(value, m.start(), explicit, bool(m.group("mark"))))
    if not candidates:
        return None
    explicit_ones = [c for c in candidates if c.explicit]
    pick = explicit_ones[-1] if explicit_ones else candidates[-1]
    # 年・時刻・電話番号などを金額と誤認しないよう、桁が多すぎる裸の数値は捨てる
    if not pick.explicit and abs(pick.value) >= 100000:
        return None
    return pick


def find_quantity(text: str) -> tuple[int | None, int | None]:
    for pat in _QTY_PATTERNS:
        m = pat.search(text)
        if m:
            qty = int(m.group("qty"))
            unit = m.groupdict().get("unit")
            if 1 <= qty <= 999:
                return qty, (_to_int(unit) if unit else None)
    return None, None


# ---------------------------------------------------------------- 行の役割

_KEYWORDS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore", re.compile(r"お?預[りか]|お?釣|釣銭|現計|クレジット|カード|電子マネー|交通系|paypay|QR|"
                          r"ポイント|残高|担当|レジ|登録番号|TEL|電話|〒|https?|領収|点数|対象|\d{1,2}:\d{2}|"
                          r"内訳|伝票|No\.|お買上|ありがとう", re.I)),
    ("subtotal", re.compile(r"小\s*計|税抜\s*合計")),
    ("tax_inclusive", re.compile(r"内\s*(?:消費)?税|\(内")),
    ("tax", re.compile(r"消費税|外\s*税|税\s*額|^税\b|^\(?税\s*\d+%|税\s*\(?\d+%")),
    ("total", re.compile(r"合\s*計|お会計|ご?請求|お?支払(?:い)?金額|総\s*額")),
    ("coupon", re.compile(r"クーポン")),
    ("discount", re.compile(r"値\s*引|割\s*引|値下|お?値引き|割引")),
    ("service_charge", re.compile(r"サービス料|チャージ|席料|深夜料")),
]
_DATE = re.compile(r"(20\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})")
_RATE = re.compile(r"(8|10)\s*%")


def classify(text: str, amount: Money | None) -> str:
    for role, pat in _KEYWORDS:
        if pat.search(text):
            return role
    if amount is None:
        return "text"
    if amount.value < 0:
        return "discount"
    return "item"


def _name_part(text: str, amount: Money | None) -> str:
    head = text[: amount.start] if amount else text
    for pat in _QTY_PATTERNS:
        head = pat.sub(" ", head)
    head = _REDUCED_MARK.sub(" ", head)
    head = re.sub(r"(?<!\S)\d{1,2}(?=\s*$)", " ", head)  # 「生ビール 2」の末尾の数量
    return re.sub(r"\s+", " ", head).strip(" .:・-")


def _bare_trailing_qty(text: str, amount: Money | None) -> int | None:
    if amount is None:
        return None
    m = re.search(r"(?:^|\s)(\d{1,2})\s*$", text[: amount.start])
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- 出力


@dataclass
class DraftItem:
    id: str
    name: str
    quantity: int
    line_minor: int
    tax_group: str | None
    confidence: float
    needs_review: bool
    reasons: list[str]
    bbox: list[int]
    source_lines: list[int]
    unit_price_minor: int | None = None


@dataclass
class DraftAdjustment:
    id: str
    kind: str  # tax | discount | coupon | service_charge
    label: str
    amount_minor: int
    tax_group: str | None
    scope_item_ids: list[str] | None
    confidence: float
    needs_review: bool
    bbox: list[int]
    source_lines: list[int]


@dataclass
class ReceiptDraft:
    parser_version: str
    store_name: str | None
    purchased_on: str | None
    items: list[DraftItem]
    adjustments: list[DraftAdjustment]
    subtotal_minor: int | None
    total_minor: int | None
    tax_mode: str  # exclusive | inclusive | unknown
    difference_minor: int | None  # total − (items + adjustments)。0 以外なら画面で訂正が必要
    warnings: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)  # 行ごとの判定(訂正・評価用)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def parse_receipt(tokens: list[OcrToken], *, low_confidence: float = LOW_CONFIDENCE) -> ReceiptDraft:
    lines = group_lines(tokens)
    items: list[DraftItem] = []
    adjs: list[DraftAdjustment] = []
    warnings: list[dict[str, Any]] = []
    line_log: list[dict[str, Any]] = []
    subtotal: int | None = None
    total: int | None = None
    total_line: int | None = None
    inclusive_tax_seen = False
    rates_seen: set[str] = set()
    store_name: str | None = None
    purchased_on: str | None = None
    pending_name: Line | None = None  # 金額が次の行にある商品名
    pending_keyword: Line | None = None  # 金額が次の行にある「小計」「合計」など
    after_total = False

    def add_item(name: str, src: list[Line], amount: Money, qty: int | None, unit: int | None) -> None:
        reasons: list[str] = []
        q = qty or 1
        if unit is not None and qty is not None and unit * qty != abs(amount.value):
            reasons.append("QTY_PRICE_INCONSISTENT")
        score = min(s.score for s in src)
        if score < low_confidence:
            reasons.append("LOW_CONFIDENCE")
        if not name:
            reasons.append("NAME_MISSING")
        mark = amount.reduced_mark or any(_REDUCED_MARK.search(s.text) for s in src)
        bbox = [min(s.bbox[0] for s in src), min(s.bbox[1] for s in src),
                max(s.bbox[2] for s in src), max(s.bbox[3] for s in src)]
        items.append(DraftItem(
            id=f"ocr-i{len(items) + 1}", name=name or "(商品名不明)", quantity=q, line_minor=amount.value,
            tax_group="8" if mark else None, confidence=round(score, 4), needs_review=bool(reasons),
            reasons=reasons, bbox=bbox, source_lines=[s.index for s in src], unit_price_minor=unit,
        ))

    for ln in lines:
        text = ln.text
        amount = find_amount(text)
        role = classify(text, amount)
        if purchased_on is None and (d := _DATE.search(text)):
            purchased_on = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
            role = "date"
        if (r := _RATE.search(text)) and role in ("tax", "tax_inclusive", "ignore"):
            rates_seen.add(r.group(1))
        line_log.append({"index": ln.index, "text": text, "role": role, "score": round(ln.score, 4),
                         "bbox": list(ln.bbox), "amount": amount.value if amount else None})

        if role == "text":
            if store_name is None and not items and amount is None:
                store_name = text
            elif not after_total:
                qty, unit = find_quantity(text)
                if qty is not None and items and items[-1].source_lines[-1] == ln.index - 1:
                    # 「2個 x @650」のように数量だけの行 → 直前の商品の数量
                    items[-1].quantity = qty
                    items[-1].unit_price_minor = unit
                    items[-1].source_lines.append(ln.index)
                    if unit is not None and unit * qty != abs(items[-1].line_minor):
                        items[-1].reasons.append("QTY_PRICE_INCONSISTENT")
                        items[-1].needs_review = True
                else:
                    pending_name = ln
            continue
        if role in ("ignore", "date") or after_total:
            pending_name = pending_keyword = None
            continue
        if amount is None:
            # 「小計」と金額が別の行に分かれた(傾き等)場合に、隣の金額だけの行と組み合わせる
            pending_name = None
            prev = items[-1] if items else None
            if prev is not None and prev.source_lines == [ln.index - 1] and "NAME_MISSING" in prev.reasons:
                items.pop()  # 直前の「金額だけの行」は商品ではなくこの行の金額
                amount = Money(prev.line_minor, 0, True, False)
                line_log[-2]["role"] = "merged"
                line_log[-1]["merged_with"] = ln.index - 1
            else:
                pending_keyword = ln
                continue
        if (role in ("item", "discount") and pending_keyword is not None
                and pending_keyword.index == ln.index - 1 and not _name_part(text, amount)):
            role = classify(pending_keyword.text, None)
            text = f"{pending_keyword.text} {text}"
            amount = find_amount(text)
            assert amount is not None
            line_log[-1]["role"] = role
            line_log[-1]["merged_with"] = pending_keyword.index
        pending_keyword = None

        if role == "item":
            qty, unit = find_quantity(text)
            if qty is None:
                qty = _bare_trailing_qty(text, amount)
            name = _name_part(text, amount)
            src = [ln]
            if not name and pending_name is not None and pending_name.index == ln.index - 1:
                name = _name_part(pending_name.text, None)
                src = [pending_name, ln]
            add_item(name, src, amount, qty, unit)
        elif role == "subtotal":
            subtotal = amount.value
        elif role == "total":
            total, total_line, after_total = amount.value, ln.index, True
        elif role == "tax_inclusive":
            inclusive_tax_seen = True
        elif role in ("tax", "discount", "coupon", "service_charge"):
            value = amount.value
            if role in ("discount", "coupon"):
                value = -abs(value)
            rate = _RATE.search(text)
            # 商品の直後の値引きは、その商品だけの値引きとして扱う
            scope = None
            right_after_item = bool(items) and items[-1].source_lines[-1] == ln.index - 1
            if role in ("discount", "coupon") and right_after_item and subtotal is None:
                scope = [items[-1].id]
            adjs.append(DraftAdjustment(
                id=f"ocr-a{len(adjs) + 1}", kind=role, label=_name_part(text, amount) or role, amount_minor=value,
                tax_group=rate.group(1) if rate and role == "tax" else None, scope_item_ids=scope,
                confidence=round(ln.score, 4), needs_review=ln.score < low_confidence or scope is not None,
                bbox=list(ln.bbox), source_lines=[ln.index],
            ))
        pending_name = None

    # 軽減税率の記号がある場合、8% と 10% の両方の税行があれば記号なしの商品を 10% とみなす
    if {"8", "10"} <= rates_seen and any(i.tax_group == "8" for i in items):
        for it in items:
            if it.tax_group is None:
                it.tax_group = "10"

    items_sum = sum(i.line_minor for i in items)
    tax_adjs = [a for a in adjs if a.kind == "tax"]
    others = sum(a.amount_minor for a in adjs if a.kind != "tax")
    tax_sum = sum(a.amount_minor for a in tax_adjs)
    tax_mode = "unknown"
    if total is not None:
        if tax_adjs and items_sum + others + tax_sum == total:
            tax_mode = "exclusive"
        elif items_sum + others == total:
            tax_mode = "inclusive"
            if tax_adjs:
                # 内税の税額表示を調整行にすると二重計上になるため外す(表示用として行ログには残る)
                adjs = [a for a in adjs if a.kind != "tax"]
        elif inclusive_tax_seen:
            tax_mode = "inclusive"
            adjs = [a for a in adjs if a.kind != "tax"]

    breakdown = items_sum + sum(a.amount_minor for a in adjs)
    difference = None if total is None else total - breakdown
    if total is None:
        warnings.append({"code": "TOTAL_NOT_FOUND",
                         "message": "合計金額を読み取れませんでした。レシートの合計を入力してください"})
    elif difference:
        warnings.append({"code": "TOTAL_MISMATCH", "message": "読み取った明細の合計がレシートの合計と一致しません",
                         "difference": difference})
    if subtotal is not None and tax_mode == "exclusive" and subtotal != items_sum + sum(
        a.amount_minor for a in adjs if a.kind in ("discount", "coupon") and a.scope_item_ids
    ):
        warnings.append({"code": "SUBTOTAL_MISMATCH", "message": "商品の合計が小計と一致しません",
                         "subtotal": subtotal, "items_sum": items_sum})
    if not items:
        warnings.append({"code": "NO_ITEMS", "message": "商品の行を読み取れませんでした"})
    if any(i.needs_review for i in items):
        warnings.append({"code": "ITEMS_NEED_REVIEW", "message": "確認が必要な商品があります"})
    if total_line is not None and any(i.source_lines[0] > total_line for i in items):
        warnings.append({"code": "ITEM_AFTER_TOTAL", "message": "合計より後に商品らしい行があります"})

    return ReceiptDraft(
        parser_version=PARSER_VERSION, store_name=store_name, purchased_on=purchased_on, items=items,
        adjustments=adjs, subtotal_minor=subtotal, total_minor=total, tax_mode=tax_mode,
        difference_minor=difference, warnings=warnings, lines=line_log,
    )
