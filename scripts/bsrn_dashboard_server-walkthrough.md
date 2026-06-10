# bsrn_dashboard_server.py — Walkthrough

## What Changed
**2026-06-10 (palette B)** — New cohesive color scheme: dark petrol header (#12343c) with the BSRN orange (#ffbd3d) as title color, 3px accent line, and header primary buttons (dark amber text). Sidebar changed from periwinkle to light petrol (#e8eff1) with dark labels and bordered job buttons; selected job keeps the teal-blue accent. Warning amber unified to #ffbd3d with dark amber text; the "error" color changed from a teal-ish #5c9791 to terracotta #c4573b so errors actually read as alarms.

**2026-06-10 (later)** — Header changed to the curator-specified CMYK 0/26/76/0 (`#ffbd3d`). The collapsible sidebar panels now show their titles (they were white-on-white) plus a rotating arrow indicating they expand. Stepper readability fixed: the step states no longer reuse the global badge color classes (which painted whole segments solid green), so steps now show colored dots and connector segments with dark text on the white card.

**2026-06-10** —
1. **Workflow-oriented layout:** the sidebar now follows the curator's actual order of work — "1 · Start Download" form on top, "2 · Current batch" job list below it, and rarely-used actions (Update reference IDs, New workflow) folded into a collapsed "Utilities" panel at the bottom. The job detail view opens with a six-step workflow stepper (File → Metadata → Format → QC → Approval → Import), followed by a full-width "Next step" action card and the artifact cards in processing order.
2. **Visual refresh within the curator palette:** serif headings (Georgia), Segoe UI body text, and a subtle yellow/teal radial wash behind the page. No colors changed; the existing yellow/teal variables are untouched.
3. **Security headers:** every response now sends `X-Content-Type-Options: nosniff`; the dashboard page is `Cache-Control: no-store` and served artifacts `no-cache`, so the browser always revalidates plots and reports — stale thumbnails are no longer possible even without the `?v=` cache-buster.

## What This File Does
This script runs the local curator dashboard at `http://127.0.0.1:8765/`. It shows the state of the current run, serves the generated artifacts (metadata files, format reports, QC plots), and provides real buttons that launch the workflow scripts (download/check, QC continuation, data exports, import-file generation) as server-side commands. It only accepts requests from the local machine and protects every action with a per-session token.

## The Big Picture
The page is one self-contained HTML document. Python renders the skeleton plus a JSON payload of all job states; a small JavaScript block reads that payload and builds the sidebar, summary metrics, stepper, and cards in the browser. Action buttons are plain HTML forms that POST back to the same server.

## Section-by-Section Walkthrough (changed parts only)

#### Sidebar (HTML in `render_dashboard`)
**What it does:** Start Download (open by default), then the batch list, then a collapsed Utilities panel holding the reference-ID refresh and the New-workflow form.
**Why it exists:** The previous order put the job list first and scattered three always-visible panels below it; starting a run is the most common entry action and now sits on top, while destructive or rare actions are tucked away.
**To change it:** The panels are plain HTML inside `render_dashboard`. The header's "Start New Workflow" link still works: a small script opens the collapsed Utilities panel when that link is clicked (it targets the `newWorkflowPanel` id).

#### Workflow stepper (`stepper(row)` in the JavaScript)
**What it does:** Draws six connected dots, one per workflow stage, colored by state (green ok, yellow warning, muted red error, grey idle), with the stage name and current status underneath.
**Why it exists:** One glance now answers "where is this job in the pipeline and what blocks it", which previously required reading four separate badges and the gate card.
**To change it:** Each step is a `[label, value]` pair in the `steps` array; the color logic reuses the existing `cls()` classifier. Add or remove stages there.

#### "Next step" card (`actionCard`)
**What it does:** Same logic as the old "Import gate" card (Continue to QC / Approve / Reject / Generate import files), renamed and stretched across the full card grid with a blue accent border.
**Why it exists:** It contains the only buttons that move the workflow forward, so it should be impossible to miss.
**To change it:** Styling is the `.card.action` CSS rule; the button logic is unchanged.

#### Response headers (`send_html`, `serve_project_file`)
**What it does:** Adds `nosniff` everywhere, `no-store` for the dashboard page, `no-cache` for artifacts.
**Why it exists:** `nosniff` stops the browser from guessing content types of served files; the cache rules guarantee regenerated plots are always re-fetched.
**To change it:** If artifact folders ever grow huge and localhost re-fetching feels slow, switch artifacts to `Last-Modified`-based caching instead of removing the header.

## Things to Know
- The static snapshot template in `bsrn_download_check.py` mirrors this layout and must be kept in sync by hand (known, deliberate duplication).
- The CSP only allows same-origin resources, so no external fonts or scripts can be added without also changing the `Content-Security-Policy` header in `send_html`.
- Existing protections that should not be weakened: loopback + Host-header check (`is_loopback_request`), the CSRF token on every form, the 128 KB form-size limit, and the path confinement of served files to `output/current` (`serve_project_file`).
