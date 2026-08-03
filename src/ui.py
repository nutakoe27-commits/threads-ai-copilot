"""Локальная панель: письма, воронка, логи.

ЗАЧЕМ. Читать утренний список файлом можно, но отмечать статусы командой
в терминале — то, что бросают через две недели. Панель убирает это трение:
письмо копируется кнопкой, статус ставится кнопкой.

И вторая половина — логи. Файл на тысячу строк не читают. Отфильтрованные
последние записи с цветом по уровню — читают.

УСТРОЙСТВО. Только стандартная библиотека, ни одной зависимости. Сервер
слушает 127.0.0.1, то есть доступен только с этого компьютера. Это не
осторожность ради осторожности: здесь лежат письма, контакты компаний
и ключи в логах, и выставлять это наружу незачем.

Запуск:  python3 run.py --stage ui
Открыть: http://127.0.0.1:8765
"""

from __future__ import annotations

import html as html_module
import json
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import db, log, metrics, outreach, scoring

logger = log.get("ui")

HOST = "127.0.0.1"
PORT = 8765

STYLE = """
:root {
  --bg: #14161a; --panel: #1b1e24; --line: #2a2f38; --text: #e6e8ec;
  --dim: #9aa2b1; --accent: #6ea8fe; --ok: #7ee787; --warn: #f0c674;
  --err: #ff7b72;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif;
}
header {
  position: sticky; top: 0; z-index: 10; background: var(--panel);
  border-bottom: 1px solid var(--line); padding: 12px 20px;
  display: flex; gap: 18px; align-items: center; flex-wrap: wrap;
}
header b { font-size: 16px; }
nav a {
  color: var(--dim); text-decoration: none; padding: 5px 10px; border-radius: 6px;
}
nav a:hover { background: var(--line); color: var(--text); }
nav a.on { background: var(--accent); color: #10131a; font-weight: 600; }
main { padding: 20px; max-width: 1100px; margin: 0 auto; }
h2 { font-size: 18px; margin: 26px 0 12px; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px 18px; margin-bottom: 14px;
}
.muted { color: var(--dim); font-size: 13px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.tiles { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
.tile {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 16px; min-width: 130px;
}
.tile .n { font-size: 26px; font-weight: 600; }
.tile .l { color: var(--dim); font-size: 12px; }
pre.letter {
  background: #0e1014; border: 1px solid var(--line); border-radius: 8px;
  padding: 14px; white-space: pre-wrap; font: 14px/1.6 "SF Mono", Menlo, monospace;
  margin: 10px 0;
}
button, .btn {
  background: var(--line); color: var(--text); border: 1px solid #39404c;
  border-radius: 7px; padding: 6px 12px; cursor: pointer; font-size: 13px;
}
button:hover { background: #333a45; }
button.primary { background: var(--accent); color: #10131a; border-color: var(--accent); }
button.danger { background: #3a2226; border-color: #5a2c33; color: var(--err); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--dim); font-weight: 500; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.lvl-ERROR { color: var(--err); }
.lvl-WARNING { color: var(--warn); }
.lvl-INFO { color: var(--text); }
.lvl-DEBUG { color: var(--dim); }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 20px;
  font-size: 12px; background: var(--line); color: var(--dim);
}
.badge.hot { background: #2a3a2a; color: var(--ok); }
.score { font-size: 20px; font-weight: 600; color: var(--accent); }
"""

SCRIPT = """
function copyLetter(id) {
  const el = document.getElementById(id);
  navigator.clipboard.writeText(el.innerText).then(() => {
    const b = document.getElementById(id + '-btn');
    const was = b.innerText; b.innerText = 'скопировано'; b.classList.add('primary');
    setTimeout(() => { b.innerText = was; b.classList.remove('primary'); }, 1400);
  });
}
function mark(inn, status) {
  fetch('/mark', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({inn: inn, status: status})
  }).then(r => r.json()).then(() => location.reload());
}
"""


def esc(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""))


