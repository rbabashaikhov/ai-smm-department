// Audit details are written by the backend without secrets. This is a
// second line: any key that looks like a credential is masked before it
// reaches the screen, whatever a future writer puts there.

const SECRET_KEY = /(password|passwd|secret|token|cookie|authorization|csrf|api[_-]?key|private[_-]?key)/i;

export const REDACTED = "[redacted]";

export function redactSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactSecrets);

  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, inner]) => [
        key,
        SECRET_KEY.test(key) ? REDACTED : redactSecrets(inner),
      ]),
    );
  }

  return value;
}
