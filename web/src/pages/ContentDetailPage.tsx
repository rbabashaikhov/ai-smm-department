import { useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router";

import {
  isInvalidStateTransition,
  isPublicationNotEditable,
  isStaleRevision,
  isVersionConflict,
  type PublicationNotEditableDetails,
} from "../api/errors";
import { qk } from "../api/queryKeys";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ErrorBanner } from "../components/ErrorBanner";
import { SnapshotView } from "../components/SnapshotView";
import {
  Card,
  KeyValues,
  Loading,
  PageHeader,
  StatusBadge,
  shortId,
} from "../components/ui";
import { useMe } from "../features/auth/AuthProvider";
import {
  RevisionForm,
  draftFromRevision,
  type RevisionDraft,
} from "../features/content/RevisionForm";
import {
  useApprovals,
  useContentCommands,
  useContentItem,
  useRevisions,
} from "../features/content/hooks";
import {
  useProject,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import type {
  Approval,
  ContentItemDetail,
  MaterializeResult,
  Revision,
} from "../types/api";

type DialogKind = "submit" | "approve" | "reject" | "materialize";

/** What a dialog acts on, frozen when it opened. A background refetch
 *  never changes which revision a confirmation sends. */
interface DialogState {
  kind: DialogKind;
  revision: Revision;
  itemVersion: number;
}

/** Page-level outcomes that make the screen's copy of the item stale. */
type Notice =
  | { kind: "version_conflict"; draft?: RevisionDraft }
  | { kind: "stale_revision" }
  | { kind: "invalid_transition"; message: string }
  | { kind: "not_editable"; details: PublicationNotEditableDetails; message: string };

const DIALOG_TEXT: Record<DialogKind, { title: string; confirm: string }> = {
  submit: { title: "Отправить на ревью", confirm: "Submit Review" },
  approve: { title: "Одобрить ревизию", confirm: "Approve this revision" },
  reject: { title: "Отклонить ревизию", confirm: "Reject this revision" },
  materialize: {
    title: "Подготовить публикацию (Create delivery snapshot)",
    confirm: "Create delivery snapshot",
  },
};

export function ContentDetailPage() {
  const { contentItemId = "" } = useParams();
  const me = useMe();
  const queryClient = useQueryClient();
  const itemQuery = useContentItem(contentItemId);
  const item = itemQuery.data;
  const projectId = item?.project_id;
  useReportActiveProject(projectId);

  const project = useProject(projectId);
  const can = useProjectPermissions(projectId);
  const revisions = useRevisions(contentItemId);
  const approvals = useApprovals(contentItemId);
  const commands = useContentCommands(contentItemId, projectId);

  const [editing, setEditing] = useState<{ baseVersion: number; base: Revision } | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [note, setNote] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [lastMaterialize, setLastMaterialize] = useState<MaterializeResult | null>(null);

  const tz = project.data?.default_timezone;

  if (itemQuery.isPending) return <Loading />;
  if (itemQuery.isError || !item) {
    return <ErrorBanner error={itemQuery.error} onRetry={() => itemQuery.refetch()} />;
  }

  const current = item.current_revision;
  const archived = item.status === "archived";

  const reload = async () => {
    setNotice(null);
    setEditing(null);
    await queryClient.invalidateQueries({ queryKey: qk.contentItem(contentItemId) });
  };

  /** Turn a refusal that makes the screen stale into a page notice. */
  const handleStale = (error: unknown, draft?: RevisionDraft): boolean => {
    if (isVersionConflict(error)) {
      setNotice({ kind: "version_conflict", draft });
    } else if (isStaleRevision(error)) {
      setNotice({ kind: "stale_revision" });
      // The reviewer read old text: fetch the current revision now.
      void queryClient.invalidateQueries({ queryKey: qk.contentItem(contentItemId) });
    } else if (isInvalidStateTransition(error)) {
      setNotice({ kind: "invalid_transition", message: error.message });
      void queryClient.invalidateQueries({ queryKey: qk.contentItem(contentItemId) });
    } else if (isPublicationNotEditable(error)) {
      setNotice({ kind: "not_editable", details: error.details, message: error.message });
    } else {
      return false;
    }

    setDialog(null);

    return true;
  };

  const openDialog = (kind: DialogKind) => {
    setNote("");
    setNotice(null);
    setLastMaterialize(null);
    Object.values(commands).forEach((m) => m.reset());
    setDialog({ kind, revision: current, itemVersion: item.version });
  };

  const decisionMutations = {
    submit: commands.submitReview,
    approve: commands.approve,
    reject: commands.reject,
  };

  const mutationFor = (kind: DialogKind) =>
    kind === "materialize" ? commands.materialize : decisionMutations[kind];

  const confirm = () => {
    if (!dialog) return;

    const { kind, revision, itemVersion } = dialog;
    const options = {
      onSuccess: (result: unknown) => {
        if (kind === "materialize") setLastMaterialize(result as MaterializeResult);
        setDialog(null);
      },
      onError: (error: unknown) => {
        handleStale(error);
      },
    };

    if (kind === "materialize") {
      commands.materialize.mutate(revision.id, options);
    } else {
      decisionMutations[kind].mutate(
        {
          revision_id: revision.id,
          expected_item_version: itemVersion,
          ...(note.trim() ? { note: note.trim() } : {}),
        },
        options,
      );
    }
  };

  const activeMutation = dialog ? mutationFor(dialog.kind) : null;
  // Errors already turned into a page notice are not repeated in the dialog.
  const dialogError =
    activeMutation?.error && !notice ? activeMutation.error : null;

  return (
    <>
      <PageHeader
        title={item.title}
        subtitle={
          <>
            <StatusBadge status={item.status} /> · {item.content_type} · version {item.version}{" "}
            · revision #{current.revision_number}
            {projectId ? (
              <>
                {" "}
                · <Link to={`/projects/${encodeURIComponent(projectId)}/content`}>{projectId}</Link>
              </>
            ) : null}
          </>
        }
        actions={
          can.canEditContent && !archived ? (
            <ContentActions
              item={item}
              editing={editing !== null}
              onNewRevision={() => {
                setNotice(null);
                commands.createRevision.reset();
                setEditing({ baseVersion: item.version, base: current });
              }}
              onOpen={openDialog}
            />
          ) : null
        }
      />

      {notice ? <NoticeBanner notice={notice} onReload={reload} /> : null}
      {lastMaterialize ? <MaterializedBanner result={lastMaterialize} /> : null}
      {itemQuery.isRefetching ? <div className="muted small">Обновление…</div> : null}

      {editing ? (
        <Card title={`New Revision (на основе #${editing.base.revision_number}, version ${editing.baseVersion})`}>
          <p className="muted small">
            Существующая ревизия не изменяется: сохранение создаёт новую ревизию, материал
            возвращается в draft, текущее одобрение перестаёт действовать.
          </p>
          <RevisionForm
            initial={draftFromRevision(editing.base)}
            submitLabel="Save as new revision"
            pending={commands.createRevision.isPending}
            onCancel={() => setEditing(null)}
            onSubmit={(input, draft) => {
              commands.createRevision.mutate(
                { ...input, expected_item_version: editing.baseVersion },
                {
                  onSuccess: () => setEditing(null),
                  onError: (error) => {
                    if (handleStale(error, draft)) setEditing(null);
                  },
                },
              );
            }}
            footer={
              commands.createRevision.isError && !notice ? (
                <ErrorBanner error={commands.createRevision.error} />
              ) : null
            }
          />
        </Card>
      ) : null}

      <div className="grid-2 wide-left">
        <Card title={`Current revision #${current.revision_number}`}>
          <ApprovalState item={item} />
          <SnapshotView
            title={current.title}
            body={current.body}
            format={current.format}
            images={current.images}
            items={current.items}
          />
          <RevisionMeta revision={current} meId={me.user.id} tz={tz} />
        </Card>
        <div>
          <Card title="Item">
            <KeyValues
              rows={[
                ["id", <code key="id">{item.id}</code>],
                ["status", <StatusBadge key="s" status={item.status} />],
                ["version", item.version],
                ["current_revision_id", <code key="c">{item.current_revision_id}</code>],
                [
                  "approved_revision_id",
                  item.approved_revision_id ? (
                    <code key="a">{item.approved_revision_id}</code>
                  ) : (
                    "—"
                  ),
                ],
                ["approval_in_effect", item.approval_in_effect ? "yes" : "no"],
                ["created", formatDateTime(item.created_at, tz)],
                ["updated", formatDateTime(item.updated_at, tz)],
                ["platform", project.data?.default_platform ?? "—"],
              ]}
            />
          </Card>
          <Card title="Delivery">
            <p className="muted small">
              Материализация создаёт неизменяемый delivery snapshot (Publication в статусе
              approved). Ставит в очередь только admin; публикует только worker.
            </p>
            {lastMaterialize ? (
              <p>
                Snapshot:{" "}
                <Link to={`/publications/${lastMaterialize.publication.id}`}>
                  Publication #{lastMaterialize.publication.id}
                </Link>{" "}
                <StatusBadge status={lastMaterialize.publication.status} />
              </p>
            ) : (
              <p className="muted small">
                Текущий API не отдаёт список snapshot-ов материала; ссылка появляется после
                «Подготовить публикацию» или при конфликте с предыдущим snapshot.
              </p>
            )}
          </Card>
        </div>
      </div>

      <Card title="Approval history">
        <ApprovalHistory
          query={approvals}
          item={item}
          revisions={revisions.data?.items ?? []}
          meId={me.user.id}
          tz={tz}
        />
      </Card>

      <Card title="Revision history">
        <RevisionHistory query={revisions} item={item} tz={tz} />
      </Card>

      {dialog ? (
        <ConfirmDialog
          title={DIALOG_TEXT[dialog.kind].title}
          confirmLabel={DIALOG_TEXT[dialog.kind].confirm}
          danger={dialog.kind === "reject"}
          pending={activeMutation?.isPending}
          error={dialogError}
          onCancel={() => setDialog(null)}
          onConfirm={confirm}
        >
          <DialogBody
            dialog={dialog}
            platform={project.data?.default_platform}
            note={note}
            onNote={setNote}
          />
        </ConfirmDialog>
      ) : null}
    </>
  );
}

function ContentActions({
  item,
  editing,
  onNewRevision,
  onOpen,
}: {
  item: ContentItemDetail;
  editing: boolean;
  onNewRevision: () => void;
  onOpen: (kind: DialogKind) => void;
}) {
  // Which commands to offer follows the item status the server reported.
  // The server checks the transition again and answers 409 if it moved.
  return (
    <div className="button-row">
      <button type="button" onClick={onNewRevision} disabled={editing}>
        New Revision
      </button>
      {item.status === "draft" ? (
        <button type="button" onClick={() => onOpen("submit")}>
          Submit Review
        </button>
      ) : null}
      {item.status === "in_review" ? (
        <>
          <button type="button" className="btn-primary" onClick={() => onOpen("approve")}>
            Approve
          </button>
          <button type="button" className="btn-danger" onClick={() => onOpen("reject")}>
            Reject
          </button>
        </>
      ) : null}
      {item.status === "approved" && item.approval_in_effect ? (
        <button type="button" className="btn-primary" onClick={() => onOpen("materialize")}>
          Подготовить публикацию
        </button>
      ) : null}
    </div>
  );
}

function DialogBody({
  dialog,
  platform,
  note,
  onNote,
}: {
  dialog: DialogState;
  platform: string | undefined;
  note: string;
  onNote: (value: string) => void;
}) {
  const { kind, revision } = dialog;

  return (
    <>
      <KeyValues
        rows={[
          ["revision", `#${revision.revision_number}`],
          ["revision_id", <code key="r">{revision.id}</code>],
          ["content_hash", <code key="h">{revision.content_hash.slice(0, 16)}…</code>],
          ...(kind === "materialize"
            ? ([["platform", <strong key="p">{platform ?? "project default"}</strong>]] as [
                string,
                ReactNode,
              ][])
            : []),
        ]}
      />
      {kind === "materialize" ? (
        <p className="small">
          Будет создан <strong>delivery snapshot</strong> этой ревизии — Publication в статусе{" "}
          <code>approved</code>, без даты. Это <strong>не публикация</strong> и не постановка в
          очередь.
        </p>
      ) : null}
      {kind === "approve" ? (
        <p className="small">Вы одобряете именно этот текст. Новая ревизия отменит одобрение.</p>
      ) : null}
      <SnapshotView
        title={revision.title}
        body={revision.body}
        format={revision.format}
        images={revision.images}
        items={revision.items}
      />
      {kind === "reject" || kind === "approve" || kind === "submit" ? (
        <label>
          Note {kind === "reject" ? "(optional, reason)" : "(optional)"}
          <textarea
            rows={2}
            maxLength={2000}
            value={note}
            onChange={(e) => onNote(e.target.value)}
            aria-label="Decision note"
          />
        </label>
      ) : null}
    </>
  );
}

function ApprovalState({ item }: { item: ContentItemDetail }) {
  if (item.approval_in_effect) {
    return (
      <div className="banner banner-ok">
        Revision #{item.current_revision.revision_number} одобрена и одобрение действует.
      </div>
    );
  }

  if (item.status === "in_review") {
    return <div className="banner banner-info">На ревью: ждёт решения человека.</div>;
  }

  if (item.status === "rejected") {
    return (
      <div className="banner banner-warn">Отклонено. Следующий шаг — новая ревизия.</div>
    );
  }

  return null;
}

function RevisionMeta({
  revision,
  meId,
  tz,
}: {
  revision: Revision;
  meId: string;
  tz: string | undefined;
}) {
  return (
    <KeyValues
      rows={[
        ["revision_id", <code key="id">{revision.id}</code>],
        ["content_hash", <code key="h">{revision.content_hash}</code>],
        ["source", revision.source],
        ["source_ref", revision.source_ref ?? "—"],
        ["editor_score", revision.editor_score ?? "—"],
        ["editor_notes", revision.editor_notes || "—"],
        [
          "created_by",
          revision.created_by_user_id
            ? revision.created_by_user_id === meId
              ? "you"
              : shortId(revision.created_by_user_id)
            : `agent:${revision.source}`,
        ],
        ["created", formatDateTime(revision.created_at, tz)],
        [
          "metadata",
          Object.keys(revision.metadata).length ? (
            <pre key="m" className="json">
              {JSON.stringify(revision.metadata, null, 2)}
            </pre>
          ) : (
            "—"
          ),
        ],
      ]}
    />
  );
}

function ApprovalHistory({
  query,
  item,
  revisions,
  meId,
  tz,
}: {
  query: ReturnType<typeof useApprovals>;
  item: ContentItemDetail;
  revisions: Revision[];
  meId: string;
  tz: string | undefined;
}) {
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  if (query.data.items.length === 0) return <p className="muted">Решений пока нет.</p>;

  const numberOf = (id: string) => revisions.find((r) => r.id === id)?.revision_number;

  // Only the newest approval of the approved current revision is in
  // effect; everything else is history, whatever its decision was.
  const inEffect = (approval: Approval) =>
    item.approval_in_effect &&
    approval.decision === "approved" &&
    approval.revision_id === item.approved_revision_id;

  return (
    <table className="table compact">
      <thead>
        <tr>
          <th>When</th>
          <th>Decision</th>
          <th>Revision</th>
          <th>By</th>
          <th>Note</th>
          <th>State</th>
        </tr>
      </thead>
      <tbody>
        {query.data.items.map((approval: Approval) => (
          <tr key={approval.id}>
            <td>{formatDateTime(approval.created_at, tz)}</td>
            <td>
              <StatusBadge status={approval.decision} />
            </td>
            <td>
              #{numberOf(approval.revision_id) ?? "?"} <code>{shortId(approval.revision_id)}</code>
            </td>
            <td>{approval.actor_user_id === meId ? "you" : shortId(approval.actor_user_id)}</td>
            <td>{approval.note || <span className="muted">—</span>}</td>
            <td>
              {inEffect(approval) ? (
                <span className="badge badge-approved">in effect</span>
              ) : approval.revision_id !== item.current_revision_id ? (
                <span className="badge badge-superseded">superseded</span>
              ) : (
                <span className="muted">—</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RevisionHistory({
  query,
  item,
  tz,
}: {
  query: ReturnType<typeof useRevisions>;
  item: ContentItemDetail;
  tz: string | undefined;
}) {
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;

  return (
    <table className="table compact">
      <thead>
        <tr>
          <th>#</th>
          <th>Title / body</th>
          <th>Format</th>
          <th>Source</th>
          <th>Hash</th>
          <th>Created</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {[...query.data.items].reverse().map((revision) => (
          <tr key={revision.id}>
            <td>{revision.revision_number}</td>
            <td>
              <details>
                <summary>
                  {revision.title || revision.body.slice(0, 80)}
                  {!revision.title && revision.body.length > 80 ? "…" : ""}
                </summary>
                <pre className="snapshot-body">{revision.body}</pre>
              </details>
            </td>
            <td>{revision.format}</td>
            <td>
              {revision.source}
              {revision.source_ref ? <div className="muted small">{revision.source_ref}</div> : null}
            </td>
            <td>
              <code>{revision.content_hash.slice(0, 12)}</code>
            </td>
            <td>{formatDateTime(revision.created_at, tz)}</td>
            <td>
              {revision.id === item.current_revision_id ? (
                <span className="badge">current</span>
              ) : null}{" "}
              {revision.id === item.approved_revision_id ? (
                <span className="badge badge-approved">approved</span>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NoticeBanner({ notice, onReload }: { notice: Notice; onReload: () => void }) {
  switch (notice.kind) {
    case "version_conflict":
      return (
        <div className="banner banner-error" role="alert">
          <strong>VERSION_CONFLICT.</strong> Контент был изменён другим действием или в другой
          вкладке. Ничего не сохранено. Обновите страницу, прежде чем продолжать (reload
          required).
          {notice.draft ? (
            <details>
              <summary>Ваш несохранённый текст</summary>
              <pre className="snapshot-body">{notice.draft.body}</pre>
            </details>
          ) : null}
          <div className="banner-actions">
            <button type="button" onClick={onReload}>
              Reload
            </button>
          </div>
        </div>
      );
    case "stale_revision":
      return (
        <div className="banner banner-warn" role="alert">
          <strong>STALE_REVISION.</strong> Вы смотрели не текущую ревизию — решение не
          записано. Текущая ревизия загружена заново; проверьте текст и повторите.
        </div>
      );
    case "invalid_transition":
      return (
        <div className="banner banner-warn" role="alert">
          <strong>INVALID_STATE_TRANSITION.</strong> {notice.message} Состояние перезагружено.
        </div>
      );
    case "not_editable":
      return (
        <div className="banner banner-error" role="alert">
          <strong>PUBLICATION_NOT_EDITABLE.</strong> {notice.message}
          <KeyValues
            rows={[
              ["blocking publication", `#${notice.details.publication_id}`],
              ["publication status", <StatusBadge key="s" status={notice.details.publication_status} />],
              [
                "previous_revision_id",
                notice.details.previous_revision_id ? (
                  <code key="p">{notice.details.previous_revision_id}</code>
                ) : (
                  "—"
                ),
              ],
            ]}
          />
          <p className="small">
            Сначала явно отмените прежний delivery snapshot (admin+), затем снова нажмите
            «Подготовить публикацию». Автоматической отмены нет.
          </p>
          <div className="banner-actions">
            <Link className="btn" to={`/publications/${notice.details.publication_id}`}>
              Open Publication #{notice.details.publication_id}
            </Link>
          </div>
        </div>
      );
  }
}

function MaterializedBanner({ result }: { result: MaterializeResult }) {
  return (
    <div className="banner banner-ok" role="status">
      Delivery snapshot{" "}
      {result.result === "created" ? "создан" : "уже существовал (unchanged)"}:{" "}
      <Link to={`/publications/${result.publication.id}`}>
        Publication #{result.publication.id}
      </Link>{" "}
      · platform {result.platform} · status <StatusBadge status={result.publication.status} />.
      Не опубликовано и не в очереди — постановку делает admin.
    </div>
  );
}
