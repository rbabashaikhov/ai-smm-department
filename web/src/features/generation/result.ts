import type {
  GenerationResult,
  GenerationTokenUsage,
  ParsedGenerationResult,
} from "../../types/api";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const finiteNonNegative = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;

function tokenUsage(value: unknown): GenerationTokenUsage | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;

  const raw = value as Record<string, unknown>;

  if (![raw.input, raw.output, raw.cached].every(finiteNonNegative)) return undefined;

  return {
    input: raw.input as number,
    output: raw.output as number,
    cached: raw.cached as number,
  };
}

/** Safely project the worker's intentionally open-ended result JSON for UI use. */
export function parseGenerationResult(result: GenerationResult): ParsedGenerationResult {
  const ids = result.content_item_ids;
  const contentItemIds =
    Array.isArray(ids) && ids.every((id) => typeof id === "string" && UUID.test(id))
      ? [...ids]
      : null;
  const parsed: ParsedGenerationResult = { contentItemIds };

  if (typeof result.series_title === "string") parsed.seriesTitle = result.series_title;
  if (Number.isInteger(result.revision_count) && finiteNonNegative(result.revision_count)) {
    parsed.revisionCount = result.revision_count;
  }

  const tokens = tokenUsage(result.tokens);
  if (tokens) parsed.tokens = tokens;
  if (finiteNonNegative(result.cost_usd)) parsed.costUsd = result.cost_usd;
  if (finiteNonNegative(result.seconds)) parsed.seconds = result.seconds;

  return parsed;
}
