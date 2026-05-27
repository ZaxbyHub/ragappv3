import { normalizeBasePath } from "./normalize-base-path";

export { normalizeBasePath };

export const APP_BASENAME = normalizeBasePath(
  import.meta.env.VITE_APP_BASENAME || import.meta.env.BASE_URL || "/"
);

export function appPath(path: string, basename = APP_BASENAME): string {
  const base = normalizeBasePath(basename);
  const suffix = path.startsWith("/") ? path : `/${path}`;
  if (!base) return suffix;
  if (suffix === "/") return `${base}/`;
  return `${base}${suffix}`;
}

export function logSubpathConfig(): void {
  const viteAppBasename = import.meta.env.VITE_APP_BASENAME ?? "(not set)";
  const viteApiUrl = import.meta.env.VITE_API_URL ?? "(not set)";
  const baseUrl = import.meta.env.BASE_URL ?? "(not set)";

  console.info(
    "[KnowledgeVault] Subpath config:\n" +
      `  VITE_APP_BASENAME = ${JSON.stringify(viteAppBasename)}\n` +
      `  VITE_API_URL      = ${JSON.stringify(viteApiUrl)}${!import.meta.env.VITE_API_URL ? "  (derived)" : ""}\n` +
      `  BASE_URL          = ${JSON.stringify(baseUrl)}\n` +
      `  APP_BASENAME      = ${JSON.stringify(APP_BASENAME)}`
  );

  if (import.meta.env.VITE_APP_BASENAME && !import.meta.env.VITE_API_URL) {
    console.info(
      "[KnowledgeVault] VITE_API_URL not set — derived from VITE_APP_BASENAME"
    );
  }
}