def page(title: str, active: str, body: str, refresh: int = 0) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    tabs = [("/", "Письма"), ("/stats", "Воронка"), ("/logs", "Логи")]
    nav = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{name}</a>'
        for href, name in tabs
    )
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{esc(title)}</title><style>{STYLE}</style></head><body>
<header><b>GTM-система</b><nav>{nav}</nav>
<span class="muted">{date.today().isoformat()}</span></header>
<main>{body}</main><script>{SCRIPT}</script></body></html>""".encode("utf-8")


def letters_page() -> bytes:
    """Главная: письма, готовые к отправке, и кнопки статусов."""
    conn = db.connect()

    drafts = conn.execute(
        """
        SELECT t.id, t.inn, t.step, t.subject, t.body, t.why,
               c.name, c.contact_email, c.site_url, c.site_type,
               c.signal_score, c.signal_reasons, c.staff, c.revenue
        FROM touches t JOIN companies c ON c.inn = t.inn
        WHERE t.status = 'draft'
        ORDER BY COALESCE(c.signal_score, 0) DESC, t.step, c.name
        """
    ).fetchall()

    statuses = outreach.summary(conn)
    conn.close()

    tiles = "".join(
        f'<div class="tile"><div class="n">{count}</div>'
        f'<div class="l">{esc(name)}</div></div>'
        for name, count in sorted(statuses.items())
    )
    parts = [f'<div class="tiles"><div class="tile"><div class="n">{len(drafts)}</div>'
             f'<div class="l">готово к отправке</div></div>{tiles}</div>']

    if not drafts:
        parts.append(
            '<div class="card"><b>Писем нет.</b><p class="muted">'
            'Соберите их по порядку:<br>'
            '<code>python3 run.py --stage targets</code><br>'
            '<code>python3 run.py --stage dossier</code><br>'
            '<code>python3 run.py --stage compose</code></p></div>')
        return page("Письма", "/", "".join(parts))

    for row in drafts:
        letter_id = f"letter-{row['id']}"
        try:
            reasons = json.loads(row["signal_reasons"] or "[]")
        except (json.JSONDecodeError, TypeError):
            reasons = []

        badges = "".join(f'<span class="badge{" hot" if index == 0 else ""}">{esc(r)}</span> '
                         for index, r in enumerate(reasons[:4]))
        step_label = "первое письмо" if row["step"] == 1 else f"дожим {row['step'] - 1}"
        facts = []
        if row["staff"]:
            facts.append(f"{row['staff']:.0f} чел.")
        if row["revenue"]:
            facts.append(f"{row['revenue'] / 1e6:.0f} млн ₽")
        if row["site_type"]:
            facts.append(esc(row["site_type"]))

        parts.append(f"""<div class="card">
<div class="row" style="justify-content:space-between">
  <div><b>{esc(row['name'])}</b>
    <span class="muted"> — {step_label} · {' · '.join(facts)}</span></div>
  <div class="score">{row['signal_score'] or 0:.1f}</div>
</div>
<div style="margin:8px 0">{badges}</div>
<div class="muted">{esc(row['why'] or '')}</div>
<div class="row" style="margin-top:8px">
  {'<a class="btn" href="' + esc(row['site_url']) + '" target="_blank">сайт</a>' if row['site_url'] else ''}
  <span class="muted">{esc(row['contact_email'] or 'почты нет')}</span>
  <span class="muted">ИНН {esc(row['inn'])}</span>
</div>
<div style="margin-top:10px"><b>Тема:</b> {esc(row['subject'])}</div>
<pre class="letter" id="{letter_id}">{esc(row['body'])}</pre>
<div class="row">
  <button id="{letter_id}-btn" onclick="copyLetter('{letter_id}')">копировать письмо</button>
  <button class="primary" onclick="mark('{row['inn']}','sent')">отправлено</button>
  <button onclick="mark('{row['inn']}','replied')">ответили</button>
  <button onclick="mark('{row['inn']}','meeting')">договорились</button>
  <button class="danger" onclick="mark('{row['inn']}','refused')">просили не писать</button>
