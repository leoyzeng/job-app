"""A small application form served on this computer, used by the browser checks.

It has one of each kind of field that real application forms use, including a
file upload, so the check exercises the same operations autofill will need.
"""

from __future__ import annotations

import asyncio
import socket

from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Job App check form</title>
<style>
  body { font: 16px/1.5 Bahnschrift, "Segoe UI", system-ui, sans-serif; max-width: 34rem; margin: 3rem auto; padding: 0 1.5rem; color: #17202b; }
  label, fieldset { display: block; margin-top: 1rem; }
  input[type=text], input[type=email], select, textarea { display: block; width: 100%; padding: .45rem .6rem; font: inherit; }
  .note { background: #ffe347; padding: .5rem .8rem; border-radius: 3px; }
</style></head>
<body>
<h1>Job App check form</h1>
<p class="note">This is a test page on your own computer. Job App is checking that it can fill in a form. The window closes by itself.</p>
<form id="application">
  <label>Full name <input type="text" id="name" name="name" autocomplete="name"></label>
  <label>Email <input type="email" id="email" name="email" autocomplete="email"></label>
  <label>Highest degree
    <select id="degree" name="degree">
      <option value="">Choose one</option><option>Bachelor's</option><option>Master's</option><option>Doctorate</option>
    </select>
  </label>
  <fieldset><legend>Are you legally authorized to work in the United States?</legend>
    <label><input type="radio" id="auth-yes" name="authorized" value="Yes"> Yes</label>
    <label><input type="radio" id="auth-no" name="authorized" value="No"> No</label>
  </fieldset>
  <label><input type="checkbox" id="updates" name="updates"> Email me about similar roles</label>
  <label>Why do you want this role? <textarea id="why" name="why" rows="3"></textarea></label>
  <label>Resume <input type="file" id="resume" name="resume" accept=".pdf,.docx"></label>
</form>
</body></html>"""

READ_VALUES = """() => ({
  name: document.querySelector('#name').value,
  email: document.querySelector('#email').value,
  degree: document.querySelector('#degree').value,
  authorized: (document.querySelector('input[name=authorized]:checked') || {}).value || '',
  updates: document.querySelector('#updates').checked,
  why: document.querySelector('#why').value,
  resume: (document.querySelector('#resume').files[0] || {}).name || '',
})"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalFormSite:
    """Serves the form on a spare port, inside the event loop that is already running."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self._server = None
        self._task = None

    async def __aenter__(self) -> "LocalFormSite":
        import uvicorn

        async def form(request):
            return HTMLResponse(FORM)

        app = Starlette(routes=[Route("/", form)])
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None, access_log=False,
                           timeout_graceful_shutdown=1)
        )
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("The local test page could not be started.")
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
