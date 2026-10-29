/** この端末だけに保存する設定(サーバーには送らない)。保存できない環境では何もしない */

const NAME_KEY = "warilink.defaultName";

export function loadDefaultName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveDefaultName(name: string): void {
  try {
    localStorage.setItem(NAME_KEY, name);
  } catch {
    /* 保存できなくても動作は変わらない */
  }
}

const inviteKey = (publicId: string) => `warilink.invite.${publicId}`;

/** 作成者が発行した招待リンクを、この端末にだけ覚えておく(サーバーはハッシュしか持たないため) */
export function loadInviteUrl(publicId: string): string | null {
  try {
    return localStorage.getItem(inviteKey(publicId));
  } catch {
    return null;
  }
}

export function saveInviteUrl(publicId: string, url: string | null): void {
  try {
    if (url) localStorage.setItem(inviteKey(publicId), url);
    else localStorage.removeItem(inviteKey(publicId));
  } catch {
    /* 保存できなくても動作は変わらない */
  }
}
