---
tracker:
  kind: linear
  project_slug: https://linear.app/inductive-network/project/symphony-16eac9a024a5/overview
  active_states:
  - Todo
  - In Progress
  terminal_states:
  - Done
  - Canceled
  - Duplicate
polling:
  interval_ms: 30000
workspace:
  root: ~/.symphony/workspaces/https-linear.app-inductive-network-project-symphony-16eac9a024a5-overview
  repo_url: https://github.com/codatta/symphony.git
agent:
  runner: claude_code
  max_concurrent_agents: 1
  max_turns: 20
github:
  token: $GITHUB_TOKEN
  owner: codatta
  repo: symphony
---

You are working on Linear issue {{ issue.identifier }}.

Title: {{ issue.title }}
State: {{ issue.state }}
URL: {{ issue.url }}

Description:
{{ issue.description }}

{% if issue.comments %}
Review feedback — address each point before submitting:
{% for comment in issue.comments %}
- {{ comment }}
{% endfor %}
{% endif %}

## Instructions

The repository is already checked out in your workspace on a dedicated branch
prepared by Symphony. Do not clone the repository or create a new branch.

1. Implement the changes. Keep the scope to what the issue describes.

2. Push and open a PR:
   git push -u origin HEAD
   gh pr create --title "{{ issue.title }}" --body "Resolves {{ issue.url }}"

3. Post the PR URL as a comment on the Linear issue using LINEAR_API_KEY.

4. Update the Linear issue state to "In Review" (query workflow states first to get the
   state ID, then call issueUpdate with the state ID).

Use $GITHUB_TOKEN for git authentication and $LINEAR_API_KEY for all Linear API calls.
