"""Web research on the local site (and one stable public fact)."""

from __future__ import annotations

import re

from .registry import Ctx, check, fail, ok, setup


def _text(ctx: Ctx, name: str) -> str | None:
    path = ctx.p(name)
    return path.read_text(errors="replace") if path.exists() else None


@setup
def nothing(ctx: Ctx):
    """Nothing to prepare (the site is always served; the sandbox is always made)."""


@setup
def web_flaky(ctx: Ctx):
    ctx.server.state.reset()          # the page fails twice with 503, then works


@check
def web_local_fact_ok(ctx: Ctx):
    text = _text(ctx, "answer.txt")
    if text is None:
        return fail("no answer.txt")
    return ok("37 days") if re.search(r"\b37\b", text) else fail(f"answer.txt says {text.strip()[:80]!r}")


@check
def web_compare_ok(ctx: Ctx):
    text = _text(ctx, "laptops.md")
    if text is None:
        return fail("no laptops.md")
    rows = [line for line in text.splitlines() if line.count("|") >= 3]
    order = [name for line in rows for name in ("Beta", "Alpha", "Gamma") if name in line]
    if order != ["Beta", "Alpha", "Gamma"]:
        return fail(f"table rows in order {order}, want Beta, Alpha, Gamma (cheapest first)")
    flat = text.replace(",", "")
    missing = [v for v in ("999", "1149", "1899", "1.42", "1.19", "2.05", "14", "17", "21") if v not in flat]
    return fail(f"table lacks {missing}") if missing else ok("Markdown table, cheapest first, all 9 values")


@check
def web_slow_ok(ctx: Ctx):
    text = _text(ctx, "q3.txt")
    if text is None:
        return fail("no q3.txt")
    flat = text.replace(",", "")
    missing = [v for v in ("4.82", "1375", "2.9") if v not in flat]
    return fail(f"q3.txt lacks {missing}") if missing else ok("all three numbers from the slow page")


@check
def web_flaky_ok(ctx: Ctx):
    text = _text(ctx, "version.txt")
    tries = ctx.server.fetched("/flaky/", ctx.started) if ctx.server else "?"
    if text is None:
        return fail(f"no version.txt ({tries} requests to the flaky page)")
    if "4.7.2" not in text:
        return fail(f"version.txt says {text.strip()[:60]!r}")
    return ok(f"4.7.2 after {tries} requests (the first 2 fail with 503)")


@check
def web_public_ok(ctx: Ctx):
    text = _text(ctx, "python.txt")
    if text is None:
        return fail("no python.txt")
    return ok("2008") if "2008" in text else fail(f"python.txt says {text.strip()[:60]!r}")


@check
def web_follow_ok(ctx: Ctx):
    text = _text(ctx, "wiki.txt")
    if text is None:
        return fail("no wiki.txt")
    if "port veyra" not in text.lower() or "1742" not in text:
        return fail(f"wiki.txt says {text.strip()[:80]!r} (want Port Veyra, 1742)")
    return ok("Port Veyra, 1742 (3 links deep)")

