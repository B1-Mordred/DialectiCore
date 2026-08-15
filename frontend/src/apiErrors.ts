export class ApiRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

export function apiRequestErrorMessage(
  status: number,
  body: string,
  statusText = "",
): string {
  const detail = apiErrorDetail(body);
  const suffix = detail ? `: ${detail}` : statusText.trim() ? `: ${statusText.trim()}` : "";
  return `Request failed ${status}${suffix}`;
}

export function apiErrorDetail(body: string): string {
  const trimmed = body.trim();
  if (!trimmed) {
    return "";
  }
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    const detail = objectValue(parsed, "detail");
    return compactApiDetail(detail) || compactApiDetail(parsed);
  } catch {
    return compactText(trimmed);
  }
}

export function actionErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function compactApiDetail(value: unknown): string {
  if (typeof value === "string") {
    return compactText(value);
  }
  if (Array.isArray(value)) {
    return compactText(
      value
        .map((entry) => {
          if (typeof entry === "string") {
            return entry;
          }
          if (entry && typeof entry === "object") {
            const message = objectValue(entry, "msg");
            const location = objectValue(entry, "loc");
            const locationText = Array.isArray(location) ? location.join(".") : "";
            if (typeof message === "string") {
              return locationText ? `${locationText}: ${message}` : message;
            }
          }
          return "";
        })
        .filter(Boolean)
        .join("; ")
    );
  }
  if (value && typeof value === "object") {
    const message = objectValue(value, "message") ?? objectValue(value, "error");
    if (typeof message === "string") {
      return compactText(message);
    }
  }
  return "";
}

function objectValue(value: unknown, key: string): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  return (value as Record<string, unknown>)[key];
}

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim().slice(0, 320);
}
