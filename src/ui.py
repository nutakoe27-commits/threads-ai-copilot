"""Локальная панель: запуск этапов, письма, воронка, настройки, логи.

ЗАЧЕМ. Раньше панель только показывала результат, а управление жило
в терминале. Это две разные привычки, и вторая отмирает первой: через
две недели человек перестаёт открывать терминал, а вместе с ним перестаёт
отмечать статусы и править настройки. Поэтому управление переехало сюда
целиком (DECISIONS.md, Р-050).

Что можно делать из браузера:

  Прогон     — запустить любой этап, видеть прогресс и живой лог,
               остановить на полпути.
  Письма     — прочитать, скопировать кнопкой, отметить статус.
  Воронка    — конверсии по дням и предупреждение о падении.
  Настройки  — править критерии отбора и все тексты писем, с проверкой
               перед записью и копией предыдущей версии.
  Логи       — последние записи с фильтром по уровню и этапу.

УСТРОЙСТВО. Только стандартная библиотека, ни одной зависимости. Сервер
слушает 127.0.0.1, то есть доступен только с этого компьютера. Это не
осторожность ради осторожности: отсюда запускаются прогоны и правятся
настройки, а пароля у панели нет — он и не нужен, пока её не видно снаружи.

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

from . import db, jobs, log, metrics, outreach, pipeline, settings

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
h2:first-child { margin-top: 0; }
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
pre.letter, pre.tail {
  background: #0e1014; border: 1px solid var(--line); border-radius: 8px;
  padding: 14px; white-space: pre-wrap; font: 14px/1.6 "SF Mono", Menlo, monospace;
  margin: 10px 0;
}
pre.tail { max-height: 340px; overflow-y: auto; font-size: 13px; }
button, .btn {
  background: var(--line); color: var(--text); border: 1px solid #39404c;
  border-radius: 7px; padding: 6px 12px; cursor: pointer; font-size: 13px;
  text-decoration: none; display: inline-block;
}
button:hover, .btn:hover { background: #333a45; }
button:disabled { opacity: .45; cursor: default; }
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

/* --- прогон --- */
.stage {
  display: flex; gap: 14px; align-items: flex-start;
  padding: 12px 0; border-bottom: 1px solid var(--line);
}
.stage:last-child { border-bottom: none; }
.stage .who { flex: 1; min-width: 220px; }
.stage .who b { display: block; }
.bar {
  height: 10px; background: #0e1014; border: 1px solid var(--line);
  border-radius: 6px; overflow: hidden; margin: 10px 0 6px;
}
.bar > i {
  display: block; height: 100%; width: 0;
  background: linear-gradient(90deg, #4f7fd4, var(--accent));
  transition: width .35s ease;
}
.bar.done > i { background: var(--ok); }
.bar.failed > i { background: var(--err); }
.bar.cancelled > i { background: var(--warn); }
textarea {
  width: 100%; min-height: 460px; background: #0e1014; color: var(--text);
  border: 1px solid var(--line); border-radius: 8px; padding: 12px;
  font: 13px/1.6 "SF Mono", Menlo, monospace; resize: vertical;
}
select {
  background: #0e1014; color: var(--text); border: 1px solid var(--line);
  border-radius: 7px; padding: 6px 10px; font-size: 14px;
}
.note { padding: 10px 12px; border-radius: 8px; margin: 10px 0; font-size: 14px; }
.note.ok { background: #1c2b1e; color: var(--ok); }
.note.bad { background: #3a2226; color: var(--err); }
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
  fetch('/api/mark', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({inn: inn, status: status})
  }).then(r => r.json()).then(() => location.reload());
}

/* --- страница прогона --- */
let polling = null;

function runStage(stage, dry) {
  fetch('/api/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stage: stage, dry_run: !!dry})
  }).then(r => r.json()).then(data => {
    if (!data.ok) { alert(data.error); return; }
    poll();
  });
}
function stopJob() {
  fetch('/api/stop', {method: 'POST'}).then(() => poll());
}
function tick() {
  fetch('/api/job').then(r => r.json()).then(render);
}
function poll() {
  tick();
  if (!polling) polling = setInterval(tick, 1000);
}
function render(job) {
  const box = document.getElementById('job');
  if (!box) return;
  if (!job || !job.stage) {
    box.innerHTML = '<p class="muted">Прогонов ещё не было. ' +
      'Нажмите этап ниже — здесь появится прогресс и живой лог.</p>';
    if (polling) { clearInterval(polling); polling = null; }
    return;
  }
  const running = job.status === 'running';
  if (!running && polling) { clearInterval(polling); polling = null; }

  const words = {running: 'идёт', done: 'готово', failed: 'ошибка',
                 cancelled: 'остановлено'};
  const counted = job.total ? (job.done + ' из ' + job.total) : '';
  const percent = job.total ? job.percent : (running ? 8 : 100);

  const head = '<div class="row" style="justify-content:space-between">' +
    '<b>' + esc(job.title) + (job.dry_run ? ' — проба' : '') + '</b>' +
    '<span class="muted">' + words[job.status] + ' · ' + job.seconds + ' с</span></div>';

  const bar = '<div class="bar ' + (running ? '' : job.status) + '"><i style="width:' +
    percent + '%"></i></div>' +
    '<div class="muted">' + counted + (job.label ? ' · ' + esc(job.label) : '') + '</div>';

  const tail = '<pre class="tail" id="tail">' + job.lines.map(esc).join('\\n') + '</pre>';

  let foot = '';
  if (running) {
    foot = '<button class="danger" onclick="stopJob()">остановить</button>' +
      '<span class="muted"> прервётся на следующей компании</span>';
  } else if (job.error) {
    foot = '<div class="note bad">' + esc(job.error) + '</div>';
  } else if (Object.keys(job.result || {}).length) {
    foot = '<div class="note ok">' + Object.keys(job.result)
      .map(k => esc(k) + ': ' + esc(job.result[k])).join(' · ') + '</div>';
  }

  box.innerHTML = head + bar + tail + foot;
  const t = document.getElementById('tail');
  if (t && running) t.scrollTop = t.scrollHeight;

  const buttons = document.querySelectorAll('button.stage-btn');
  for (let i = 0; i < buttons.length; i++) buttons[i].disabled = running;
}
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* --- страница настроек --- */
function saveSettings() {
  const key = document.getElementById('file-key').value;
  const text = document.getElementById('editor').value;
  fetch('/api/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: key, text: text})
  }).then(r => r.json()).then(data => {
    const box = document.getElementById('save-note');
    if (data.ok) {
      box.className = 'note ok';
      box.innerText = 'Сохранено. Копия предыдущей версии: ' +
        (data.backup || 'файла раньше не было');
    } else {
      box.className = 'note bad';
      box.innerText = 'Не сохранено, файл на диске не тронут. ' + data.error;
    }
  });
}
"""


