"""Minimal local web UI (standard library only).

Security posture (tested): binds to 127.0.0.1 only; rejects any Host header that is not the exact local address (DNS-rebinding
defence); every state-changing request is a POST with a per-session CSRF token and a same-origin check; bodies are size-limited;
all dynamic text is HTML-escaped; a strict Content-Security-Policy is sent; artifact names are allow-listed; there is no way to
read arbitrary server paths (brownfield `--context` is CLI-only on purpose). It is a single-user tool, not a hardened web app.
"""

import hmac
import html
import re
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

from copilot.audit import verify
from copilot.cli import system_clock
from copilot.engine import Engine, GateError, OutboundConfirmationRequired, ProviderFailure
from copilot.models import Action, Decision, Session
from copilot.providers import Provider, create_provider
from copilot.stages import STAGE_BY_NUMBER, STAGES
from copilot.store import SessionStore, StoreError

MAX_BODY = 65_536
ARTIFACT_RE = re.compile(r"^(?:\d\d_[a-z]+\.md|SUMMARY\.md|00_brief\.md|audit\.jsonl)$")
SESSION_PATH_RE = re.compile(r"^/s/([A-Za-z0-9_-]{1,64})(?:/(propose|decide)|/a/([A-Za-z0-9_.-]{1,80}))?$")
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
STYLE = "body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}pre{white-space:pre-wrap;background:#f4f4f4;padding:.75rem;border-radius:4px}textarea{width:100%;min-height:8rem}.warn{background:#fff3cd;padding:.5rem}.err{background:#f8d7da;padding:.5rem}.ok{color:#146c2e}"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


class CopilotServer(HTTPServer):
    def __init__(self, address: tuple[str, int], store: SessionStore, provider: Provider) -> None:
        super().__init__(address, Handler)
        self.store = store
        self.provider = provider
        self.csrf_key = secrets.token_bytes(32)
        self.allowed_hosts = {f"127.0.0.1:{self.server_address[1]}", f"localhost:{self.server_address[1]}"}

    def token(self, scope: str) -> str:
        return hmac.new(self.csrf_key, scope.encode(), "sha256").hexdigest()

    def engine(self) -> Engine:
        return Engine(self.provider, self.store, clock=system_clock)


def page(title: str, body: str, status_note: str = "") -> bytes:
    return (
        f"<!doctype html><html lang=en><meta charset=utf-8><title>{esc(title)}</title><style>{STYLE}</style>"
        f"<h1>{esc(title)}</h1>{status_note}{body}</html>"
    ).encode()


