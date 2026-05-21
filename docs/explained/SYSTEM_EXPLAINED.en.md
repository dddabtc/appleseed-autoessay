> Document version: 2026-05-11
> Companion to: README.md and docs/DESIGN.md
> Audience: readers who want a plain-language explanation of what Appleseed
>           does before reading the technical design.

# Appleseed AutoEssay, Explained

## 1. What The System Is For

Appleseed AutoEssay helps a researcher develop a paper in a controlled
workspace instead of asking a chat window for one long answer.

The user brings the topic, paper language, target area, author details, and
any notes that matter. The system then searches for sources, asks the user to
review the source choices, reads the approved set, proposes a direction,
drafts the manuscript, improves it, checks it, and exports files.

The user remains the author. The system does not submit the paper anywhere.
It keeps records so the user can see what was searched, what was approved,
what was uploaded, what was written, and what was checked.

## 2. Why It Is Not One Big Prompt

A single prompt can produce fluent academic-looking prose, but fluency is not
the same as scholarship. The text may cite weak sources, overstate what the
sources prove, or change the thesis during revision.

Appleseed separates the work:

- first search for possible sources;
- then ask the user which sources belong in the project;
- then build a shorter reading list;
- then ask the user to approve that list;
- then draft from that material;
- then improve and check the manuscript before export.

This is slower than a chat reply. The benefit is that the choices are visible.
If the paper goes wrong, the user can inspect the earlier step and rerun only
the part that needs to change.

## 3. The Paper Workflow

### Project Setup

The user creates a run with a clear research question, a domain, a paper mode,
and a manuscript language. Interface language and manuscript language are
separate. A Chinese-speaking user can write an English paper; an English UI can
also produce a Chinese manuscript.

For Chinese topics, detailed setup notes matter. Years, places, model names,
data sources, product codes, or source limits help the search stay close to
the topic.

### Proposal Review

The system prepares a proposal so the user can check the intended direction
before searching deeply. If the run starts without a proposal and the user
later edits the research details, the source search is marked for review again
so later work does not silently follow the old direction.

### Source Search

The first search produces a rough candidate list. This list can include noise,
especially when a topic has words that appear in unrelated fields. The
workspace now opens the searched-results view when the project is waiting for
search review, so the user does not land on an empty reading-list tab.

The user can approve, reject, or pin search candidates. The next step will not
use the rough results until this review is saved.

### Source Reading List

After search review, the system builds a shorter source list. It ranks,
deduplicates, checks topic fit again, and records why material was kept or
dropped.

OpenAlex and Crossref records do not always point directly to a PDF. Appleseed
now tries a bounded landing-page lookup to find the real PDF link. If normal
download still fails, it can try a headless browser download. If the paper is
not available through those safe routes, the source remains in the list with a
manual upload request.

### Uploading Missing PDFs

When a source card says a PDF is needed, the user can click the upload button
on that source card. The upload is tied to that source automatically. A global
upload button remains available when the user wants to add a source manually.

User-uploaded PDFs are kept with the run's source files. Rerunning source
search or source selection does not delete those uploads.

### Deep Source Review

Before the system reads and writes from the shorter list, the user reviews it.
This second source review is important: it decides what the paper may honestly
use as evidence.

### Synthesis, Direction, And Draft

The system summarizes the approved sources, proposes paper directions, and
waits for the user to choose one. Drafting starts after that choice.

The draft should stay within the selected sources. Unsupported claims should be
visible for review instead of quietly becoming confident prose.

### Style, Review, And Integrity

Near the end, the manuscript now goes through focused improvement passes before
the final checks. These passes look for weak evidence, over-strong conclusions,
citation problems, incomplete sections, and writing that needs a narrower
revision.

The final review is still not a promise that the paper is ready to submit
unchanged. It is a structured way to find the risks before export.

### Export

When the user accepts the final result, Appleseed exports the manuscript and
citation files. Typical outputs include Markdown, HTML, DOCX, BibTeX, CSL JSON,
a source-usage table, and self-check reports.

## 4. Recovery And Run Management

If a run is blocked, the workspace now opens the tab that matches the blocked
step and shows the reason. For example, an export issue opens the Export tab
instead of leaving the user on an unrelated view. Live messages refresh the
banner, so the continue or retry button should appear without a manual page
refresh.

Deleting a run deletes that run only. It does not delete every run under the
same paper project. Deleted runs can be restored when workspace policy allows
it. If work finished after a delete or cancel request, the restored run shows a
warning so the user can review what happened before continuing.

## 5. What The User Should Still Check

Appleseed helps organize the work, but it does not replace scholarly judgment.
Before using the exported manuscript, the user should check:

- whether the sources really belong to the topic;
- whether the conclusion is no stronger than the evidence;
- whether every important claim has support;
- whether uploaded PDFs were allowed to be used;
- whether author names, affiliations, and order are correct;
- whether the final file matches the target venue's rules.

The safest way to use Appleseed is to treat it as a traceable drafting and
review workspace, not as an automatic publication system.
