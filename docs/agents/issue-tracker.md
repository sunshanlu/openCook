# Issue tracker: GitHub

Issues and specs live in `sunshanlu/openCook` GitHub Issues. Use `gh --repo sunshanlu/openCook` (or the equivalent `-R`) explicitly; the upstream remote is not the publishing target.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Verify the target against `origin`, then keep the explicit `--repo sunshanlu/openCook` argument for every publishing operation.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub Issue. For child tickets, also follow **Ticket publishing and relationships** below.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Ticket publishing and relationships

Used by `/to-tickets`, `/wayfinder`, and their ticket consumers. Both the parent and its child tickets are GitHub **Issues**. A sub-issue is a native parent-child relationship, not a separate Task work-item type. This workflow uses one parent and one level of children, even where GitHub supports deeper nesting.

- **Capabilities**: check `gh issue create --help` and `gh issue edit --help` for parent and dependency flags, then check the actual host/repository's API availability, hierarchy restrictions, and permissions. This workflow keeps children in the parent's repository. CLI support does not prove the host supports the operation. Older CLI versions can use `gh api`; inspect the API documentation for the host and selected API version rather than assuming current GitHub.com fields.
- **Create a child**: where supported, `gh issue create --title "..." --body-file <file> --parent <parent-number>`. Add `--blocked-by <blocker-number>,<blocker-number>` for its approved blockers. Omit `--blocked-by` when there are none. The parent and blocking relationships are independent operations, even if one command requests both.
- **Attach an existing child**: confirm it has no conflicting parent, then use `gh issue edit <child-number> --parent <parent-number>` where supported. Otherwise use the host's sub-issues API. Reparenting needs an explicit repair choice.
- **IDs and API fallback**: REST routes use issue numbers, while relationship payloads reference numeric database IDs; resolve IDs with `gh api repos/<owner>/<repo>/issues/<number> --jq .id`. Do not pass a `#number` or GraphQL `node_id` as the database ID. For sub-issue POST and dependency POST, use the input fields specified by the selected API version, not a field name copied from a different version.
- **Blocking**: where supported, `gh issue edit <blocked-number> --add-blocked-by <blocker-number>`; otherwise use `POST repos/<owner>/<repo>/issues/<blocked-number>/dependencies/blocked_by` with that API version's blocker-ID payload. Sub-issues do not establish blocking. Native dependency failure means incomplete publication; body-only edges require explicit user approval.
- **Read and enumerate**: read bodies/comments using `gh issue view`; enumerate the parent's native children with `GET repos/<owner>/<repo>/issues/<parent-number>/sub_issues` and verify each child's parent with `GET repos/<owner>/<repo>/issues/<child-number>/parent`. Follow pagination. Read dependency endpoints for the complete blocker set, including closed blockers; an open-blocker summary alone cannot verify every edge. For frontier selection, inspect blocker states and assignees.
- **Verify and resume**: read back each child Issue, labels, its parent, and directed blockers against the approved graph. Keep created IDs/URLs and successful relationships on partial failure, reconcile remote state before retrying a create, and add only missing items or edges. Report publication as incomplete until every required relationship is verified. Preserve the parent's body/state while attaching approved children. A body link or checklist is not a verified sub-issue.

Official references: [CLI create](https://cli.github.com/manual/gh_issue_create), [CLI edit](https://cli.github.com/manual/gh_issue_edit), [sub-issues API](https://docs.github.com/en/rest/issues/sub-issues), [issue dependencies API](https://docs.github.com/en/rest/issues/issue-dependencies).

## Wayfinding operations

The **map** is an Issue with child Issues linked as native sub-issues.

- **Map**: an Issue labelled `wayfinder:map`, holding Notes / Decisions-so-far / Fog. `gh issue create --label wayfinder:map`.
- **Child ticket**: create and attach using **Ticket publishing and relationships**. Include `Part of #<map>` for readers and labels `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). The `wayfinder:task` label does not change its platform type from Issue.
- **Blocking**: use the separate dependency operations above. A ticket is unblocked when every blocker is closed. Textual dependencies require an explicit approved fallback.
- **Frontier query**: enumerate the map's native sub-issues, exclude closed or assigned children and those with an open blocker; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, before work.
- **Resolve**: `gh issue comment <n> --body-file <answer-file>`, then `gh issue close <n>`, verify, then append a context pointer (gist + link) to the map's Decisions-so-far. This map-content update is part of wayfinding, not `/to-tickets` publication.
