import type { Revision } from "../../types/api";

/** The AI Editor's own assessment, as the generation worker stores it in
 *  `revision.metadata.editor_verdict`. Every field is optional: nothing is
 *  invented when the stored JSON lacks it or has the wrong type. */
export interface ParsedEditorVerdict {
  approved?: boolean;
  score?: number;
  narrativeScore?: number;
  issues: string[];
  revisionCount?: number;
  feedback?: string;
}

export type EditorVerdictState =
  | { kind: "none" }
  | { kind: "malformed" }
  | { kind: "verdict"; verdict: ParsedEditorVerdict };

const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

/** Sources that are an AI agent's own output. A human revision may inherit
 *  `editor_verdict` through copied metadata, but the AI never reviewed it. */
const AI_SOURCES: ReadonlySet<string> = new Set(["strategist", "copywriter", "editor"]);

/** Safely project a revision's unknown metadata into a verdict. A revision
 *  without `editor_verdict` (a manual one) has no AI verdict at all. */
export function parseEditorVerdict(revision: Revision): EditorVerdictState {
  if (!AI_SOURCES.has(revision.source)) return { kind: "none" };

  const metadata: unknown = revision.metadata;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) {
    return { kind: "none" };
  }

  const raw = (metadata as Record<string, unknown>).editor_verdict;
  if (raw === undefined || raw === null) return { kind: "none" };
  if (typeof raw !== "object" || Array.isArray(raw)) return { kind: "malformed" };

  const fields = raw as Record<string, unknown>;
  const verdict: ParsedEditorVerdict = {
    issues: Array.isArray(fields.issues)
      ? fields.issues.filter((i): i is string => typeof i === "string" && i.trim() !== "")
      : [],
  };

  if (typeof fields.approved === "boolean") verdict.approved = fields.approved;
  if (finite(fields.score)) verdict.score = fields.score;
  else if (finite(revision.editor_score)) verdict.score = revision.editor_score;
  if (finite(fields.narrative_score)) verdict.narrativeScore = fields.narrative_score;
  if (Number.isInteger(fields.revision_count) && (fields.revision_count as number) >= 0) {
    verdict.revisionCount = fields.revision_count as number;
  }
  if (typeof revision.editor_notes === "string" && revision.editor_notes.trim() !== "") {
    verdict.feedback = revision.editor_notes;
  }

  return { kind: "verdict", verdict };
}

/**
 * AI Editor verdict of one revision. Read-only and derived from the revision
 * it is given, so it can never show another revision's verdict. It is an AI
 * assessment only: it neither approves nor submits anything.
 */
export function EditorVerdict({ revision }: { revision: Revision }) {
  const state = parseEditorVerdict(revision);

  if (state.kind === "none") return null;

  if (state.kind === "malformed") {
    return (
      <section className="banner banner-warn" data-testid="editor-verdict" aria-label="AI Editor verdict">
        <strong>AI Editor:</strong> вердикт сохранён в неизвестном формате и не может быть показан.
        <HumanApprovalNote />
      </section>
    );
  }

  const { verdict } = state;
  const result =
    verdict.approved === true
      ? { label: "Passed — AI Editor не нашёл блокирующих замечаний", tone: "banner-ok" }
      : verdict.approved === false
        ? { label: "Needs improvement — AI Editor рекомендует доработку", tone: "banner-warn" }
        : { label: "Результат AI Editor не указан", tone: "banner-info" };

  return (
    <section
      className={`banner ${result.tone}`}
      data-testid="editor-verdict"
      aria-label="AI Editor verdict"
    >
      <div>
        <strong>AI Editor:</strong> <span data-testid="editor-verdict-result">{result.label}</span>
      </div>
      <ul className="small">
        {verdict.score !== undefined ? <li>Editor score: {verdict.score}</li> : null}
        {verdict.narrativeScore !== undefined ? (
          <li>Narrative score: {verdict.narrativeScore}</li>
        ) : null}
        {verdict.revisionCount !== undefined ? (
          <li>Revision count (доработок AI Editor): {verdict.revisionCount}</li>
        ) : null}
      </ul>
      {verdict.issues.length > 0 ? (
        <div className="small">
          <strong>Issues:</strong>
          <ul>
            {verdict.issues.map((issue, index) => (
              <li key={index}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {verdict.feedback ? (
        <div className="small">
          <strong>Feedback:</strong> <span style={{ whiteSpace: "pre-wrap" }}>{verdict.feedback}</span>
        </div>
      ) : null}
      <HumanApprovalNote />
    </section>
  );
}

function HumanApprovalNote() {
  return (
    <p className="muted small">
      Это оценка AI, а не одобрение человеком. Одобрение даёт только человек в Approval.
    </p>
  );
}
