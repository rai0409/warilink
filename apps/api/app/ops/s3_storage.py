"""S3 の外部保管と削除ジャーナル(STEP 02)。AWS SDK(boto3)を呼ぶのは aws_client() だけ。

設定: `s3://バケット/接頭辞`(WARILINK_BACKUP_OFFSITE・WARILINK_DELETION_JOURNAL)。接頭辞は必須。実バケット名はコードに書かない。

AWS への接続は、運用者が WARILINK_AWS_LIVE=1 を明示したときだけ行う(資格情報が環境にあっても自動で接続しない)。
boto3 は任意の依存グループ(`uv sync --group aws`)で、API と CI には入れない。

完全性の扱い:
- ETag を SHA-256 と見なさない。アップロードでは SHA-256(ChecksumSHA256)を送って S3 側でも検証させ、
  応答の値・読み戻した実バイトのハッシュと照合する。
- バージョン管理が前提。応答に VersionId が無ければ失敗にする。読み戻しは記録した VersionId を指定する。
- バケットの設定(バージョン管理・Object Lock・既定の暗号化)はコードで変えない。check_bucket() で確かめるだけ。
- エラーの表示は AWS のエラーコードと短い日本語だけ(バケット名・キー・応答本文・資格情報を出さない)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ops.deletion_journal import (
    EMPTY,
    Checkpoint,
    JournalConflict,
    JournalCorrupt,
    JournalState,
    JournalUnavailable,
    _AppendJournal,
    replay,
)
from app.ops.offsite import OffsiteError

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._=-]+$")
MIN_LOCK_DAYS = 30


class AwsUnavailable(Exception):
    """AWS へ接続しない・できない(未許可・SDK なし)。"""


@dataclass(frozen=True)
class S3Location:
    bucket: str
    prefix: str


def parse_s3_url(value: str) -> S3Location:
    """`s3://バケット/接頭辞`。接頭辞は英数字と ._=- の区切りだけ(`..`・空の区切り・先頭の / は拒否)。"""
    if not value.startswith("s3://"):
        raise OffsiteError("S3 の保管先は s3://バケット/接頭辞 の形式にしてください")
    bucket, _, prefix = value[5:].partition("/")
    prefix = prefix.rstrip("/")
    if not BUCKET_RE.fullmatch(bucket) or ".." in bucket:
        raise OffsiteError("S3 のバケット名が不正です")
    parts = prefix.split("/") if prefix else []
    if not parts or any(p in ("", ".", "..") or not SEGMENT_RE.fullmatch(p) for p in parts):
        raise OffsiteError("S3 の接頭辞(s3://バケット/接頭辞)を指定してください。使える文字は英数字と ._=- です")
    return S3Location(bucket, prefix)


def aws_client(service: str, env: dict[str, str] | None = None) -> Any:
    """boto3 のクライアント。WARILINK_AWS_LIVE=1 の明示が無ければ作らない(テスト・開発で誤って接続しない)。"""
    e = os.environ if env is None else env
    if e.get("WARILINK_AWS_LIVE") != "1":
        raise AwsUnavailable("AWS への接続は WARILINK_AWS_LIVE=1 を設定したときだけ行います")
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415
    except ImportError:
        raise AwsUnavailable("boto3 が入っていません(cd apps/api && uv sync --group aws)") from None
    return boto3.client(service, config=Config(retries={"max_attempts": 3, "mode": "standard"},
                                               connect_timeout=10, read_timeout=60))


def error_code(e: Exception) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9.]{1,64}", code):
            return code
    return type(e).__name__


DENIED = {"AccessDenied", "AccessDeniedException", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken",
          "InvalidToken", "UnrecognizedClientException", "AllAccessDisabled", "InvalidClientTokenId"}
NOT_FOUND = {"NoSuchKey", "NoSuchVersion", "NotFound", "404", "NoSuchBucket"}
CONFLICT = {"PreconditionFailed", "412", "ConditionalRequestConflict"}


def safe_message(e: Exception, what: str) -> str:
    """利用者・ログに出す失敗の説明。エラーコードだけを使い、応答本文・キー・バケット名を含めない。"""
    code = error_code(e)
    if code in DENIED:
        return f"{what}: 権限・署名の確認に失敗しました({code})"
    if "Timeout" in code or code in ("EndpointConnectionError", "ConnectionClosedError", "ConnectTimeoutError"):
        return f"{what}: 接続できないか時間切れです({code})"
    return f"{what}: 失敗しました({code})"


def _b64(hexdigest: str) -> str:
    return base64.b64encode(bytes.fromhex(hexdigest)).decode()


def _hex(b64: str) -> str | None:
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        return None
    return raw.hex() if len(raw) == 32 else None


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_key(key: str) -> str:
    parts = key.split("/")
    if not key or any(p in ("", ".", "..") for p in parts) or key.startswith("/"):
        raise OffsiteError("保管先の外へは書き込めません")
    return key


@dataclass
class S3Storage:
    """OffsiteStorage の S3 実装(単一の PUT。5 GB を超えるファイルは未対応)。"""

    client: Any
    location: S3Location
    name: str = "s3"

    def _key(self, key: str) -> str:
        return f"{self.location.prefix}/{_check_key(key)}"

    def stat(self, key: str) -> tuple[str, str | None] | None:
        """(SHA-256, VersionId)。無ければ None。S3 の SHA-256 が無い・形式が違うものは「無い」とみなす(置き直す)。"""
        try:
            r = self.client.head_object(Bucket=self.location.bucket, Key=self._key(key), ChecksumMode="ENABLED")
        except Exception as e:  # noqa: BLE001 - SDK の例外は種類が多い。コードだけで分ける
            if error_code(e) in NOT_FOUND:
                return None
            raise OffsiteError(safe_message(e, "S3 の確認")) from None
        digest = _hex(r.get("ChecksumSHA256") or "")
        if digest is None or r.get("DeleteMarker"):
            return None
        return digest, r.get("VersionId")

    def sha256(self, key: str) -> str | None:
        st = self.stat(key)
        return st[0] if st else None

    def put(self, key: str, src: Path) -> str:
        digest = _sha256_file(src)
        try:
            with src.open("rb") as f:
                r = self.client.put_object(Bucket=self.location.bucket, Key=self._key(key), Body=f,
                                           ChecksumSHA256=_b64(digest), Metadata={"warilink-sha256": digest})
        except Exception as e:  # noqa: BLE001
            raise OffsiteError(safe_message(e, "S3 への書き込み")) from None
        if _hex(r.get("ChecksumSHA256") or "") != digest:
            raise OffsiteError("S3 が受け取った内容の SHA-256 が一致しません")
        vid = r.get("VersionId")
        if not vid or vid == "null":
            raise OffsiteError("S3 のバージョン管理が有効ではありません(世代を記録できないため失敗とします)")
        return str(vid)

    def get(self, key: str, version_id: str | None, dst: Path) -> str:
        """記録した世代を取得して dst に書く。戻り値は実バイトの SHA-256。世代の指定は必須。"""
        if not version_id:
            raise OffsiteError("S3 から読み戻すときは世代(VersionId)を指定してください")
        try:
            r = self.client.get_object(Bucket=self.location.bucket, Key=self._key(key), VersionId=version_id,
                                       ChecksumMode="ENABLED")
        except Exception as e:  # noqa: BLE001
            raise OffsiteError(safe_message(e, "S3 からの読み戻し")) from None
        if r.get("VersionId") != version_id:
            raise OffsiteError("S3 が返した世代が指定と違います")
        h = hashlib.sha256()
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        body = r["Body"]
        try:
            with tmp.open("wb") as out:
                for chunk in iter(lambda: body.read(1 << 20), b""):
                    h.update(chunk)
                    out.write(chunk)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as e:  # noqa: BLE001 - 読み取り途中の切断など
            tmp.unlink(missing_ok=True)
            raise OffsiteError(safe_message(e, "S3 からの読み戻し")) from None
        stored = _hex(r.get("ChecksumSHA256") or "")
        if stored is not None and stored != h.hexdigest():
            tmp.unlink(missing_ok=True)
            raise OffsiteError("S3 から読み戻した内容の SHA-256 が一致しません")
        os.replace(tmp, dst)
        return h.hexdigest()

    def check_bucket(self) -> list[str]:
        """前提条件(バージョン管理・Object Lock の既定の保持 30 日以上・KMS の既定の暗号化)。設定は変えない。"""
        b, out = self.location.bucket, []
        try:
            if self.client.get_bucket_versioning(Bucket=b).get("Status") != "Enabled":
                out.append("S3 のバージョン管理が有効ではありません")
        except Exception as e:  # noqa: BLE001
            out.append(safe_message(e, "S3 のバージョン管理の確認"))
        try:
            lock = self.client.get_object_lock_configuration(Bucket=b).get("ObjectLockConfiguration", {})
            ret = lock.get("Rule", {}).get("DefaultRetention", {})
            days = ret.get("Days") or (ret.get("Years") or 0) * 365
            if lock.get("ObjectLockEnabled") != "Enabled" or ret.get("Mode") not in ("COMPLIANCE", "GOVERNANCE") \
                    or days < MIN_LOCK_DAYS:
                out.append(f"S3 の Object Lock(既定の保持 {MIN_LOCK_DAYS} 日以上)が設定されていません")
        except Exception as e:  # noqa: BLE001
            out.append(safe_message(e, "S3 の Object Lock の確認"))
        try:
            rules = self.client.get_bucket_encryption(Bucket=b).get("ServerSideEncryptionConfiguration", {}) \
                .get("Rules", [])
            algos = {r.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") for r in rules}
            if not algos & {"aws:kms", "aws:kms:dsse"}:
                out.append("S3 の既定の暗号化が KMS ではありません")
        except Exception as e:  # noqa: BLE001
            out.append(safe_message(e, "S3 の暗号化の確認"))
        return out


def storage_from_url(value: str, *, client: Any = None, env: dict[str, str] | None = None) -> S3Storage:
    loc = parse_s3_url(value)
    if client is None:
        try:
            client = aws_client("s3", env)
        except AwsUnavailable as e:
            raise OffsiteError(str(e)) from None
    return S3Storage(client, loc)


# ---------------------------------------------------------------- 削除ジャーナル(STEP 01 の形式のまま S3 に置く)


@dataclass
class S3Journal(_AppendJournal):
    """1 件 = 1 オブジェクト(`接頭辞/records/{seq:012d}.json`)と `接頭辞/HEAD`。

    - 記録は If-None-Match: * で置く(同じ番号の上書き・重複を S3 が拒否する)。成功の応答を耐久化の確認とする。
    - HEAD は If-Match(ETag は同時更新の目印として使うだけで、内容の検証には使わない)で更新する。
      競合したら読み直して再試行する(JournalConflict)。
    - 読むときは全世代を一覧し、記録のキーに 2 つ以上の世代・削除の印があれば「世代のすり替え」として拒否する。
      Object Lock は上書き・削除を防ぐ設定の 1 つで、これだけで全履歴の完全性を保証するものではない
      (連番・hash の連鎖と HEAD の照合を併用する)。
    """

    location: S3Location
    client_: Any = None
    name: str = "s3"
    _cache: dict[str, tuple[str, dict]] = field(default_factory=dict)  # key → (VersionId, 記録)
    _head_etag: str | None = None

    @property
    def client(self) -> Any:
        if self.client_ is None:
            try:
                self.client_ = aws_client("s3")
            except AwsUnavailable as e:
                raise JournalUnavailable(str(e)) from None
        return self.client_

    def _rkey(self, seq: int) -> str:
        return f"{self.location.prefix}/records/{seq:012d}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        yield  # 直列化は条件付き書き込みで行う

    def _versions(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        kw: dict[str, Any] = {"Bucket": self.location.bucket, "Prefix": f"{self.location.prefix}/records/"}
        while True:
            r = self.client.list_object_versions(**kw)
            for v in r.get("Versions", []):
                out.setdefault(v["Key"], []).append({"id": v["VersionId"], "marker": False})
            for m in r.get("DeleteMarkers", []):
                out.setdefault(m["Key"], []).append({"id": m["VersionId"], "marker": True})
            if not r.get("IsTruncated"):
                return out
            kw["KeyMarker"], kw["VersionIdMarker"] = r.get("NextKeyMarker"), r.get("NextVersionIdMarker")

    def _get_json(self, key: str, version_id: str | None = None) -> tuple[dict, dict]:
        kw: dict[str, Any] = {"Bucket": self.location.bucket, "Key": key}
        if version_id:
            kw["VersionId"] = version_id
        r = self.client.get_object(**kw)
        try:
            return json.loads(r["Body"].read()), r
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise JournalCorrupt("削除ジャーナルの形式が壊れています") from None

    def _state_locked(self) -> JournalState:
        try:
            versions = self._versions()
            records = []
            for i, key in enumerate(sorted(versions), start=1):
                vs = versions[key]
                if len(vs) != 1 or vs[0]["marker"]:
                    raise JournalCorrupt("削除ジャーナルの記録に複数の世代か削除の印があります(世代のすり替えのおそれ)")
                if key != self._rkey(i):
                    raise JournalCorrupt(f"削除ジャーナルの記録の連番が途切れています(seq {i})")
                cached = self._cache.get(key)
                if cached is None or cached[0] != vs[0]["id"]:
                    rec, _ = self._get_json(key, vs[0]["id"])
                    self._cache[key] = (vs[0]["id"], rec)
                records.append(self._cache[key][1])
            head: Checkpoint | None = None
            self._head_etag = None
            try:
                raw, r = self._get_json(f"{self.location.prefix}/HEAD")
                head, self._head_etag = Checkpoint.from_json(raw), r.get("ETag")
            except JournalCorrupt:
                raise
            except Exception as e:  # noqa: BLE001
                if error_code(e) not in NOT_FOUND:
                    raise
        except (JournalCorrupt, JournalUnavailable):
            raise
        except Exception as e:  # noqa: BLE001
            raise JournalUnavailable(safe_message(e, "削除ジャーナルの読み取り")) from None
        # HEAD がまだ無い = 最初の記録を置いた直後に HEAD を書けなかった場合だけを許す(1 つ遅れ。2 件以上なら欠落)
        return replay(records, head if head is not None else EMPTY)

    def _write(self, rec: dict) -> None:
        body = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        try:
            self.client.put_object(Bucket=self.location.bucket, Key=self._rkey(rec["seq"]), Body=body,
                                   IfNoneMatch="*", ChecksumSHA256=base64.b64encode(hashlib.sha256(body).digest())
                                   .decode())
        except Exception as e:  # noqa: BLE001
            if error_code(e) in CONFLICT:
                raise JournalConflict("同じ番号の記録が先に書かれました") from None
            raise JournalUnavailable(safe_message(e, "削除ジャーナルへの書き込み")) from None
        head = json.dumps(Checkpoint(rec["seq"], rec["hash"]).to_json()).encode()
        cond = {"IfMatch": self._head_etag} if self._head_etag else {"IfNoneMatch": "*"}
        try:
            self.client.put_object(Bucket=self.location.bucket, Key=f"{self.location.prefix}/HEAD", Body=head,
                                   **cond)
        except Exception as e:  # noqa: BLE001
            if error_code(e) in CONFLICT:
                return  # 他の書き手が HEAD を先に進めた。記録は耐久化済み(HEAD の 1 つ遅れは読み取りで許す)
            raise JournalUnavailable(safe_message(e, "削除ジャーナルの HEAD の更新")) from None
