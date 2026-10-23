"""整数円の配分プリミティブ。

すべての金額は通貨の最小単位(JPY は 1 円)の Python int で扱い、float は一切使わない。
端数は最大剰余法(Hamilton 法)で配分し、同順位の剰余は `seed` と参加者キーの SHA-256 で
決定論的に順位付けする。入力順序に依存しないため、同じ入力は常に同じ結果になる。

参考: Spliit (MIT) `src/lib/shares.ts` の Hamilton 法・決定論的タイブレークの考え方と対照した
独自実装(コードの複写はしていない)。third_party_notices/NOTICE.md 参照。
"""

from __future__ import annotations

import hashlib
from collections.abc import Hashable, Mapping

# JS の Number.MAX_SAFE_INTEGER (2^53-1) より十分小さい上限。API 境界で検証する。
MAX_ABS_AMOUNT = 10**12


class MoneyError(ValueError):
    """金額計算の入力が不正、または不変条件が破れたことを表す。"""

    def __init__(self, code: str, message: str, **detail: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class InvariantViolation(MoneyError):
    """計算結果の検証失敗。確定処理を必ず中止する。"""

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__("INVARIANT_VIOLATION", message, **detail)


def ensure_int(value: object, name: str) -> int:
    # bool は int のサブクラスなので明示的に除外する
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError("INVALID_AMOUNT", f"{name} は整数でなければなりません", field=name)
    if abs(value) > MAX_ABS_AMOUNT:
        raise MoneyError("AMOUNT_OUT_OF_RANGE", f"{name} が上限を超えています", field=name)
    return value


def tiebreak_key(seed: str, key: Hashable) -> str:
    return hashlib.sha256(f"{seed}\x1f{key}".encode()).hexdigest()


def apportion[K: Hashable](total: int, weights: Mapping[K, int], seed: str) -> dict[K, int]:
    """`total` を非負整数の重みに比例して整数配分する。

    保証:
      - sum(result.values()) == total(負の total も可。絶対値を配分して符号を戻す)
      - 重み 0 のキーは 0
      - 各キーの配分額は理想値 total*w/W の floor か ceil(絶対値ベース)
      - 結果は weights の挿入順序に依存しない
    """
    ensure_int(total, "total")
    if not weights:
        raise MoneyError("NO_PARTICIPANTS", "配分先が1人以上必要です")
    for k, w in weights.items():
        if isinstance(w, bool) or not isinstance(w, int) or w < 0:
            raise MoneyError("INVALID_WEIGHT", "重みは0以上の整数でなければなりません", key=str(k))
    weight_sum = sum(weights.values())
    if weight_sum == 0:
        raise MoneyError("ZERO_WEIGHT", "重みの合計が0です")

    sign = -1 if total < 0 else 1
    magnitude = abs(total)
    base: dict[K, int] = {}
    remainders: list[tuple[int, str, K]] = []
    for k, w in weights.items():
        q, r = divmod(magnitude * w, weight_sum)
        base[k] = q
        remainders.append((r, tiebreak_key(seed, k), k))

    leftover = magnitude - sum(base.values())
    # 剰余の大きい順。同値は seed 由来のハッシュ順(入力順序非依存)。
    remainders.sort(key=lambda t: (-t[0], t[1]))
    for i in range(leftover):
        base[remainders[i][2]] += 1

    result = {k: sign * v for k, v in base.items()}
    if sum(result.values()) != total:
        raise InvariantViolation("配分合計が元金額と一致しません", total=total)
    return result
