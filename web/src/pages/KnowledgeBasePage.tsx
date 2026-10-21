import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ErrorBanner } from "../components/ErrorBanner";
import { Card, Empty, Loading, PageHeader } from "../components/ui";
import { useProject, useReportActiveProject } from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import type { KnowledgeDocument } from "../types/api";

const formatSize = (bytes: number) =>
  bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;

function DocumentView({ projectId, name }: { projectId: string; name: string }) {
  const api = useApi();
  const project = useProject(projectId);
  const document = useQuery({
    queryKey: qk.knowledgeDocument(projectId, name),
    queryFn: () => api.projects.knowledgeDocument(projectId, name),
  });

  if (document.isPending) return <Loading />;
  if (document.isError) {
    return <ErrorBanner error={document.error} onRetry={() => void document.refetch()} />;
  }

  return (
    <Card title={name}>
      <div className="muted small">
        {document.data.kind} · {formatSize(document.data.size_bytes)} · изменён{" "}
        {formatDateTime(document.data.updated_at, project.data?.default_timezone)}
      </div>
      {/* Plain text on purpose: the Markdown is shown as the agents read it,
          and nothing from the file is ever rendered as HTML. */}
      <pre className="kb-content" aria-label="Содержимое документа">
        {document.data.content}
      </pre>
    </Card>
  );
}

function DocumentList({
  documents,
  selected,
}: {
  documents: KnowledgeDocument[];
  selected: string | null;
}) {
  return (
    <ul className="kb-list" aria-label="Документы">
      {documents.map((doc) => (
        <li key={doc.name}>
          <Link
            to={`?doc=${encodeURIComponent(doc.name)}`}
            aria-current={doc.name === selected ? "true" : undefined}
          >
            {doc.name}
          </Link>
          <span className="muted small"> {formatSize(doc.size_bytes)}</span>
        </li>
      ))}
    </ul>
  );
}

/** SMM-034A: the project's knowledge documents, read-only. */
export function KnowledgeBasePage() {
  const { projectId = "" } = useParams();
  const [params] = useSearchParams();
  const api = useApi();
  useReportActiveProject(projectId);

  const knowledge = useQuery({
    queryKey: qk.knowledge(projectId),
    queryFn: () => api.projects.knowledge(projectId),
  });

  const header = (
    <PageHeader
      title="Knowledge Base"
      subtitle="Документы проекта, которые читают агенты при генерации"
    />
  );

  if (knowledge.isPending) return <>{header}<Loading /></>;
  if (knowledge.isError) {
    return (
      <>
        {header}
        <ErrorBanner error={knowledge.error} onRetry={() => void knowledge.refetch()} />
      </>
    );
  }

  const { available, documents } = knowledge.data;

  if (!available) {
    return (
      <>
        {header}
        <div className="banner banner-warn" data-testid="knowledge-unavailable">
          База знаний этого проекта недоступна на этом развёртывании Control Center.
        </div>
      </>
    );
  }

  const requested = params.get("doc");
  const selected = documents.some((d) => d.name === requested) ? requested : null;

  return (
    <>
      {header}
      <div className="banner banner-info" data-testid="knowledge-read-only">
        Только просмотр. Редактирование базы знаний из Control Center пока недоступно.
      </div>
      {documents.length === 0 ? (
        <Empty>В базе знаний проекта нет документов.</Empty>
      ) : (
        <div className="kb-layout">
          <Card title={`Документы · ${documents.length}`}>
            <DocumentList documents={documents} selected={selected} />
          </Card>
          {selected ? (
            <DocumentView key={selected} projectId={projectId} name={selected} />
          ) : (
            <Empty>Выберите документ слева.</Empty>
          )}
        </div>
      )}
    </>
  );
}
