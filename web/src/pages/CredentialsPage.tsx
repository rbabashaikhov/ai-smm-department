// The credentials manager. Tokens go in and never come out:
//
// - the input is type=password, autocomplete off, and is cleared after
//   every submit, success or failure;
// - the mutation is created with gcTime 0 and reset as soon as it settles,
//   so its variables (the token) do not stay in TanStack Query's cache;
// - nothing here reads a token back: the API has no route that returns one,
//   and what is shown is the fingerprint, status and dates.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { useApi } from "../api/context";
import { ApiError, ErrorCode } from "../api/errors";
import { qk } from "../api/queryKeys";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ErrorBanner } from "../components/ErrorBanner";
import { Card, Empty, KeyValues, Loading, PageHeader } from "../components/ui";
import { accountLabel, readinessLabel } from "../features/accounts/labels";
import {
  useProjectAccounts,
  useProjectPermissions,
  useReportActiveProject,
} from "../features/projects/hooks";
import { formatDateTime } from "../lib/time";
import type { Credential, SocialAccount } from "../types/api";

function describeCredentialError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;

  switch (error.code) {
    case ErrorCode.CREDENTIAL_ACCOUNT_MISMATCH:
      return "Этот токен принадлежит другому аккаунту Threads. Ничего не сохранено.";
    case ErrorCode.ACCOUNT_ALREADY_CONNECTED:
      return "Этот аккаунт Threads уже подключён к другому аккаунту проекта. Ничего не сохранено.";
    case ErrorCode.CREDENTIAL_REJECTED:
      return "Threads отклонил токен (истёк, отозван или без нужных прав). Ничего не сохранено.";
    case ErrorCode.CREDENTIALS_NOT_CONFIGURED:
      return "На сервере не настроен ключ шифрования. Ничего не сохранено.";
    default:
      return null;
  }
}

function CredentialForm({ account, projectId }: { account: SocialAccount; projectId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [token, setToken] = useState("");
  const [expires, setExpires] = useState("");
  const [verify, setVerify] = useState(true);
  const [lastError, setLastError] = useState<unknown>(null);
  const [saved, setSaved] = useState<Credential | null>(null);

  const save = useMutation({
    gcTime: 0,
    mutationFn: (body: { access_token: string; expires_at: string | null; verify: boolean }) =>
      api.accounts.setCredential(account.id, body),
    onSettled: () => {
      setToken("");
    },
    onSuccess: async (credential) => {
      setSaved(credential);
      setLastError(null);
      await queryClient.invalidateQueries({ queryKey: qk.accounts(projectId) });
    },
    onError: (error) => {
      setSaved(null);
      setLastError(error);
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();

    if (!token.trim() || save.isPending) return;

    const body = {
      access_token: token,
      expires_at: expires ? new Date(`${expires}T00:00:00Z`).toISOString() : null,
      verify,
    };

    setToken("");
    save.mutate(body, { onSettled: () => save.reset() });
  };

  const friendly = describeCredentialError(lastError);

  return (
    <form className="revision-form" onSubmit={submit} aria-label={`Credential form ${account.display_name}`}>
      <div className="form-row">
        <label className="grow">
          {account.credential ? "Новый токен (заменит текущий)" : "Токен доступа"}
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            maxLength={4096}
            aria-label={`Токен ${account.display_name}`}
          />
        </label>
        <label>
          Срок действия до
          <input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </label>
      </div>
      <label className="checkbox">
        <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />
        Проверить подключение (GET /me, ничего не публикует)
      </label>
      {!verify ? (
        <div className="muted small">
          Без проверки токен сохранится, но аккаунт не станет готовым к публикации.
        </div>
      ) : null}
      {friendly ? (
        <div className="banner banner-error" role="alert">
          {friendly}
        </div>
      ) : lastError ? (
        <ErrorBanner error={lastError} />
      ) : null}
      {saved ? (
        <div className="banner banner-info" role="status">
          Сохранено. Отпечаток <code>{saved.fingerprint}</code>
          {saved.verification_status === "verified" ? ", подключение проверено." : ", без проверки."}
        </div>
      ) : null}
      <div className="form-actions">
        <button type="submit" disabled={!token.trim() || save.isPending}>
          {account.credential ? "Заменить" : "Сохранить"}
          {verify ? " и проверить" : ""}
        </button>
      </div>
    </form>
  );
}

function RevokeAction({ credential, projectId, label }: { credential: Credential; projectId: string; label: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  const revoke = useMutation({
    mutationFn: () => api.accounts.revokeCredential(credential.id),
    onSuccess: async () => {
      setConfirming(false);
      await queryClient.invalidateQueries({ queryKey: qk.accounts(projectId) });
    },
  });

  return (
    <>
      <button type="button" className="danger" onClick={() => setConfirming(true)}>
        Отозвать токен
      </button>
      {confirming ? (
        <ConfirmDialog
          title={`Отозвать токен «${label}»?`}
          confirmLabel="Отозвать"
          danger
          pending={revoke.isPending}
          error={revoke.error}
          onConfirm={() => revoke.mutate()}
          onCancel={() => {
            revoke.reset();
            setConfirming(false);
          }}
        >
          <p>
            Зашифрованный токен будет удалён из системы. Публикации этого аккаунта не будут
            отправлены, пока не сохранён новый токен. В самом Threads токен этим не отзывается.
          </p>
        </ConfirmDialog>
      ) : null}
    </>
  );
}

export function CredentialsPage() {
  const { projectId = "" } = useParams();
  useReportActiveProject(projectId);
  const can = useProjectPermissions(projectId);
  const accounts = useProjectAccounts(projectId);
  const base = `/projects/${encodeURIComponent(projectId)}`;

  if (!can.canManageAccounts) {
    return (
      <>
        <PageHeader title="Учётные данные" />
        <div className="banner banner-warn">
          {can.readOnly
            ? "Control Center работает только на чтение: изменение учётных данных отключено на сервере."
            : "Управлять учётными данными может только владелец проекта (owner)."}
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Учётные данные"
        subtitle="Токены хранятся зашифрованно и не могут быть прочитаны обратно"
        actions={<Link to={`${base}/accounts`}>← Аккаунты</Link>}
      />
      {accounts.isPending ? <Loading /> : null}
      {accounts.isError ? <ErrorBanner error={accounts.error} onRetry={() => accounts.refetch()} /> : null}
      {accounts.data && accounts.data.length === 0 ? (
        <Empty>
          Сначала добавьте аккаунт на странице <Link to={`${base}/accounts`}>«Аккаунты»</Link>.
        </Empty>
      ) : null}
      {accounts.data?.map((account) => (
        <Card
          key={account.id}
          title={`${accountLabel(account)} · ${account.platform} · ${readinessLabel(account)}`}
          actions={
            account.credential ? (
              <RevokeAction credential={account.credential} projectId={projectId} label={account.display_name} />
            ) : null
          }
        >
          {account.credential ? (
            <KeyValues
              rows={[
                ["Отпечаток", <code key="fp">{account.credential.fingerprint}</code>],
                ["Проверен", formatDateTime(account.credential.verified_at, "UTC")],
                ["Действует до", formatDateTime(account.credential.expires_at, "UTC")],
                ["Сохранён", formatDateTime(account.credential.created_at, "UTC")],
              ]}
            />
          ) : (
            <p className="muted">Токена нет.</p>
          )}
          <CredentialForm account={account} projectId={projectId} />
        </Card>
      ))}
    </>
  );
}