class Handler(BaseHTTPRequestHandler):
    server: CopilotServer
    server_version = "CopilotUI"
    sys_version = ""

    def log_message(
        self, format: str, *args: object
    ) -> None:  # keep the console quiet and free of request data
        return None

    # ----- plumbing -------------------------------------------------------------------------------------------

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        for name, value in {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            **(extra or {}),
        }.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str) -> None:
        self._send(status, page("Error", f'<p class="err">{esc(message)}</p><p><a href="/">Home</a></p>'))

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _form(self) -> dict[str, str] | None:
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            self._fail(415, "unsupported content type")
            return None
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlsplit(origin).netloc not in self.server.allowed_hosts:
            self._fail(403, "cross-origin request refused")
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._fail(411, "length required")
            return None
        if not 0 <= length <= MAX_BODY:
            self._fail(413, "request too large")
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True, max_num_fields=60)
        return {k: v[0] for k, v in parsed.items()}

    def _csrf_ok(self, form: dict[str, str], scope: str) -> bool:
        if not hmac.compare_digest(form.get("csrf", ""), self.server.token(scope)):
            self._fail(403, "invalid or missing CSRF token (reload the page)")
            return False
        return True

    # ----- routing --------------------------------------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._fail(400, "unexpected Host header")
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            return self._home()
        match = SESSION_PATH_RE.fullmatch(path)
        if not match or match.group(2):
            return self._fail(404, "not found")
        session = self._load(match.group(1))
        if session is None:
            return None
        if match.group(3):
            return self._artifact(session.id, match.group(3))
        return self._session_page(session)

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._fail(400, "unexpected Host header")
        path = urllib.parse.urlsplit(self.path).path
        if path == "/new":
            form = self._form()
            if form is None or not self._csrf_ok(form, "new"):
                return None
            return self._new(form.get("brief", ""))
        match = SESSION_PATH_RE.fullmatch(path)
        if not match or not match.group(2):
            return self._fail(404, "not found")
        session = self._load(match.group(1))
        form = self._form() if session else None
        if session is None or form is None or not self._csrf_ok(form, session.id):
            return None
        return self._propose(session, form) if match.group(2) == "propose" else self._decide(session, form)

    def _load(self, session_id: str) -> Session | None:
        try:
            return self.server.store.load(session_id)
        except StoreError:
            self._fail(404, "unknown session")
            return None

    # ----- pages ----------------------------------------------------------------------------------------------

    def _home(self) -> None:
        ids = self.server.store.list_sessions()
        items = "".join(f'<li><a href="/s/{esc(i)}">{esc(i)}</a></li>' for i in ids) or "<li>none yet</li>"
        external = self.server.provider.is_external
        provider_name = esc(self.server.provider.name)
        note = (
            '<p class="warn">External provider: every stage asks you to confirm exactly what will be sent.</p>'
            if external
            else f"<p>Provider: <code>{provider_name}</code> (runs locally, nothing leaves this machine).</p>"
        )
        body = (
            f'{note}<form method=post action="/new"><input type=hidden name=csrf value="{self.server.token("new")}">'
            "<p><label>Brief (max 20,000 characters)<br><textarea name=brief maxlength=20000 required></textarea></label></p>"
            "<p>Secrets and personal data are redacted before anything is stored or sent. Brownfield context is CLI-only.</p>"
            f"<button>Start session</button></form><h2>Sessions</h2><ul>{items}</ul>"
        )
        self._send(200, page("Engineering Copilot", body))

    def _new(self, brief: str) -> None:
        try:
            session = self.server.engine().start(brief)
        except (ValueError, GateError) as exc:
            return self._fail(400, str(exc))
        self._redirect(f"/s/{session.id}")

    def _redirect(self, location: str) -> None:
        self._send(303, b"", extra={"Location": location})

    def _artifact(self, session_id: str, name: str) -> None:
        if not ARTIFACT_RE.fullmatch(name):
            return self._fail(404, "not found")
        try:
            text = self.server.store.read_artifact(session_id, name)
        except StoreError:
            return self._fail(404, "not found")
        self._send(200, text.encode(), "text/plain; charset=utf-8")

    def _session_page(self, session: Session, message: str = "", preview_html: str = "") -> None:
        token = self.server.token(session.id)
        stage = session.current_stage
        rows = []
        for spec in STAGES:
            record = session.records.get(spec.number)
            done = record is not None and record.final_text is not None
            link = (
                f' (<a href="/s/{esc(session.id)}/a/{spec.number:02d}_{spec.key}.md">view</a>)'
                if done
                else ""
            )
            mark = (
                "done"
                if done
                else ("current" if spec.number == stage and session.status != "complete" else "pending")
            )
            rows.append(f"<li>{spec.number}. {esc(spec.title)}: {mark}{link}</li>")
        body = f"<p>Status: <b>{esc(session.status)}</b>, provider <code>{esc(session.provider)}</code></p><ol>{''.join(rows)}</ol>"
        if session.injection_flags:
            body += f'<p class="warn">The brief contains instruction-like phrases ({esc(", ".join(session.injection_flags))}); treated as data.</p>'
        if session.redaction_summary:
            body += f'<p class="warn">Redacted before storing/sending: {esc(session.redaction_summary)}</p>'
        body += preview_html or self._current(session, token)
        sid = esc(session.id)
        body += f'<p><a href="/s/{sid}/a/00_brief.md">brief</a> | <a href="/s/{sid}/a/audit.jsonl">audit log</a> | <a href="/">home</a></p>'
        note = f'<p class="err">{esc(message)}</p>' if message else ""
        self._send(200, page(f"Session {session.id}", body, note))

    def _current(self, session: Session, token: str) -> str:
        sid, stage = esc(session.id), session.current_stage
        if session.status == "complete":
            return f'<p class="ok">All seven stages are complete.</p><p><a href="/s/{sid}/a/SUMMARY.md">SUMMARY.md</a></p>'
        if session.status == "aborted":
            return "<p>This session was aborted.</p>"
        spec = STAGE_BY_NUMBER[stage]
        record = session.record(stage)
        head = f"<h2>Stage {stage}/7: {esc(spec.title)}</h2><p>{esc(spec.purpose)}</p>"
        if session.status == "awaiting_proposal":
            if record.rejections >= 3:
                return head + (
                    f'<form method=post action="/s/{sid}/decide"><input type=hidden name=csrf value="{token}"><input type=hidden name=action value=modify>'
                    "<p class=warn>Too many rejections: write this stage yourself. Required headings: "
                    f"{esc(', '.join(spec.required_sections))}</p><textarea name=text required></textarea><button>Save my text</button></form>"
                )
            return head + (
                f'<form method=post action="/s/{sid}/propose"><input type=hidden name=csrf value="{token}">'
                "<button>Ask the provider for a proposal</button></form>"
            )
        proposal = record.proposals[-1]
        out = head + f"<h3>Proposal (revision {proposal.revision})</h3><pre>{esc(proposal.text)}</pre>"
        out += f"<p>Redacted before sending: {esc(proposal.redaction_in)}; found in the answer: {esc(proposal.redaction_out)}</p>"
        if proposal.missing_sections:
            out += f'<p class="warn">Missing sections (you cannot accept as-is): {esc(", ".join(proposal.missing_sections))}</p>'
        questions = "".join(
            f'<p><label>{esc(q)}: {esc(text)}<br><input name="answer_{esc(q)}" size=80 placeholder="your answer, or ASSUME"></label></p>'
            for q, text in proposal.open_questions.items()
        )
        return out + (
            f'<form method=post action="/s/{sid}/decide"><input type=hidden name=csrf value="{token}">{questions}'
            "<p><label><input type=radio name=action value=accept checked> accept</label> "
            "<label><input type=radio name=action value=modify> modify (append your text)</label> "
            "<label><input type=radio name=action value=reject> reject (give a reason)</label></p>"
            "<p><label>Your text to append / reason for rejecting<br><textarea name=text></textarea></label></p>"
            "<button>Submit decision</button></form>"
        )

    # ----- actions --------------------------------------------------------------------------------------------

    def _propose(self, session: Session, form: dict[str, str]) -> None:
        engine = self.server.engine()
        try:
            engine.propose(session, confirm_outbound=form.get("confirm") == "yes")
        except OutboundConfirmationRequired as needed:
            p = needed.preview
            preview = (
                f"<h2>Outbound preview: stage {p.stage}</h2><p class=warn>This exact text will be sent to the external provider "
                f"<code>{esc(p.provider)}</code>. Redacted (counts only): {esc(p.redaction)}. Pattern-based redaction misses names and "
                f"free-text personal data.</p><pre>{esc(p.system)}\n---\n{esc(p.user)}</pre>"
                f'<form method=post action="/s/{esc(session.id)}/propose"><input type=hidden name=csrf value="{self.server.token(session.id)}">'
                "<input type=hidden name=confirm value=yes><button>Send exactly this</button></form>"
            )
            return self._session_page(session, preview_html=preview)
        except ProviderFailure as exc:
            return self._session_page(self.server.store.load(session.id), f"Provider failed: {exc}")
        except GateError as exc:
            return self._session_page(self.server.store.load(session.id), str(exc))
        self._redirect(f"/s/{session.id}")

    def _decide(self, session: Session, form: dict[str, str]) -> None:
        raw_action = form.get("action", "")
        if raw_action not in {"accept", "modify", "reject"}:
            return self._fail(400, "unknown action")
        action = cast(Action, raw_action)
        text = form.get("text", "").strip()
        answers = {k[len("answer_") :]: v for k, v in form.items() if k.startswith("answer_")}
        decision = Decision(
            session.current_stage,
            action,
            notes=text if action == "reject" else "",
            answers=answers,
            text=text if (action == "modify" and session.status == "awaiting_proposal") else None,
            append=text if (action == "modify" and session.status == "awaiting_decision" and text) else None,
        )
        try:
            self.server.engine().decide(session, decision)
        except GateError as exc:
            return self._session_page(self.server.store.load(session.id), f"Blocked: {exc}")
        self._redirect(f"/s/{session.id}")


def make_server(out: Path, provider: Provider, port: int = 0) -> CopilotServer:
    return CopilotServer(("127.0.0.1", port), SessionStore(out), provider)


def serve(out: Path, provider_name: str, port: int) -> None:
    server = make_server(out, create_provider(provider_name), port)
    print(f"Engineering Copilot UI: http://127.0.0.1:{server.server_address[1]}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


__all__ = ["CopilotServer", "make_server", "serve", "verify"]
