/** Extract only an invite code, without navigating to the supplied URL. */
export function inviteToken(input: string): string | null {
  const value = input.trim();
  if (/^[A-Za-z0-9_-]{1,512}$/.test(value)) return value;
  try {
    const url = new URL(value, window.location.origin);
    if (url.pathname === "/join" && url.hash.startsWith("#token=")) {
      const token = new URLSearchParams(url.hash.slice(1)).get("token");
      return token && /^[A-Za-z0-9_-]{1,512}$/.test(token) ? token : null;
    }
    const match = url.pathname.match(/^\/join\/([A-Za-z0-9_-]{1,512})\/?$/);
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}