def esc(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""))


TABS = [("/", "Прогон"), ("/letters", "Письма"), ("/stats", "Воронка"),
        ("/settings", "Настройки"), ("/logs", "Логи")]


def page(title: str, active: str, body: str, refresh: int = 0,
         onload: str = "") -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    nav = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{name}</a>'
        for href, name in TABS
    )
    boot = f"<script>{onload}</script>" if onload else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{esc(title)}</title><style>{STYLE}</style></head><body>
<header><b>GTM-система</b><nav>{nav}</nav>
<span class="muted">{date.today().isoformat()}</span></header>
<main>{body}</main><script>{SCRIPT}</script>{boot}</body></html>""".encode("utf-8")


# ------------------------------------------------------------------ прогон

def run_page() -> bytes:
    """Главная: кнопки этапов, прогресс и живой лог."""
    conn = db.connect()
    counts = dict(conn.execute(
        "SELECT COALESCE(icp_status,'—'), COUNT(*) FROM companies GROUP BY 1"
    ).fetchall())
    drafts = conn.execute(
        "SELECT COUNT(*) FROM touches WHERE status = 'draft'").fetchone()[0]
    conn.close()

    tiles = "".join(
        f'<div class="tile"><div class="n">{value}</div>'
        f'<div class="l">{esc(name)}</div></div>'
        for name, value in (("компаний в базе", sum(counts.values())),
                            ("прошли отбор", counts.get("passed", 0)),
                            ("писем готово", drafts)))

    stages = []
    for item in pipeline.STAGES:
        costs = f' · тратит: {esc(item["costs"])}' if item["costs"] else ""
        stages.append(f"""<div class="stage">
  <div class="who"><b>{esc(item['title'])}</b>
    <span class="muted">{esc(item['detail'])}</span>
    <div class="muted">{esc(item['minutes'])}{costs}</div></div>
  <div class="row">
    <button class="primary stage-btn"
            onclick="runStage('{item['key']}', false)">запустить</button>
    <button class="stage-btn" onclick="runStage('{item['key']}', true)"
            title="выполнить всё, кроме записи в базу">проба</button>
  </div>
</div>""")

    body = f"""<div class="tiles">{tiles}</div>
