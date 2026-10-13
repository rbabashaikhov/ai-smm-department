import { useState } from "react";

export const MAX_TOPICS = 10;
export const MAX_TOPIC_LENGTH = 80;

/** A small list editor: add with Enter or the button, remove with ×. */
export function TopicsInput({
  value,
  onChange,
  disabled = false,
}: {
  value: string[];
  onChange: (topics: string[]) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const topic = draft.split(/\s+/).filter(Boolean).join(" ");

    if (!topic || topic.length > MAX_TOPIC_LENGTH || value.length >= MAX_TOPICS) return;
    if (value.some((t) => t.toLowerCase() === topic.toLowerCase())) return;

    onChange([...value, topic]);
    setDraft("");
  };

  return (
    <fieldset className="topics" disabled={disabled}>
      <legend>Темы</legend>
      <ul className="chips" aria-label="Темы проекта">
        {value.map((topic) => (
          <li key={topic} className="chip">
            {topic}
            <button
              type="button"
              aria-label={`Удалить тему ${topic}`}
              onClick={() => onChange(value.filter((t) => t !== topic))}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {value.length < MAX_TOPICS ? (
        <div className="form-row">
          <input
            aria-label="Новая тема"
            value={draft}
            maxLength={MAX_TOPIC_LENGTH}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
          />
          <button type="button" onClick={add}>
            + Тема
          </button>
        </div>
      ) : (
        <div className="muted small">Не больше {MAX_TOPICS} тем.</div>
      )}
    </fieldset>
  );
}
