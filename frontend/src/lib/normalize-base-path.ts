const UNSAFE_BASE_PATH_PATTERN = /[\s;\\?#]/;

function hasControlCharacter(value: string): boolean {
  return Array.from(value).some((char) => {
    const code = char.charCodeAt(0);
    return code < 32 || code === 127;
  });
}

export function normalizeBasePath(value?: string | null): string {
  const raw = value ?? "";
  if (!raw) return "";
  if (raw !== raw.trim()) {
    throw new Error("Base path cannot contain leading or trailing whitespace");
  }
  if (/^https?:\/\//i.test(raw) || (raw.startsWith("//") && /[^/]/.test(raw))) {
    throw new Error("Base path must be a path, not a URL");
  }
  if (UNSAFE_BASE_PATH_PATTERN.test(raw) || hasControlCharacter(raw)) {
    throw new Error("Base path contains unsafe characters");
  }
  if (/\/{2,}/.test(raw.replace(/^\/+|\/+$/g, ""))) {
    throw new Error("Base path cannot contain duplicate slashes");
  }

  const stripped = raw.replace(/^\/+|\/+$/g, "");
  if (!stripped) return "";
  if (stripped.split("/").some((part) => part === "." || part === "..")) {
    throw new Error("Base path cannot contain relative path segments");
  }
  return `/${stripped}`;
}
