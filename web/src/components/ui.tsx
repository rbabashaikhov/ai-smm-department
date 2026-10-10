import type { ReactNode } from "react";

import type { PageParams } from "../types/api";

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${status}`}>{status}</span>;
}

export function Loading({ label = "Загрузка…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      {label}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle ? <div className="muted">{subtitle}</div> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </div>
  );
}

export function Card({
  title,
  children,
  actions,
}: {
  title?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="card">
      {title || actions ? (
        <div className="card-header">
          {title ? <h2>{title}</h2> : <span />}
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function KeyValues({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([key, value], index) => (
        <div key={index} className="kv-row">
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Pagination({
  page,
  total,
  onChange,
}: {
  page: PageParams;
  total: number;
  onChange: (page: PageParams) => void;
}) {
  const from = total === 0 ? 0 : page.offset + 1;
  const to = Math.min(page.offset + page.limit, total);

  return (
    <div className="pagination">
      <span className="muted">
        {from}–{to} из {total}
      </span>
      <button
        type="button"
        onClick={() => onChange({ ...page, offset: Math.max(0, page.offset - page.limit) })}
        disabled={page.offset === 0}
      >
        ← Назад
      </button>
      <button
        type="button"
        onClick={() => onChange({ ...page, offset: page.offset + page.limit })}
        disabled={page.offset + page.limit >= total}
      >
        Вперёд →
      </button>
    </div>
  );
}

export function shortId(id: string | null | undefined, length = 8): string {
  if (!id) return "—";

  return id.length > length ? id.slice(0, length) : id;
}
