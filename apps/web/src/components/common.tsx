import { Button, Card, EmptyState, Notice, Skeleton } from "@repo/ui";
import { CloudOff, Inbox, Layers, LogIn, RotateCw } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ApiError } from "../api";
import { login } from "../auth";
import { cachedGet } from "../offline";
import { useApp } from "../state";

export function PageHeader({ title, description, actions, badge }: { title: string; description?: ReactNode; actions?: ReactNode; badge?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end gap-3">
      <div className="min-w-0 flex-1">
        <h1 className="m-0 flex flex-wrap items-center gap-3 text-2xl font-semibold">
          {title}
          {badge}
        </h1>
        {description && <p className="m-0 mt-1 max-w-3xl text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function SignInGate({ children }: { children: ReactNode }) {
  const { user } = useApp();
  if (user) return <>{children}</>;
  return (
    <Card>
      <EmptyState icon={<LogIn />} title="Sign in to see your wells">
        <p>Only the wells your licences cover are shown, under row-level security.</p>
        <div className="mt-2 flex gap-2">
          <Button onClick={() => void login()}>Sign in</Button>
          <Button variant="secondary" onClick={() => void login(true)}>
            Create an account
          </Button>
        </div>
        <p className="text-sm">New accounts can read the demo wells; an administrator grants roles to act.</p>
      </EmptyState>
    </Card>
  );
}

export function NeedWell({ id, children }: { id: number | null; children: (id: number) => ReactNode }) {
  if (id != null) return <>{children(id)}</>;
  return (
    <Card>
      <EmptyState icon={<Layers />} title="Select a well">
        Pick a well in the top bar, on the map or in the fleet view.
      </EmptyState>
    </Card>
  );
}

export function ErrorNote({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const msg = error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
  return (
    <Notice tone="critical" title="Something went wrong">
      <span role="alert">{msg}</span>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry} className="ml-3">
          <RotateCw /> Retry
        </Button>
      )}
    </Notice>
  );
}

type Loaded<T> = { data: T | null; error: unknown; cachedAt: string | null; loading: boolean; reload: () => void };

/** The four states every screen shows: loading (skeleton), error (with retry), empty, and
 *  stale (served from the offline package, under a gold banner). */
export function DataState<T>({
  state,
  isEmpty,
  empty,
  skeleton = "h-64",
  children,
}: {
  state: Loaded<T>;
  isEmpty?: (d: T) => boolean;
  empty?: { title: string; body?: ReactNode; icon?: ReactNode };
  skeleton?: string;
  children: (data: T) => ReactNode;
}) {
  if (state.loading && state.data == null) return <Skeleton className={`${skeleton} w-full`} />;
  if (state.error && state.data == null) return <ErrorNote error={state.error} onRetry={state.reload} />;
  if (state.data == null) return null;
  if (isEmpty?.(state.data))
    return (
      <>
        <CachedBanner at={state.cachedAt} />
        <Card>
          <EmptyState icon={empty?.icon ?? <Inbox />} title={empty?.title ?? "Nothing to show"}>
            {empty?.body}
          </EmptyState>
        </Card>
      </>
    );
  return (
    <>
      <CachedBanner at={state.cachedAt} />
      {children(state.data)}
    </>
  );
}

export function CachedBanner({ at }: { at: string | null }) {
  if (!at) return null;
  return (
    <Notice tone="gold" title="Offline: showing data saved on this device">
      <span className="inline-flex items-center gap-2">
        <CloudOff aria-hidden className="size-4" /> Saved <span className="num">{new Date(at).toLocaleString()}</span>. Numbers may be out of date.
      </span>
    </Notice>
  );
}

/** Load a GET (offline-capable); `reload` refetches. */
export function useLoad<T>(path: string | null): { data: T | null; error: unknown; cachedAt: string | null; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: unknown; cachedAt: string | null; loading: boolean }>({ data: null, error: null, cachedAt: null, loading: !!path });
  const reload = useCallback(() => {
    if (!path) return;
    setState((s) => ({ ...s, loading: true, error: null }));
    cachedGet<T>(path).then(
      (r) => setState({ data: r.data, cachedAt: r.cachedAt, error: null, loading: false }),
      (e) => setState({ data: null, cachedAt: null, error: e, loading: false }),
    );
  }, [path]);
  useEffect(reload, [reload]);
  return { ...state, reload };
}

export const words = (s: string) => s.replaceAll("_", " ");
export const pct = (x: number | null | undefined) => (x == null ? "–" : `${Math.round(x * 100)} %`);
/** A probability that may be tiny (priors, posteriors): never shows a misleading "0 %". */
export const prob = (x: number | null | undefined) =>
  x == null ? "–" : x < 0.001 ? "<0.1 %" : x < 0.1 ? `${(x * 100).toFixed(1)} %` : `${Math.round(x * 100)} %`;
