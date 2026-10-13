import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { useApi } from "../api/context";
import { qk } from "../api/queryKeys";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ErrorBanner } from "../components/ErrorBanner";
import { Card, Empty, Loading, PageHeader } from "../components/ui";
import { readinessLabel } from "../features/accounts/labels";
import {
  useProjectAccounts,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import type { SocialAccount } from "../types/api";

function AddAccount({ projectId }: { projectId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: () => api.accounts.create(projectId, name.trim()),
    onSuccess: async () => {
      setName("");
      await queryClient.invalidateQueries({ queryKey: qk.accounts(projectId) });
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim()) create.mutate();
  };

  return (
    <form className="form-row" onSubmit={submit} aria-label="Add account form">
      <label className="grow">
        Новый аккаунт Threads (название для себя)
        <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
      </label>
      <button type="submit" disabled={!name.trim() || create.isPending}>
        + Аккаунт
      </button>
      {create.isError ? <ErrorBanner error={create.error} /> : null}
    </form>
  );
}

function StatusAction({ account, projectId }: { account: SocialAccount; projectId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const disabling = account.status === "active";

  const change = useMutation({
    mutationFn: () =>
      disabling
        ? api.accounts.disable(account.id, account.version)
        : api.accounts.enable(account.id, account.version),
    onSuccess: async () => {
      setConfirming(false);
      await queryClient.invalidateQueries({ queryKey: qk.accounts(projectId) });
    },
  });

  return (
    <>
      <button type="button" onClick={() => setConfirming(true)}>
        {disabling ? "Отключить" : "Включить"}
      </button>
      {confirming ? (
        <ConfirmDialog
          title={disabling ? `Отключить «${account.display_name}»?` : `Включить «${account.display_name}»?`}
          confirmLabel={disabling ? "Отключить" : "Включить"}
          danger={disabling}
          pending={change.isPending}
          error={change.error}
          onConfirm={() => change.mutate()}
          onCancel={() => {
            change.reset();
            setConfirming(false);
          }}
        >
          {disabling ? (
            <p>
              Публикации этого аккаунта не будут отправлены ни через него, ни через другой аккаунт.
              {account.scheduled_publications > 0
                ? ` В расписании сейчас: ${account.scheduled_publications} — они завершатся ошибкой без отправки.`
                : ""}
            </p>
          ) : (
            <p>Аккаунт снова сможет публиковать, если у него есть проверенный токен.</p>
          )}
        </ConfirmDialog>
      ) : null}
    </>
  );
}

export function AccountsPage() {
  const { projectId = "" } = useParams();
  useReportActiveProject(projectId);
  const can = useProjectPermissions(projectId);
  const accounts = useProjectAccounts(projectId);
  const base = `/projects/${encodeURIComponent(projectId)}`;

  return (
    <>
      <PageHeader
        title="Аккаунты"
        subtitle="Через какие аккаунты этот проект может публиковать"
        actions={
          can.canManageAccounts ? <Link to={`${base}/credentials`}>Учётные данные →</Link> : null
        }
      />
      {can.canManageAccounts ? (
        <Card>
          <AddAccount projectId={projectId} />
        </Card>
      ) : null}
      {accounts.isPending ? <Loading /> : null}
      {accounts.isError ? (
        <ErrorBanner error={accounts.error} onRetry={() => accounts.refetch()} />
      ) : null}
      {accounts.data && accounts.data.length === 0 ? (
        <Empty>
          У проекта нет аккаунтов. Публикации нового проекта не используют аккаунт развёртывания:
          без подключённого аккаунта публиковать нельзя.
        </Empty>
      ) : null}
      {accounts.data && accounts.data.length > 0 ? (
        <table className="table" aria-label="Accounts">
          <thead>
            <tr>
              <th>Аккаунт</th>
              <th>Платформа</th>
              <th>Threads</th>
              <th>Статус</th>
              <th>Публикация</th>
              <th>Токен</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {accounts.data.map((account) => (
              <tr key={account.id} data-testid={`account-${account.id}`}>
                <td>
                  <strong>{account.display_name}</strong>
                </td>
                <td>{account.platform}</td>
                <td>{account.external_username ? `@${account.external_username}` : "—"}</td>
                <td>
                  <span className={`badge badge-${account.status}`}>{account.status}</span>
                </td>
                <td>{readinessLabel(account)}</td>
                <td>
                  {account.credential ? (
                    <span title="Отпечаток токена, не его часть">
                      <code>{account.credential.fingerprint}</code>
                      {account.credential.verified_at
                        ? ` · проверен ${formatDateTime(account.credential.verified_at, "UTC")}`
                        : " · не проверен"}
                    </span>
                  ) : (
                    "нет"
                  )}
                </td>
                <td>{can.canManageAccounts ? <StatusAction account={account} projectId={projectId} /> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </>
  );
}
