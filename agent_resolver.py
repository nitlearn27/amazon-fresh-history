"""
Runtime self-healing via DeepSeek.

When a Playwright interaction fails in a way the code doesn't understand
(an Add button that is really a size-variant control, a result-card selector
that stopped matching), the resolver:

1. replays any previously learned skill for that situation (no LLM call);
2. otherwise sends the goal + failure + a compact snapshot of the page to
   DeepSeek and asks for a recovery recipe (click/wait steps or a CSS
   selector — data only, never code);
3. executes the recipe, and only if the caller's own confirmation check
   passes does it persist the recipe as a new skill (agent_skills.py) so the
   next occurrence is instant.

Hard guardrails — the LLM plans, this module decides what is executable:
- allowed step actions: "click" and "wait" only, max 5 steps;
- selectors matching checkout/buy/pay/order-placement/address/sign-out
  patterns are rejected outright;
- no navigation: recipes act on the current page only;
- captcha and /ap/ (auth) pages are classified non-resolvable — no tokens
  are spent on failures an LLM cannot click through.

Disabled (all hooks no-op, pre-agent behavior) unless DEEPSEEK_API_KEY is set.
"""

import json
import os
import re

import requests

import agent_skills

DEEPSEEK_TIMEOUT_S = 60
MAX_STEPS = 5
SNAPSHOT_MAX_ELEMENTS = 120
HTML_SAMPLE_MAX_CHARS = 12_000

# Actions the executor will ever perform, regardless of what the LLM asks for.
ALLOWED_ACTIONS = {"click", "wait"}

# Reject any selector that could steer toward money movement or account changes.
BANNED_SELECTOR_RE = re.compile(
    r"checkout|buy.?now|place.?order|payment|pay.?now|proceed.?to|address|sign.?out|logout|delete",
    re.I,
)

# Mirrors SELECTORS["captcha_image"] in scrape_amazon_orders.py (not imported
# to avoid a circular import — the scraper imports this module for its hooks).
CAPTCHA_SELECTOR = "img[src*='opfcaptcha'], img[alt*='captcha' i], img[src*='Captcha']"

_SYSTEM_PROMPT = (
    "You are a web-automation repair assistant for an amazon.in grocery app "
    "(Playwright). An automated step failed; you are given the goal, the "
    "failure reason, and a snapshot of the current page. Respond ONLY with "
    "JSON. You may plan at most 5 steps, each either "
    '{"action": "click", "selector": "<css>"} or {"action": "wait", "ms": <int>}. '
    "Selectors must target elements present in the snapshot. NEVER target "
    "checkout, buy-now, payment, order placement, address or sign-out "
    "controls — this app stops at the cart. If the failure cannot be fixed "
    'by clicking on this page, return {"resolvable": false, "reason": "..."}.'
)


def agent_enabled() -> bool:
    if not (os.getenv("DEEPSEEK_API_KEY") or "").strip():
        return False
    return (os.getenv("AGENT_ENABLED") or "true").strip().lower() not in ("false", "0", "no")


