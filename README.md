# grumpy - the AI that vibe checks your PRs

To put it plainly: **Grumpy is a merge gate that checks the author of the PR understands the change they are putting in.**

grumpy sits in your PR pipeline as a GitHub Action. Before a PR can merge, grumpy walks whoever opened it through their own diff one section at a time, asking a question about each — the first always *"How does this change work on a high level?"* — and blocks the merge until they answer enough of them well enough to convince an LLM grader. 

## Why should you care?

AI is here, and more PRs are AI-generated or AI-assisted. People are literally taking Jira tickets, shoving them into Claude, and reviewers have to deal with the 1K+ line PRs. 

The result? More burnout. People just say: "LGTM, CI is green" and now you are shipping terrible software! For business critical software, this is unacceptable.

## Features (so far)

- **An exam sheet for every PR.** grumpy reads the diff once to form its own blind interpretation, then writes a short sheet: the fixed high-level question plus scoped questions, each anchored to a specific part of the diff. The author sees one question per screen, next to just the slice of the diff it is about.
- **Sized to the change.** One scoped question per 15 changed lines, up to `EXAM_QUESTION_COUNT`. A one-line PR gets one question, a 1,000-line monstrosity gets picked apart section by section, and nobody is quizzed on git headers.
- **Graded on submit, leniently.** Claude marks each answer against the blind interpretation. A terse but correct answer passes; contradicting the diff fails. Wrong answers can be retried, then grumpy reveals the answer and moves on. "Explain it for me" gives a plain-language walkthrough of a section with no effect on the mark, and "Skip" gives up on a question.
- **No understanding? No merge.** The session passes once `PASSINGMARKS` screens are correct and fails as soon as that is out of reach. grumpy reports it as a `grumpy/verdict` commit status; require it in branch protection and the PR cannot merge until the author passes.
- **Good development practices check.** Oversized PRs and diffs are rejected before any question is asked. More coming soon.
- **Local first.** Self-hosted only: one Docker Compose command and it's running, and it works in enterprise environments. BYOK of course.
- **No GitHub App, no OAuth, no webhooks.** It's a FastAPI service that a GitHub Action talks to over a bearer token. Nothing to install on GitHub beyond a workflow file. Just keep the grumpy token a secret.
- **Sarcastic mode.** For the thick skinned, grumpy will brutally tear your understanding. I used to have a tech lead who did this. I miss them.

## Need a demo?

See it in action on real PRs:

- [Demo 1: Pass a PR](https://github.com/bhurghundii/grumpy.dev/pull/30): answer well and `grumpy/verdict` goes green.
- [Demo 2: Fail a PR](https://github.com/bhurghundii/grumpy.dev/pull/27): answer badly and the merge stays blocked.

## Contributing

I am well aware of the irony of having vibe slop to deal with the problem of vibe slop. 

I **encourage AI generated PRs** over human ones. Why? Because the entire point is to help devs integrate vibe coding more safely. 

I still think humans > AI any day though. 

## Documentation

Full docs live at **https://bhurghundii.com/grumpy.dev/** (source in [`docs/`](docs/)):

- [Quickstart](https://bhurghundii.com/grumpy.dev/quickstart/): self-host it with Docker Compose
- [How it works](https://bhurghundii.com/grumpy.dev/how-it-works/)
- [Wire it into a repo](https://bhurghundii.com/grumpy.dev/wire-it-up/) and [block PRs until they pass](https://bhurghundii.com/grumpy.dev/blocking-prs/)
- [Configuration](https://bhurghundii.com/grumpy.dev/configuration/)
- [Deploying to Railway](https://bhurghundii.com/grumpy.dev/deploy-railway/) and the [pre-launch security checklist](https://bhurghundii.com/grumpy.dev/security/)
- [Local development](https://bhurghundii.com/grumpy.dev/development/) and [limitations](https://bhurghundii.com/grumpy.dev/limitations/)

## Quick look

```sh
git clone https://github.com/bhurghundii/grumpy.dev.git
cd grumpy.dev
cp .env.example .env
# edit .env: set GRUMPY_TOKEN to a real secret and GRUMPY_BASE_URL
docker compose up --build
curl localhost:8000/healthz
# {"status": "ok"}
```

## License
MIT
