import type { SocialAccount } from "../../types/api";

/** Server reason codes (ai_smm.account_routing) in words. */
export const NOT_READY_REASONS: Record<string, string> = {
  account_disabled: "аккаунт отключён",
  account_identity_unknown: "подключение не проверено — сохраните токен с проверкой",
  no_active_credential: "нет токена",
};

export function readinessLabel(account: SocialAccount): string {
  if (account.publishing_ready) return "готов к публикации";

  return NOT_READY_REASONS[account.not_ready_reason ?? ""] ?? account.not_ready_reason ?? "не готов";
}

export function accountLabel(account: SocialAccount): string {
  return account.external_username
    ? `${account.display_name} (@${account.external_username})`
    : account.display_name;
}
