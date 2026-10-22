import { useState } from "react";

export const MAX_TOPICS = 10;
export const MAX_TOPIC_LENGTH = 80;

/** A small list editor: add with Enter or the button, remove with ×. */
export function TopicsInput({
  value,
  onChange,
  disabled = false,
  label = "Темы",
  inputLabel = "Новая тема",
  removeLabel = "Удалить тему",
  maxItems = MAX_TOPICS,
  maxLength = MAX_TOPIC_LENGTH,
}: {
  value: string[];
  onChange: (topics: string[]) => void;
  disabled?: boolean;
  label?: string;
  inputLabel?: string;
  removeLabel?: string;
  maxItems?: number;
  maxLength?: number;
}) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const topic = draft.split(/\s+/).filter(Boolean).join(" ");

    if (!topic || topic.length > maxLength || value.length >= maxItems) return;
    if (value.some((t) => t.toLowerCase() === topic.toLowerCase())) return;

    onChange([...value, topic]);
    setDraft("");
  };

  return (
    <fieldset className="topics" disabled={disabled}>
      <legend>{label}</legend>
      <ul className="chips" aria-label={label === "Темы" ? "Темы проекта" : label}>
        {value.map((topic) => (
          <li key={topic} className="chip">
            {topic}
            <button
              type="button"
              aria-label={`${removeLabel} ${topic}`}
              onClick={() => onChange(value.filter((t) => t !== topic))}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {value.length < maxItems ? (
        <div className="form-row">
          <input
            aria-label={inputLabel}
            value={draft}
            maxLength={maxLength}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
          />
          <button type="button" onClick={add}>
            {label === "Темы" ? "+ Тема" : "+ Добавить"}
          </button>
        </div>
      ) : (
        <div className="muted small">Не больше {maxItems} элементов.</div>
      )}
    </fieldset>
  );
}
