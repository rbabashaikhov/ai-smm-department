import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";

import { useApi } from "../api/context";
import { ApiError, ErrorCode } from "../api/errors";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Card, PageHeader } from "../components/ui";
import { useAuth, useMe, useMutationsEnabled } from "../features/auth/AuthProvider";
import { canCreateProject } from "../features/auth/rbac";
import { TopicsInput } from "../features/projects/TopicsInput";
import type { ProjectCreate } from "../types/api";

const TRANSLIT: Record<string, string> = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i",
  й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t",
  у: "u", ф: "f", х: "h", ц: "ts", ч: "ch", ш: "sh", щ: "sch", ъ: "", ы: "y", ь: "",
  э: "e", ю: "yu", я: "ya",
};

/** A suggestion only: the server decides what is a valid id. */
export function suggestProjectId(name: string): string {
  return Array.from(name.toLowerCase())
    .map((ch) => TRANSLIT[ch] ?? ch)
    .join("")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/g, "");
}

export const PROJECT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$/;

const TIMEZONES = ["Europe/Moscow", "Europe/Kaliningrad", "Asia/Yekaterinburg", "Asia/Tbilisi", "Europe/Berlin", "UTC"];

function fieldOf(error: unknown): "id" | "display_name" | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code === ErrorCode.PROJECT_ALREADY_EXISTS) return "id";
  if (error.code === ErrorCode.PROJECT_NAME_TAKEN) return "display_name";

  return null;
}

export function CreateProjectPage() {
  const api = useApi();
  const me = useMe();
  const { refreshMe } = useAuth();
  const mutationsEnabled = useMutationsEnabled();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [timezone, setTimezone] = useState("Europe/Moscow");
  const [language, setLanguage] = useState("ru");
  const [topics, setTopics] = useState<string[]>([]);
  const [tone, setTone] = useState("");

  const effectiveId = idTouched ? id : suggestProjectId(name);

  const create = useMutation({
    mutationFn: (body: ProjectCreate) => api.projects.create(body),
    onSuccess: async (project) => {
      queryClient.setQueryData(qk.project(project.id), project);
      await queryClient.invalidateQueries({ queryKey: qk.projects() });
      await refreshMe();
      navigate(`/projects/${encodeURIComponent(project.id)}`);
    },
  });

  if (!canCreateProject(me.projects.map((p) => p.role), mutationsEnabled)) {
    return (
      <>
        <PageHeader title="Новый проект" />
        <div className="banner banner-warn">
          {mutationsEnabled
            ? "Создать проект может владелец (owner) хотя бы одного проекта."
            : "Control Center работает только на чтение: создание проектов отключено на сервере."}
        </div>
      </>
    );
  }

  const idValid = PROJECT_ID_PATTERN.test(effectiveId) && !effectiveId.includes("--");
  const conflictField = fieldOf(create.error);

  const submit = (event: FormEvent) => {
    event.preventDefault();

    if (!idValid || create.isPending) return;

    create.mutate({
      id: effectiveId,
      display_name: name.trim(),
      description: description.trim(),
      default_timezone: timezone,
      default_language: language,
      topics,
      tone_of_voice: tone.trim() || null,
    });
  };

  return (
    <>
      <PageHeader
        title="Новый проект"
        subtitle="Вы станете владельцем проекта"
        actions={<Link to="/projects">← Проекты</Link>}
      />
      <Card>
        <form className="revision-form" onSubmit={submit} aria-label="Create project form">
          <label>
            Название *
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={120}
              aria-invalid={conflictField === "display_name"}
            />
          </label>
          {conflictField === "display_name" ? (
            <div className="field-error" role="alert">
              У вас уже есть проект с таким названием.
            </div>
          ) : null}
          <label>
            Идентификатор *
            <input
              value={effectiveId}
              onChange={(e) => {
                setIdTouched(true);
                setId(e.target.value.toLowerCase());
              }}
              required
              maxLength={64}
              aria-invalid={!idValid || conflictField === "id"}
              aria-describedby="project-id-hint"
            />
          </label>
          <div id="project-id-hint" className="muted small">
            a–z, 0–9 и дефис, 3–64 символа. Используется в адресах и в папке базы знаний
            knowledge/projects/{effectiveId || "…"}/. Изменить после создания нельзя.
          </div>
          {conflictField === "id" ? (
            <div className="field-error" role="alert">
              Проект с таким идентификатором уже существует.
            </div>
          ) : null}
          <label>
            Описание
            <textarea rows={3} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} />
          </label>
          <TopicsInput value={topics} onChange={setTopics} />
          <label>
            Tone of Voice
            <textarea rows={4} value={tone} onChange={(e) => setTone(e.target.value)} maxLength={4000} />
          </label>
          <div className="form-row">
            <label>
              Часовой пояс
              <select value={timezone} onChange={(e) => setTimezone(e.target.value)}>
                {TIMEZONES.map((tz) => (
                  <option key={tz} value={tz}>
                    {tz}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Язык
              <select value={language} onChange={(e) => setLanguage(e.target.value)}>
                <option value="ru">ru</option>
                <option value="en">en</option>
              </select>
            </label>
          </div>
          <div className="banner banner-info" role="note">
            Новый проект не подключён к публикации. Контент можно создавать и согласовывать;
            публиковать можно только через аккаунт, подключённый владельцем в разделе «Аккаунты».
          </div>
          {create.isError && !conflictField ? <ErrorBanner error={create.error} /> : null}
          <div className="form-actions">
            <Link to="/projects">Отмена</Link>
            <button type="submit" disabled={!idValid || !name.trim() || create.isPending}>
              {create.isPending ? "Создание…" : "Создать"}
            </button>
          </div>
        </form>
      </Card>
    </>
  );
}