<div class="card"><h2>Ход прогона</h2><div id="job"></div></div>
<div class="card"><h2>Этапы</h2>
<p class="muted">Порядок сверху вниз — это и есть порядок ежедневной работы.
«Проба» делает всё, кроме записи: удобно проверить настройки, не тратя
дневной запас запросов.</p>
{''.join(stages)}</div>"""
    return page("Прогон", "/", body, onload="poll();")


# ----------------------------------------------------------------- письма

def letters_page() -> bytes:
    """Письма, готовые к отправке, и кнопки статусов."""
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
            'Соберите их на вкладке «Прогон»: «Найти компании» → '
            '«Обойти сайты» → «Написать письма».</p></div>')
        return page("Письма", "/letters", "".join(parts))

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

    return page("Письма", "/letters", "".join(parts))


# ---------------------------------------------------------------- воронка

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


# -------------------------------------------------------------- настройки

def settings_page(key: str = "icp", note: str = "") -> bytes:
    if key not in settings.FILES:
        key = "icp"
    entry = settings.FILES[key]

    options = "".join(
        f'<option value="{esc(name)}"{" selected" if name == key else ""}>'
        f'{esc(item["title"])}</option>'
        for name, item in settings.FILES.items())

    copies = settings.backups(key)
    copies_html = ""
    if copies:
        links = " ".join(
            f'<a class="btn" href="/settings?file={esc(key)}&restore={esc(item["name"])}"'
            f' title="{esc(item["name"])}">{esc(item["when"])}</a>'
            for item in copies[:8])
        copies_html = (f'<div class="row" style="margin-top:10px">'
                       f'<span class="muted">вернуть версию:</span> {links}</div>')

    banner = f'<div class="note ok">{esc(note)}</div>' if note else ""

    body = f"""<div class="card">
<div class="row">
  <select id="file-key" onchange="location='/settings?file=' + this.value">{options}</select>
  <button class="primary" onclick="saveSettings()">сохранить</button>
</div>
<p class="muted" style="margin-top:10px">{esc(entry['about'])}</p>
<p class="muted">Файл: <code>{esc(entry['path'].name)}</code>. Перед записью
содержимое проверяется: сломанный файл не сохранится, а прежняя версия уйдёт
в копию. Ключи и пароли отсюда не правятся — они в <code>.env</code>,
и панель их не показывает.</p>
{banner}<div id="save-note"></div>
<textarea id="editor" spellcheck="false">{esc(settings.read(key))}</textarea>
{copies_html}
</div>"""
    return page("Настройки", "/settings", body)


# ------------------------------------------------------------------- логи

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


# ----------------------------------------------------------------- сервер

class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: bytes,
              content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, payload: dict[str, Any]) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send(run_page())
            elif parsed.path == "/letters":
                self._send(letters_page())
            elif parsed.path == "/stats":
                self._send(stats_page())
            elif parsed.path == "/settings":
                key = query.get("file", ["icp"])[0]
                wanted = query.get("restore", [""])[0]
                note = ""
                if wanted:
                    try:
                        settings.restore(key, wanted)
                        note = f"Вернули версию {wanted}"
                    except settings.SettingsError as exc:
                        note = f"Не вернули: {exc}"
                self._send(settings_page(key, note))
            elif parsed.path == "/logs":
                self._send(logs_page(query.get("level", ["INFO"])[0],
                                     query.get("stage", [""])[0]))
            elif parsed.path == "/api/job":
                job = jobs.current()
                self._json(job.state() if job else {})
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001 — панель не должна падать целиком
            logger.exception("Ошибка страницы %s", parsed.path)
            self._send(page("Ошибка", "/",
                            f'<div class="card"><b class="lvl-ERROR">Страница '
                            f'не собралась</b><pre class="letter">{esc(exc)}</pre>'
                            f'<p class="muted">Подробности в логах.</p></div>'))

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/run":
                payload = self._body()
                stage = payload.get("stage", "")
                # Список этапов — белый: имя из браузера никогда не попадает
                # никуда, кроме сверки с ним.
                if stage not in pipeline.STAGE_KEYS:
                    self._json({"ok": False, "error": f"Неизвестный этап: {stage}"})
                    return
                try:
                    job = jobs.start(stage, bool(payload.get("dry_run")))
                except RuntimeError as exc:
                    self._json({"ok": False, "error": str(exc)})
                    return
                self._json({"ok": True, "job": job.state()})

            elif path == "/api/stop":
                job = jobs.current()
                if job:
                    job.stop()
                self._json({"ok": True})

            elif path in ("/api/mark", "/mark"):
                payload = self._body()
                outreach.mark(payload.get("status", ""), [payload.get("inn", "")])
                self._json({"ok": True})

            elif path == "/api/settings":
                payload = self._body()
                try:
                    result = settings.save(payload.get("key", ""),
                                           payload.get("text", ""))
                except settings.SettingsError as exc:
                    self._json({"ok": False, "error": str(exc)})
                    return
                self._json({"ok": True, **result})

            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка запроса %s", path)
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Стандартный вывод сервера идёт в stderr и мешает. Уводим в отладку.
        logger.debug("HTTP %s", fmt % args)


def serve(host: str = HOST, port: int = PORT,
          config: dict[str, Any] | None = None) -> None:
    """Запускает панель. Останавливается по Ctrl+C."""
    ui_cfg = (config or {}).get("ui", {})
    host = ui_cfg.get("host", host)
    port = int(ui_cfg.get("port", port))
    server = ThreadingHTTPServer((host, port), Handler)
    logger.info("Панель открыта: http://%s:%d", host, port)
    logger.info("Отсюда запускаются этапы и правятся настройки. Остановить — Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Панель остановлена")
    finally:
        server.server_close()
