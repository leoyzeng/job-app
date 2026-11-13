"""The search's parts on their own: address safety, page text, job-system readers, filters, agent answers."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request

import pytest

from app.profile.models import HardItem, SearchReading, SoftItem
from app.search import boards as boards_mod
from app.search import filters, pages, sources
from app.search.agents import clean_plan, clean_verdict, clean_web_results, evaluate
from app.search.boards import BoardStore, look_up_many, same_company, slug_candidates
from app.search.demo import ASHBY, GH, LEVER, LIST, FakeWeb, sample_routes, sample_web
from app.search.http import FetchError, SafeWeb, _CheckedRedirects, check_public
from app.search.models import Board, Posting, SearchPlan
from app.search.sources import ashby, greenhouse, lever, simplify, workday
from app.search.text import html_to_text, in_us, json_ld_postings, looks_closed, scam_signs


def run(coro_fn):
    box = {}

    def work():
        try:
            box["v"] = asyncio.run(coro_fn())
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=work)
    t.start()
    t.join(60)
    if "e" in box:
        raise box["e"]
    return box["v"]


def public(host, port, type=0):  # noqa: A002
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def private(host, port, type=0):  # noqa: A002
    return [(2, 1, 6, "", ("192.168.1.1", port))]


# --- which addresses may be read ------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://localhost:8765/api/state", "http://127.0.0.1/", "http://[::1]/", "http://10.0.0.5/admin",
    "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "http://172.20.1.1/", "http://router/",
    "http://printer.local/", "https://user:pass@example.com/", "ftp://example.com/file", "file:///C:/x",
    "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/", "javascript:alert(1)",
])
def test_local_and_odd_addresses_are_never_read(url):
    with pytest.raises(FetchError) as caught:
        check_public(url, public)
    assert caught.value.kind == "blocked"


def test_a_name_that_points_at_the_local_network_is_refused():
    with pytest.raises(FetchError) as caught:
        check_public("https://innocent-looking.example.com/job", private)
    assert caught.value.kind == "blocked"
    check_public("https://boards-api.greenhouse.io/v1/boards/x/jobs", public)  # fine


def test_redirects_are_checked_too():
    handler = _CheckedRedirects(public)
    request = urllib.request.Request("https://jobs.example.com/a")
    with pytest.raises(FetchError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1:8765/api/state")


def test_the_real_reader_refuses_this_pc_before_connecting():
    async def go():
        return await SafeWeb().get("http://127.0.0.1:9/")

    with pytest.raises(FetchError) as caught:
        run(go)
    assert caught.value.kind == "blocked"


# --- page text and signals ------------------------------------------------------------------


def test_html_to_text_keeps_the_posting_and_drops_the_rest():
    text = html_to_text("<html><head><title>x</title><script>var a=1</script></head><body><nav>Home Jobs</nav>"
                        "<h1>Robotics Intern</h1><p>Build &amp; test robots.</p><ul><li>ROS 2</li><li>C++</li></ul>"
                        "<footer>Cookies</footer></body></html>")
    assert text == "Robotics Intern\n\nBuild & test robots.\n\n- ROS 2\n\n- C++"


def test_job_posting_data_embedded_in_pages_is_read():
    markup = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization"},'
              '{"@type":"JobPosting","title":"Perception Intern","hiringOrganization":{"name":"Kestrel"},'
              '"jobLocation":[{"address":{"addressLocality":"Boston","addressRegion":"MA","addressCountry":"US"}}],'
              '"datePosted":"2026-11-01T00:00:00Z","validThrough":"2026-12-15","description":"&lt;p&gt;Hi&lt;/p&gt;",'
              '"baseSalary":{"currency":"USD","value":{"minValue":40,"maxValue":45,"unitText":"HOUR"}}}]}</script>')
    found = json_ld_postings(markup)
    assert found == [{"title": "Perception Intern", "company": "Kestrel", "location": "Boston, MA, US",
                      "description": "Hi", "posted_at": "2026-11-01", "valid_through": "2026-12-15",
                      "employment_type": "", "pay": "USD 40-45 per hour"}]


def test_closed_postings_and_scam_signs_are_noticed():
    assert looks_closed("Robotics Intern\nThis position has been filled.")
    assert looks_closed("Sorry, we are no longer accepting applications.")
    assert not looks_closed("Apply now. Applications close December 1.")
    signs = scam_signs("Pay a training fee of $50, then contact us on Telegram. Send your resume to jobs@gmail.com")
    assert signs == ["asks for a fee", "mentions contact by chat app or unusual payments",
                     "asks for applications to a personal email address"]
    assert scam_signs("We are an equal opportunity employer. Benefits include paid time off.") == []


@pytest.mark.parametrize("place, expected", [
    ("Pittsburgh, PA", True), ("Remote - US", True), ("New York, NY; London, UK", True), ("Austin, Texas", True),
    ("United States", True), ("Toronto, ON", False), ("London, United Kingdom", False), ("Bengaluru", False),
    ("Remote", None), ("", None), ("Hybrid", None),
])
def test_places_in_and_outside_the_us(place, expected):
    assert in_us(place) is expected


# --- recognising job-system links ----------------------------------------------------------


@pytest.mark.parametrize("url, expected", [
    ("https://boards.greenhouse.io/anduril/jobs/4567", {"ats": "greenhouse", "token": "anduril", "job_id": "4567"}),
    ("https://job-boards.greenhouse.io/figma/jobs/123?gh_src=x", {"ats": "greenhouse", "token": "figma", "job_id": "123"}),
    ("https://boards.greenhouse.io/embed/job_app?for=stripe&token=99", {"ats": "greenhouse", "token": "stripe", "job_id": "99"}),
    ("https://job-boards.greenhouse.io/figma", {"ats": "greenhouse", "token": "figma", "job_id": ""}),
    ("https://jobs.lever.co/palantir/0f0e5e3a-1b2c-4d5e-8f90-123456789abc/apply",
     {"ats": "lever", "token": "palantir", "job_id": "0f0e5e3a-1b2c-4d5e-8f90-123456789abc", "region": ""}),
    ("https://jobs.eu.lever.co/spotify/0f0e5e3a-1b2c-4d5e-8f90-123456789abc",
     {"ats": "lever", "token": "spotify", "job_id": "0f0e5e3a-1b2c-4d5e-8f90-123456789abc", "region": "eu"}),
    ("https://jobs.ashbyhq.com/openai/7458d4e9-da2e-47bd-98cb-adfda43d42b2/application",
     {"ats": "ashby", "token": "openai", "job_id": "7458d4e9-da2e-47bd-98cb-adfda43d42b2"}),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Software-Intern_JR1999",
     {"ats": "workday", "token": "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite",
      "job_id": "/job/US-CA-Santa-Clara/Software-Intern_JR1999", "job_path": "/job/US-CA-Santa-Clara/Software-Intern_JR1999"}),
    ("https://acme.wd1.myworkdayjobs.com/External/job/Remote/Intern_R5/apply",
     {"ats": "workday", "token": "acme.wd1.myworkdayjobs.com|acme|External", "job_id": "/job/Remote/Intern_R5",
      "job_path": "/job/Remote/Intern_R5"}),
    ("https://www.linkedin.com/jobs/view/123", None),
    ("https://careers.example.com/jobs/1", None),
])
def test_job_system_links_are_recognised(url, expected):
    assert sources.identify(url) == expected


# --- the readers, against the sample web ------------------------------------------------------


def test_greenhouse_board_is_read_with_its_html_unescaped():
    found = run(lambda: greenhouse.read_board(sample_web(), Board(ats="greenhouse", token="kestrelrobotics",
                                                                    company="Kestrel Robotics")))
    first = found[0]
    assert len(found) == 4 and first.title == "Robotics Software Intern, Summer 2027"
    assert first.company == "Kestrel Robotics" and first.location == "Pittsburgh, PA" and first.posted_at == "2026-11-02"
    assert "Write ROS 2 nodes" in first.description and "<" not in first.description and first.full
    assert first.board == "greenhouse:kestrelrobotics" and first.notes == ["Team: Engineering"]


def test_lever_postings_include_their_lists_and_pay():
    found = run(lambda: lever.read_board(sample_web(), Board(ats="lever", token="northwind-ai", company="Northwind AI")))
    swe = [p for p in found if p.title == "Software Engineering Intern"][0]
    assert swe.location == "Remote - US" and swe.type == "Intern" and swe.remote == "remote"
    assert "What we look for" in swe.description and "- Python or Go" in swe.description
    assert swe.pay == "USD 38-45 per hour wage" and swe.posted_at == "2026-11-07"


def test_lever_eu_companies_are_found_on_the_eu_host():
    web = FakeWeb({"https://api.eu.lever.co/v0/postings/euco?mode=json&limit=1": []})
    assert run(lambda: lever.find_region(web, "euco")) == "eu"
    assert run(lambda: lever.find_region(web, "nobody")) is None


def test_ashby_board_and_single_job():
    web = sample_web()
    found = run(lambda: ashby.read_board(web, Board(ats="ashby", token="tidewater", company="Tidewater")))
    assert found[0].title == "Autonomy Engineering Intern" and found[0].remote == "OnSite"
    one = run(lambda: ashby.read_job(web, "tidewater", "cccccccc-3333-4333-8333-cccccccccccc", "Tidewater"))
    assert one.url.endswith("cccccccc-3333-4333-8333-cccccccccccc")
    with pytest.raises(FetchError):
        run(lambda: ashby.read_job(web, "tidewater", "dddddddd-3333-4333-8333-cccccccccccc"))


def test_workday_search_and_job():
    host = "acme.wd1.myworkdayjobs.com"
    base = f"https://{host}/wday/cxs/acme/External"
    web = FakeWeb(
        routes={f"{base}/job/Pittsburgh-PA/Robotics-Intern_R1": {
            "jobPostingInfo": {"title": "Robotics Intern", "jobDescription": "<p>Summer 2027 internship.</p>",
                               "location": "Pittsburgh, PA", "timeType": "Full time", "canApply": True,
                               "startDate": "2027-05-17", "externalUrl": f"https://{host}/External/job/Pittsburgh-PA/Robotics-Intern_R1"},
            "hiringOrganization": {"name": "Acme Corp"}}},
        posts={f"{base}/jobs": lambda body: {"total": 1, "jobPostings": [
            {"title": "Robotics Intern", "externalPath": "/job/Pittsburgh-PA/Robotics-Intern_R1",
             "locationsText": "Pittsburgh, PA", "postedOn": "Posted Today"}]} if body["offset"] == 0 else {"total": 1, "jobPostings": []}},
    )
    board = Board(ats="workday", token=f"{host}|acme|External", company="Acme")
    found = run(lambda: workday.search(web, board, ["intern"]))
    assert [p.title for p in found] == ["Robotics Intern"] and found[0].url.endswith("/External/job/Pittsburgh-PA/Robotics-Intern_R1")
    job = run(lambda: workday.read_job(web, board.token, "/job/Pittsburgh-PA/Robotics-Intern_R1", "Acme"))
    assert job.description == "Summer 2027 internship." and job.notes == ["Workday lists a start date of 2027-05-17"]


def test_the_community_list_keeps_open_listings_for_the_wanted_terms():
    assert simplify.repos_for(["internships"], ["Summer 2027"]) == [
        ("Summer2027-Internships", "Summer 2027 internship list (Simplify, GitHub)")]
    assert [r for r, _ in simplify.repos_for(["internships", "new_grad"], ["Fall 2026"])] == [
        "Summer2026-Internships", "Summer2027-Internships", "New-Grad-Positions"]
    found = run(lambda: simplify.read_list(sample_web(), "Summer2027-Internships", "list", ["Summer 2027"]))
    assert [p.company for p in found] == ["Kestrel Robotics", "Harborlight Systems", "Quayside Robotics"]
    assert found[1].notes == ["The list says: Does Not Offer Sponsorship"] and found[1].posted_at == "2026-11-05"


# --- reading one posting from its link ---------------------------------------------------------


def test_reading_a_posting_uses_the_job_system_when_it_can():
    web = sample_web()
    posting = run(lambda: pages.read_posting(web, "https://job-boards.greenhouse.io/kestrelrobotics/jobs/5002"))
    assert posting.title == "Perception Intern (Summer 2027)" and posting.source == "greenhouse"
    assert web.requests == [f"{GH}/kestrelrobotics/jobs/5002?pay_transparency=true"]


def test_reading_a_company_page_uses_its_posting_data():
    url = "https://careers.harborlight.example/jobs/controls-intern?utm_source=Simplify"
    posting = run(lambda: pages.read_posting(sample_web(), url))
    assert posting.title == "Controls Software Intern" and posting.company == "Harborlight Systems"
    assert posting.location == "Seattle, WA, US" and posting.full and posting.open is True


def test_a_company_page_that_embeds_greenhouse_is_read_through_greenhouse():
    routes = sample_routes()
    routes["https://kestrel.example/careers?gh_jid=5001"] = (
        '<html><body><div id="grnhse_app"></div>'
        '<iframe src="https://boards.greenhouse.io/embed/job_app?for=kestrelrobotics&amp;token=5001"></iframe></body></html>')
    posting = run(lambda: pages.read_posting(FakeWeb(routes), "https://kestrel.example/careers?gh_jid=5001"))
    assert posting.title == "Robotics Software Intern, Summer 2027"
    assert posting.url == "https://kestrel.example/careers?gh_jid=5001"  # the employer's page stays the link


def test_closed_missing_and_restricted_postings():
    web = sample_web()
    closed = run(lambda: pages.read_posting(web, "https://careers.quayside.example/jobs/robotics-intern"))
    assert closed.open is False
    with pytest.raises(FetchError) as gone:
        run(lambda: pages.read_posting(web, "https://careers.quayside.example/jobs/old"))
    assert gone.value.kind == "not_found"
    with pytest.raises(FetchError) as refused:
        run(lambda: pages.read_posting(web, "https://www.linkedin.com/jobs/view/123"))
    assert refused.value.kind == "blocked" and web.requests[-1] != "https://www.linkedin.com/jobs/view/123"


def test_where_a_posting_lives():
    assert pages.site_kind("https://job-boards.greenhouse.io/x/jobs/1") == "system"
    assert pages.site_kind("https://acme.wd5.myworkdayjobs.com/x") == "system"
    assert pages.site_kind("https://www.indeed.com/viewjob?jk=1") == "board"
    assert pages.site_kind("https://careers.acme.com/jobs/1") == "site"


# --- company boards ----------------------------------------------------------------------------


def test_board_names_tried_for_a_company():
    assert slug_candidates("Boston Dynamics, Inc.") == ["bostondynamics", "boston-dynamics"]
    assert slug_candidates("Anduril") == ["anduril"]
    assert same_company("Anduril Industries", "Anduril") and not same_company("Apple", "Applied Intuition")


def test_boards_are_found_by_name_and_remembered(tmp_path):
    store = BoardStore(tmp_path / "company_boards.json")
    found = run(lambda: look_up_many(sample_web(), store, ["Kestrel Robotics", "Northwind AI", "Tidewater", "Nobody Inc"],
                                     follow=True, source="watchlist"))
    assert [b.key for b in found["Kestrel Robotics"]] == ["greenhouse:kestrelrobotics"]
    assert [b.key for b in found["Northwind AI"]] == ["lever:northwind-ai"]
    assert [b.key for b in found["Tidewater"]] == ["ashby:tidewater"]
    assert found["Nobody Inc"] == []
    store.save()
    again = BoardStore(tmp_path / "company_boards.json")
    assert {b.key for b in again.followed()} == {"greenhouse:kestrelrobotics", "lever:northwind-ai", "ashby:tidewater"}
    assert not again.needs_lookup("Nobody Inc") and "No board found" in again.company("Nobody Inc").note
    # Looked up once: a second search does not ask again.
    web = sample_web()
    run(lambda: look_up_many(web, again, ["Kestrel Robotics", "Nobody Inc"], follow=True, source="watchlist"))
    assert web.requests == []


def test_a_greenhouse_board_with_another_companys_name_is_not_taken(tmp_path):
    web = FakeWeb({f"{GH}/apex": {"name": "Apex Fitness Clubs"}})
    store = BoardStore(tmp_path / "b.json")
    found = run(lambda: boards_mod.look_up(web, store, "Apex Robotics", follow=True, source="watchlist"))
    assert found == []


def test_boards_are_learned_from_posting_links(tmp_path):
    store = BoardStore(tmp_path / "b.json")
    board = store.learn_from_url("https://jobs.lever.co/zoox/29ab6640-f7dc-4c10-a4e9-0a12d1e3bcd8", "Zoox")
    assert board.key == "lever:zoox" and not board.follow and store.boards_of("Zoox") == [board]


# --- quick title and place checks ------------------------------------------------------------


PLAN = SearchPlan(kind_words=["intern", "internship", "co-op"], role_words=["software", "robotics", "ml", "machine learning"],
                  exclude_words=["senior", "phd"], countries=["US"], years=["2027"])


@pytest.mark.parametrize("title, place, reason", [
    ("Robotics Software Intern, Summer 2027", "Pittsburgh, PA", None),
    ("ML Intern", "Remote", None),
    ("HTML Developer Intern", "Austin, TX", "title is outside the fields you want"),
    ("Internal Tools Software Engineer", "Austin, TX", "title is not the kind of job you want"),
    ("Senior Software Intern", "Austin, TX", "title says senior"),
    ("Software Engineering Intern - Summer 2026", "Austin, TX", "title names 2026"),
    ("Machine Learning Intern", "Toronto, ON", "outside the US"),
    ("Software Co-op (Fall 2027)", "Waterloo, ON", "outside the US"),
])
def test_titles_and_places_that_cannot_fit(title, place, reason):
    assert filters.problem(PLAN, Posting(company="A", title=title, location=place, url="https://a.example/1")) == reason


def test_a_kind_word_in_the_job_type_counts():
    posting = Posting(company="A", title="Robotics Software Engineer", type="Internship", location="", url="https://a.example/1")
    assert filters.problem(PLAN, posting) is None


# --- checking agents' answers ---------------------------------------------------------------------


READING = SearchReading(hard=[HardItem(text="3 month internship", test="..."), HardItem(text="based in US", test="...")],
                        soft=[SoftItem(text="prefer large company", weight=2)])


def test_planner_answers_are_tidied_and_capped():
    plan = clean_plan({
        "queries": [f"query {i}" for i in range(30)], "kind_words": ["Intern", "intern", " Co-op "],
        "role_words": ["Robotics"], "exclude_words": [], "countries": ["us", "United States"], "years": ["2027", "27"],
        "terms": ["summer 2027", "next summer"], "lists": ["internships", "everything"],
        "companies": [{"name": "Anduril", "why": "defense robotics"}, {"name": "Skydio", "why": "drones"}, "junk"],
        "notes": "x" * 1000,
    }, watchlist=["anduril"])
    assert len(plan.queries) == 12 and plan.kind_words == ["intern", "co-op"] and plan.countries == ["US"]
    assert plan.years == ["2027"] and plan.terms == ["Summer 2027"] and plan.lists == ["internships"]
    assert plan.companies == [{"name": "Skydio", "why": "drones"}] and len(plan.notes) == 400


def test_verdicts_follow_the_users_own_requirement_lines():
    verdict = clean_verdict({
        "hard": [
            {"requirement": "3 Month Internship", "result": "pass", "evidence": "12-week internship"},
            {"requirement": "based in US", "result": "fail", "evidence": ""},          # a failure without evidence
            {"requirement": "Must speak French", "result": "fail", "evidence": "French"},  # not the user's line
        ],
        "soft_score": 140, "summary": "  A   job. ", "closed": "yes",
    }, READING)
    assert verdict.hard == [
        {"requirement": "3 month internship", "result": "pass", "evidence": "12-week internship"},
        {"requirement": "based in US", "result": "unknown", "evidence": ""},
    ]
    assert verdict.soft_score == 100 and verdict.summary == "A job." and verdict.closed is False
    missing = clean_verdict({"hard": []}, READING)
    assert [h["result"] for h in missing.hard] == ["unknown", "unknown"]


def test_verdicts_for_postings_not_asked_about_are_ignored():
    from app.agents.runner import AgentReply

    async def ask(**kw):
        request = json.loads(kw["prompt"])
        assert [p["key"] for p in request["postings"]] == ["p1"]
        return AgentReply(data={"results": [{"key": "p1", "hard": [], "soft_score": 50}, {"key": "zzz", "hard": []}]})

    posting = Posting(company="A", title="Intern", url="https://a.example/1", description="Text")
    from app.paths import Paths
    from conftest import REPO

    verdicts, _ = run(lambda: evaluate(ask, Paths(REPO), READING, {}, [("p1", posting)]))
    assert list(verdicts) == ["p1"]


def test_web_agent_results_are_checked():
    rows = clean_web_results({"postings": [
        {"company": "A", "title": "Intern", "url": "https://a.example/1", "is_employer_page": True},
        {"company": "B", "title": "Intern", "url": "javascript:alert(1)"},
        {"company": "", "title": "Intern", "url": "https://c.example/1"},
        "junk",
    ]}, limit=10)
    assert [r["company"] for r in rows] == ["A"] and rows[0]["location"] == ""
    assert clean_web_results(["not", "a", "dict"], 5) == []


# --- the check command's search tests, against stand-ins ----------------------------------------------------


def test_check_reports_reachable_feeds(monkeypatch, tmp_path):
    from app.checks import search_checks
    from app.core.models import Settings
    from app.paths import Paths
    from app.search import http as http_mod

    routes = sample_routes()
    routes.update({url: {"ok": True} for _, url in search_checks.FEEDS})
    year = search_checks.next_summer()
    routes[LIST.replace("Summer2027", f"Summer{year}")] = routes[LIST]
    monkeypatch.setattr(http_mod, "SafeWeb", lambda: FakeWeb(routes))
    result = run(lambda: search_checks.feeds_check(Paths(tmp_path), Settings()))
    assert result.status == "ok" and "has 3 open listings" in result.summary

    del routes[search_checks.FEEDS[1][1]]   # a board that moved: the site still answered
    routes.pop(LIST.replace("Summer2027", f"Summer{year}"))
    result = run(lambda: search_checks.feeds_check(Paths(tmp_path), Settings()))
    assert result.status == "fail" and result.summary == f"Could not reach: the Summer {year} community list."


def test_next_summer():
    from datetime import date

    from app.checks.search_checks import next_summer

    assert next_summer(date(2026, 11, 12)) == 2027 and next_summer(date(2027, 3, 1)) == 2027


# --- the hidden browser, for real -------------------------------------------------------------------

JS_PAGE = """<html><head><title>Careers</title></head><body><div id="app">Loading...</div><script>
document.getElementById('app').innerHTML = '<h1>Robotics Intern</h1><p>' + 'Build robot arms for our Pittsburgh lab. '.repeat(30)
  + '</p><p>Summer 2027, twelve weeks.</p>';
