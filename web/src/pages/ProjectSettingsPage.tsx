import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useParams } from "react-router";

import { useApi } from "../api/context";
import { isVersionConflict } from "../api/errors";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Card, KeyValues, Loading, PageHeader } from "../components/ui";
import { useAuth } from "../features/auth/AuthProvider";
import {
  useProject,
  useProjectAccounts,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { TopicsInput } from "../features/projects/TopicsInput";
import type { JsonObject, Project, ProjectSettings } from "../types/api";

const asTopics = (config: JsonObject): string[] =>
  Array.isArray(config.topics) ? config.topics.filter((t): t is string => typeof t === "string") : [];

const asTone = (config: JsonObject): string =>
  typeof config.tone_of_voice === "string" ? config.tone_of_voice : "";

function ConflictPanel({ onReload }: { onReload: () => void }) {
  return (
    <div className="banner banner-warn" role="alert" data-testid="version-conflict">
      Настройки изменены в другом окне. Ваши правки не сохранены.{" "}
      <button type="button" onClick={onReload}>
        Перечитать
      </button>
    </div>
  );
}

/** Name, description, timezone, language: the project record, owner only. */
function RecordCard({ project, editable }: { project: Project; editable: boolean }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { refreshMe } = useAuth();
  const [form, setForm] = useState({
    display_name: project.display_name,
    description: project.description,
    default_timezone: project.default_timezone,
    default_language: project.default_language,
  });

  const save = useMutation({
    mutationFn: () =>
      api.projects.update(project.id, { expected_version: project.version, ...form }),
    onSuccess: async (updated) => {
      queryClient.setQueryData(qk.project(project.id), updated);
      await queryClient.invalidateQueries({ queryKey: qk.projects(), exact: true });
      await refreshMe();
    },
  });

  const reload = async () => {
    save.reset();
    const fresh = await queryClient.fetchQuery({
      queryKey: qk.project(project.id),
      queryFn: () => api.projects.get(project.id),
    });
    setForm({
      display_name: fresh.display_name,
      description: fresh.description,
      default_timezone: fresh.default_timezone,
      default_language: fresh.default_language,
    });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate();
  };

  return (
    <Card title={`Основное · v${project.version}`}>
      <form className="revision-form" onSubmit={submit} aria-label="Project record form">
        <KeyValues rows={[["Идентификатор", <code key="id">{project.id}</code>]]} />
        <label>
          Название
          <input
            value={form.display_name}
            disabled={!editable}
            maxLength={120}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          />
        </label>
        <label>
          Описание
          <textarea
            rows={3}
            value={form.description}
            disabled={!editable}
            maxLength={2000}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </label>
        <div className="form-row">
          <label>
            Часовой пояс
            <input
              value={form.default_timezone}
              disabled={!editable}
              maxLength={64}
              onChange={(e) => setForm({ ...form, default_timezone: e.target.value })}
            />
          </label>
          <label>
            Язык
            <input
              value={form.default_language}
              disabled={!editable}
              maxLength={16}
              onChange={(e) => setForm({ ...form, default_language: e.target.value })}
            />
          </label>
        </div>
        {isVersionConflict(save.error) ? (
          <ConflictPanel onReload={reload} />
        ) : save.isError ? (
          <ErrorBanner error={save.error} />
        ) : null}
        {editable ? (
          <div className="form-actions">
            <button type="submit" disabled={save.isPending}>
              Сохранить
            </button>
          </div>
        ) : null}
      </form>
    </Card>
  );
}

const profileFrom = (settings: ProjectSettings) => ({
  main_theme: typeof settings.content_config.main_theme === "string" ? settings.content_config.main_theme : "",
  audience: typeof settings.content_config.audience === "string" ? settings.content_config.audience : "",
  guidelines: typeof settings.brand_config.guidelines === "string" ? settings.brand_config.guidelines : "",
  excluded_topics: asTopics({ topics: settings.content_config.excluded_topics }),
  goals: asTopics({ topics: settings.content_config.goals }),
  forbidden_terms: asTopics({ topics: settings.brand_config.forbidden_terms }),
});

/** Stored profile values must never be silently replaced by display defaults. */
function profileIssues(settings: ProjectSettings): string[] {
  const issues: string[] = [];
  for (const [section, fields] of [
    ["content_config", { main_theme: 300, audience: 1000 }],
    ["brand_config", { tone_of_voice: 4000, guidelines: 4000 }],
  ] as const) {
    for (const [key, limit] of Object.entries(fields)) {
      const value = settings[section][key];
      if (key in settings[section] &&
        (typeof value !== "string" || [...value.trim()].length > limit)) issues.push(key);
    }
  }
  for (const [section, key, count, length] of [
    ["content_config", "topics", 10, 80],
    ["content_config", "excluded_topics", 10, 80],
    ["content_config", "goals", 5, 200],
    ["brand_config", "forbidden_terms", 20, 80],
  ] as const) {
    if (!(key in settings[section])) continue;
    const value = settings[section][key];
    if (!Array.isArray(value) || value.length > count ||
      value.some((item) => typeof item !== "string" || !item.trim() ||
        [...item.trim().replace(/\s+/g, " ")].length > length)) {
      issues.push(key);
      continue;
    }
    const normalized = (value as string[]).map((item) => item.trim().replace(/\s+/g, " ").toLowerCase());
    if (new Set(normalized).size !== normalized.length) issues.push(key);
  }
  return issues;
}

/** Editorial profile: typed keys of the settings JSON, owner only.
 *  The whole section is sent back, so keys this page does not edit stay. */
function ProfileCard({
  projectId,
  settings,
  editable,
}: {
  projectId: string;
  settings: ProjectSettings;
  editable: boolean;
}) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [topics, setTopics] = useState(asTopics(settings.content_config));
  const [tone, setTone] = useState(asTone(settings.brand_config));
  const [profile, setProfile] = useState(profileFrom(settings));
  const [issues, setIssues] = useState(() => profileIssues(settings));
  const clearInvalid = (key: string) => {
    if (key === "topics") setTopics([]);
    else if (key === "tone_of_voice") setTone("");
    else setProfile((current) => ({
      ...current, [key]: ["excluded_topics", "goals", "forbidden_terms"].includes(key) ? [] : "",
    }));
    setIssues((current) => current.filter((field) => field !== key));
  };

  const save = useMutation({
    mutationFn: () =>
      api.projects.updateSettings(projectId, {
        expected_version: settings.version,
        content_config: {
          ...settings.content_config, topics,
          main_theme: profile.main_theme.trim(), audience: profile.audience.trim(),
          excluded_topics: profile.excluded_topics, goals: profile.goals,
        },
        brand_config: {
          ...settings.brand_config, tone_of_voice: tone.trim(),
          guidelines: profile.guidelines.trim(), forbidden_terms: profile.forbidden_terms,
        },
      }),
    onSuccess: (updated) => queryClient.setQueryData(qk.settings(projectId), updated),
  });

  const reload = async () => {
    save.reset();
    const fresh = await queryClient.fetchQuery({
      queryKey: qk.settings(projectId),
      queryFn: () => api.projects.settings(projectId),
      staleTime: 0,
    });
    setTopics(asTopics(fresh.content_config));
    setTone(asTone(fresh.brand_config));
    setProfile(profileFrom(fresh));
    setIssues(profileIssues(fresh));
  };

  return (
    <Card title={`Редакционный профиль · v${settings.version}`}>
      <form
        className="revision-form"
        aria-label="Editorial profile form"
        onSubmit={(event) => {
          event.preventDefault();
          if (issues.length === 0) save.mutate();
        }}
      >
        {issues.map((key) => (
          <div key={key} role="alert" className="banner banner-warn">
            Некорректное сохранённое значение поля {key}. Сохранение профиля заблокировано.
            <pre>{JSON.stringify(key in settings.content_config ? settings.content_config[key] : settings.brand_config[key])}</pre>
            {editable && (
              <button type="button" onClick={() => clearInvalid(key)}>
                Очистить некорректное поле {key}
              </button>
            )}
          </div>
        ))}
        {([
          ["main_theme", "Основная тема", 300],
          ["audience", "Аудитория", 1000],
          ["guidelines", "Редакционные правила", 4000],
        ] as const).map(([key, label, limit]) => (
          <label key={key}>
            {label}
            <textarea
              rows={3} value={profile[key]} maxLength={limit} disabled={!editable}
              onChange={(e) => setProfile({ ...profile, [key]: e.target.value })}
            />
          </label>
        ))}
        {([
          ["excluded_topics", "Исключённые темы", "Новая исключённая тема", 10, 80, "Удалить исключённую тему"],
          ["goals", "Цели", "Новая цель", 5, 200, "Удалить цель"],
          ["forbidden_terms", "Запрещённые слова", "Новое запрещённое слово", 20, 80, "Удалить запрещённое слово"],
        ] as const).map(([key, label, inputLabel, maxItems, maxLength, removeLabel]) => (
          <TopicsInput
            key={key} label={label} inputLabel={inputLabel}
            maxItems={maxItems} maxLength={maxLength} removeLabel={removeLabel}
            value={profile[key]} disabled={!editable}
            onChange={(value) => setProfile({ ...profile, [key]: value })}
          />
        ))}
        <TopicsInput value={topics} onChange={setTopics} disabled={!editable} />
        <label>
          Tone of Voice
          <textarea
            rows={5}
            value={tone}
            disabled={!editable}
            maxLength={4000}
            onChange={(e) => setTone(e.target.value)}
          />
        </label>
        <div className="muted small">
          Используется при генерации контента в следующих этапах; сейчас хранится и редактируется.
        </div>
        {isVersionConflict(save.error) ? (
          <ConflictPanel onReload={reload} />
        ) : save.isError ? (
          <ErrorBanner error={save.error} />
        ) : null}
        {editable ? (
          <div className="form-actions">
            <button type="submit" disabled={save.isPending || issues.length > 0}>
              Сохранить
            </button>
          </div>
        ) : null}
      </form>
    </Card>
  );
}

