"use client";

/**
 * Schedules list page -- STUB.
 *
 * This is the **skeleton** for the Web UI in Task 10. It deliberately
 * implements ONLY:
 *
 *   - fetch `GET /api/schedules` and render the returned list
 *   - render a "no schedules" empty state when the API returns []
 *
 * It does NOT implement create / edit / pause / resume / detail
 * drawer. Those belong to a follow-up. The full plan describes a
 * list + detail + create form with cron NL preview, which is large
 * enough to be its own sub-project; this stub ships just enough
 * surface to prove the backend wiring works end-to-end.
 *
 * The follow-up is owned by a separate ticket. See:
 *   - backend/app/gateway/routers/schedules.py   (REST surface)
 *   - backend/CLAUDE.md "Scheduling" section    (full design)
 */

import { CalendarIcon, Loader2Icon } from "lucide-react";
import { useEffect, useState } from "react";

import { getBackendBaseURL } from "@/core/config";

type Schedule = {
  id: string;
  title: string;
  kind: string; // "cron" | "one_shot"
  next_fire_at: string | null;
  status: string; // "active" | "paused" | "deleted"
};

type LoadState =
  | { kind: "loading" }
  | { kind: "ok"; schedules: Schedule[] }
  | { kind: "empty" }
  | { kind: "error"; message: string };

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  // Server stores Asia/Shanghai by default; render in the user's locale
  // without forcing a tz conversion (cheap and correct enough for a stub).
  return d.toLocaleString();
}

async function fetchSchedules(): Promise<Schedule[]> {
  const res = await fetch(`${getBackendBaseURL()}/api/schedules`, {
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error(`Failed to load schedules: ${res.status} ${res.statusText}`);
  }
  const data = (await res.json()) as Schedule[];
  return Array.isArray(data) ? data : [];
}

export default function SchedulesPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchSchedules()
      .then((schedules) => {
        if (cancelled) return;
        setState(schedules.length === 0 ? { kind: "empty" } : { kind: "ok", schedules });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        setState({ kind: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex size-full flex-col">
      {/* Page header */}
      <div className="flex items-center justify-between border-b px-6 py-4">
        <div>
          <h1 className="text-xl font-semibold">Schedules</h1>
          <p className="text-muted-foreground mt-0.5 text-sm">
            Cron tasks and one-shot reminders managed by the agent. Read-only
            view; create / edit / pause UI ships in a follow-up.
          </p>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6" data-testid="schedules-list-root">
        {state.kind === "loading" && (
          <div className="text-muted-foreground flex h-40 items-center justify-center gap-2 text-sm">
            <Loader2Icon className="h-4 w-4 animate-spin" />
            Loading schedules...
          </div>
        )}

        {state.kind === "error" && (
          <div className="flex h-64 flex-col items-center justify-center gap-2 text-center">
            <p className="font-medium text-red-600">Failed to load schedules</p>
            <p className="text-muted-foreground text-sm">{state.message}</p>
          </div>
        )}

        {state.kind === "empty" && (
          <div
            className="flex h-64 flex-col items-center justify-center gap-3 text-center"
            data-testid="schedules-empty-state"
          >
            <div className="bg-muted flex h-14 w-14 items-center justify-center rounded-full">
              <CalendarIcon className="text-muted-foreground h-7 w-7" />
            </div>
            <div>
              <p className="font-medium">No schedules yet</p>
              <p className="text-muted-foreground mt-1 text-sm">
                Ask the agent in chat to set one up — e.g. &ldquo;remind me
                every weekday at 9am to review yesterday&rsquo;s PRs&rdquo;.
              </p>
            </div>
          </div>
        )}

        {state.kind === "ok" && (
          <div className="flex flex-col gap-2" data-testid="schedules-list">
            {state.schedules.map((s) => (
              <div
                key={s.id}
                className="flex items-center justify-between gap-4 rounded-md border px-4 py-3"
                data-testid="schedules-list-item"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{s.title}</div>
                  <div className="text-muted-foreground mt-0.5 text-xs">
                    {s.kind === "cron" ? "Cron" : "One-shot"} · status: {s.status}
                  </div>
                </div>
                <div className="text-muted-foreground shrink-0 text-xs">
                  Next fire: {formatDateTime(s.next_fire_at)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
