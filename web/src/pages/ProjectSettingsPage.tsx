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

/** Topics and tone of voice: typed keys of the settings JSON, owner only.
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

  const save = useMutation({
    mutationFn: () =>
      api.projects.updateSettings(projectId, {
        expected_version: settings.version,
        content_config: { ...settings.content_config, topics },
        brand_config: { ...settings.brand_config, tone_of_voice: tone },
      }),
    onSuccess: (updated) => queryClient.setQueryData(qk.settings(projectId), updated),
  });

  const reload = async () => {
    save.reset();
    const fresh = await queryClient.fetchQuery({
      queryKey: qk.settings(projectId),
      queryFn: () => api.projects.settings(projectId),
    });
    setTopics(asTopics(fresh.content_config));
    setTone(asTone(fresh.brand_config));
  };

  return (
    <Card title={`Редакционный профиль · v${settings.version}`}>
      <form
        className="revision-form"
        aria-label="Editorial profile form"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
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
            <button type="submit" disabled={save.isPending}>
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
          <code>knowledge/projects/{project.data.id}/</code> — ведётся в репозитории. Control
          Center не читает и не изменяет файлы базы знаний.
        </p>
      </Card>
    </>
  );
}