export function ProjectSettingsPage() {
  const { projectId = "" } = useParams();
  const api = useApi();
  useReportActiveProject(projectId);
  const can = useProjectPermissions(projectId);
  const project = useProject(projectId);
  const accounts = useProjectAccounts(projectId);
  const settings = useQuery({
    queryKey: qk.settings(projectId),
    queryFn: () => api.projects.settings(projectId),
  });

  if (project.isPending || settings.isPending) return <Loading />;
  if (project.isError) return <ErrorBanner error={project.error} onRetry={() => project.refetch()} />;
  if (settings.isError) return <ErrorBanner error={settings.error} onRetry={() => settings.refetch()} />;

  const ready = (accounts.data ?? []).filter((a) => a.publishing_ready).length;

  return (
    <>
      <PageHeader
        title="Настройки"
        subtitle={can.canEditSettings ? "Изменения сохраняются по карточкам" : "Только просмотр"}
      />
      <RecordCard key={`record-${project.data.version}`} project={project.data} editable={can.canEditSettings} />
      <ProfileCard
        key={`profile-${settings.data.version}`}
        projectId={projectId}
        settings={settings.data}
        editable={can.canEditSettings}
      />
      <Card title="Публикация (только чтение)">
        <KeyValues
          rows={[
            ["Платформа", project.data.default_platform],
            ["Аккаунты, готовые к публикации", accounts.data ? String(ready) : "…"],
            [
              "Аккаунт развёртывания (legacy)",
              project.data.legacy_publishing ? "разрешён оператором" : "не используется",
            ],
          ]}
        />
      </Card>
      <Card title="База знаний (только чтение)">
        <p>
          Документы из <code>knowledge/projects/{project.data.id}/</code> доступны для
          просмотра в разделе «База знаний», если каталог подключён к Control Center.
          Редактирование файлов пока недоступно.
        </p>
      </Card>
    </>
  );
}
