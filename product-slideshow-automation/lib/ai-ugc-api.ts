export const AI_UGC_PUBLIC_BASE_URL =
  process.env.NEXT_PUBLIC_AI_UGC_BASE_URL || "http://localhost:3000";

export class AiUgcApiError extends Error {
  status: number;
  payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "AiUgcApiError";
    this.status = status;
    this.payload = payload;
  }
}

function getErrorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "error" in payload) {
    const error = (payload as { error?: unknown }).error;
    if (typeof error === "string") return error;
    if (error && typeof error === "object" && "message" in error) {
      const message = (error as { message?: unknown }).message;
      if (typeof message === "string") return message;
    }
  }

  return fallback;
}

export async function aiUgcFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const response = await fetch(`/api/ai-ugc${normalizedPath}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init.headers || {}),
    },
  });

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    throw new AiUgcApiError(
      getErrorMessage(payload, `AI UGC request failed with ${response.status}`),
      response.status,
      payload,
    );
  }

  return payload as T;
}

export function aiUgcDownloadUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `/api/ai-ugc${normalizedPath}`;
}

export function aiUgcAppUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${AI_UGC_PUBLIC_BASE_URL.replace(/\/$/, "")}${normalizedPath}`;
}
