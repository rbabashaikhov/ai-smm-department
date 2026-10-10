// The fields of one revision. Used for the first revision (Create
// Content) and for every later one (New Revision). There is no "edit"
// form: submitting always creates a new, immutable revision.

import { useState, type FormEvent, type ReactNode } from "react";

import {
  CONTENT_FORMATS,
  type ContentFormat,
  type JsonObject,
  type Revision,
  type RevisionInput,
} from "../../types/api";

export interface RevisionDraft {
  title: string;
  body: string;
  format: ContentFormat;
  images: string;
  items: string;
  metadata: string;
  source_ref: string;
  editor_score: string;
  editor_notes: string;
}

export const emptyDraft = (): RevisionDraft => ({
  title: "",
  body: "",
  format: "text",
  images: "[]",
  items: "[]",
  metadata: "{}",
  source_ref: "",
  editor_score: "",
  editor_notes: "",
});

export const draftFromRevision = (revision: Revision): RevisionDraft => ({
  title: revision.title ?? "",
  body: revision.body,
  format: revision.format,
  images: JSON.stringify(revision.images, null, 2),
  items: JSON.stringify(revision.items, null, 2),
  metadata: JSON.stringify(revision.metadata, null, 2),
  source_ref: revision.source_ref ?? "",
  editor_score: revision.editor_score === null ? "" : String(revision.editor_score),
  editor_notes: revision.editor_notes,
});

function parseJson<T>(text: string, kind: "array" | "object", field: string): T {
  let value: unknown;

  try {
    value = JSON.parse(text.trim() || (kind === "array" ? "[]" : "{}"));
  } catch {
    throw new Error(`${field}: not valid JSON`);
  }

  if (kind === "array" && !Array.isArray(value)) throw new Error(`${field}: must be a JSON array`);
  if (kind === "object" && (Array.isArray(value) || typeof value !== "object" || value === null)) {
    throw new Error(`${field}: must be a JSON object`);
  }

  return value as T;
}

/** Parse the form into the API body. Throws an Error naming the field. */
export function draftToInput(draft: RevisionDraft): RevisionInput {
  const score = draft.editor_score.trim();

  return {
    title: draft.title.trim() || null,
    body: draft.body,
    format: draft.format,
    images: parseJson<JsonObject[]>(draft.images, "array", "images"),
    items: parseJson<JsonObject[]>(draft.items, "array", "items"),
    metadata: parseJson<JsonObject>(draft.metadata, "object", "metadata"),
    // A person typing in the panel is the author: provenance "human".
    source: "human",
    source_ref: draft.source_ref.trim() || null,
    editor_score: score === "" ? null : Number(score),
    editor_notes: draft.editor_notes,
  };
}

export function RevisionForm({
  initial,
  submitLabel,
  pending,
  onSubmit,
  onCancel,
  header,
  footer,
}: {
  initial: RevisionDraft;
  submitLabel: string;
  pending: boolean;
  onSubmit: (input: RevisionInput, draft: RevisionDraft) => void;
  onCancel?: () => void;
  header?: ReactNode;
  footer?: ReactNode;
}) {
  const [draft, setDraft] = useState(initial);
  const [parseError, setParseError] = useState<string | null>(null);

  const set = <K extends keyof RevisionDraft>(key: K, value: RevisionDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setParseError(null);

    try {
      onSubmit(draftToInput(draft), draft);
    } catch (error) {
      setParseError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <form className="revision-form" onSubmit={submit} aria-label="Revision form">
      {header}
      <div className="form-row">
        <label className="grow">
          Revision title
          <input value={draft.title} onChange={(e) => set("title", e.target.value)} maxLength={500} />
        </label>
        <label>
          Format
          <select
            value={draft.format}
            onChange={(e) => set("format", e.target.value as ContentFormat)}
          >
            {CONTENT_FORMATS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        Body
        <textarea
          rows={10}
          value={draft.body}
          onChange={(e) => set("body", e.target.value)}
          required
          maxLength={20000}
        />
      </label>
      <div className="muted small">{draft.body.length} / 20000</div>
      <div className="form-row">
        <label className="grow">
          Images (JSON array)
          <textarea
            rows={3}
            className="mono"
            value={draft.images}
            onChange={(e) => set("images", e.target.value)}
          />
        </label>
        <label className="grow">
          Items (JSON array)
          <textarea
            rows={3}
            className="mono"
            value={draft.items}
            onChange={(e) => set("items", e.target.value)}
          />
        </label>
      </div>
      <details>
        <summary>Source, editor and metadata</summary>
        <div className="form-row">
          <label className="grow">
            source_ref
            <input
              value={draft.source_ref}
              onChange={(e) => set("source_ref", e.target.value)}
              maxLength={500}
            />
          </label>
          <label>
            Editor score (0–10)
            <input
              type="number"
              min={0}
              max={10}
              step={0.1}
              value={draft.editor_score}
              onChange={(e) => set("editor_score", e.target.value)}
            />
          </label>
        </div>
        <label>
          Editor notes
          <textarea
            rows={2}
            value={draft.editor_notes}
            onChange={(e) => set("editor_notes", e.target.value)}
            maxLength={5000}
          />
        </label>
        <label>
          Metadata (JSON object)
          <textarea
            rows={3}
            className="mono"
            value={draft.metadata}
            onChange={(e) => set("metadata", e.target.value)}
          />
        </label>
      </details>
      {parseError ? (
        <div className="banner banner-error" role="alert">
          {parseError}
        </div>
      ) : null}
      {footer}
      <div className="form-actions">
        {onCancel ? (
          <button type="button" onClick={onCancel} disabled={pending}>
            Отмена
          </button>
        ) : null}
        <button type="submit" className="btn-primary" disabled={pending}>
          {pending ? "Сохранение…" : submitLabel}
        </button>
      </div>
    </form>
  );
}
