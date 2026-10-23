"use client";

import { useState } from "react";
import { api, errorMessage } from "@/lib/api";

export function InviteBox({ publicId }: { publicId: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function create() {
    try {
      const inv = await api<{ token: string }>(`/groups/${publicId}/invites`, { method: "POST", body: {} });
      setUrl(`${location.origin}/join/${inv.token}`);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div>
      <p className="row">
        <button type="button" className="secondary" onClick={create}>招待URLを作成</button>
      </p>
      {url && (
        <p>
          <label htmlFor="invite">招待URL(知っている人は誰でも参加できます)</label>
          <input id="invite" readOnly value={url} onFocus={(e) => e.currentTarget.select()} />
        </p>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