</script></body></html>"""


def test_javascript_pages_are_read_in_the_hidden_browser(monkeypatch):
    pytest.importorskip("playwright.async_api")
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    from conftest import free_port

    port = free_port()
    url = f"http://127.0.0.1:{port}/jobs/1"
    # The test page is on this PC, which the real app never reads; allow it for this test only.
    monkeypatch.setattr(pages, "check_public", lambda *a, **k: None)

    async def scenario():
        app = Starlette(routes=[Route("/jobs/1", lambda r: HTMLResponse(JS_PAGE))])
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        reader = pages.HeadlessReader(channel="chromium-not-installed")
        try:
            web = FakeWeb({})
            web._answer = lambda u, table, body=None: __import__("app.search.http", fromlist=["Response"]).Response(
                url=u, status=200, headers={"content-type": "text/html"}, body=JS_PAGE.encode())
            posting = await pages.read_posting(web, url, headless=reader)
            return posting, reader.used
        finally:
            await reader.close()
            server.should_exit = True
            await task

    try:
        posting, used = run(scenario)
    except FetchError as exc:
        pytest.skip(f"No browser for the hidden-browser test: {exc}")
    assert used == 1 and posting.full
    assert "Build robot arms for our Pittsburgh lab." in posting.description and "Loading" not in posting.description


def test_the_hidden_browser_refuses_this_pc():
    pytest.importorskip("playwright.async_api")
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    from conftest import free_port

    port = free_port()
    hits = []

    async def page_handler(request):
        hits.append(request.url.path)
        return HTMLResponse("<h1>Router admin</h1>")

    async def scenario():
        app = Starlette(routes=[Route("/admin", page_handler)])
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        reader = pages.HeadlessReader(channel="chromium-not-installed")
        try:
            return await reader.render(f"http://127.0.0.1:{port}/admin")
        finally:
            await reader.close()
            server.should_exit = True
            await task

    with pytest.raises(FetchError) as caught:
        run(scenario)
    if "No browser is available" in str(caught.value):
        pytest.skip("No browser for the hidden-browser test")
    assert caught.value.kind == "network" and hits == []  # the page was there, and was never asked for