def _deepseek_chat(messages: list[dict]) -> dict | None:
    """One DeepSeek chat call; returns the parsed JSON object or None."""
    base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY', '').strip()}"},
            json={
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=DEEPSEEK_TIMEOUT_S,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as exc:
        print(f"[agent] DeepSeek call failed: {exc}")
        return None


async def _non_resolvable(page) -> str | None:
    """Reason string when the current page cannot be fixed by clicking."""
    url = (page.url or "").lower()
    if "/ap/" in url:
        return "on an auth (/ap/) page"
    try:
        captcha = page.locator(CAPTCHA_SELECTOR).first
        if await captcha.count() > 0 and await captcha.is_visible():
            return "captcha shown"
    except Exception:
        pass
    return None


async def _page_snapshot(page) -> dict:
    """Compact JSON snapshot of the page's interactive elements."""
    elements = await page.evaluate(
        r"""(maxElements) => {
            const trim = (s, n) => {
                s = (s || '').replace(/\s+/g, ' ').trim();
                return s.length > n ? s.slice(0, n) + '…' : s;
            };
            const isVisible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const sels = "button, a[href], input, [role='button'], [data-csa-c-type], [onclick]";
            const out = [];
            for (const el of document.querySelectorAll(sels)) {
                if (!isVisible(el)) continue;
                const rec = { tag: el.tagName.toLowerCase() };
                if (el.id) rec.id = el.id;
                const cls = trim(el.className && el.className.baseVal !== undefined
                                 ? el.className.baseVal : el.className, 80);
                if (cls) rec.class = cls;
                for (const a of el.attributes) {
                    if (a.name.startsWith('data-csa-') || a.name === 'aria-label' || a.name === 'name') {
                        rec[a.name] = trim(a.value, 90);
                    }
                }
                const text = trim(el.textContent, 60);
                if (text) rec.text = text;
                out.push(rec);
                if (out.length >= maxElements) break;
            }
            return out;
        }""",
        SNAPSHOT_MAX_ELEMENTS,
    )
    return {"url": page.url, "title": await page.title(), "elements": elements}


def _validate_steps(steps) -> list[dict] | None:
    """Return sanitized steps, or None if anything is disallowed."""
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        return None
    clean = []
    for step in steps:
        if not isinstance(step, dict):
            return None
        action = step.get("action")
        if action not in ALLOWED_ACTIONS:
            return None
        if action == "click":
            selector = step.get("selector")
            if not isinstance(selector, str) or not selector.strip():
                return None
            if BANNED_SELECTOR_RE.search(selector):
                print(f"[agent] Rejected banned selector: {selector!r}")
                return None
            clean.append({"action": "click", "selector": selector.strip()})
        else:
            ms = step.get("ms")
            if not isinstance(ms, (int, float)) or not (0 < ms <= 10_000):
                return None
            clean.append({"action": "wait", "ms": int(ms)})
    return clean


async def _execute_steps(page, steps: list[dict]) -> tuple[str | None, list[dict]]:
    """Run validated steps. Returns (error, completed_steps) — error is None on
    full success; completed_steps are the ones that actually ran (they may have
    changed page state even when a later step failed)."""
    completed: list[dict] = []
    for step in steps:
        try:
            if step["action"] == "click":
                loc = page.locator(step["selector"]).first
                await loc.scroll_into_view_if_needed(timeout=3_000)
                await loc.click(timeout=5_000)
            else:
                await page.wait_for_timeout(step["ms"])
            completed.append(step)
        except Exception as exc:
            return f"step {step} failed: {exc}", completed
    return None, completed


def _substitute_vars(text: str, template_vars: dict | None) -> str:
    """Fill {placeholders} in a stored skill selector with this run's values."""
    for key, value in (template_vars or {}).items():
        text = text.replace("{" + key + "}", value)
    return text


def _templatize(text: str, template_vars: dict | None) -> str:
    """Replace run-specific values (e.g. the ASIN) with {placeholders} so the
    persisted skill generalises to other products."""
    for key, value in (template_vars or {}).items():
        if value:
            text = text.replace(value, "{" + key + "}")
    return text


async def attempt_action_recovery(
    page, context: str, goal: str, failure_reason: str, confirm,
    template_vars: dict | None = None,
) -> str | None:
    """Try to recover a failed interaction. Returns "skill:<id>" when a learned
    skill fixed it, "agent" when DeepSeek fixed it (and the recipe was saved),
    or None when unrecovered (caller fails exactly as before).

    `template_vars` (e.g. {"asin": "B0..."}) generalise learned skills: their
    values are replaced by {placeholders} when a recipe is persisted and
    substituted back at replay time, so a recipe learned on one product works
    for every product."""
    reason = await _non_resolvable(page)
    if reason:
        print(f"[agent] Skipping recovery for {context!r}: {reason}.")
        return None

    # 1. Learned skills first — no LLM cost.
    for skill in agent_skills.applicable_action_skills(context):
        steps = [
            {**s, "selector": _substitute_vars(s["selector"], template_vars)}
            if s.get("action") == "click" else s
            for s in (skill.get("steps") or [])
        ]
        # Applicable when the stored marker matches the page — or, since the
        # LLM's marker choice can be unreliable, when the skill's first click
        # target exists (the natural probe for "can this recipe even start").
        marker = _substitute_vars(skill.get("marker_selector") or "", template_vars)
        first_click = next((s["selector"] for s in steps if s.get("action") == "click"), None)
        applicable = False
        for probe in (marker, first_click):
            if not probe:
                continue
            try:
                if await page.locator(probe).first.count() > 0:
                    applicable = True
                    break
            except Exception:
                continue
        if not applicable:
            continue
        print(f"[agent] Replaying learned skill {skill['id']} ({skill.get('description', '')[:60]})…")
        err, _ = await _execute_steps(page, steps)
        if err is None and await confirm():
            agent_skills.record_hit(skill["id"])
            print(f"[agent] Skill {skill['id']} resolved the failure.")
            return f"skill:{skill['id']}"
        print(f"[agent] Skill {skill['id']} did not resolve it ({err or 'confirmation failed'}).")

    if not agent_enabled():
        return None

    # 2. Ask DeepSeek for a recovery recipe.
    print(f"[agent] Asking DeepSeek to recover {context!r} ({failure_reason}).")
    snapshot = await _page_snapshot(page)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({
            "goal": goal,
            "failure_reason": failure_reason,
            "page": snapshot,
            "respond_with": {
                "resolvable": "bool",
                "reason": "short explanation",
                "steps": [{"action": "click|wait", "selector": "css (for click)", "ms": "int (for wait)"}],
                "skill_description": "one line describing when this recipe applies",
                "marker_selector": "css selector that identifies pages where this recipe applies",
            },
        }, ensure_ascii=False)},
    ]

    executed: list[dict] = []
    for attempt in (1, 2, 3):
        plan = _deepseek_chat(messages)
        if plan is None:
            return None
        if not plan.get("resolvable"):
            print(f"[agent] DeepSeek says not resolvable: {plan.get('reason')!r}")
            return None
        steps = _validate_steps(plan.get("steps"))
        if steps is None:
            print(f"[agent] DeepSeek plan rejected by validation: {plan.get('steps')!r}")
            return None
        print(f"[agent] Executing DeepSeek plan (attempt {attempt}): {steps}")
        err, completed = await _execute_steps(page, steps)
        executed.extend(completed)
        if err is None and await confirm():
            # The reusable recipe is everything executed across attempts (e.g.
            # attempt 1 opened a variant sheet, attempt 2 clicked its Add).
            agent_skills.add_skill({
                "kind": "action",
                "context": context,
                "steps": [
                    {**s, "selector": _templatize(s["selector"], template_vars)}
                    if s.get("action") == "click" else s
                    for s in executed
                ],
                "marker_selector": _templatize(
                    (plan.get("marker_selector") or "").strip()[:300], template_vars
                ),
                "description": (plan.get("skill_description") or plan.get("reason") or "")[:200],
                "source": "deepseek",
            })
            print("[agent] DeepSeek plan worked — saved as a skill.")
            return "agent"
        feedback = err or "steps executed but the confirmation check still fails"
        print(f"[agent] Plan attempt {attempt} failed: {feedback}")
        messages.append({"role": "assistant", "content": json.dumps(plan)})
        # Observation after action: a fresh snapshot so the model can see what
        # its steps changed (e.g. a variant sheet that is now open).
        messages.append({"role": "user", "content": json.dumps({
            "result": "failure",
            "detail": feedback,
            "page_now": await _page_snapshot(page),
            "instruction": (
                "The page snapshot above reflects the current state AFTER your "
                "previous steps ran. Plan the NEXT steps from this state to "
                "complete the goal, or return resolvable=false."
            ),
        }, ensure_ascii=False)})
    return None