</div></div>""")

    return page("Письма", "/", "".join(parts))


def stats_page() -> bytes:
    conn = db.connect()
    report = metrics.report(conn, days=14)
    table = metrics.daily_table(conn, days=14)
    drops = metrics.check_drops(conn)
    conn.close()

    warning = ""
    if drops:
        items = "".join(f"<li>{esc(d)}</li>" for d in drops)
        warning = (f'<div class="card" style="border-color:#5a2c33">'
                   f'<b class="lvl-ERROR">Конверсия упала</b><ul>{items}</ul>'
                   f'<p class="muted">Так выглядит деградация источника. '
                   f'Что делать — в RUNBOOK.md, раздел 6.</p></div>')

    body = (warning
            + f'<div class="card"><pre class="letter">{esc(report)}</pre></div>'
            + (f'<div class="card"><pre class="letter">{esc(table)}</pre></div>'
               if table else ""))
    return page("Воронка", "/stats", body)


def logs_page(level: str = "INFO", stage: str = "") -> bytes:
    rows = log.recent(limit=300, level=level, stage=stage)

    conn = db.connect()
    stages = [r["stage"] for r in conn.execute(
        "SELECT DISTINCT stage FROM log_entries WHERE stage IS NOT NULL "
        "ORDER BY stage").fetchall()]
    conn.close()

    def link(href: str, name: str, on: bool) -> str:
        highlight = ' style="background:#6ea8fe;color:#10131a"' if on else ""
        return f'<a class="btn" href="{href}"{highlight}>{esc(name)}</a>'

    filters = " ".join(link(f"/logs?level={lv}", lv, lv == level)
                       for lv in ("DEBUG", "INFO", "WARNING", "ERROR"))
    stage_links = " ".join(
        [link(f"/logs?level={level}", "все этапы", not stage)]
        + [link(f"/logs?level={level}&stage={urllib.parse.quote(s)}", s, s == stage)
           for s in stages])

    lines = "".join(
        f'<tr><td class="muted">{esc(r["ts"][11:19])}</td>'
        f'<td class="lvl-{esc(r["level"])}">{esc(r["level"])}</td>'
        f'<td class="muted">{esc(r["stage"])}</td>'
        f'<td class="muted">{esc(r["source"])}</td>'
        f'<td class="lvl-{esc(r["level"])}">{esc(r["message"])}</td></tr>'
        for r in rows)

    body = f"""<div class="card"><div class="row">{filters}</div>
<div class="row" style="margin-top:8px">{stage_links}</div></div>
<div class="card"><table><tr><th>время</th><th>уровень</th><th>этап</th>
<th>модуль</th><th>сообщение</th></tr>{lines}</table>
<p class="muted">Показаны последние 300 записей. Полная история —
в файле logs/{date.today().isoformat()}.log</p></div>"""
    return page("Логи", "/logs", body, refresh=10)


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send(letters_page())
            elif parsed.path == "/stats":
                self._send(stats_page())
            elif parsed.path == "/logs":
                self._send(logs_page(query.get("level", ["INFO"])[0],
                                     query.get("stage", [""])[0]))
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001 — панель не должна падать целиком
            logger.exception("Ошибка страницы %s", parsed.path)
            self._send(page("Ошибка", "/",
                            f'<div class="card"><b class="lvl-ERROR">Страница '
                            f'не собралась</b><pre class="letter">{esc(exc)}</pre>'
                            f'<p class="muted">Подробности в логах.</p></div>'))

    def do_POST(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/mark":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            outreach.mark(payload.get("status", ""), [payload.get("inn", "")])
            self._send(json.dumps({"ok": True}).encode(), "application/json")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось отметить статус")
            self._send(json.dumps({"ok": False, "error": str(exc)}).encode(),
                       "application/json")

    def log_message(self, fmt: str, *args: Any) -> None:
        # Стандартный вывод сервера идёт в stderr и мешает. Уводим в отладку.
        logger.debug("HTTP %s", fmt % args)


def serve(host: str = HOST, port: int = PORT,
          config: dict[str, Any] | None = None) -> None:
    """Запускает панель. Останавливается по Ctrl+C."""
    settings = (config or {}).get("ui", {})
    host = settings.get("host", host)
    port = int(settings.get("port", port))
    server = ThreadingHTTPServer((host, port), Handler)
    logger.info("Панель открыта: http://%s:%d", host, port)
    logger.info("Остановить — Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Панель остановлена")
    finally:
        server.server_close()
