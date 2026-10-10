import type { JsonObject } from "../types/api";

/** Exact text and media of a revision or a Publication, read-only. */
export function SnapshotView({
  title,
  body,
  format,
  images,
  items,
}: {
  title: string | null;
  body: string;
  format: string;
  images: JsonObject[];
  items: JsonObject[];
}) {
  return (
    <div className="snapshot" data-testid="snapshot">
      <div className="snapshot-meta muted small">
        format: <code>{format}</code> · {body.length} chars · {images.length} images ·{" "}
        {items.length} items
      </div>
      {title ? <div className="snapshot-title">{title}</div> : null}
      <pre className="snapshot-body">{body}</pre>
      {images.length > 0 ? (
        <details open>
          <summary>Images ({images.length})</summary>
          <ol className="media-list">
            {images.map((image, index) => (
              <li key={index}>
                <code>{String(image.url ?? image.path ?? image.alt ?? JSON.stringify(image))}</code>
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      {items.length > 0 ? (
        <details>
          <summary>Items ({items.length})</summary>
          <pre className="json">{JSON.stringify(items, null, 2)}</pre>
        </details>
      ) : null}
    </div>
  );
}
