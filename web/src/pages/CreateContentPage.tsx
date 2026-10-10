import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Card, PageHeader } from "../components/ui";
import { RevisionForm, emptyDraft } from "../features/content/RevisionForm";
import { useProjectPermissions, useReportActiveProject } from "../features/projects/hooks";
import type { ContentItemCreate } from "../types/api";

/** One request creates the item and its first revision, per the backend. */
export function CreateContentPage() {
  const { projectId = "" } = useParams();
  const api = useApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const can = useProjectPermissions(projectId);
  useReportActiveProject(projectId);

  const [title, setTitle] = useState("");
  const [contentType, setContentType] = useState("post");

  const create = useMutation({
    mutationFn: (body: ContentItemCreate) => api.content.create(projectId, body),
    onSuccess: async (item) => {
      queryClient.setQueryData(qk.contentItem(item.id), item);
      await queryClient.invalidateQueries({ queryKey: qk.projectContent(projectId) });
      navigate(`/content/${item.id}`);
    },
  });

  if (!can.canEditContent) {
    return (
      <>
        <PageHeader title="Create Content" />
        <div className="banner banner-warn">
          {can.readOnly
            ? "Control Center работает только на чтение: создание контента отключено на сервере."
            : "Создавать контент может роль editor и выше."}
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Create Content"
        subtitle="Новый материал создаётся вместе с первой ревизией (status: draft)"
        actions={<Link to={`/projects/${encodeURIComponent(projectId)}/content`}>← Content</Link>}
      />
      <Card>
        <RevisionForm
          initial={emptyDraft()}
          submitLabel="Create Content"
          pending={create.isPending}
          onSubmit={(revision) => {
            create.mutate({ title: title.trim(), content_type: contentType.trim(), revision });
          }}
          header={
            <div className="form-row">
              <label className="grow">
                Title
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  required
                  maxLength={500}
                />
              </label>
              <label>
                Content type
                <input
                  value={contentType}
                  onChange={(e) => setContentType(e.target.value)}
                  required
                  maxLength={40}
                />
              </label>
            </div>
          }
          footer={create.isError ? <ErrorBanner error={create.error} /> : null}
        />
      </Card>
    </>
  );
}
