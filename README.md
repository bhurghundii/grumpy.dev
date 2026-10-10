# grumpy - the AI that vibe checks your PRs

To put it plainly: **Grumpy is a merge gate that checks the author of the PR understands the change they are putting in.**

grumpy sits in your PR pipeline as a GitHub Action. Before a PR can merge, grumpy walks whoever opened it through their own diff one section at a time, asking a question about each — the first always *"How does this change work on a high level?"* — and blocks the merge until they answer enough of them well enough to convince an LLM grader. 

## Why should you care?

AI is here, and more PRs are AI-generated or AI-assisted. People are literally taking Jira tickets, shoving them into Claude, and reviewers have to deal with the 1K+ line PRs. 

The result? More burnout. People just say: "LGTM, CI is green" and now you are shipping terrible software! For business critical software, this is unacceptable.

## Features (so far)

- **Local First** grumpy is self-hosted only - one Docker Compose command and it's running. You can get it to run in enterprise envs. BYOK of course.
- **No GitHub App, no OAuth, no webhooks.** It's a FastAPI service (for now) that a GitHub Action talks to over a bearer token. Nothing to install on the GitHub side beyond a workflow file. Just keep the Grumpy token a secret.
- **Answers get graded, reviewers can see if they get answered** Answers are graded by Claude against a blind interpretation of the diff. The results can be checked by the reviewer so they know the author actually has taken some effort understanding their work.
- **Questions get asked for the entire PR** grumpy writes a question per part of the change, so a 1,000-line monstrosity gets picked apart section by section rather than waved through with one vague summary.
- **Good development practices check** - Large PRs get outright rejected. More coming soon.
- **No understanding? No merge** - Grumpy can block a PR if understanding criteria isn't met. 
- **Sarcastic Mode** - For the thick skinned, Grumpy will brutally tear your understanding. I used to have a tech lead who did this. I miss them.

## Need a demo? 

Check out the PRs and see it in action 

## Contributing

I am well aware of the irony of having vibe slop to deal with the problem of vibe slop. 

I **encourage AI generated PRs** over human ones. Why? Because the entire point is to help devs integrate vibe coding more safely. 

I still think humans > AI any day though. 

## Documentation

Full docs live at **https://bhurghundii.github.io/grumpy/** (source in [`docs/`](docs/)):

- [Quickstart](https://bhurghundii.github.io/grumpy/quickstart/): self-host it with Docker Compose
- [How it works](https://bhurghundii.github.io/grumpy/how-it-works/)
- [Wire it into a repo](https://bhurghundii.github.io/grumpy/wire-it-up/) and [block PRs until they pass](https://bhurghundii.github.io/grumpy/blocking-prs/)
- [Configuration](https://bhurghundii.github.io/grumpy/configuration/)
- [Deploying to Railway](https://bhurghundii.github.io/grumpy/deploy-railway/) and the [pre-launch security checklist](https://bhurghundii.github.io/grumpy/security/)
- [Local development](https://bhurghundii.github.io/grumpy/development/) and [limitations](https://bhurghundii.github.io/grumpy/limitations/)

## Quick look

```sh
git clone https://github.com/<your-fork>/grumpy.git
cd grumpy
cp .env.example .env
# edit .env: set GRUMPY_TOKEN to a real secret and GRUMPY_BASE_URL
docker compose up --build
curl localhost:8000/healthz
# {"status": "ok"}
```

## License
MIT