async def _selector_matches(page, selector: str, expectation: str, min_count: int) -> bool:
    try:
        loc = page.locator(selector)
        if await loc.count() < min_count:
            return False
        text = (await loc.first.inner_text(timeout=2_000)) or ""
        return bool(re.search(expectation, text))
    except Exception:
        return False


async def resolve_selector(page, slot: str, description: str, expectation: str, min_count: int = 1) -> str | None:
    """Find a working CSS selector for a named slot when the built-in one
    matches nothing. Learned overrides are tried first; then DeepSeek proposes
    one from a trimmed HTML sample. A selector only counts (and is only
    persisted) when it matches >= min_count elements whose text matches the
    `expectation` regex."""
    for skill in agent_skills.selector_overrides(slot):
        sel = skill.get("selector") or ""
        if sel and await _selector_matches(page, sel, expectation, min_count):
            agent_skills.record_hit(skill["id"])
            print(f"[agent] Selector skill {skill['id']} matched for slot {slot!r}: {sel!r}")
            return sel

    if not agent_enabled():
        return None
    if await _non_resolvable(page):
        return None

    html = await page.evaluate(
        r"""(maxChars) => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script, style, svg, noscript, link, iframe').forEach(e => e.remove());
            return clone.outerHTML.replace(/\s+/g, ' ').slice(0, maxChars);
        }""",
        HTML_SAMPLE_MAX_CHARS,
    )
    print(f"[agent] Asking DeepSeek for a selector for slot {slot!r}.")
    plan = _deepseek_chat([
        {"role": "system", "content": (
            "You are a web-automation repair assistant. Given trimmed HTML of an "
            "amazon.in page, respond ONLY with JSON: "
            '{"selector": "<css selector>", "reason": "..."}. The selector must '
            f"match {description}. Prefer stable attributes (ids, data-*, "
            "semantic classes) over generated class names."
        )},
        {"role": "user", "content": json.dumps({
            "url": page.url,
            "need": description,
            "html_sample": html,
        }, ensure_ascii=False)},
    ])
    sel = (plan or {}).get("selector")
    if not isinstance(sel, str) or not sel.strip() or BANNED_SELECTOR_RE.search(sel):
        return None
    sel = sel.strip()
    if not await _selector_matches(page, sel, expectation, min_count):
        print(f"[agent] DeepSeek selector {sel!r} did not validate on the page.")
        return None
    agent_skills.add_skill({
        "kind": "selector",
        "slot": slot,
        "selector": sel,
        "description": (plan.get("reason") or description)[:200],
        "source": "deepseek",
    })
    print(f"[agent] DeepSeek selector {sel!r} validated for slot {slot!r} — saved as a skill.")
    return sel
