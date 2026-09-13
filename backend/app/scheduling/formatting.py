"""Format chat-push messages for schedule run results.

Pure, sync helpers used by the executor (Task 7) and the REST router
(Task 8) to render a run result into the text pushed back to a chat
channel (Feishu / IM). No DB, no async — easy to unit test.

The format is deliberately minimal:

* success → ``📅 <title>（尝试 <attempt>）\\n<body>`` (+ run_url)
* retry   → ``🔄 重试 (<attempt>/3)\\n📅 <title>\\n<body>`` (+ run_url)
* failed  → ``❌ <title> 失败：<body>`` (+ run_url)
* generic → ``📅 <title>\\n<body>`` (+ run_url)

If the rendered text exceeds ``max_length`` it is truncated with the
suffix ``…(内容截断)…`` so the receiver can see that the message was
cut. The run_url is preserved on success even after truncation, since
the link to the run is the most important call-to-action; on
truncation of retry/failed we drop the link because the prefix already
carries the failure context.
"""

from __future__ import annotations

from typing import Any

# Truncation marker (Chinese: "content truncated"). Kept short so it
# fits in the budget after a long body is cut.
_TRUNCATE_SUFFIX = "…(内容截断)…"


def format_push_message(
    schedule: Any,
    attempt: int,
    status: str | None,
    body: str,
    run_url: str | None,
    max_length: int,
) -> str:
    """Render a run result into the text pushed to a chat channel.

    Parameters
    ----------
    schedule:
        Object exposing ``.title``. A ``Schedule`` instance or a duck-type
        (e.g. the ``_FakeSchedule`` used in the unit tests).
    attempt:
        1-based attempt number; rendered into the success and retry headers.
    status:
        One of ``"success"``, ``"retry"``, ``"failed"`` or ``None``. Any
        other value falls back to the generic header.
    body:
        Free-form body text (the run output for success/retry, the error
        summary for failed).
    run_url:
        Optional link to the run page; appended to success, retry, and
        failed messages when present.
    max_length:
        Hard cap on the returned text. Truncation is applied only when
        the natural rendering exceeds this cap; the truncation marker
        replaces the tail of the body.
    """
    title: str = getattr(schedule, "title", "")

    if status == "success":
        prefix = f"📅 {title}（尝试 {attempt}）\n"
        render_body = body
    elif status == "retry":
        prefix = f"🔄 重试 ({attempt}/3)\n📅 {title}\n"
        render_body = body
    elif status == "failed":
        # Failed: the body *is* the error reason; keep it inline with the
        # header so a single glance at the line gives the operator
        # everything they need to triage.
        prefix = f"❌ {title} 失败：{body}\n"
        render_body = ""
    else:  # None / "generic" / anything unexpected
        prefix = f"📅 {title}\n"
        render_body = body

    # Assemble the body + (optional) run_url line. ``render_body`` may be
    # empty for the failed path (the body was already inlined into the
    # header), in which case we just emit the prefix.
    parts: list[str] = [prefix.rstrip("\n")]
    if render_body:
        parts.append(render_body)
    if run_url:
        parts.append(run_url)
    text = "\n".join(parts)

    # Truncation: if the natural rendering exceeds ``max_length`` we
    # keep the head and append the truncation marker. On success we
    # preserve the run_url (the link to the run is the most important
    # call-to-action after a success); on other statuses the prefix
    # already carries enough context so we drop the link.
    if len(text) > max_length:
        if run_url and status == "success":
            suffix = f"{_TRUNCATE_SUFFIX}\n{run_url}"
        else:
            suffix = _TRUNCATE_SUFFIX
        keep = max(0, max_length - len(suffix))
        text = text[:keep] + suffix

    return text
